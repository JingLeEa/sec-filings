"""Apply a human review and refresh only its dependent filing comparisons."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.review_overrides import (
    FINAL_STATUSES, StaleReview, apply_alignment_reviews,
    apply_disclosure_reports, fingerprint, manual_change_label, read_reviews, review_versions, split_reports,
)
from sec_disclosure.storage.pipeline_snapshots import (
    ALIGNMENT_FILES, DISCLOSURE_FILES, alignment_output, folder_documents,
    read_snapshots,
)


class SupersededReview(RuntimeError):
    pass


def affected_pairs(year, years, existing=()):
    years = sorted(set(map(int, years)))
    pairs = set(zip(years, years[1:])) | {tuple(map(int, pair)) for pair in existing}
    return sorted(pair for pair in pairs if int(year) in pair)


def combined(reports):
    result = deepcopy(reports["alignments.json"])
    result["alignments"] += deepcopy(reports["needs_review.json"]["alignments"])
    for key in ("included_statuses", "alignment_count", "overall_counts"):
        result.pop(key, None)
    return result


def write_reports(output, report):
    for name, payload in split_reports(report).items():
        write_json(Path(output) / name, payload)


def retain_unchanged_results(report, prior):
    """Reuse analysis only for an unchanged decision, metadata and evidence."""
    def identity(row):
        return fingerprint({key: value for key, value in row.items()
                            if key not in {"match_id", "change_analysis", "materiality_analysis"}})
    previous = {identity(row): row for row in (prior or {}).get("alignments", [])}
    for row in report["alignments"]:
        old = previous.get(identity(row))
        if old is not None:
            for field in ("change_analysis", "materiality_analysis"):
                if field in old:
                    row[field] = deepcopy(old[field])
    return report


class Refresh:
    def __init__(self, job, output, update):
        self.job, self.output, self.update = job, Path(output), update
        self.settings = job["settings"]
        self.company = job["company"]
        self.mode = self.settings["input_source"]
        self.data = Path(self.settings["data_dir"])
        self.disclosures = Path(self.settings["disclosures_dir"])
        self.alignments = Path(self.settings["alignments_dir"])
        self.reviews = Path(self.settings["reviews_dir"])
        self.env_file = self.settings.get("env_file")
        self.expected, self.files, self.documents = {}, {}, {}
        self.completed = list(job.get("completed_steps", []))
        self.output.mkdir(parents=True, exist_ok=True)

    def folders(self, kind, previous, current):
        if kind == "raw":
            return self.data / "raw" / self.company / str(previous)
        root = self.disclosures if kind == "disclosures" else self.alignments
        return root / self.company / (str(previous) if kind == "disclosures" else f"{previous}-{current}")

    def fetch(self, kind, previous, current):
        key = (self.company, kind, int(previous), int(current))
        if key in self.documents:
            return deepcopy(self.documents[key])
        if self.mode == "postgres":
            scope = (kind, str(previous), str(current))
            snapshot = read_snapshots(self.company, [scope], env_file=self.env_file)[scope]
            self.expected[key] = snapshot["revision"]
            documents = snapshot["documents"]
        else:
            folder = self.folders(kind, previous, current)
            names = ([f"{previous}_chunks.json", f"{previous}_chunk_sentences.json"] if kind == "raw"
                     else DISCLOSURE_FILES if kind == "disclosures" else ALIGNMENT_FILES)
            documents = folder_documents(folder, names, include_change_cache=kind == "alignments")
            for name, entry in documents.items():
                if name in names:
                    self.files[folder / name] = entry["sha256"]
        self.documents[key] = documents
        backup = self.output / "before" / kind / f"{previous}-{current}" / "snapshot.json"
        if not backup.exists():
            write_json(backup, {"documents": documents, "revision": self.expected.get(key)})
        return deepcopy(documents)

    def scopes(self):
        if self.mode == "postgres":
            import psycopg
            from sec_disclosure.storage.disclosures import connection_arguments
            with psycopg.connect(**connection_arguments(self.env_file)) as conn, conn.cursor() as cur:
                cur.execute("SELECT previous_year FROM pipeline_stage_snapshots WHERE company=%s "
                            "AND kind='disclosures' AND documents->'token_usage.json'->'payload'->>'run_complete'='true'",
                            (self.company,))
                years = [row[0] for row in cur.fetchall()]
                cur.execute("SELECT previous_year,current_year FROM alignment_runs WHERE company=%s", (self.company,))
                return years, cur.fetchall()
        years = []
        for folder in (self.disclosures / self.company).glob("*"):
            if re.fullmatch(r"\d{4}", folder.name) and (folder / "token_usage.json").exists():
                if json.loads((folder / "token_usage.json").read_text()).get("run_complete"):
                    years.append(int(folder.name))
        pairs = [tuple(map(int, folder.name.split("-"))) for folder in (self.alignments / self.company).glob("*")
                 if re.fullmatch(r"\d{4}-\d{4}", folder.name) and (folder / "alignments.json").exists()]
        return years, pairs

    def check_current(self):
        # A review for another filing/group has its own dependency plan. It
        # must not cancel this job's other, still-necessary comparison updates.
        current = read_reviews(self.reviews, self.company, self.job["stage"],
                               self.job["previous_year"], self.job["current_year"])
        saved = self.job["review"]
        available = list(current.values()) if self.job["stage"] == "disclosure" else review_versions(current)
        if saved not in available:
            raise SupersededReview("A newer human review is already saved.")
        if self.job["stage"] == "alignment" and any(
                candidate.get("previous_ids") == saved.get("previous_ids")
                and candidate.get("current_ids") == saved.get("current_ids")
                and candidate.get("reviewed_at", "") > saved["reviewed_at"] for candidate in available):
            raise SupersededReview("A newer review exists for these alignment members.")
        if self.mode == "files":
            for path, expected in self.files.items():
                if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise StaleReview("Pipeline source files changed during refresh. Retry with current inputs.")

    def phase(self, name):
        self.check_current()
        self.update(phase=name)

    def finished(self, name):
        if name not in self.completed:
            self.completed.append(name)
        self.update(completed_steps=self.completed)

    def revision(self, kind, previous, current):
        import psycopg
        from sec_disclosure.storage.disclosures import connection_arguments
        with psycopg.connect(**connection_arguments(self.env_file)) as conn, conn.cursor() as cur:
            cur.execute("SELECT revision FROM pipeline_stage_snapshots WHERE company=%s AND kind=%s "
                        "AND previous_year=%s AND current_year=%s", (self.company, kind, int(previous), int(current)))
            row = cur.fetchone()
            return row[0] if row else None

    def publish(self, kind, previous, current, output):
        self.check_current()
        key = (self.company, kind, int(previous), int(current))
        if self.mode == "postgres":
            from sec_disclosure.storage.disclosures import store_disclosures
            from sec_disclosure.storage.alignments import store_alignments
            if key not in self.expected:
                self.expected[key] = self.revision(kind, previous, current)
            if kind == "disclosures":
                store_disclosures(output, self.company, int(previous), env_file=self.env_file,
                                  require_complete=True, expected_snapshots=self.expected)
            else:
                store_alignments(output, self.company, int(previous), int(current), env_file=self.env_file,
                                 expected_snapshots=self.expected)
            self.expected[key] = self.revision(kind, previous, current)
        else:
            names = DISCLOSURE_FILES if kind == "disclosures" else ALIGNMENT_FILES
            target = self.folders(kind, previous, current)
            backup = self.output / "before" / kind / f"{previous}-{current}"
            for name in names:
                path = target / name
                if path.exists() and not (backup / name).exists():
                    write_json(backup / name, json.loads(path.read_text()))
                if (Path(output) / name).exists():
                    write_json(path, json.loads((Path(output) / name).read_text()))
                    self.files[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            if kind == "alignments":
                for directory in ("requests", "change_analysis_cache", "materiality"):
                    for path in (Path(output) / directory).rglob("*.json"):
                        write_json(target / path.relative_to(output), json.loads(path.read_text()))
        self.documents.pop(key, None)

    def apply_disclosures(self, year):
        self.phase(f"Applying disclosure reviews · FY{year}")
        documents = self.fetch("disclosures", year, year)
        if documents["token_usage.json"]["payload"].get("run_complete") is not True:
            raise ValueError("Finish disclosure extraction before refreshing human reviews.")
        reports = {name: entry["payload"] for name, entry in documents.items()}
        reviews = read_reviews(self.reviews, self.company, "disclosure", year)
        if self.job["stage"] == "disclosure" and int(year) == self.job["previous_year"]:
            selected = next((row for name in ("disclosures.json", "review_candidates.json")
                             for row in reports[name]["disclosures"] if row["disclosure_id"] == self.job["record_id"]), None)
            from sec_disclosure.review_overrides import disclosure_fingerprint
            if selected is None or disclosure_fingerprint({**selected, "fiscal_year": int(year)}) != self.job["review"]["source_fingerprint"]:
                raise StaleReview("The reviewed disclosure's source changed. Review the current disclosure before refreshing.")
        effective = apply_disclosure_reports(reports, reviews)
        if effective != reports:
            output = self.output / "disclosures" / str(year)
            for name, payload in effective.items():
                write_json(output / name, payload)
            self.publish("disclosures", year, year, output)
        self.finished(f"disclosure:{year}")

    def data_for(self, previous, current):
        from sec_disclosure.agents.alignment_data import AlignmentData
        for year in (previous, current):
            self.fetch("raw", year, year)
            self.fetch("disclosures", year, year)
        return AlignmentData(self.data, self.company, (str(previous), str(current)),
                             disclosures_dir=self.disclosures, input_source=self.mode, env_file=self.env_file)

    def load_output(self, output):
        return combined({name: json.loads((Path(output) / name).read_text())
                         for name in ("alignments.json", "needs_review.json")})

    def apply_alignment(self, output, data, *, prior=None):
        report = self.load_output(output)
        if prior is not None:
            report = retain_unchanged_results(report, prior)
        reviews = read_reviews(self.reviews, self.company, "alignment", *data.years)
        report = apply_alignment_reviews(report, data.records, reviews)
        if self.job["stage"] == "alignment":
            if not any(row.get("manual_review", {}).get("reviewed_at") == self.job["review"]["reviewed_at"]
                       and row.get("manual_review", {}).get("original_match_id") == self.job["record_id"]
                       for row in report["alignments"]):
                raise StaleReview("The reviewed alignment's members or source text changed. Review the current group first.")
        write_reports(output, report)
        return report

    def refresh_pair(self, previous, current):
        from sec_disclosure.agents.disclosure_alignment import main as align
        data = self.data_for(previous, current)
        output = self.output / "alignments" / f"{previous}-{current}"
        key = (self.company, "alignments", int(previous), int(current))
        exists = (self.revision("alignments", previous, current) is not None if self.mode == "postgres"
                  else (self.folders("alignments", previous, current) / "alignments.json").exists())
        prior = None
        if exists:
            snapshot = {"documents": self.fetch("alignments", previous, current)}
            prior = combined({name: snapshot["documents"][name]["payload"] for name in
                              ("alignments.json", "needs_review.json")})
            if self.job["stage"] == "alignment":
                document = alignment_output(snapshot, output)
                write_json(output / "alignments.json", document)
            else:
                # Reuse only exact request hashes; never copy old matching decisions.
                for name, entry in snapshot["documents"].items():
                    if name.startswith(("requests/", "change_analysis_cache/")):
                        write_json(output / name, entry["payload"])
        elif self.mode == "postgres":
            self.expected[key] = None
        env = ["--env-file", str(self.env_file)] if self.env_file else []
        common = ["--ticker", self.company, "--previous-year", str(previous), "--current-year", str(current),
                  "--data-dir", str(self.data), "--disclosures-dir", str(self.disclosures),
                  "--output-dir", str(output), "--input-source", self.mode, "--no-store-postgres",
                  "--workers", "3", "--request-interval", str(self.settings.get("request_interval", 2)), "--fault-tolerant", *env]
        if self.job["stage"] == "disclosure":
            self.phase(f"Aligning disclosures · {previous}–{current}")
            if align([*common, "--skip-change-analysis"]) != 0:
                raise RuntimeError("Alignment did not finish. Saved requests can be resumed with Retry.")
        elif not exists:
            raise ValueError("No saved alignment comparison exists for this review.")
        self.apply_alignment(output, data, prior=prior if self.job["stage"] == "disclosure" else None)
        self.publish("alignments", previous, current, output)
        self.finished(f"alignment:{previous}-{current}")
        self.phase(f"Updating change analysis · {previous}–{current}")
        if align([*common, "--change-analysis-only"]) != 0:
            raise RuntimeError("Change analysis did not finish. Retry resumes its saved work.")
        report = self.apply_alignment(output, data)
        from sec_disclosure.analysis.aligned_changes import enrich_alignment
        from sec_disclosure.llm.config import load_config
        from sec_disclosure.llm.embeddings import make_embedder
        eligible = [row for row in report["alignments"] if row["status"] in FINAL_STATUSES or manual_change_label(row)]
        working = {**report, "alignments": eligible}
        embedder = make_embedder(config=load_config(self.env_file), model=self.settings.get("embedding_model", "bge-m3"),
                                 cache_dir=self.data / "embeddings", progress=True)
        enriched = enrich_alignment(output, data, embedder=embedder, document=working)
        analyses = {row["match_id"]: row["change_analysis"] for row in enriched["alignments"]}
        for row in report["alignments"]:
            if row["match_id"] in analyses:
                row["change_analysis"] = analyses[row["match_id"]]
        write_reports(output, report)
        self.publish("alignments", previous, current, output)
        self.finished(f"change-analysis:{previous}-{current}")
        self.phase(f"Assessing materiality · {previous}–{current}")
        from sec_disclosure.agents.materiality import main as classify
        materiality_output = output / "human_materiality"
        working = {**report, "alignments": [row for row in report["alignments"]
                                          if (row["status"] in FINAL_STATUSES or manual_change_label(row))
                                          and "materiality_analysis" not in row],
                   "human_review_refresh": True, "included_statuses": FINAL_STATUSES + ["needs_review"]}
        working = deepcopy(working)
        for row in working["alignments"]:
            if manual_change_label(row):
                row["evidence"] = [{"disclosure_id": key, "sentences": [
                    {**sentence, "paragraph_id": source["paragraph_id"], "source_url": source["source_url"]}
                    for source in data.records[key]["sources"] for sentence in source["sentences"]]}
                    for key in row["previous_ids"] + row["current_ids"]]
        write_json(materiality_output / "alignments.json", working)
        if classify(["--ticker", self.company, "--previous-year", str(previous), "--current-year", str(current),
                     "--alignment-dir", str(materiality_output), "--input-source", "files", "--concurrency", "3",
                     "--request-interval", str(self.settings.get("request_interval", 2)), *env]) != 0:
            raise RuntimeError("Materiality did not finish. Retry resumes its saved work.")
        result = json.loads((materiality_output / "alignments.json").read_text())
        assessments = {row["match_id"]: row.get("materiality_analysis") for row in result["alignments"]}
        for row in report["alignments"]:
            if assessments.get(row["match_id"]) is not None:
                row["materiality_analysis"] = assessments[row["match_id"]]
        for path in (materiality_output / "materiality").rglob("*.json"):
            write_json(output / "materiality" / path.relative_to(materiality_output / "materiality"),
                       json.loads(path.read_text()))
        write_reports(output, report)
        self.publish("alignments", previous, current, output)
        self.finished(f"materiality:{previous}-{current}")

    def run(self):
        years, existing = self.scopes()
        self.pairs = (affected_pairs(self.job["previous_year"], years, existing) if self.job["stage"] == "disclosure"
                      else [(self.job["previous_year"], self.job["current_year"])])
        self.check_current()
        self.update(affected_pairs=[list(pair) for pair in self.pairs])
        if self.job["stage"] == "disclosure":
            self.apply_disclosures(self.job["previous_year"])
        for pair in self.pairs:
            self.refresh_pair(*pair)
        self.check_current()


def run_job(job, output, update):
    previous = os.environ.get("SEC_UI_MANUAL_REVIEWS_DIR")
    os.environ["SEC_UI_MANUAL_REVIEWS_DIR"] = job["settings"]["reviews_dir"]
    try:
        Refresh(job, output, update).run()
    finally:
        if previous is None:
            os.environ.pop("SEC_UI_MANUAL_REVIEWS_DIR", None)
        else:
            os.environ["SEC_UI_MANUAL_REVIEWS_DIR"] = previous
