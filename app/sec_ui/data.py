"""Read aligned disclosures for the UI, from PostgreSQL or from the saved JSON files.

Both sources are turned into the same flat "record" dictionaries, so the pages never need
to know where the data came from. This module has no Streamlit code, so it can be tested alone.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config

STATUS_LABELS = {
    "ai_verified": "AI verified",
    "auto_matched": "Auto matched (identical text)",
    "unmatched": "Unmatched",
    "needs_review": "Needs review",
}
RELATIONSHIP_LABELS = {
    "one_to_one": "One to one",
    "one_to_many": "One earlier, many later",
    "many_to_one": "Many earlier, one later",
    "many_to_many": "Many to many",
    "previous_only": "Earlier filing only (removed)",
    "current_only": "Later filing only (new)",
}
NOT_LABELLED = "Not labelled"
NOT_ASSESSED = "Not assessed"
NOT_CLASSIFIED = "Not classified"


class DataError(RuntimeError):
    """A problem the page can show to the user in plain words."""


# --------------------------------------------------------------------------- sources


@dataclass
class Source:
    scopes: dict = field(default_factory=dict)  # (company, previous, current) -> "files" | "postgres"
    notes: list = field(default_factory=list)

    @property
    def companies(self):
        return sorted({key[0] for key in self.scopes})

    def pairs(self, company):
        return sorted((p, c) for co, p, c in self.scopes if co == company)

    def kinds(self):
        return sorted(set(self.scopes.values()))


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DataError(f"Could not read {path}: {error}") from None


def file_scopes() -> dict:
    root = config.alignments_dir()
    scopes = {}
    if root is None or not Path(root).is_dir():
        return scopes
    for company_dir in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        for pair_dir in sorted(p for p in company_dir.iterdir() if p.is_dir()):
            match = re.fullmatch(r"(\d{4})-(\d{4})", pair_dir.name)
            if match and (pair_dir / "alignments.json").is_file():
                scopes[(company_dir.name.lower(), int(match[1]), int(match[2]))] = "files"
    return scopes


def _postgres_connection():
    src = str(config.ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import psycopg
        from sec_disclosure.storage.disclosures import connection_arguments
    except ImportError as error:
        raise DataError(f"psycopg or the project package is not installed ({error}).") from None
    arguments = dict(connection_arguments())
    arguments["connect_timeout"] = 3
    try:
        return psycopg.connect(**arguments)
    except Exception as error:  # noqa: BLE001 - message is shown to the user, without credentials
        raise DataError(f"Could not connect to PostgreSQL ({type(error).__name__}). Is `docker compose up -d` running?") from None


def postgres_scopes() -> dict:
    with _postgres_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT company, previous_year, current_year FROM alignment_runs "
                    "WHERE run_complete ORDER BY company, previous_year, current_year")
        return {(company, int(previous), int(current)): "postgres" for company, previous, current in cur.fetchall()}


def discover_source() -> Source:
    """Combine database and file scopes. In auto mode a scope in the database wins over the same scope in a file."""
    mode = config.data_source_mode()
    source = Source()
    if mode in {"auto", "postgres"}:
        try:
            source.scopes.update(postgres_scopes())
        except DataError as error:
            if mode == "postgres":
                source.notes.append(str(error))
            else:
                source.notes.append(f"Database not used: {error}")
        except Exception as error:  # noqa: BLE001 - e.g. tables not created yet
            source.notes.append(f"Database not used ({type(error).__name__}).")
    if mode in {"auto", "files"}:
        for key, kind in file_scopes().items():
            source.scopes.setdefault(key, kind)
    if not source.scopes:
        source.notes.append("No aligned data found. Load the database or point SEC_UI_ALIGNMENTS_DIR at the test-data folder.")
    return source


# --------------------------------------------------------------------------- loading


def _file_disclosures(company: str, year: int) -> dict:
    root = config.disclosures_dir()
    lookup = {}
    if root is None:
        return lookup
    folder = Path(root) / company / str(year)
    for name in ("disclosures.json", "review_candidates.json"):
        path = folder / name
        if path.is_file():
            for row in _read_json(path).get("disclosures", []):
                lookup[row["disclosure_id"]] = row
    return lookup


def _file_reports(company, previous, current):
    base = Path(config.alignments_dir()) / company / f"{previous}-{current}"
    reports = {"alignments.json": _read_json(base / "alignments.json"), "needs_review.json": None}
    if (base / "needs_review.json").is_file():
        reports["needs_review.json"] = _read_json(base / "needs_review.json")
    lookup = {}
    for year in (previous, current):
        lookup.update(_file_disclosures(company, year))
    return reports, lookup


def _postgres_reports(company, previous, current):
    src = str(config.ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from sec_disclosure.storage.exports import DownstreamExportError, get_alignment_reports
    except ImportError as error:
        raise DataError(f"The project package is not importable ({error}).") from None
    try:
        reports = get_alignment_reports(company, previous, current)
    except DownstreamExportError as error:
        raise DataError(str(error)) from None
    return reports, {}


def load_comparison(company: str, previous: int, current: int, source: Source | None = None) -> dict:
    """Return {"meta": ..., "records": [...]} for one company and year pair."""
    source = source or discover_source()
    kind = source.scopes.get((company, previous, current))
    if kind is None:
        raise DataError(f"No data for {company.upper()} {previous}-{current}.")
    reports, lookup = _postgres_reports(company, previous, current) if kind == "postgres" else _file_reports(company, previous, current)
    records = []
    for file_name in ("alignments.json", "needs_review.json"):
        envelope = reports.get(file_name)
        for raw in (envelope or {}).get("alignments", []):
            records.append(normalise(raw, lookup, company, previous, current, file_name))
    records.sort(key=lambda r: r["match_id"])
    main = reports["alignments.json"] or {}
    meta = {
        "company": company, "previous_year": previous, "current_year": current, "source": kind,
        "run_complete": bool(main.get("run_complete", True)), "note": main.get("note", ""),
        "has_text": any(r["previous_text"] or r["current_text"] for r in records),
        "record_count": len(records),
    }
    return {"meta": meta, "records": records}


# --------------------------------------------------------------------------- normalising


def _members(raw: dict, side: str, lookup: dict) -> list:
    ids = sorted(raw.get(f"{side}_ids") or [])  # filing order, same whether the data came from files or PostgreSQL
    metadata = {m.get("disclosure_id"): m for m in raw.get(f"{side}_disclosures") or []}
    members = []
    for disclosure_id in ids:
        merged = dict(lookup.get(disclosure_id, {}))
        merged.update({k: v for k, v in metadata.get(disclosure_id, {}).items() if v is not None})
        merged["disclosure_id"] = disclosure_id
        members.append(merged)
    return members


def _method_label(analysis: dict, method: str):
    value = analysis.get(method)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("change_taxonomy", "label", "final_taxonomy", "taxonomy"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def _unique(values):
    seen, result = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalise(raw: dict, lookup: dict, company: str, previous: int, current: int, source_file: str = "alignments.json") -> dict:
    analysis = raw.get("change_analysis") or {}
    materiality = raw.get("materiality_analysis") or {}
    earlier, later = _members(raw, "previous", lookup), _members(raw, "current", lookup)
    previous_ids, current_ids = [m["disclosure_id"] for m in earlier], [m["disclosure_id"] for m in later]
    evidence = []
    for entry in raw.get("evidence") or []:
        disclosure_id = entry.get("disclosure_id")
        side = "earlier" if disclosure_id in previous_ids else "later" if disclosure_id in current_ids else ""
        for sentence in entry.get("sentences") or []:
            evidence.append({"side": side, "disclosure_id": disclosure_id, "sentence_id": sentence.get("sentence_id"),
                             "paragraph_id": sentence.get("paragraph_id"), "source_url": sentence.get("source_url"),
                             "text": sentence.get("text", "")})
    urls = _unique([e["source_url"] for e in evidence] +
                   [s.get("source_url") for m in earlier + later for s in (m.get("sources") or []) if isinstance(s, dict)])
    categories = _unique([m.get("taxonomy") for m in later + earlier])
    label = analysis.get("final_taxonomy") if isinstance(analysis.get("final_taxonomy"), str) else None
    decision = materiality.get("materiality")
    material = decision if decision in {"Yes", "No"} else None
    members = earlier + later
    status = raw.get("status", "")
    previous_text = "\n\n".join(m["content"] for m in earlier if m.get("content"))
    current_text = "\n\n".join(m["content"] for m in later if m.get("content"))
    text_source = "disclosure"
    if (previous_ids and not previous_text) or (current_ids and not current_text):
        # Full disclosure text is not loaded: fall back to the sentences the aligner cited.
        text_source = "cited sentences"
        if previous_ids and not previous_text:
            previous_text = " ".join(e["text"] for e in evidence if e["side"] == "earlier" and e["text"])
        if current_ids and not current_text:
            current_text = " ".join(e["text"] for e in evidence if e["side"] == "later" and e["text"])
    return {
        "text_source": text_source,
        "match_id": raw.get("match_id", ""), "company": company, "previous_year": previous, "current_year": current,
        "source_file": source_file, "status": status, "status_label": STATUS_LABELS.get(status, status or "Unknown"),
        "relationship": raw.get("relationship", ""),
        "relationship_label": RELATIONSHIP_LABELS.get(raw.get("relationship", ""), raw.get("relationship", "")),
        "match_method": raw.get("match_method") or "", "explanation": raw.get("explanation", ""),
        "review_reasons": list(raw.get("review_reasons") or []), "verifier_review_reason": raw.get("verifier_review_reason", ""),
        "unmatched_type": raw.get("unmatched_type"), "unmatched_notes": list(raw.get("unmatched_notes") or []),
        "item": next((m.get("item") for m in members if m.get("item")), ""),
        "sections": _unique([m.get("section") for m in members]),
        "category": categories[0] if categories else NOT_CLASSIFIED, "categories": categories,
        "change_label": label or NOT_LABELLED,
        "methods": {name: _method_label(analysis, name) for name in ("lexical", "semantic", "llm")},
        "change_status": analysis.get("status"),
        "lexical": analysis.get("lexical") if isinstance(analysis.get("lexical"), dict) else {},
        "semantic": analysis.get("semantic") if isinstance(analysis.get("semantic"), dict) else {},
        "materiality": {"Yes": "Material", "No": "Not material"}.get(material, NOT_ASSESSED),
        "materiality_score": _number(materiality.get("materiality_score")),
        "confidence_score": _number(materiality.get("confidence_score")),
        "materiality_reason": materiality.get("reason", ""), "key_change": materiality.get("key_change", ""),
        "previous_ids": previous_ids, "current_ids": current_ids,
        "previous_members": earlier, "current_members": later,
        "previous_text": previous_text, "current_text": current_text,
        "previous_summary": " ".join(m["summary"] for m in earlier if m.get("summary")),
        "current_summary": " ".join(m["summary"] for m in later if m.get("summary")),
        "missing_side": "earlier" if not previous_ids else "later" if not current_ids else "",
        "evidence": evidence, "source_urls": urls,
    }


# --------------------------------------------------------------------------- tables and exports


TABLE_COLUMNS = ["match", "item", "change_label", "materiality", "summary", "category", "relationship_label", "status_label"]


def table_row(record: dict) -> dict:
    summary = record["current_summary"] or record["previous_summary"]
    return {"match": record["match_id"].rsplit("_", 1)[-1], "item": record["item"], "category": record["category"],
            "change_label": record["change_label"], "materiality": record["materiality"],
            "relationship_label": record["relationship_label"], "status_label": record["status_label"],
            "summary": summary}


def filter_records(records, *, items=None, categories=None, change_labels=None, materiality=None,
                   statuses=None, relationships=None, text=""):
    needle = text.strip().lower()
    result = []
    for r in records:
        if items and r["item"] not in items:
            continue
        if categories and not set(categories) & (set(r["categories"]) or {NOT_CLASSIFIED}):
            continue
        if change_labels and r["change_label"] not in change_labels:
            continue
        if materiality and r["materiality"] not in materiality:
            continue
        if statuses and r["status"] not in statuses:
            continue
        if relationships and r["relationship"] not in relationships:
            continue
        if needle:
            haystack = " ".join([r["current_summary"], r["previous_summary"], r["current_text"], r["previous_text"],
                                 r["explanation"], r["key_change"], " ".join(r["sections"])]).lower()
            if needle not in haystack:
                continue
        result.append(r)
    return result


def export_rows(records, *, include_text: bool = True) -> list:
    """Flat rows for CSV. Evidence references (IDs and URLs) are always kept."""
    rows = []
    for r in records:
        row = {
            "match_id": r["match_id"], "company": r["company"].upper(), "previous_year": r["previous_year"],
            "current_year": r["current_year"], "item": r["item"], "sections": " | ".join(r["sections"]),
            "content_category": r["category"], "relationship": r["relationship"], "status": r["status"],
            "change_label": r["change_label"], "lexical_label": r["methods"]["lexical"] or "",
            "semantic_label": r["methods"]["semantic"] or "", "llm_label": r["methods"]["llm"] or "",
            "materiality": r["materiality"], "materiality_score": r["materiality_score"],
            "confidence_score": r["confidence_score"], "key_change": r["key_change"],
            "materiality_reason": r["materiality_reason"], "explanation": r["explanation"],
            "previous_disclosure_ids": "; ".join(r["previous_ids"]), "current_disclosure_ids": "; ".join(r["current_ids"]),
            "sentence_ids": "; ".join(e["sentence_id"] for e in r["evidence"] if e["sentence_id"]),
            "source_urls": "; ".join(r["source_urls"]),
        }
        if include_text:
            row["previous_text"], row["current_text"] = r["previous_text"], r["current_text"]
        rows.append(row)
    return rows


def count_by(records, key: str) -> dict:
    counts = {}
    for r in records:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return counts
