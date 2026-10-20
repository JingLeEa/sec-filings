"""Rule-based extraction of query filters (company, period, item, change type, ...) from a question.

Deterministic on purpose: filters decide which rows the answer is grounded in, so they must be
predictable and testable without a model. The LLM only gets involved for questions the rules
cannot route (see planner.py).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "bac": ("bank of america", "bofa", "boa", "bankamerica", "bac"),
    "jpm": ("jpmorgan chase", "jp morgan chase", "jpmorgan", "jp morgan", "j.p. morgan", "chase", "jpmc", "jpm"),
    "intc": ("intel corporation", "intel", "intc"),
    "amd": ("advanced micro devices", "amd"),
}
COMPANY_NAMES = {"bac": "Bank of America", "jpm": "JPMorgan Chase", "intc": "Intel", "amd": "AMD"}

ITEM_PATTERNS: list[tuple[str, str]] = [
    ("1A", r"\bitem\s*1\s*a\b|\brisk\s+factors?\b"),
    ("7", r"\bitem\s*7\b(?!\s*a)|\bmd\s*&\s*a\b|\bmd&a\b|\bmanagement'?s\s+discussion\b"),
    ("8", r"\bitem\s*8\b|\bfinancial\s+statements?\b|\bnotes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\b"),
    ("15", r"\bitem\s*15\b|\bexhibits?\b"),
    ("1", r"\bitem\s*1\b(?!\s*a|\d)|\bbusiness\s+section\b|\bitem\s+one\b"),
]

# Order matters: the first label whose pattern matches a span claims it.
CHANGE_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("Unchanged", r"\bunchanged\b|\bidentical\b|\bdid\s*n[o']t\s+change\b|\bstayed\s+the\s+same\b|\bno\s+change"),
    ("New", r"\bnew(?:ly)?\b|\badd(?:ed|s|ing)?\b|\bintroduc\w*|\bappeared\b|\bfirst\s+time\b"),
    ("Removed", r"\bremov\w*|\bdropp?(?:ed|s)?\b|\bdelet\w*|\beliminat\w*|\bno\s+longer\b|\bdisappear\w*|\bdiscontinu\w*"),
    ("Expanded", r"\bexpand\w*|\blengthen\w*|\bgrew\s+longer\b|\bmore\s+detail\w*|\belaborat\w*"),
    ("Reduced", r"\breduc\w*|\bshorten\w*|\bcondens\w*|\btrimm\w*|\bshorter\b|\bless\s+detail\w*"),
    ("Reworded", r"\breword\w*|\brephras\w*|\bmeaning\s+shift\w*"),
    ("Modified", r"\bmodifi\w*|\brevis\w*"),
]
CHANGE_TYPES = ("Unchanged", "Modified", "Reworded", "Expanded", "Reduced", "New", "Removed")

DISCLOSURE_TAXONOMY_PATTERNS: list[tuple[str, str]] = [
    ("Cybersecurity & Data", r"\bcyber\w*|\bdata\s+(?:privacy|protection|breach\w*|security)\b|\binformation\s+security\b"),
    ("Technology & AI", r"\bartificial\s+intelligence\b|\bai\b|\bgenerative\b|\bmachine\s+learning\b|\btechnology\s+(?:&|and)\s+ai\b"),
    ("Supply Chain & Third Parties", r"\bsupply\s+chain\w*|\bsuppliers?\b|\bthird[\s-]part(?:y|ies)\b|\bvendors?\b"),
    ("Regulation, Legal & Compliance", r"\bregulat\w*|\blegal\b|\bcompliance\b|\blitigation\b|\blawsuits?\b"),
    ("Financial & Capital Resources", r"\bcapital\s+resources?\b|\bliquidity\b|\bfunding\b|(?<!human )\bcapital\b(?!\s+one)"),
    ("Human Capital & Organization", r"\bhuman\s+capital\b|\bemployees?\b|\bworkforce\b|\btalent\b|\bheadcount\b"),
    ("Operations & Capacity", r"\boperations?\s+(?:&|and)\s+capacity\b|\bmanufactur\w*|\bfabs?\b|\bcapacity\b"),
    ("Strategy & Business Model", r"\bstrateg\w*|\bbusiness\s+model\b"),
]

STATUS_PATTERNS: list[tuple[str, str]] = [
    ("needs_review", r"\bneeds?[\s_-]review\b|\bflagged\b|\bfor\s+review\b|\bunresolved\b|\bpending\s+review\b"),
    ("ai_verified", r"\bai[\s_-]verified\b"),
    ("auto_matched", r"\bauto[\s_-]?matched\b|\bexact\s+match\w*"),
    ("unmatched", r"\bunmatched\b"),
]
RELATIONSHIP_PATTERNS: list[tuple[str, str]] = [
    ("many_to_many", r"\bmany[\s_-]to[\s_-]many\b"),
    ("one_to_many", r"\bone[\s_-]to[\s_-]many\b|\bsplit\b"),
    ("many_to_one", r"\bmany[\s_-]to[\s_-]one\b|\bmerged?\b|\bconsolidated\b"),
    ("one_to_one", r"\bone[\s_-]to[\s_-]one\b"),
]
MATCH_ID_RE = re.compile(r"\b[a-z]{2,6}_\d{4}_\d{4}_M\d{1,5}\b", re.IGNORECASE)
DISCLOSURE_ID_RE = re.compile(r"\b[a-z]{2,6}_\d{4}_[0-9A-Z]{1,3}_D\d{2,5}\b")
YEAR_RE = r"(?:fy\s*)?(20\d{2})"
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20}


@dataclass
class QueryFilters:
    companies: list[str] = field(default_factory=list)
    previous_year: int | None = None
    current_year: int | None = None
    items: list[str] = field(default_factory=list)
    change_types: list[str] = field(default_factory=list)
    disclosure_taxonomies: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    match_ids: list[str] = field(default_factory=list)
    min_materiality: float | None = None
    max_confidence: float | None = None
    limit: int | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}

    def describe(self) -> str:
        parts = []
        if self.companies:
            parts.append(", ".join(COMPANY_NAMES.get(c, c.upper()) for c in self.companies))
        if self.previous_year and self.current_year:
            parts.append(f"{self.previous_year}→{self.current_year}")
        elif self.current_year:
            parts.append(f"FY{self.current_year} 10-K vs prior year")
        elif self.previous_year:
            parts.append(f"FY{self.previous_year} 10-K vs next year")
        if self.items:
            parts.append("Item " + "/".join(self.items))
        if self.change_types:
            parts.append("/".join(self.change_types))
        if self.disclosure_taxonomies:
            parts.append("; ".join(self.disclosure_taxonomies))
        if self.statuses:
            parts.append("status " + "/".join(self.statuses))
        if self.relationships:
            parts.append("/".join(self.relationships))
        if self.min_materiality is not None:
            parts.append(f"materiality ≥ {self.min_materiality:g}")
        if self.max_confidence is not None:
            parts.append(f"confidence ≤ {self.max_confidence:g}")
        return " · ".join(parts) if parts else "all loaded comparisons"


def _matches(patterns: Iterable[tuple[str, str]], text: str) -> list[str]:
    found = []
    for label, pattern in patterns:
        if re.search(pattern, text) and label not in found:
            found.append(label)
    return found


def find_companies(text: str, known: Iterable[str] = ()) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for ticker, aliases in COMPANY_ALIASES.items():
        for alias in aliases:
            # 'chase' alone is ambiguous inside 'jpmorgan chase' - harmless, same ticker.
            if re.search(rf"(?<![\w.]){re.escape(alias)}(?:'s)?(?![\w])", lowered):
                found.append(ticker)
                break
    for ticker in known:  # tickers present in the database but missing from the alias table
        if ticker not in found and re.search(rf"\b{re.escape(ticker.lower())}\b", lowered):
            found.append(ticker)
    return found


def resolve_years(text: str, runs: Iterable[tuple[int, int]] = ()) -> tuple[int | None, int | None]:
    """Map year mentions to (previous_year, current_year) using the loaded comparison runs.

    "between 2023 and 2024", "2023 to 2024", "2024-2025", "2023 vs 2024" -> explicit pair.
    "since 2023" / "from 2023" -> previous_year. A single year otherwise means the filing for that year,
    i.e. current_year, unless no loaded run ends in that year (then it is read as previous_year).
    """
    lowered = text.lower()
    pair = re.search(rf"{YEAR_RE}\s*(?:-|–|—|/|to|and|vs\.?|versus|→|->)\s*{YEAR_RE}", lowered)
    if pair:
        a, b = sorted((int(pair.group(1)), int(pair.group(2))))
        if a != b:
            return a, b
    years = [int(y) for y in re.findall(rf"\b{YEAR_RE}\b", lowered)]
    if not years:
        return None, None
    year = years[0]
    if re.search(rf"\b(?:since|from|after)\s+(?:fy\s*)?{year}\b", lowered):
        return year, None
    run_list = list(runs)
    if run_list and not any(c == year for _, c in run_list) and any(p == year for p, _ in run_list):
        return year, None
    return None, year


def find_limit(text: str) -> int | None:
    lowered = re.sub(r"\bitem\s*\d+\s*a?\b|\b(?:fy\s*)?20\d{2}\b|\b10-?k\b", " ", text.lower())
    m = re.search(r"\b(?:top|first|last|best|biggest|largest|most\s+\w+\s+)?\s*(\d{1,3}|"
                  + "|".join(NUMBER_WORDS) + r")\s+(?:most\s+)?(?:\w+\s+){0,2}(?:changes?|disclosures?|items?|"
                  r"examples?|risk\s+factors?|results?|rows?|alignments?|matches?)\b", lowered)
    m = m or re.search(r"\btop\s+(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")\b", lowered)
    if not m:
        return None
    raw = m.group(1)
    value = NUMBER_WORDS.get(raw) or int(raw)
    if 2000 <= value <= 2100:  # a year, not a count
        return None
    return max(1, min(value, 100))


def find_threshold(text: str, keyword: str) -> float | None:
    """Explicit numeric thresholds such as 'materiality above 0.7' or 'confidence below 80%'."""
    m = re.search(rf"{keyword}\w*(?:\s+score)?\s*(?:of\s+)?(?:is\s+)?(?:>=|<=|>|<|above|below|over|under|"
                  rf"less\s+than|more\s+than|greater\s+than|at\s+least|at\s+most)\s*(\d*\.?\d+)\s*(%?)", text.lower())
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2) == "%" or value > 1:
        value /= 100
    return value


def normalise_match_id(value: str) -> str:
    """'BAC_2023_2024_m4' -> 'bac_2023_2024_M0004' (the pipeline's canonical spelling)."""
    head, number = value.rsplit("_", 1)
    return f"{head.lower()}_M{int(number[1:]):04d}"


def parse_filters(question: str, runs: Iterable[tuple[str, int, int]] = ()) -> QueryFilters:
    """Extract every filter the question states. ``runs`` is (company, previous_year, current_year) for loaded runs."""
    run_list = list(runs)
    text = question.lower()
    filters = QueryFilters()
    filters.match_ids = sorted({normalise_match_id(m) for m in MATCH_ID_RE.findall(question)})
    filters.companies = find_companies(question, {r[0] for r in run_list})
    company_runs = [(p, c) for co, p, c in run_list if not filters.companies or co in filters.companies]
    filters.previous_year, filters.current_year = resolve_years(question, company_runs or [(p, c) for _, p, c in run_list])
    filters.items = _matches(ITEM_PATTERNS, text)
    if "1A" in filters.items and "1" in filters.items and not re.search(r"\bitem\s*1\b(?!\s*a)", text):
        filters.items.remove("1")
    filters.change_types = _matches(CHANGE_TYPE_PATTERNS, text)
    # "what's new"/"new risk factors" mean New; "new" inside "renewed"/"newer" is excluded by the word boundary.
    filters.disclosure_taxonomies = _matches(DISCLOSURE_TAXONOMY_PATTERNS, re.sub(STATUS_PATTERNS[1][1], " ", text))
    filters.statuses = _matches(STATUS_PATTERNS, text)
    filters.relationships = _matches(RELATIONSHIP_PATTERNS, text)
    filters.limit = find_limit(question)
    filters.min_materiality = find_threshold(question, "materiality")
    filters.max_confidence = find_threshold(question, "confidence")
    return filters
