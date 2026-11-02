"""Parameterised SQL for each intent, written against the ``alignment_changes`` view (db/init/09_chatbot.sql).

Every value from the question reaches PostgreSQL as a bound parameter, never as SQL text.
"""
from __future__ import annotations

from dataclasses import dataclass

from .entities import QueryFilters
from .planner import Intent, Plan

EXCERPT_CHARS = 600
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
LOW_CONFIDENCE_DEFAULT = 0.8

CARD_COLUMNS = f"""
    company, previous_year, current_year, match_id, item, section, disclosure_taxonomy, status, relationship,
    change_type, semantic_label, lexical_label, round(semantic_score::numeric, 4) AS semantic_score,
    round(length_ratio::numeric, 3) AS length_ratio, word_count_delta, material,
    materiality_score, confidence_score, key_change, materiality_reason, explanation,
    previous_summary, current_summary,
    left(previous_text, {EXCERPT_CHARS}) AS previous_excerpt,
    left(current_text, {EXCERPT_CHARS}) AS current_excerpt,
    previous_ids, current_ids"""


@dataclass(frozen=True)
class SqlQuery:
    name: str
    sql: str
    params: dict


def where_clause(filters: QueryFilters, *, alias: str = "", skip: tuple[str, ...] = ()) -> tuple[str, dict]:
    """AND-joined conditions for every filter that is set. ``skip`` drops named filters (e.g. change_types)."""
    p = f"{alias}." if alias else ""
    conditions: list[str] = []
    params: dict = {}

    def add(name: str, sql: str, value) -> None:
        if name in skip or value in (None, []):
            return
        conditions.append(sql)
        params[name] = value

    add("companies", f"{p}company = ANY(%(companies)s)", filters.companies)
    if filters.previous_year and filters.current_year and filters.current_year - filters.previous_year > 1:
        # A multi-year span ("2023 to 2025") covers every comparison window inside it.
        add("previous_year", f"{p}previous_year >= %(previous_year)s", filters.previous_year)
        add("current_year", f"{p}current_year <= %(current_year)s", filters.current_year)
    else:
        add("previous_year", f"{p}previous_year = %(previous_year)s", filters.previous_year)
        add("current_year", f"{p}current_year = %(current_year)s", filters.current_year)
    add("items", f"{p}item = ANY(%(items)s)", filters.items)
    add("change_types", f"{p}change_type = ANY(%(change_types)s)", filters.change_types)
    add("disclosure_taxonomies", f"{p}disclosure_taxonomy = ANY(%(disclosure_taxonomies)s)", filters.disclosure_taxonomies)
    add("statuses", f"{p}status = ANY(%(statuses)s)", filters.statuses)
    add("relationships", f"{p}relationship = ANY(%(relationships)s)", filters.relationships)
    add("match_ids", f"{p}match_id = ANY(%(match_ids)s)", filters.match_ids)
    add("material", f"{p}material = %(material)s", filters.material)
    add("min_materiality", f"{p}materiality_score {'>' if filters.min_materiality_strict else '>='} %(min_materiality)s",
        filters.min_materiality)
    add("max_confidence", f"{p}confidence_score {'<' if filters.max_confidence_strict else '<='} %(max_confidence)s",
        filters.max_confidence)
    return (" AND ".join(conditions) if conditions else "TRUE"), params


def _limit(filters: QueryFilters, default: int = DEFAULT_LIMIT) -> int:
    return max(1, min(filters.limit or default, MAX_LIMIT))


def _per_run_counts(where: str, params: dict, name: str = "counts_by_run") -> SqlQuery:
    return SqlQuery(name, f"""
        SELECT company, previous_year, current_year, count(*) AS n
        FROM alignment_changes WHERE {where}
        GROUP BY 1, 2, 3 ORDER BY 1, 2""", params)


def _cards(name: str, where: str, params: dict, order: str, limit: int) -> SqlQuery:
    return SqlQuery(name, f"SELECT {CARD_COLUMNS}\nFROM alignment_changes WHERE {where}\nORDER BY {order}\nLIMIT {int(limit)}",
                    params)


MATERIAL_ORDER = "materiality_score DESC NULLS LAST, confidence_score DESC NULLS LAST, match_id"
UNCHANGED_EXCLUDED = "change_type IS DISTINCT FROM 'Unchanged'"
# Counting what "changed": analysed rows with an actual change (not-analysed rows have no label yet).
CHANGED_ONLY = "change_type IS NOT NULL AND change_type <> 'Unchanged'"
# Breakdown dimensions a distribution question can ask for ("by status", "by change type and item").
GROUP_COLUMNS = {"change_type": "coalesce(change_type, 'Not analysed (needs review)')", "item": "item",
                 "status": "status", "company": "company", "relationship": "relationship",
                 "disclosure_taxonomy": "disclosure_taxonomy", "material": "coalesce(material, 'Not analysed')"}


def _pending_one_sided(filters: QueryFilters) -> SqlQuery | None:
    """Current-only / previous-only candidates still in needs_review: not yet labelled New/Removed."""
    wanted = [r for t, r in (("New", "current_only"), ("Removed", "previous_only")) if t in filters.change_types]
    if not wanted:
        return None
    where, params = where_clause(filters, skip=("change_types", "statuses", "relationships"))
    params = {**params, "pending_relationships": wanted}
    return SqlQuery("pending_review_candidates", f"""
        SELECT relationship, count(*) AS n
        FROM alignment_changes
        WHERE {where} AND status = 'needs_review' AND relationship = ANY(%(pending_relationships)s)
        GROUP BY 1""", params)


def build_queries(plan: Plan) -> list[SqlQuery]:
    f = plan.filters
    where, params = where_clause(f)
    if plan.exclude_unchanged and not f.change_types:
        where = f"{where} AND {UNCHANGED_EXCLUDED}"
    queries: list[SqlQuery] = []

    if plan.intent == Intent.MATCH_DETAIL:
        queries.append(SqlQuery("match_detail", """
            SELECT company, previous_year, current_year, match_id, item, section, disclosure_taxonomy, status,
                   relationship, match_method, change_type, lexical_label, semantic_label, llm_label,
                   semantic_score, lexical_cosine, sequence_match_ratio, length_ratio, word_count_delta,
                   semantic_rationale, material, materiality_score, confidence_score, key_change, materiality_reason,
                   explanation, review_reasons, verifier_review_reason, unmatched_type, previous_ids, current_ids,
                   previous_summary, current_summary,
                   left(previous_text, 3000) AS previous_excerpt, left(current_text, 3000) AS current_excerpt
            FROM alignment_changes WHERE match_id = ANY(%(match_ids)s) ORDER BY match_id""",
                                {"match_ids": f.match_ids}))

    elif plan.intent == Intent.MATERIAL:
        material_where = where if f.min_materiality is not None else f"{where} AND material = 'Yes'"
        queries.append(_per_run_counts(material_where, params, "material_counts_by_run"))
        queries.append(_cards("most_material_changes", material_where, params, MATERIAL_ORDER, _limit(f)))

    elif plan.intent == Intent.COUNT:
        if plan.exclude_unchanged and not f.change_types:
            where = f"{where_clause(f)[0]} AND {CHANGED_ONLY}"
        queries.append(_per_run_counts(where, params))
        queries.append(SqlQuery("count_by_item", f"""
            SELECT item, count(*) AS n FROM alignment_changes WHERE {where} GROUP BY 1 ORDER BY 2 DESC""", params))
        queries.append(_cards("examples", where, params, MATERIAL_ORDER, _limit(f, 8)))
        pending = _pending_one_sided(f)
        if pending:
            queries.append(pending)

    elif plan.intent == Intent.LIST:
        queries.append(_per_run_counts(where, params))
        queries.append(_cards("matching_changes", where, params, MATERIAL_ORDER, _limit(f)))
        pending = _pending_one_sided(f)
        if pending:
            queries.append(pending)

    elif plan.intent == Intent.DISTRIBUTION:
        dims = plan.group_by or (["item", "change_type"] if plan.group_by_item else ["change_type"])
        select = ", ".join(f"{GROUP_COLUMNS[d]} AS {d}" for d in dims)
        outer = dims[:-1]   # percentages within each outer group ("by item": share of each change type per item)
        queries.append(SqlQuery("change_type_distribution", f"""
            SELECT {select},
                   count(*) AS n,
                   round(100.0 * count(*) / sum(count(*)) OVER ({'PARTITION BY ' + ', '.join(GROUP_COLUMNS[d] for d in outer) if outer else ''}), 1) AS pct,
                   count(*) FILTER (WHERE material = 'Yes') AS material_yes,
                   round(avg(materiality_score)::numeric, 3) AS avg_materiality
            FROM alignment_changes WHERE {where}
            GROUP BY {', '.join(GROUP_COLUMNS[d] for d in dims)}
            ORDER BY {', '.join(outer + ['n DESC'])}""", params))
        queries.append(_per_run_counts(where, params))
        queries.append(_cards("most_material_examples", f"{where} AND {UNCHANGED_EXCLUDED}", params,
                              MATERIAL_ORDER, _limit(f, 5)))

    elif plan.intent == Intent.COMPARE:
        queries.append(SqlQuery("change_profile_by_run", f"""
            SELECT company, previous_year, current_year, count(*) AS alignments,
                   count(*) FILTER (WHERE change_type = 'Unchanged') AS unchanged,
                   count(*) FILTER (WHERE change_type = 'Modified')  AS modified,
                   count(*) FILTER (WHERE change_type = 'Reworded')  AS reworded,
                   count(*) FILTER (WHERE change_type = 'Expanded')  AS expanded,
                   count(*) FILTER (WHERE change_type = 'Reduced')   AS reduced,
                   count(*) FILTER (WHERE change_type = 'New')       AS new,
                   count(*) FILTER (WHERE change_type = 'Removed')   AS removed,
                   count(*) FILTER (WHERE status = 'needs_review')   AS needs_review,
                   count(*) FILTER (WHERE material = 'Yes')          AS material_yes,
                   round(100.0 * count(*) FILTER (WHERE change_type IS NOT NULL AND change_type <> 'Unchanged')
                         / nullif(count(change_type), 0), 1)          AS pct_changed_of_analysed,
                   round(avg(materiality_score)::numeric, 3)          AS avg_materiality
            FROM alignment_changes WHERE {where}
            GROUP BY 1, 2, 3 ORDER BY 2, 1""", params))
        queries.append(SqlQuery("material_changes_by_item", f"""
            SELECT company, previous_year, current_year, item, count(*) AS material_changes
            FROM alignment_changes WHERE {where} AND material = 'Yes'
            GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 5 DESC""", params))
        queries.append(SqlQuery("top_material_per_run", f"""
            SELECT * FROM (
                SELECT {CARD_COLUMNS},
                       row_number() OVER (PARTITION BY company, previous_year ORDER BY {MATERIAL_ORDER}) AS rank
                FROM alignment_changes WHERE {where} AND material = 'Yes') ranked
            WHERE rank <= 3 ORDER BY company, previous_year, rank""", params))

    elif plan.intent == Intent.LOW_CONFIDENCE:
        threshold = f.max_confidence if f.max_confidence is not None else LOW_CONFIDENCE_DEFAULT
        low_where, low_params = where_clause(f, skip=("max_confidence",))
        low_params = {**low_params, "threshold": threshold}
        low_where = f"{low_where} AND confidence_score {'<' if f.max_confidence_strict else '<='} %(threshold)s"
        queries.append(SqlQuery("confidence_distribution", f"""
            SELECT confidence_score, count(*) AS n, count(*) FILTER (WHERE material = 'Yes') AS material_yes
            FROM alignment_changes WHERE {where_clause(f, skip=("max_confidence",))[0]} AND confidence_score IS NOT NULL
            GROUP BY 1 ORDER BY 1""", where_clause(f, skip=("max_confidence",))[1]))
        queries.append(_per_run_counts(low_where, low_params, "low_confidence_by_run"))
        queries.append(_cards("lowest_confidence", low_where, low_params,
                              "confidence_score ASC, materiality_score DESC NULLS LAST, match_id", _limit(f, 12)))

    elif plan.intent == Intent.DISAGREEMENTS:
        dis_where = (f"{where} AND analysis_status = 'completed' AND semantic_label IS NOT NULL "
                     f"AND lexical_label IS NOT NULL AND semantic_label <> lexical_label")
        queries.append(SqlQuery("disagreement_pairs", f"""
            SELECT semantic_label, lexical_label, change_type AS final_label, count(*) AS n
            FROM alignment_changes WHERE {dis_where}
            GROUP BY 1, 2, 3 ORDER BY 4 DESC""", params))
        queries.append(_per_run_counts(dis_where, params, "disagreements_by_run"))
        queries.append(SqlQuery("agreement_rate", f"""
            SELECT count(*) FILTER (WHERE semantic_label = lexical_label) AS agree,
                   count(*) FILTER (WHERE semantic_label <> lexical_label) AS disagree,
                   count(*) AS compared
            FROM alignment_changes WHERE {where} AND analysis_status = 'completed'
              AND semantic_label IS NOT NULL AND lexical_label IS NOT NULL""", params))
        queries.append(_cards("disagreement_examples", dis_where, params, MATERIAL_ORDER, _limit(f, 10)))

    elif plan.intent == Intent.SUMMARY:
        queries.append(SqlQuery("change_type_by_run", f"""
            SELECT company, previous_year, current_year,
                   coalesce(change_type, 'Not analysed (needs review)') AS change_type, count(*) AS n,
                   count(*) FILTER (WHERE material = 'Yes') AS material_yes
            FROM alignment_changes WHERE {where}
            GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 5 DESC""", params))
        queries.append(SqlQuery("changes_by_item", f"""
            SELECT item, count(*) FILTER (WHERE {UNCHANGED_EXCLUDED} AND change_type IS NOT NULL) AS changed,
                   count(*) FILTER (WHERE material = 'Yes') AS material_yes, count(*) AS total
            FROM alignment_changes WHERE {where} GROUP BY 1 ORDER BY 3 DESC""", params))
        queries.append(_cards("top_material_changes", f"{where} AND material = 'Yes'", params, MATERIAL_ORDER,
                              _limit(f, 8)))
        for change in ("New", "Removed"):
            queries.append(_cards(f"top_{change.lower()}_disclosures", f"{where} AND change_type = '{change}'", params,
                                  MATERIAL_ORDER, 4))
    return queries


def topic_cards(hits: list[tuple[int, str, float]], filters: QueryFilters, *, exclude_unchanged: bool) -> SqlQuery:
    """Rows for retrieved alignments, kept in retrieval order and still subject to the question's filters."""
    where, params = where_clause(filters, skip=("disclosure_taxonomies",))
    if exclude_unchanged and not filters.change_types:
        where = f"{where} AND {UNCHANGED_EXCLUDED}"
    params = {**params, "hit_runs": [h[0] for h in hits], "hit_ids": [h[1] for h in hits],
              "hit_scores": [float(h[2]) for h in hits]}
    return SqlQuery("topic_matches", f"""
        SELECT {CARD_COLUMNS}, round(h.score::numeric, 4) AS relevance
        FROM unnest(%(hit_runs)s::bigint[], %(hit_ids)s::text[], %(hit_scores)s::float[]) WITH ORDINALITY
             AS h(hit_run, hit_match, score, rank)
        JOIN alignment_changes v ON v.run_id = h.hit_run AND v.match_id = h.hit_match
        WHERE {where}
        ORDER BY h.rank""", params)
