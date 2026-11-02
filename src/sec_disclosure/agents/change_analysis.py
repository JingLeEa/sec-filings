"""Label finalized alignment groups; keep exported change analysis compact."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy

from sec_disclosure.llm.concurrency import run_jobs
from sec_disclosure.llm.disclosures import digest, write_json
from .alignment_exact import valid_exact_proof
from .alignment_grouping import supported_match
from .alignment_prompts import CHANGE_ANALYSIS_PROMPT
from sec_disclosure.review_overrides import manual_change_label


CHANGE_POLICY = "finalized_disclosure_change_v2"
CHANGE_TAXONOMY = ("New", "Removed", "Expanded", "Reduced", "Modified", "Reworded", "Unchanged")
MATCHED_TAXONOMY = set(CHANGE_TAXONOMY) - {"New", "Removed"}
CHANGE_BATCH_SIZE = 12
CHANGE_BATCH_CHARS = 48000
CHANGE_EXECUTION_POLICY = "label_batch_repair_v1"


def empty_change_analysis():
    return {"status": "not_started", "lexical": None, "semantic": None,
            "llm": None, "final_taxonomy": None}


def compact_change_analysis(analysis, *, manual_label=None):
    """Normalize legacy results and emit only the user's five result fields."""
    if not isinstance(analysis, dict):
        return empty_change_analysis()
    status = analysis.get("status", "not_started")
    label = analysis.get("llm")
    if isinstance(label, dict):
        label = label.get("taxonomy")
    if label is None and status == "completed" and manual_label is None:
        label = analysis.get("final_taxonomy")
    if not isinstance(label, str) or label not in CHANGE_TAXONOMY:
        label = None
    if status in {"semantic_done", "in_progress"}:
        status = "in_progress"
    if status not in {"not_started", "in_progress", "completed", "needs_review"}:
        status = "not_started"
    if status == "completed" and label is None and manual_label is None:
        status = "needs_review"
    lexical = analysis.get("lexical")
    semantic = analysis.get("semantic")
    enriched = isinstance(lexical, dict) or isinstance(semantic, dict)
    final = analysis.get("final_taxonomy")
    return {"status": status,
            "lexical": deepcopy(lexical) if isinstance(lexical, dict) else None,
            "semantic": deepcopy(semantic) if isinstance(semantic, dict) else None,
            "llm": label if status == "completed" else None,
            "final_taxonomy": manual_label if manual_label in CHANGE_TAXONOMY else
                              final if enriched and isinstance(final, str) and final in CHANGE_TAXONOMY
                              else label if status == "completed" else None}


def effective_change_analysis(row):
    return compact_change_analysis(row.get("change_analysis"), manual_label=manual_change_label(row))


def selected_records(row, data):
    members = row["previous_ids"] + row["current_ids"]
    if not members or len(members) != len(set(members)):
        raise ValueError("Change analysis requires unique, nonempty alignment members.")
    data.item_for(members)
    for side, year in (("previous", data.years[0]), ("current", data.years[1])):
        if any(data.records[key]["fiscal_year"] != year for key in row[side + "_ids"]):
            raise ValueError("Change analysis contains a disclosure on the wrong year side.")
    return [{**data.view(key, full=True), "content": data.records[key]["content"],
             "source_locations": [{"paragraph_id": source["paragraph_id"], "source_url": source["source_url"],
                                   "sentence_ids": [s["sentence_id"] for s in source["sentences"]]}
                                  for source in data.records[key]["sources"]]} for key in members]


def analysis_identity(row, data, runtime):
    identity = {"policy": CHANGE_POLICY,
                "alignment": {key: value for key, value in row.items()
                              if key not in {"change_analysis", "materiality_analysis"}},
                "records": selected_records(row, data)}
    if row["status"] == "ai_verified":
        if runtime.config is None:
            raise ValueError("AI-verified change analysis requires an LLM configuration.")
        identity.update(model=runtime.config.model, base_url=runtime.config.base_url,
                        prompt=CHANGE_ANALYSIS_PROMPT,
                        settings={key: getattr(runtime.args, key) for key in
                                  ("max_steps", "max_tokens", "max_prompt_chars")})
    return digest(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode())


def cache_analysis(runtime, identity, analysis):
    """Provenance stays in the internal cache, outside the exported result."""
    write_json(runtime.output / "change_analysis_cache" / f"{identity}.json",
               {"schema_version": "change-analysis-cache-1.0", "policy": CHANGE_POLICY,
                "input_sha256": identity, "analysis": compact_change_analysis(analysis)})


def cached_analysis(runtime, identity):
    path = runtime.output / "change_analysis_cache" / f"{identity}.json"
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text())
        # Retire the old empty final field without invalidating paid label caches.
        result = compact_change_analysis(saved["analysis"])
        if (saved.get("schema_version") == "change-analysis-cache-1.0" and saved.get("policy") == CHANGE_POLICY
                and saved.get("input_sha256") == identity
                and result["status"] in {"completed", "needs_review"}
                and (result["llm"] is None or result["llm"] in MATCHED_TAXONOMY)):
            return result
    except (ValueError, KeyError, TypeError):
        pass
    return None


def citation_sources(record):
    return {sentence["sentence_id"]: {"sentence_id": sentence["sentence_id"], "text": sentence["text"],
            "paragraph_id": source["paragraph_id"], "source_url": source["source_url"]}
            for source in record["sources"] for sentence in source["sentences"]}


def logical_analysis(row, data):
    if row["status"] == "auto_matched":
        if not supported_match(row, data) or not valid_exact_proof(row, data):
            raise ValueError("Auto-matched change analysis requires verified full-text equality.")
        label = "Unchanged"
    else:
        before, after = row["previous_ids"], row["current_ids"]
        expected = "removed_disclosure" if before and not after else "introduced_disclosure" if after and not before else None
        members = set(before + after)
        cited = {entry["disclosure_id"] for entry in row["evidence"] if entry.get("sentences")}
        if expected is None or row.get("unmatched_type") != expected or cited != members:
            raise ValueError("Unmatched change analysis requires a validated one-sided decision and citations.")
        for entry in row["evidence"]:
            source = citation_sources(data.records[entry["disclosure_id"]])
            if any(sentence != source.get(sentence.get("sentence_id")) for sentence in entry["sentences"]):
                raise ValueError("Unmatched evidence differs from the selected source sentences.")
        label = "Removed" if before else "New"
    return {**empty_change_analysis(), "status": "completed", "llm": label, "final_taxonomy": label}


def validate_change_action(action, rows, data=None):
    if action.get("action") != "classify" or not isinstance(action.get("analyses"), list):
        raise ValueError("Return action:classify with an analyses array.")
    allowed, seen, results = {row["match_id"] for row in rows}, set(), []
    for entry in action["analyses"]:
        if not isinstance(entry, dict):
            raise ValueError("Every change analysis must be an object.")
        key = entry.get("match_id")
        if not isinstance(key, str) or key not in allowed or key in seen:
            raise ValueError("Every supplied match_id must be classified exactly once.")
        if "llm" not in entry:
            raise ValueError("Every change analysis must contain its llm label.")
        label = entry["llm"]
        if label is not None and (not isinstance(label, str) or label not in MATCHED_TAXONOMY):
            raise ValueError("Matched llm label must be Expanded, Reduced, Modified, Reworded, Unchanged, or null.")
        results.append({"match_id": key, "llm": label})
        seen.add(key)
    if seen != allowed:
        raise ValueError("Change analysis omitted a supplied match_id.")
    return results


def classifier_records(rows, data):
    members = sorted({key for row in rows for key in row["previous_ids"] + row["current_ids"]})
    return [{field: data.records[key][field] for field in ("disclosure_id", "fiscal_year", "item", "content")}
            for key in members]


def completion_budget(rows, maximum):
    """Allow ample JSON headroom while capping the label-only completion."""
    response = {"action": "classify", "analyses": [{"match_id": row["match_id"], "llm": "Unchanged"} for row in rows]}
    return min(maximum, max(1024, len(json.dumps(response, ensure_ascii=False).encode()) + 512))


class ChangeRepair:
    """Keep valid labels and request corrections only for unresolved match IDs."""
    def __init__(self, rows):
        self.rows = {row["match_id"]: row for row in rows}
        self.required = set(self.rows)
        self.accepted, self.issues, self.events = {}, {}, []
        self.started = False

    @property
    def pending(self):
        return self.required - self.accepted.keys()

    def snapshot(self):
        return deepcopy({"accepted": self.accepted, "issues": self.issues,
                         "events": self.events, "started": self.started})

    def restore(self, state):
        if (not set(state["accepted"]) <= self.required
                or any(label is not None and (not isinstance(label, str) or label not in MATCHED_TAXONOMY)
                       for label in state["accepted"].values())):
            raise ValueError("Change-analysis checkpoint has invalid accepted labels.")
        self.accepted, self.issues = deepcopy(state["accepted"]), deepcopy(state["issues"])
        self.events, self.started = deepcopy(state["events"]), state["started"]

    def receive(self, action, visible):
        if action.get("action") != "classify" or not isinstance(action.get("analyses"), list):
            raise ValueError("Return action:classify with an analyses array.")
        self.started = True
        pending = set(self.pending)
        counts = Counter(entry.get("match_id") for entry in action["analyses"]
                         if isinstance(entry, dict) and isinstance(entry.get("match_id"), str))
        for entry in action["analyses"]:
            key = entry.get("match_id") if isinstance(entry, dict) else None
            if not isinstance(key, str) or key not in pending:
                self.events.append({"outcome": "ignored", "reason": "foreign_or_already_resolved_match_id"})
                continue
            try:
                if counts[key] != 1:
                    raise ValueError("Return this match_id exactly once.")
                label = validate_change_action({"action": "classify", "analyses": [entry]}, [self.rows[key]])[0]["llm"]
            except (ValueError, KeyError, TypeError) as error:
                self.issues[key] = str(error)
                self.events.append({"match_id": key, "outcome": "rejected", "error": str(error)})
                continue
            self.accepted[key] = label
            self.issues.pop(key, None)
            self.events.append({"match_id": key, "outcome": "accepted"})
        for key in self.pending:
            if key not in counts:
                self.issues[key] = "A label for this match_id was omitted."

    def correction_payload(self, payload):
        rows = [row for row in payload["alignments"] if row["match_id"] in self.pending]
        members = {key for row in rows for key in row["previous_ids"] + row["current_ids"]}
        return {**payload, "alignments": rows,
                "records": [record for record in payload["records"] if record["disclosure_id"] in members],
                "input_hashes": {key: value for key, value in payload["input_hashes"].items() if key in self.pending},
                "validation_errors": {key: self.issues.get(key, "Return a valid label.") for key in sorted(self.pending)},
                "instruction": "Valid labels are saved. Return only the remaining supplied match_ids and their llm labels."}

    def results(self, reason=None):
        return [{"match_id": key, "llm": self.accepted.get(key)} for key in self.rows]

    def audit(self):
        return {"accepted_match_ids": sorted(self.accepted), "pending_match_ids": sorted(self.pending),
                "events": deepcopy(self.events)}


def analysis_jobs(rows, data):
    pending, item = [], None
    for row in rows:
        current_item = data.item_for(row["previous_ids"] + row["current_ids"])
        size = len(json.dumps(classifier_records([*pending, row], data), ensure_ascii=False))
        if pending and (current_item != item or len(pending) >= CHANGE_BATCH_SIZE or size > CHANGE_BATCH_CHARS):
            yield pending
            pending = []
        pending.append(row)
        item = current_item
    if pending:
        yield pending


def change_summary(report, error=None):
    eligible = [row for row in report["alignments"] if row["status"] in {"ai_verified", "auto_matched", "unmatched"}
                or manual_change_label(row)]
    for row in eligible:
        row["change_analysis"] = effective_change_analysis(row)
    states = Counter(row["change_analysis"]["status"] for row in eligible)
    return {"policy": CHANGE_POLICY, "complete": report["run_complete"] and not error and not states["not_started"],
            "error": error, "eligible_alignments": len(eligible),
            "deferred_alignment_review": sum(row["status"] == "needs_review" and not manual_change_label(row)
                                             for row in report["alignments"]),
            "by_status": dict(states),
            "by_method": dict(Counter("pending" if row["change_analysis"]["status"] == "not_started"
                                      else "manual" if manual_change_label(row)
                                      else "llm" if row["status"] == "ai_verified" else "logic" for row in eligible)),
            "taxonomy_counts": dict(Counter(row["change_analysis"]["final_taxonomy"] for row in eligible
                                            if row["change_analysis"]["final_taxonomy"] is not None))}


def run_change_analysis(report, data, runtime, persist):
    if not report["run_complete"]:
        raise ValueError("Finish alignment before running change analysis.")
    pending, identities = [], {}
    blocked = set(report["coverage"]["pending_disclosure_ids"]) | set(report["coverage"]["conflicting_disclosure_ids"])
    for row in report["alignments"]:
        if manual_change_label(row):
            row["change_analysis"] = effective_change_analysis(row)
            continue
        if row["status"] == "needs_review":
            row["change_analysis"] = empty_change_analysis()
            continue
        if row["status"] not in {"ai_verified", "auto_matched", "unmatched"}:
            raise ValueError("Unknown alignment status for change analysis.")
        if blocked.intersection(row["previous_ids"] + row["current_ids"]):
            raise ValueError("Resolve pending/conflicting alignment assignments before change analysis.")
        if row["status"] in {"auto_matched", "unmatched"}:
            row["change_analysis"] = logical_analysis(row, data)
        else:
            if (not supported_match(row, data) or any(data.records[key]["extraction_status"] == "needs_review"
                                                     for key in row["previous_ids"] + row["current_ids"])):
                raise ValueError("LLM change analysis requires a finalized, source-supported matched group.")
            identity = analysis_identity(row, data, runtime)
            identities[row["match_id"]] = identity
            row["change_analysis"] = cached_analysis(runtime, identity) or empty_change_analysis()
            if row["change_analysis"]["status"] == "not_started":
                pending.append(row)
    report["change_analysis_summary"] = change_summary(report)
    persist()
    jobs = list(analysis_jobs(pending, data))
    print(f"[{runtime.output.name}] Change analysis: {len(pending)} pending groups in {len(jobs)} batches; "
          f"{runtime.args.workers} configured workers, up to {min(runtime.args.workers, len(jobs))} active batch jobs; "
          f"at most {CHANGE_BATCH_SIZE} groups per request.", flush=True)

    def work(batch):
        members = {key for row in batch for key in row["previous_ids"] + row["current_ids"]}
        payload = {"change_analysis": True, "previous_year": data.years[0], "current_year": data.years[1],
                   "item": data.item_for(members),
                   "alignments": [{key: row[key] for key in ("match_id", "previous_ids", "current_ids", "relationship")} for row in batch],
                   "input_hashes": {row["match_id"]: identities[row["match_id"]] for row in batch},
                   "records": classifier_records(batch, data)}
        # Keep existing per-group label caches, but isolate legacy graph states
        # that did not preserve partial classifications during correction turns.
        job_id = "change_analysis_" + digest(json.dumps(
            {"payload": payload, "execution_policy": CHANGE_EXECUTION_POLICY},
            sort_keys=True, ensure_ascii=False).encode())[:24]
        result, _, _ = runtime.classification(job_id, CHANGE_ANALYSIS_PROMPT, payload, data, members, ChangeRepair(batch))
        return result

    def consume(batch, results):
        labels = {entry["match_id"]: entry["llm"] for entry in results or []}
        for row in batch:
            label = labels.get(row["match_id"])
            result = {**empty_change_analysis(), "status": "completed" if label is not None else "needs_review",
                      "llm": label, "final_taxonomy": label}
            cache_analysis(runtime, identities[row["match_id"]], result)
            row["change_analysis"] = result
        report["change_analysis_summary"] = change_summary(report)
        persist()

    run_jobs(jobs, work, consume, runtime.args.workers)
    return report
