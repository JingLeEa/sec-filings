"""Synthesis-report lookup: answer summary and financial-trend questions from precomputed agent reports first.

A synthesis report (agents/synthesis.py) covers one company over a span of fiscal years: an executive summary,
key findings that cite alignment match_ids, a longitudinal and a financial analysis, and financial trends
computed in code from XBRL facts. The agent saves each report to synthesis_reports (company, years, report
jsonb); the chatbot reads it through the chatbot_synthesis_* views (db/init/11_chatbot_synthesis.sql).
Report files made elsewhere load with scripts/load_synthesis_to_postgres.py.

Summary questions use the report covering the asked company and years; without one the chatbot falls back to
its SQL summary over the alignments. Financial questions read the report's computed trends; the alignments hold
no financial figures, so without a report the chatbot says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .db import Database, DatabaseError, QueryResult
from .entities import QueryFilters

# Question words -> trend metric name patterns (SQL LIKE on the lower-cased metric, as the agent names them:
# growth rows use the XBRL label, e.g. "Revenue from Contract with Customer...",
# "Net Cash Provided by (Used in) Operating Activities"; ratio rows a fixed name, e.g. "Gross margin").
METRIC_PATTERNS: list[tuple[str, list[str]]] = [
    (r"\bgross\s+margins?\b", ["gross margin%"]),
    (r"\boperating\s+margins?\b", ["operating margin%"]),
    (r"\bnet\s+margins?\b|\bprofit\s+margins?\b", ["net margin%"]),
    (r"\bmargins?\b|\bprofitab\w*", ["gross margin%", "operating margin%", "net margin%"]),
    (r"\brevenues?\b|\bsales\b|\btop[\s-]line\b", ["revenue%", "net revenue%", "total revenue%"]),
    (r"\bgross\s+profit\b", ["gross profit%"]),
    (r"\boperating\s+income\b|\boperating\s+profit\b", ["operating income%"]),
    (r"\bnet\s+income\b|\bearnings\b(?!\s+per)|\bbottom[\s-]line\b", ["net income%"]),
    (r"\beps\b|\bearnings\s+per\s+share\b", ["%eps%", "%earnings per share%"]),
    (r"\bcash\s+flows?\b|\bcash\s+from\s+operations\b", ["operating cash flow%", "%operating activit%"]),
    (r"\bcapex\b|\bcapital\s+expenditures?\b", ["capital expenditure%", "payments to acquire property, plant%"]),
    (r"\bcurrent\s+ratio\b|\bliquidity\b", ["current ratio%"]),
    (r"\bdebt\b|\bleverage\b", ["debt%"]),
    (r"\bassets\b", ["assets%", "total assets%"]),
    (r"\bequity\b", ["%equity%"]),
]
SPECIFIC_MARGIN = 3   # the first three patterns: a named margin wins over the generic "margin" rule
FINANCIAL_RE = re.compile("|".join(p for p, _ in METRIC_PATTERNS)
                          + r"|\bfinancial\s+(?:trends?|performance|results|metrics|figures|position)\b|\bfinancials\b",
                          re.IGNORECASE)


def wanted_metrics(question: str) -> list[str]:
    """LIKE patterns for the trend metrics the question asks about; empty = all trends."""
    patterns: list[str] = []
    for index, (regex, likes) in enumerate(METRIC_PATTERNS):
        if index == SPECIFIC_MARGIN and patterns:
            continue
        if re.search(regex, question, re.IGNORECASE):
            patterns.extend(p for p in likes if p not in patterns)
    return patterns


def span_for(filters: QueryFilters) -> tuple[int | None, int | None]:
    """The years a report must cover. '2024 10-K' = 2023-2024; 'from 2023' = 2023-2024; '2023 to 2025' as given."""
    p, c = filters.previous_year, filters.current_year
    if p and c:
        return p, c
    if c:
        return c - 1, c
    if p:
        return p, p + 1
    return None, None


@dataclass
class ReportLookup:
    results: list[QueryResult] = field(default_factory=list)
    found: bool = False
    companies_with_reports: list[str] = field(default_factory=list)
    companies_without: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ReportStore:
    def __init__(self, db: Database):
        self.db = db

    def available(self) -> bool:
        try:
            return self.db.query("synthesis_tables",
                                 "SELECT to_regclass('public.chatbot_synthesis_reports') AS t").rows[0]["t"] is not None
        except DatabaseError:
            return False

    def _reports(self, companies: list[str], filters: QueryFilters) -> QueryResult:
        start, end = span_for(filters)
        return self.db.query("synthesis_report", """
            SELECT DISTINCT ON (r.company)
                   r.report_key, r.company, r.start_year, r.end_year, r.executive_summary, r.longitudinal_analysis,
                   r.financial_analysis, r.limitations, r.valid, r.loaded_at,
                   r.material_changes_selected, r.financial_metrics,
                   (SELECT max(a.loaded_at) FROM alignment_runs a
                     WHERE a.company = r.company AND a.previous_year >= r.start_year AND a.current_year <= r.end_year)
                       > r.loaded_at AS alignments_newer
            FROM chatbot_synthesis_reports r
            WHERE r.company = ANY(%(companies)s)
              AND (%(start)s::int IS NULL OR (r.start_year <= %(start)s AND r.end_year >= %(end)s))
            ORDER BY r.company, (r.end_year - r.start_year), r.end_year DESC""",
                             {"companies": companies, "start": start, "end": end})

    def lookup(self, filters: QueryFilters, *, topic: str | None = None, financial_question: str | None = None,
               limit: int = 12) -> ReportLookup:
        out = ReportLookup()
        if not filters.companies:
            out.notes.append("synthesis reports are per company; name a company to use one")
            return out
        if not self.available():
            out.notes.append("no synthesis reports table yet (run the synthesis agent or scripts/load_synthesis_to_postgres.py)")
            return out
        reports = self._reports(filters.companies, filters)
        out.results.append(reports)
        found = {r["company"] for r in reports.rows}
        out.companies_with_reports = [c for c in filters.companies if c in found]
        out.companies_without = [c for c in filters.companies if c not in found]
        out.found = bool(reports.rows)
        for r in reports.rows:
            if r["alignments_newer"]:
                out.notes.append(f"alignment data for {r['company'].upper()} was reloaded after its synthesis report "
                                 "was loaded; the report may be out of date")
            if r["valid"] is False:
                out.notes.append(f"the {r['company'].upper()} synthesis report failed the agent's own validation")
        if not reports.rows:
            return out
        ids = [r["report_key"] for r in reports.rows]
        if financial_question is not None:
            out.results.append(self.trends(ids, wanted_metrics(financial_question)))
            return out
        out.results.extend(self.findings(ids, topic, limit))
        return out

    def findings(self, report_ids: list[str], topic: str | None, limit: int) -> list[QueryResult]:
        params = {"ids": report_ids, "topic": topic}
        topic_sql = ("AND to_tsvector('english', f.title || ' ' || f.analysis) @@ "
                     "websearch_to_tsquery('english', %(topic)s)") if topic else ""
        findings = self.db.query("synthesis_findings", f"""
            SELECT r.company, r.start_year, r.end_year, f.ordinal, f.title, f.analysis, f.match_ids, f.metric_ids
            FROM chatbot_synthesis_findings f JOIN chatbot_synthesis_reports r USING (report_key, company)
            WHERE f.report_key = ANY(%(ids)s) {topic_sql}
            ORDER BY r.company, f.ordinal LIMIT {int(limit)}""", params)
        if topic and not findings.rows:   # topic not in the findings: show the report's findings, say so
            findings = self.findings(report_ids, None, limit)[0]
            findings.name = "synthesis_findings_all"
        match_ids = sorted({m for row in findings.rows for m in row["match_ids"]})
        evidence = self.db.query("synthesis_finding_evidence", """
            SELECT company, previous_year, current_year, match_id, item, section, change_type, material,
                   materiality_score, key_change, left(previous_text, 400) AS previous_excerpt,
                   left(current_text, 400) AS current_excerpt
            FROM alignment_changes WHERE match_id = ANY(%(ids)s) ORDER BY match_id""", {"ids": match_ids})
        return [findings, evidence]

    def trends(self, report_ids: list[str], metrics: list[str]) -> QueryResult:
        return self.db.query("synthesis_trends", """
            SELECT r.company, t.kind, t.metric, t.unit, t.year, t.from_year, t.to_year, t.old_value, t.new_value,
                   t.value, round(t.growth_pct, 2) AS growth_pct, t.formula, t.source_ids
            FROM chatbot_synthesis_trends t JOIN chatbot_synthesis_reports r USING (report_key, company)
            WHERE t.report_key = ANY(%(ids)s)
              AND (%(metrics)s::text[] IS NULL OR lower(t.metric) LIKE ANY(%(metrics)s))
            ORDER BY r.company, t.kind DESC, t.metric, coalesce(t.year, t.to_year)""",
                             {"ids": report_ids, "metrics": metrics or None})
