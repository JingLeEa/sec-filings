"""Peer-comparison lookup: answer cross-company questions from precomputed peer-agent output first.

Order of work for a peer question (Intent.PEER_COMPARE):
  1. coverage   - which loaded peer runs cover the named companies and year (cheap, one query)
  2. lookup     - comparisons for those company pairs, either direction, ranked by topic when one is given
  3. no-match   - target disclosures on the topic where the agent found no comparable peer disclosure
  4. context    - each side's year-over-year change from alignment_changes, joined by disclosure ID
  5. fallback   - only when 2 finds nothing: per-company alignment search (topic) or change profiles (no topic)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .db import Database, DatabaseError, QueryResult
from .entities import QueryFilters
from .queries import SqlQuery
from .retrieval import SYNONYMS, tsquery_text

PAIR_DOCUMENT = """(
    setweight(to_tsvector('english', coalesce(v.topic_a, '') || ' ' || coalesce(v.topic_b, '') || ' ' ||
                                     coalesce(v.summary_a, '') || ' ' || coalesce(v.summary_b, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(v.comparison, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(v.quote_a, '') || ' ' || coalesce(v.quote_b, '')), 'C'))"""
TARGET_DOCUMENT = """(
    setweight(to_tsvector('english', coalesce(t.target_topic, '') || ' ' || coalesce(t.target_summary, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(res.rationale, '')), 'C'))"""
DEFAULT_LIMIT = 10


def topic_tsquery(topic: str) -> str:
    """Stricter than alignment search: every word of a multi-word topic must appear (or the phrase itself),
    so 'export controls' does not match 'change in control'. Single words still expand through SYNONYMS."""
    words = re.findall(r"[a-z0-9]+", topic.lower())
    if not words:
        return ""
    if len(words) == 1:
        return tsquery_text(SYNONYMS.get(words[0], [words[0]]))
    groups = []
    for word in words:
        options = [word] + [o for o in SYNONYMS.get(word, []) if " " not in o]
        groups.append("(" + " | ".join(dict.fromkeys(re.sub(r"[^a-z0-9]", "", o.lower()) for o in options)) + ")")
    return f"({' <-> '.join(words)}) | ({' & '.join(groups)})"


@dataclass
class PeerLookup:
    results: list[QueryResult] = field(default_factory=list)
    found: bool = False
    covered_pairs: list[tuple[str, str]] = field(default_factory=list)
    missing_pairs: list[tuple[str, str]] = field(default_factory=list)
    partial_runs: list[str] = field(default_factory=list)
    fiscal_year: int | None = None
    notes: list[str] = field(default_factory=list)


def fiscal_year(filters: QueryFilters) -> int | None:
    """Peer comparisons are same-year; '2024 10-K' and 'between 2023 and 2024' both resolve to the later filing."""
    return filters.current_year or filters.previous_year


def wanted_pairs(companies: list[str], runs: list[dict]) -> list[tuple[str, str]]:
    """Unordered company pairs the question asks about. One company -> pairs with every peer in loaded runs."""
    if len(companies) >= 2:
        return [(a, b) for i, a in enumerate(companies) for b in companies[i + 1:]]
    company = companies[0]
    others: list[str] = []
    for run in runs:
        members = [run["target"], *run["peers"]]
        if company in members:
            others.extend(m for m in members if m != company and m not in others)
    return [(company, other) for other in others]


def _pair_condition(alias: str = "v") -> str:
    return (f"(({alias}.company_a = %(pa)s AND {alias}.company_b = %(pb)s) OR "
            f"({alias}.company_a = %(pb)s AND {alias}.company_b = %(pa)s))")


class PeerStore:
    def __init__(self, db: Database):
        self.db = db

    def available(self) -> bool:
        try:
            row = self.db.query("peer_tables", "SELECT to_regclass('public.peer_comparison_pairs') AS v").rows[0]
            return row["v"] is not None
        except DatabaseError:
            return False

    def lookup(self, filters: QueryFilters, topic: str | None, *, limit: int | None = None) -> PeerLookup:
        out = PeerLookup(fiscal_year=fiscal_year(filters))
        if not filters.companies:
            out.notes.append("peer lookup needs at least one company")
            return out
        if not self.available():
            out.notes.append("no peer-comparison tables yet (load reports with scripts/load_peer_comparisons_to_postgres.py)")
            return out
        year = out.fiscal_year
        coverage = self.db.query("peer_coverage", """
            SELECT peer_run_id, target, fiscal_year, peers, items, run_complete, jobs_completed, jobs_planned
            FROM peer_comparison_runs
            WHERE (%(year)s::int IS NULL OR fiscal_year = %(year)s)
              AND (target = ANY(%(companies)s) OR peers && %(companies)s)
            ORDER BY fiscal_year DESC, target""", {"year": year, "companies": filters.companies})
        out.results.append(coverage)
        runs = coverage.rows
        pairs = wanted_pairs(filters.companies, runs)
        for a, b in pairs:
            covering = [r for r in runs if (r["target"] == a and b in r["peers"]) or (r["target"] == b and a in r["peers"])]
            (out.covered_pairs if covering else out.missing_pairs).append((a, b))
            out.partial_runs.extend(f"{r['target']} {r['fiscal_year']} ({r['jobs_completed']}/{r['jobs_planned']} "
                                    "disclosures)" for r in covering if not r["run_complete"])
        out.partial_runs = list(dict.fromkeys(out.partial_runs))
        if not out.covered_pairs:
            return out

        n = max(1, min(limit or filters.limit or DEFAULT_LIMIT, 50))
        tsq = topic_tsquery(topic) if topic else ""
        comparison_rows: list[dict] = []
        no_match_rows: list[dict] = []
        for a, b in out.covered_pairs:
            params = {"pa": a, "pb": b, "year": year, "items": filters.items or None, "tsq": tsq or None}
            where = (f"{_pair_condition()} AND (%(year)s::int IS NULL OR v.fiscal_year = %(year)s) "
                     "AND (%(items)s::text[] IS NULL OR v.item = ANY(%(items)s))")
            if tsq:
                sql = f"""
                    SELECT v.*, round(ts_rank_cd({PAIR_DOCUMENT}, to_tsquery('english', %(tsq)s), 32)::numeric, 4) AS relevance
                    FROM peer_comparison_pairs v
                    WHERE {where} AND {PAIR_DOCUMENT} @@ to_tsquery('english', %(tsq)s)
                    ORDER BY relevance DESC, (v.relationship = 'same_topic') DESC, v.retrieval_score DESC NULLS LAST
                    LIMIT {n}"""
            else:
                sql = f"""
                    SELECT v.*, NULL::numeric AS relevance
                    FROM peer_comparison_pairs v WHERE {where}
                    ORDER BY (v.relationship = 'same_topic') DESC, v.retrieval_score DESC NULLS LAST, v.comparison_id
                    LIMIT {n}"""
            comparison_rows.extend(self.db.query("peer_comparisons", sql, params).rows)

            no_match_sql = f"""
                SELECT r.target AS company_a, res.peer AS company_b, r.fiscal_year, t.item, t.target_disclosure_id
                       AS disclosure_a, t.target_topic AS topic_a, t.target_summary AS summary_a, res.status, res.rationale
                FROM peer_comparison_results res
                JOIN peer_comparison_targets t USING (peer_run_id, target_disclosure_id)
                JOIN peer_comparison_runs r USING (peer_run_id)
                WHERE res.status <> 'compared'
                  AND ((r.target = %(pa)s AND res.peer = %(pb)s) OR (r.target = %(pb)s AND res.peer = %(pa)s))
                  AND (%(year)s::int IS NULL OR r.fiscal_year = %(year)s)
                  AND (%(items)s::text[] IS NULL OR t.item = ANY(%(items)s))
                  {"AND " + TARGET_DOCUMENT + " @@ to_tsquery('english', %(tsq)s)" if tsq else ""}
                ORDER BY t.target_disclosure_id LIMIT 5"""
            no_match_rows.extend(self.db.query("peer_no_match", no_match_sql, params).rows)

        if tsq:
            comparison_rows.sort(key=lambda r: -(float(r["relevance"] or 0)))
        comparison_rows = comparison_rows[:n]
        out.results.append(QueryResult("peer_comparisons", "-- per-pair peer_comparison_pairs lookup (see notes)",
                                       {"pairs": out.covered_pairs, "topic": topic, "year": year},
                                       columns=list(comparison_rows[0].keys()) if comparison_rows else [],
                                       rows=comparison_rows))
        if no_match_rows:
            out.results.append(QueryResult("peer_no_match", "-- targets the agent could not match", None,
                                           columns=list(no_match_rows[0].keys()), rows=no_match_rows))
        out.found = bool(comparison_rows)
        if comparison_rows:
            out.results.append(self.year_over_year(comparison_rows))
        return out

    def year_over_year(self, rows: list[dict]) -> QueryResult:
        """How each compared disclosure changed in its own company's alignments (both adjacent windows)."""
        ids = sorted({r["disclosure_a"] for r in rows} | {r["disclosure_b"] for r in rows})
        query = SqlQuery("peer_year_over_year", """
            SELECT am.disclosure_id, am.side, v.company, v.previous_year, v.current_year, v.match_id, v.status,
                   v.change_type, v.material, v.materiality_score, v.key_change
            FROM alignment_members am
            JOIN alignment_changes v ON v.run_id = am.run_id AND v.match_id = am.match_id
            WHERE am.disclosure_id = ANY(%(ids)s)
            ORDER BY am.disclosure_id, v.previous_year""", {"ids": ids})
        return self.db.query(query.name, query.sql, query.params)


def fallback_filters(filters: QueryFilters) -> QueryFilters:
    """Alignment filters for the same companies/year: the peer year is the alignment's current_year."""
    year = fiscal_year(filters)
    return replace(filters, current_year=year, previous_year=None if year else filters.previous_year,
                   disclosure_taxonomies=[])
