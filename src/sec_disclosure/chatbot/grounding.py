"""Post-hoc grounding checks for LLM answers.

The synthesiser is told to cite only IDs and quote only text from the evidence pack. This module checks
that it did: every cited match_id / disclosure_id must be in the pack (or, failing that, exist in the
database), and every long quotation must appear in the evidence text. IDs that exist nowhere are removed
from the answer; unverifiable quotes are reported as warnings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

MATCH_ID_PATTERN = re.compile(r"\b[a-z]{2,6}_\d{4}_\d{4}_M\d{4,5}\b")
DISCLOSURE_ID_PATTERN = re.compile(r"\b[a-z]{2,6}_\d{4}_[0-9A-Z]{1,3}_D\d{2,5}\b")
QUOTE_PATTERN = re.compile(r"[\"“]([^\"”]{40,}?)[\"”]")
MIN_QUOTE_CHARS = 40


@dataclass
class GroundingReport:
    cited_match_ids: list[str] = field(default_factory=list)
    unknown_ids: list[str] = field(default_factory=list)          # not in the evidence pack
    removed_ids: list[str] = field(default_factory=list)          # not in the database either -> stripped
    unverified_quotes: list[str] = field(default_factory=list)
    checked_quotes: int = 0

    @property
    def grounded(self) -> bool:
        return not self.removed_ids and not self.unverified_quotes

    def warnings(self) -> list[str]:
        notes = []
        if self.removed_ids:
            notes.append("Removed references to IDs that do not exist in the database: " + ", ".join(self.removed_ids))
        if self.unknown_ids and set(self.unknown_ids) - set(self.removed_ids):
            extra = sorted(set(self.unknown_ids) - set(self.removed_ids))
            notes.append("Cited IDs that exist but were not in the retrieved evidence: " + ", ".join(extra))
        if self.unverified_quotes:
            notes.append(f"{len(self.unverified_quotes)} quotation(s) could not be found verbatim in the evidence.")
        return notes


def normalise(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", re.sub(r"[^\w%$.,'-]+", " ", text.lower())).strip()


def collect_ids(values: Iterable) -> set[str]:
    """Every match/disclosure ID that appears anywhere inside the evidence rows."""
    found: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, str):
            found.update(MATCH_ID_PATTERN.findall(value))
            found.update(DISCLOSURE_ID_PATTERN.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                walk(v)
    walk(list(values))
    return found


def collect_text(values: Iterable) -> str:
    parts: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                walk(v)
    walk(list(values))
    return normalise(" \n ".join(parts))


def check_answer(answer: str, evidence_rows: list[dict],
                 id_exists: Callable[[list[str]], set[str]] | None = None) -> tuple[str, GroundingReport]:
    """Return the answer with non-existent IDs removed, plus a report of what was checked."""
    report = GroundingReport()
    known = collect_ids(evidence_rows)
    cited = list(dict.fromkeys(MATCH_ID_PATTERN.findall(answer) + DISCLOSURE_ID_PATTERN.findall(answer)))
    report.cited_match_ids = [c for c in cited if MATCH_ID_PATTERN.fullmatch(c)]
    report.unknown_ids = [c for c in cited if c not in known]
    match_unknown = [c for c in report.unknown_ids if MATCH_ID_PATTERN.fullmatch(c)]
    existing = id_exists(match_unknown) if (id_exists and match_unknown) else set()
    # Disclosure IDs are only trusted when they come from the evidence (the disclosures table may be empty).
    report.removed_ids = [c for c in report.unknown_ids if c not in existing]
    for bad in report.removed_ids:
        answer = re.sub(rf"\[?\b{re.escape(bad)}\b\]?", "[unverified reference removed]", answer)

    evidence_text = collect_text(evidence_rows)
    for quote in QUOTE_PATTERN.findall(answer):
        q = normalise(quote.strip(" .…"))
        if len(q) < MIN_QUOTE_CHARS:
            continue
        report.checked_quotes += 1
        # Allow an ellipsis inside a quote: every fragment must be present.
        fragments = [normalise(f) for f in re.split(r"\.\.\.|…", quote) if len(normalise(f)) >= 15]
        if not all(f in evidence_text for f in fragments or [q]):
            report.unverified_quotes.append(quote[:120])
    return answer, report
