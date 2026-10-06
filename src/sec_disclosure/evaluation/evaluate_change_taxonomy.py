"""Evaluate saved change labels on comparable benchmark sentence pairs.

Source identity and correspondence determine eligibility; gold labels never
choose a prediction. Group-level predictions are scored only when their mapped
benchmark rows share a gold label. Alignment and evidence failures are coverage
diagnostics, not taxonomy errors.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sec_disclosure.evaluation.report import render_markdown_report, render_report, summarize_alignment_inventory


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LABELS = ("New", "Removed", "Reworded", "Expanded", "Reduced", "Modified")
STATUSES = (
    "scored", "pair_mismatch", "missing_prediction", "unmapped_evidence",
    "ambiguous_sentence", "ambiguous_prediction", "needs_review",
    "missing_taxonomy", "invalid_taxonomy", "mixed_gold_group",
)
ACCEPTED_STATUSES = {"ai_verified", "auto_matched", "unmatched"}
REQUIRED_COLUMNS = (
    "Record ID", "Company", "Previous Fiscal Year", "Current Fiscal Year",
    "Previous Paragraph / Chunk ID", "Current Paragraph / Chunk ID",
    "Previous Disclosure Text", "Current Disclosure Text", "Change Taxonomy",
)
ID_PATTERN = re.compile(
    r"(?P<company>[a-z0-9][a-z0-9.-]*)_(?P<year>\d{4})_(?:item)?"
    r"(?P<item>\d+[a-z]?)_p(?P<paragraph>\d+)_s(?P<sentence>\d+)$", re.I,
)
ROW_COLUMNS = (
    "record_id", "company", "previous_year", "current_year", "item",
    "gold_taxonomy", "predicted_taxonomy", "status", "reason", "correct",
    "match_id", "candidate_match_ids", "prediction_files", "group_gold_labels",
    "comparison_previous_year", "comparison_current_year", "period_matching",
    "previous_lookup", "current_lookup", "previous_sentence_id", "current_sentence_id",
    "previous_text", "current_text",
)


def normalize_text(value: str) -> str:
    """Normalize formatting only, preserving case, words, amounts and dates."""
    value = value.translate(str.maketrans({
        "\u00a0": " ", "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-",
    }))
    value = re.sub(r"(?m)^\s*[•◦▪●‣]\s*", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_company(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def normalize_ticker(value: str) -> str:
    value = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", value):
        raise ValueError("Use a ticker such as AMD, MU, NVDA or BRK-B")
    return value


def annotation_company_ids(rows: list[dict[str, str]]) -> dict[str, set[str]]:
    """Associate CSV display names with the tickers actually used in source IDs."""
    identities: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        label = normalize_company(row.get("Company") or "")
        ids = identities[label]
        for column in ("Previous Paragraph / Chunk ID", "Current Paragraph / Chunk ID"):
            match = ID_PATTERN.fullmatch((row.get(column) or "").strip())
            if match:
                ids.add(normalize_ticker(match.group("company")))
    return dict(identities)


def read_annotation_identity(path: Path) -> tuple[list[dict[str, str]], dict[str, set[str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "Company" not in (reader.fieldnames or []):
            raise ValueError(f"Annotation is missing its Company column: {path}")
        rows = list(reader)
    return rows, annotation_company_ids(rows)


def infer_annotation_ticker(path: Path) -> str:
    """Infer one company from source-ID prefixes, falling back to ticker labels."""
    rows, identities = read_annotation_identity(path)
    tickers = set()
    for label, source_ids in identities.items():
        if source_ids:
            tickers.update(source_ids)
        else:
            raw_names = {row["Company"].strip() for row in rows
                         if normalize_company(row.get("Company") or "") == label and row.get("Company")}
            for name in raw_names:
                try:
                    tickers.add(normalize_ticker(name))
                except ValueError:
                    raise ValueError("Cannot infer a ticker from the CSV company label. Pass --ticker TICKER and --company LABEL") from None
    if len(tickers) != 1:
        raise ValueError("Annotation does not identify exactly one ticker. Pass --ticker TICKER to select a company")
    return next(iter(tickers))


def filing_accession(value: str) -> str:
    """Identify the SEC filing in dashed index URLs or archive directory URLs."""
    match = re.search(r"/(\d{10})-(\d{2})-(\d{6})(?:-index|/|\.)", value)
    if match:
        return "".join(match.groups())
    match = re.search(r"/(\d{18})(?:/|(?:-index)?\.)", value)
    return match.group(1) if match else ""


def normalize_item(value: str) -> str:
    match = re.fullmatch(r"(?:item\s*)?(\d+[a-z]?)", value.strip(), re.I)
    return match.group(1).upper() if match else ""


def canonical_id(value: str) -> str:
    match = ID_PATTERN.fullmatch(value.strip())
    if not match:
        return value.strip().casefold()
    parts = match.groupdict()
    return "_".join((
        parts["company"].casefold(), parts["year"], parts["item"].upper(),
        f"P{int(parts['paragraph'])}", f"S{int(parts['sentence'])}",
    ))


def normalize_label(value: Any) -> str | None:
    if isinstance(value, list):
        if len(value) != 1:
            return None
        value = value[0]
    if not isinstance(value, str):
        return None
    mapping = {label.casefold(): label for label in LABELS}
    # This is the benchmark's explicit unchanged-as-Reworded convention.
    mapping["unchanged"] = "Reworded"
    return mapping.get(value.strip().casefold())


@dataclass
class GoldRow:
    record_id: str
    company: str
    previous_year: str
    current_year: str
    item: str
    label: str
    previous_id: str
    current_id: str
    previous_text: str
    current_text: str
    previous_filing_url: str = ""
    current_filing_url: str = ""

    @property
    def period(self) -> tuple[str, str, str]:
        return self.company, self.previous_year, self.current_year


@dataclass
class Prediction:
    key: tuple[str, str, str, str]
    item: str
    path: Path
    row: dict[str, Any]


@dataclass
class Occurrence:
    sentence_id: str
    text: str
    memberships: dict[tuple[str, str, str, str], set[str]] = field(default_factory=dict)


def load_gold(path: Path, ticker: str, items: set[str], company_label: str | None = None) -> list[GoldRow]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Annotation columns missing: {', '.join(sorted(missing))}")
        if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
            raise ValueError("Annotation contains duplicate column names")
        table = list(reader)
        identities = annotation_company_ids(table)
        label_override = normalize_company(company_label) if company_label else ""
        result = []
        seen = set()
        for line, row in enumerate(table, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Annotation row {line}: inconsistent CSV column count")
            if not any((value or "").strip() for value in row.values()):
                continue
            company_name = normalize_company(row["Company"])
            source_tickers = identities.get(company_name, set())
            selected = (company_name == normalize_company(ticker)
                        or bool(label_override) and company_name == label_override
                        or source_tickers == {ticker})
            if not selected:
                continue
            company = ticker
            previous_year = row["Previous Fiscal Year"].strip()
            current_year = row["Current Fiscal Year"].strip()
            item_values = [normalize_item(row.get(name, "")) for name in (
                "Item", "Column 9", "Previous Section / Subsection", "Current Section / Subsection",
            )]
            item_values += [match.group("item").upper()
                            for name in ("Previous Paragraph / Chunk ID", "Current Paragraph / Chunk ID")
                            if (match := ID_PATTERN.fullmatch(row[name].strip()))]
            present_items = set(filter(None, item_values))
            if len(present_items) != 1:
                raise ValueError(f"Annotation row {line}: missing or conflicting SEC Item")
            item = present_items.pop()
            if items and item not in items:
                continue
            if not re.fullmatch(r"\d{4}", previous_year) or not re.fullmatch(r"\d{4}", current_year) or previous_year >= current_year:
                raise ValueError(f"Annotation row {line}: invalid fiscal year pair")
            record_id = row["Record ID"].strip()
            key = company, previous_year, current_year, record_id
            if not record_id or key in seen:
                raise ValueError(f"Annotation row {line}: missing or duplicate Record ID {record_id!r}")
            seen.add(key)
            label = normalize_label(row["Change Taxonomy"])
            if label is None:
                raise ValueError(f"Annotation row {line}: unknown Change Taxonomy {row['Change Taxonomy']!r}")
            before = row["Previous Disclosure Text"]
            after = row["Current Disclosure Text"]
            if not normalize_text(before) and not normalize_text(after):
                raise ValueError(f"Annotation row {line}: both disclosure sides are empty")
            for name, text, year in (("Previous Paragraph / Chunk ID", before, previous_year),
                                     ("Current Paragraph / Chunk ID", after, current_year)):
                if row[name].strip() and not normalize_text(text):
                    raise ValueError(f"Annotation row {line}: source ID without disclosure text")
                source_match = ID_PATTERN.fullmatch(row[name].strip())
                allowed_prefixes = {ticker}
                if label_override and company_name == label_override:
                    allowed_prefixes.add(label_override)
                if source_match and (normalize_ticker(source_match.group("company")) not in allowed_prefixes or source_match.group("year") != year):
                    raise ValueError(f"Annotation row {line}: source ID contradicts company/year")
            result.append(GoldRow(record_id, company, previous_year, current_year, item, label,
                                  row["Previous Paragraph / Chunk ID"], row["Current Paragraph / Chunk ID"], before, after,
                                  row.get("Previous Filing SEC URL", ""), row.get("Current Filing SEC URL", "")))
    if not result:
        raise ValueError(f"No benchmark rows for {ticker} and the requested Items")
    return result


def load_predictions(paths: list[Path], ticker: str) -> tuple[dict, dict]:
    predictions: dict[tuple[str, str, str, str], Prediction] = {}
    occurrences: dict[tuple[str, str, str, str, str], dict[str, Occurrence]] = defaultdict(dict)
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"Result must be a JSON object: {path}")
        company = normalize_ticker(str(document.get("company", "")))
        if company != ticker:
            raise ValueError(f"Result company does not match --ticker: {path}")
        before_year, after_year = str(document.get("previous_year", "")), str(document.get("current_year", ""))
        if not re.fullmatch(r"\d{4}", before_year) or not re.fullmatch(r"\d{4}", after_year) or before_year >= after_year:
            raise ValueError(f"Result has invalid comparison years: {path}")
        if not isinstance(document.get("alignments"), list):
            raise ValueError(f"Result requires an alignments array: {path}")
        for row in document["alignments"]:
            key = company, before_year, after_year, row.get("match_id", "")
            if not key[-1] or key in predictions:
                raise ValueError(f"Missing or duplicate match_id in {path}: {key[-1]!r}")
            item_set = {normalize_item(member.get("item", ""))
                        for side in ("previous_disclosures", "current_disclosures") for member in row.get(side, [])}
            if len(item_set) != 1 or "" in item_set:
                raise ValueError(f"Missing or cross-Item member metadata for {key[-1]}")
            item = item_set.pop()
            previous_ids, current_ids = set(row.get("previous_ids", [])), set(row.get("current_ids", []))
            if (not previous_ids and not current_ids) or previous_ids & current_ids:
                raise ValueError(f"Invalid disclosure membership for {key[-1]}")
            for side, ids in (("previous_disclosures", previous_ids), ("current_disclosures", current_ids)):
                if {member.get("disclosure_id") for member in row.get(side, [])} != ids:
                    raise ValueError(f"Disclosure IDs and member metadata disagree for {key[-1]}")
            predictions[key] = Prediction(key, item, path, row)
            for evidence in row.get("evidence", []):
                disclosure_id = evidence.get("disclosure_id", "")
                if disclosure_id not in previous_ids | current_ids:
                    raise ValueError(f"Citation outside alignment {key[-1]}: {disclosure_id}")
                side = "previous" if disclosure_id in previous_ids else "current"
                year = before_year if side == "previous" else after_year
                scope = company, before_year, after_year, item, side
                for sentence in evidence.get("sentences", []):
                    sid, text = sentence.get("sentence_id", ""), normalize_text(sentence.get("text", ""))
                    if not sid or not text:
                        raise ValueError(f"Citation lacks sentence ID/text: {key[-1]}")
                    match = ID_PATTERN.fullmatch(sid)
                    if match and (normalize_ticker(match.group("company")) != company or match.group("year") != year or match.group("item").upper() != item):
                        raise ValueError(f"Citation contradicts company/year/Item: {sid}")
                    source_key = canonical_id(sid)
                    occurrence = occurrences[scope].setdefault(source_key, Occurrence(sid, text))
                    if occurrence.text != text:
                        raise ValueError(f"Contradictory text for source sentence {sid}")
                    occurrence.memberships.setdefault(key, set()).add(disclosure_id)
    return predictions, dict(occurrences)


def resolve_sentence(sources: dict[str, Occurrence], source_id: str, text: str) -> tuple[list[Occurrence], str]:
    if not normalize_text(text):
        return [], "absent"
    normalized = normalize_text(text)
    identified = sources.get(canonical_id(source_id)) if source_id.strip() else None
    if identified is not None and identified.text == normalized:
        return [identified], "id_and_text"
    found = [source for source in sources.values() if source.text == normalized]
    return found, "exact_text" if len(found) == 1 else "ambiguous_text" if found else "not_found"


def supports_pair(prediction: Prediction, before: Occurrence, after: Occurrence) -> bool:
    sources = prediction.row.get("grouping", {}).get("source_alignments", [])
    if not sources:
        return True
    # Shared-anchor chains do not imply every previous/current cross-product.
    return any(before.memberships[prediction.key] & set(source.get("previous_ids", []))
               and after.memberships[prediction.key] & set(source.get("current_ids", [])) for source in sources)


def comparison_filings(predictions: dict) -> dict[tuple, dict[str, str]]:
    found: dict[tuple, dict[str, set[str]]] = {}
    for prediction in predictions.values():
        period = prediction.key[:3]
        filings = found.setdefault(period, {"previous": set(), "current": set()})
        for evidence in prediction.row.get("evidence", []):
            side = "previous" if evidence["disclosure_id"] in prediction.row["previous_ids"] else "current"
            for sentence in evidence.get("sentences", []):
                accession = filing_accession(sentence.get("source_url", ""))
                if accession:
                    filings[side].add(accession)
    result = {}
    for period, filings in found.items():
        if any(len(values) > 1 for values in filings.values()):
            raise ValueError(f"Multiple source filings for one comparison side: {period}")
        result[period] = {side: next(iter(values), "") for side, values in filings.items()}
    return result


def resolve_period(row: GoldRow, filings: dict) -> tuple[tuple | None, str]:
    before, after = filing_accession(row.previous_filing_url), filing_accession(row.current_filing_url)
    if (row.previous_filing_url.strip() and not before) or (row.current_filing_url.strip() and not after):
        raise ValueError(f"Unrecognized SEC filing URL in benchmark record {row.record_id}")
    if before or after:
        candidates = [period for period, sources in filings.items() if period[0] == row.company
                      and (not before or sources["previous"] == before)
                      and (not after or sources["current"] == after)]
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous filing pair for benchmark record {row.record_id}")
        if candidates:
            return candidates[0], "filing_accessions"
        # Never match recurring boilerplate in a demonstrably different filing.
        # Without source URL metadata, years remain the only available key.
        if row.period in filings and not any(filings[row.period].values()):
            return row.period, "year_columns"
        return None, "missing_filing_pair"
    return (row.period, "year_columns") if row.period in filings else (None, "missing_year_pair")


def evaluate_rows(gold: list[GoldRow], predictions: dict, occurrences: dict) -> list[dict[str, Any]]:
    results = []
    group_labels: dict[tuple, set[str]] = defaultdict(set)
    filings = comparison_filings(predictions)
    for row in gold:
        period, period_matching = resolve_period(row, filings)
        scopes = [(*(period or row.period), row.item, side) for side in ("previous", "current")]
        # A missing authoritative filing pair must not search the nominal year.
        available = occurrences if period is not None else {}
        before, previous_lookup = resolve_sentence(available.get(scopes[0], {}), row.previous_id, row.previous_text)
        after, current_lookup = resolve_sentence(available.get(scopes[1], {}), row.current_id, row.current_text)
        candidates = {key for source in before + after for key in source.memberships}
        record = dict(
            record_id=row.record_id, company=row.company, previous_year=row.previous_year,
            current_year=row.current_year, item=row.item, gold_taxonomy=row.label,
            predicted_taxonomy="", status="", reason="", correct="", match_id="",
            candidate_match_ids=";".join(sorted(key[-1] for key in candidates)),
            prediction_files=";".join(sorted({str(predictions[key].path) for key in candidates})),
            group_gold_labels="", previous_lookup=previous_lookup, current_lookup=current_lookup,
            comparison_previous_year=period[1] if period else "", comparison_current_year=period[2] if period else "",
            period_matching=period_matching,
            previous_sentence_id=";".join(source.sentence_id for source in before),
            current_sentence_id=";".join(source.sentence_id for source in after),
            previous_text=row.previous_text, current_text=row.current_text,
        )
        eligible = set()
        if period is None:
            record.update(status="missing_prediction", reason="No saved result for the benchmark's SEC filing pair" if period_matching == "missing_filing_pair" else "No saved result for this comparison")
        elif len(before) > 1 or len(after) > 1:
            record.update(status="ambiguous_sentence", reason="Repeated source text could not be resolved to one occurrence")
        elif previous_lookup == "not_found" or current_lookup == "not_found":
            status = "unmapped_evidence" if candidates else "missing_prediction"
            record.update(status=status, reason="Benchmark sentence text is absent from saved citations; correspondence cannot be verified")
        else:
            if before and after:
                shared = before[0].memberships.keys() & after[0].memberships.keys()
                eligible = {key for key in shared if supports_pair(predictions[key], before[0], after[0])}
            else:
                populated = (before or after)[0]
                empty_side = "current_ids" if before else "previous_ids"
                eligible = {key for key in populated.memberships if not predictions[key].row.get(empty_side)}
            if not eligible:
                record.update(status="pair_mismatch", reason="Source occurrences belong to different comparisons, or the saved comparison includes an annotated absent side")
            elif len(eligible) > 1:
                record.update(status="ambiguous_prediction", reason="Multiple alignment groups represent the annotated pair")
            else:
                key = next(iter(eligible))
                prediction = predictions[key]
                record.update(match_id=key[-1], prediction_files=str(prediction.path), _key=key)
                group_labels[key].add(row.label)
        results.append(record)
    # Eligibility is determined before labels are compared. All uniquely mapped
    # gold rows participate in the homogeneity check, even with absent labels.
    for record in results:
        key = record.pop("_key", None)
        if key is None:
            continue
        prediction = predictions[key]
        record["group_gold_labels"] = ";".join(label for label in LABELS if label in group_labels[key])
        analysis = prediction.row.get("change_analysis") or {}
        raw_label = analysis.get("final_taxonomy")
        label = normalize_label(raw_label)
        record["predicted_taxonomy"] = label or ""
        if prediction.row.get("status") not in ACCEPTED_STATUSES:
            record.update(status="needs_review", reason="The saved alignment has not been finalized")
        elif raw_label is None or raw_label == "" or raw_label == []:
            record.update(status="missing_taxonomy", reason="final_taxonomy has not been populated")
        elif label is None:
            record.update(status="invalid_taxonomy", reason=f"Expected one of the six labels; received {raw_label!r}")
        elif len(group_labels[key]) != 1:
            record.update(status="mixed_gold_group", reason="Mapped benchmark rows in this disclosure group have different gold taxonomies")
        else:
            record.update(status="scored", reason="Unique matching source pair in a group with a uniform mapped gold label",
                          correct=record["gold_taxonomy"] == label)
    assert all(record["status"] in STATUSES for record in results)
    return results


def calculate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["status"] == "scored"]
    confusion = {gold: {predicted: 0 for predicted in LABELS} for gold in LABELS}
    for row in scored:
        confusion[row["gold_taxonomy"]][row["predicted_taxonomy"]] += 1
    per_class = {}
    for label in LABELS:
        true_positive = confusion[label][label]
        support = sum(confusion[label].values())
        predicted = sum(confusion[gold][label] for gold in LABELS)
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        class_rows = [row for row in rows if row["gold_taxonomy"] == label]
        per_class[label] = dict(
            precision=precision if scored else None, recall=recall if scored else None,
            f1=f1 if scored else None, support=support, predicted_count=predicted,
            benchmark_count=len(class_rows), coverage=support / len(class_rows) if class_rows else None,
            status_counts={status: sum(row["status"] == status for row in class_rows) for status in STATUSES},
        )
    correct = sum(confusion[label][label] for label in LABELS)
    return dict(
        benchmark_rows=len(rows), scored_rows=len(scored), excluded_rows=len(rows) - len(scored),
        coverage=len(scored) / len(rows) if rows else None, correct_rows=correct,
        accuracy=correct / len(scored) if scored else None,
        macro_f1=sum(per_class[label]["f1"] for label in LABELS) / len(LABELS) if scored else None,
        weighted_f1=sum(per_class[label]["f1"] * per_class[label]["support"] for label in LABELS) / len(scored) if scored else None,
        status_counts={status: sum(row["status"] == status for row in rows) for status in STATUSES},
        per_class=per_class, confusion_matrix=confusion,
    )


def build_summary(rows: list[dict], inputs: list[Path]) -> dict[str, Any]:
    periods = sorted({f"{row['previous_year']}-{row['current_year']}" for row in rows})
    items = sorted({row["item"] for row in rows})
    return dict(
        schema_version="1", evaluation_target="change_taxonomy", evaluation_unit="benchmark_sentence_pair",
        labels=list(LABELS),
        method=dict(
            eligibility="Unique source occurrences in the same saved comparison; no inferred cross-links in consolidated groups",
            group_projection="Exclude groups whose uniquely mapped benchmark rows have different gold labels",
            evidence_scope="Saved sentence citations only; absent citations do not prove an alignment mismatch",
            text_matching="Formatting-normalized exact text, with compatible IDs used only when text also matches; no fuzzy matching",
            period_matching="SEC filing accession IDs from supplied URLs take precedence over annotation year columns; years are used when filing identities are unavailable",
            label_source="change_analysis.final_taxonomy only; no inference from relationship, status, lexical, semantic or llm",
            unchanged_mapping="Unchanged maps to Reworded, following the supplied benchmark convention",
            macro_f1="Unweighted mean over all six labels; unsupported classes contribute zero when scored rows exist",
            empty_score="Scores are null when no rows are eligible",
        ),
        inputs=[dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for path in inputs],
        overall=calculate_metrics(rows),
        by_period={period: calculate_metrics([row for row in rows if f"{row['previous_year']}-{row['current_year']}" == period]) for period in periods},
        by_item={item: calculate_metrics([row for row in rows if row["item"] == item]) for item in items},
        by_period_and_item={period: {item: calculate_metrics([row for row in rows if f"{row['previous_year']}-{row['current_year']}" == period and row["item"] == item]) for item in items} for period in periods},
    )


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(output: Path, rows: list[dict], summary: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (output / "evaluation_report.html").write_text(render_report(summary), encoding="utf-8")
    (output / "evaluation_report.md").write_text(render_markdown_report(summary), encoding="utf-8")
    write_csv(output / "row_results.csv", ROW_COLUMNS, rows)
    scopes = [("overall", summary["overall"])] + [(period, metrics) for period, metrics in summary["by_period"].items()]
    class_rows, confusion_rows = [], []
    for scope, metrics in scopes:
        for label in LABELS:
            values = metrics["per_class"][label]
            class_rows.append(dict(scope=scope, label=label, **{name: values[name] for name in (
                "precision", "recall", "f1", "support", "predicted_count", "benchmark_count", "coverage",
            )}))
            for predicted in LABELS:
                confusion_rows.append(dict(scope=scope, gold_taxonomy=label, predicted_taxonomy=predicted,
                                           count=metrics["confusion_matrix"][label][predicted]))
    write_csv(output / "per_class_metrics.csv", ("scope", "label", "precision", "recall", "f1", "support", "predicted_count", "benchmark_count", "coverage"), class_rows)
    write_csv(output / "confusion_matrix.csv", ("scope", "gold_taxonomy", "predicted_taxonomy", "count"), confusion_rows)


def resolve_annotation_path(directory: Path, ticker: str | None, company_label: str | None = None) -> Path:
    """Select by filename or CSV identity without a default company."""
    preferred = directory / f"{ticker}.csv" if ticker else None
    if preferred and preferred.is_file():
        return preferred
    candidates = sorted(path for path in directory.glob("*.csv") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"No annotation CSV found in {directory}; pass --annotation PATH")
    if not ticker:
        raise ValueError("Multiple annotation CSVs found. Pass --ticker TICKER or --annotation PATH")
    matches = []
    for path in candidates:
        try:
            _, identities = read_annotation_identity(path)
        except ValueError:
            continue
        if normalize_company(ticker) in identities or any(ids == {ticker} for ids in identities.values()) or (
            company_label and normalize_company(company_label) in identities
        ):
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Multiple annotation CSVs identify {ticker}. Pass --annotation PATH")
    raise ValueError(f"Multiple annotation CSVs found and {preferred} is missing. Name the benchmark {ticker}.csv or pass --annotation PATH")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Company ticker, e.g. MU or NVDA; inferred from a single selected annotation when omitted")
    parser.add_argument("--company", help="Optional company label in the CSV, e.g. Micron, when it differs from the ticker")
    parser.add_argument("--annotation", type=Path, help="Benchmark CSV; otherwise discover it in repository data/annotation by ticker filename, source IDs or a sole CSV")
    parser.add_argument("--results-dir", type=Path, default=PROJECT_ROOT / "data/alignments", help="Root containing ticker/year-pair/alignments_result.json; default: repository data/alignments")
    parser.add_argument("--review-dir", type=Path, help="Optional root containing ticker/year-pair/needs_review.json; review rows are never scored")
    parser.add_argument("--items", nargs="+", help="Optional SEC Item filter; default is all Items present in the benchmark")
    parser.add_argument("--output-dir", type=Path, help="Default: repository data/evaluation/TICKER; regenerates evaluation_report.md each run")
    args = parser.parse_args(argv)
    try:
        ticker = normalize_ticker(args.ticker) if args.ticker is not None else None
        annotation = args.annotation or resolve_annotation_path(PROJECT_ROOT / "data/annotation", ticker, args.company)
        ticker = ticker or infer_annotation_ticker(annotation)
        items = {normalize_item(item) for item in args.items or []}
        if "" in items:
            raise ValueError("Invalid --items value")
        gold = load_gold(annotation, ticker, items, company_label=args.company)
        # Filenames use fiscal years, which need not match benchmark filing-year
        # columns. Discover available comparisons before resolving SEC identities.
        paths = sorted((args.results_dir / ticker).glob("*/alignments_result.json"))
        if args.review_dir:
            paths.extend(sorted((args.review_dir / ticker).glob("*/needs_review.json")))
        if not paths:
            raise ValueError(f"No result files under {args.results_dir / ticker}")
        predictions, occurrences = load_predictions(paths, ticker)
        for prediction in predictions.values():
            if prediction.path.parent.name != f"{prediction.key[1]}-{prediction.key[2]}":
                raise ValueError(f"Comparison years disagree with folder name: {prediction.path}")
        rows = evaluate_rows(gold, predictions, occurrences)
        summary = build_summary(rows, [annotation, *paths])
        filings = comparison_filings(predictions)
        summary["source_comparisons"] = [
            dict(company=period[0], previous_year=period[1], current_year=period[2], **sources,
                 benchmark_rows=sum((row["comparison_previous_year"], row["comparison_current_year"]) == period[1:] for row in rows),
                 prediction_rows=sum(key[:3] == period for key in predictions))
            for period, sources in sorted(filings.items())
        ]
        summary["by_result_period"] = {
            f"{period[1]}-{period[2]}": calculate_metrics([
                row for row in rows if (row["comparison_previous_year"], row["comparison_current_year"]) == period[1:]
            ]) for period in sorted(filings)
        }
        summary["period_matching_counts"] = dict((method, sum(row["period_matching"] == method for row in rows))
                                                  for method in sorted({row["period_matching"] for row in rows}))
        available_rows = [row for row in rows if row["comparison_previous_year"]]
        summary["available_comparisons"] = calculate_metrics(available_rows)
        summary["available_by_item"] = {
            item: calculate_metrics([row for row in available_rows if row["item"] == item])
            for item in summary["by_item"]
        }
        comparison_counts: dict[tuple, int] = defaultdict(int)
        for row in rows:
            comparison_counts[(row["company"], row["previous_year"], row["current_year"],
                               row["comparison_previous_year"], row["comparison_current_year"],
                               row["period_matching"])] += 1
        summary["benchmark_comparisons"] = [
            dict(zip(("company", "previous_year", "current_year", "comparison_previous_year",
                      "comparison_current_year", "period_matching"), key), benchmark_rows=count)
            for key, count in sorted(comparison_counts.items())
        ]
        summary["alignment_inventory"] = summarize_alignment_inventory(predictions, rows, ACCEPTED_STATUSES)
        scored_rows = [row for row in rows if row["status"] == "scored"]
        two_sided = sum(bool(normalize_text(row["previous_text"])) and bool(normalize_text(row["current_text"]))
                        for row in scored_rows)
        summary["coverage_accounting"] = dict(
            denominator_unit="benchmark_annotation_csv_row",
            available_benchmark_rows=len(available_rows),
            unavailable_benchmark_rows=len(rows) - len(available_rows),
            uniquely_mapped_rows=sum(bool(row["match_id"]) for row in rows),
            scored_two_sided_rows=two_sided,
            scored_one_sided_rows=len(scored_rows) - two_sided,
        )
        output = args.output_dir or PROJECT_ROOT / "data/evaluation" / ticker
        destinations = [output / name for name in ("summary.json", "evaluation_report.html", "evaluation_report.md", "row_results.csv", "per_class_metrics.csv", "confusion_matrix.csv")]
        if {path.resolve() for path in destinations} & {path.resolve() for path in [annotation, *paths]}:
            raise ValueError("An output destination would overwrite an input")
        write_outputs(output, rows, summary)
        metrics = summary["overall"]
        print(f"Annotation: {annotation}")
        print(f"Benchmark rows: {metrics['benchmark_rows']}")
        for period in summary["source_comparisons"]:
            print(f"Result {period['previous_year']}-{period['current_year']}: {period['benchmark_rows']} benchmark rows refer to these filings")
        print(f"Full benchmark coverage: {metrics['scored_rows']} / {metrics['benchmark_rows']} annotation CSV rows = {metrics['coverage']:.1%}")
        if available_rows:
            print(f"Available-comparison coverage: {len(scored_rows)} / {len(available_rows)} annotation CSV rows = {summary['available_comparisons']['coverage']:.1%}")
        if metrics["scored_rows"]:
            print(f"Taxonomy accuracy: {metrics['accuracy']:.4f}; macro-F1: {metrics['macro_f1']:.4f}")
        else:
            print("Taxonomy scores unavailable: no eligible pairs")
        for status, count in metrics["status_counts"].items():
            if status != "scored" and count:
                print(f"{status}: {count}")
        print(f"Outputs: {output}")
        print(f"Markdown report: {output / 'evaluation_report.md'}")
        print(f"HTML report: {output / 'evaluation_report.html'}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Evaluation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
