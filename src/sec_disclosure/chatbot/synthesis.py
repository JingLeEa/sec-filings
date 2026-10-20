"""Answer synthesis: an evidence pack from query results, an LLM prompt that must stay inside it,
and deterministic templated answers for offline mode (or when the LLM call fails)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from .db import QueryResult
from .entities import COMPANY_NAMES
from .planner import Intent, Plan

TEXT_LIMITS = {"previous_excerpt": 500, "current_excerpt": 500, "explanation": 450, "materiality_reason": 450,
               "previous_summary": 350, "current_summary": 350, "semantic_rationale": 200}
MAX_ROWS_PER_TABLE = 25
MAX_PACK_CHARS = 60000

SYSTEM_PROMPT = """You are an analyst assistant for SEC 10-K disclosure changes. You answer ONLY from the evidence
pack supplied with each question: rows queried from a PostgreSQL database built by a disclosure-comparison
pipeline (alignment -> change detection -> materiality).

Data semantics:
- Each row is one alignment between a previous-year and current-year disclosure (match_id like bac_2023_2024_M0012).
- change_type is the final change label: Unchanged, Modified, Reworded, Expanded, Reduced, New (current-only),
  Removed (previous-only). It is NULL for rows still in needs_review (not analysed yet).
- semantic_label / lexical_label are the embedding-based and TF-IDF/difflib labels (semantic_label may also be
  'Relocated' = same content moved to another section); semantic_score is the
  normalised embedding similarity; length_ratio = current length / previous length.
- material, materiality_score (0-1) and confidence_score (0-1) come from an LLM materiality assessment;
  key_change and materiality_reason are its explanation. Summaries are model-generated; previous_excerpt and
  current_excerpt are the original filing sentences.
- Years are fiscal years of the 10-K (2024 = the FY2024 10-K). Tickers: bac = Bank of America,
  jpm = JPMorgan Chase, intc = Intel.

Rules:
1. Use only facts, numbers and IDs that appear in the evidence pack. Never invent a disclosure, match_id,
   score or quotation. If the evidence is empty or does not answer the question, say so plainly.
2. Cite match_ids in square brackets after each claim, e.g. [bac_2023_2024_M0107].
3. Quote filing text only by copying it exactly from previous_excerpt/current_excerpt, inside double quotes.
4. Report counts and scores exactly as given; say "materiality score 0.85" rather than vague words.
5. Be concise: a 1-2 sentence direct answer first, then supporting bullets or a small table. Mention the scope
   (companies, years, items) the data covers. Note when results were truncated (a row limit) or when
   needs_review rows were excluded from change labels.
6. Treat materiality and labels as pipeline outputs, not your own judgement; do not give investment advice."""


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _trim_row(row: dict) -> dict:
    trimmed = {}
    for key, value in row.items():
        if value is None or value == [] or value == "":
            continue
        value = _jsonable(value)
        limit = TEXT_LIMITS.get(key)
        if limit and isinstance(value, str) and len(value) > limit:
            value = value[:limit].rsplit(" ", 1)[0] + " …"
        trimmed[key] = value
    return trimmed


def evidence_pack(results: list[QueryResult]) -> list[dict]:
    pack = []
    for result in results:
        rows = [_trim_row(r) for r in result.rows[:MAX_ROWS_PER_TABLE]]
        entry = {"table": result.name, "row_count": len(result.rows), "rows": rows}
        if len(result.rows) > MAX_ROWS_PER_TABLE:
            entry["note"] = f"only the first {MAX_ROWS_PER_TABLE} of {len(result.rows)} rows are shown"
        pack.append(entry)
    return pack


def user_prompt(question: str, plan: Plan, pack: list[dict], notes: list[str]) -> str:
    body = json.dumps(pack, ensure_ascii=False, default=str)
    if len(body) > MAX_PACK_CHARS:
        body = body[:MAX_PACK_CHARS] + ' ... [evidence truncated]'
    return (f"Question: {question}\n\nQuery type: {plan.intent.value}\nFilters applied: {plan.filters.describe()}\n"
            + (f"Topic searched: {plan.topic}\n" if plan.topic else "")
            + ("Retrieval notes: " + "; ".join(notes) + "\n" if notes else "")
            + f"\nEvidence pack (JSON):\n{body}\n\nWrite the answer in Markdown following the rules.")


# ---------------------------------------------------------------- offline (template) answers

def _company(code: str) -> str:
    return COMPANY_NAMES.get(code, code.upper())


def _period(row: dict) -> str:
    return f"{row['previous_year']}→{row['current_year']}"


def _score(value) -> str:
    return "–" if value is None else f"{float(value):.2f}"


def _card(row: dict, *, why: bool = False) -> str:
    head = (f"- **[{row['match_id']}]** {_company(row['company'])} {_period(row)} · Item {row.get('item') or '?'}"
            f" · {row.get('change_type') or 'not analysed'}")
    if row.get("materiality_score") is not None:
        head += f" · materiality {_score(row['materiality_score'])} (confidence {_score(row.get('confidence_score'))})"
    lines = [head]
    if row.get("section"):
        lines.append(f"  - Section: {row['section']}")
    if row.get("key_change"):
        lines.append(f"  - Key change: {row['key_change']}")
    if why and row.get("materiality_reason"):
        lines.append(f"  - Why: {row['materiality_reason'][:400]}")
    elif why and row.get("explanation"):
        lines.append(f"  - Alignment note: {row['explanation'][:400]}")
    if row.get("length_ratio") is not None and row.get("change_type") in ("Expanded", "Reduced"):
        lines.append(f"  - Length ratio {float(row['length_ratio']):.2f}, word count Δ {row.get('word_count_delta')}")
    quote_key = "current_excerpt" if row.get("current_excerpt") else "previous_excerpt"
    if row.get(quote_key):
        label = "Current text" if quote_key == "current_excerpt" else "Previous text"
        quote = row[quote_key][:260].rsplit(" ", 1)[0]
        lines.append(f"  - {label}: \"{quote} …\"")
    return "\n".join(lines)


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_(no rows)_"
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        out.append("| " + " | ".join("" if row.get(c) is None else str(_jsonable(row.get(c))) for c in columns) + " |")
    return "\n".join(out)


def _by_name(results: list[QueryResult]) -> dict[str, QueryResult]:
    return {r.name: r for r in results}


def _total(result: QueryResult | None) -> int:
    return sum(int(r["n"]) for r in result.rows) if result else 0


def _pending_note(results: dict[str, QueryResult]) -> str:
    pending = results.get("pending_review_candidates")
    n = _total(pending)
    return (f"\n\n_{n} further one-sided candidate(s) are still in needs_review and have no change label yet._"
            if n else "")


def offline_answer(question: str, plan: Plan, results: list[QueryResult], notes: list[str]) -> str:
    r = _by_name(results)
    scope = plan.filters.describe()
    intent = plan.intent

    if intent == Intent.MATCH_DETAIL:
        rows = r["match_detail"].rows
        if not rows:
            return f"No alignment with ID {', '.join(plan.filters.match_ids)} exists in the database."
        parts = []
        for row in rows:
            parts.append(_card(row, why=True))
            if row.get("semantic_label") or row.get("lexical_label"):
                parts.append(f"  - Labels: semantic {row.get('semantic_label')} (score {_score(row.get('semantic_score'))}),"
                             f" lexical {row.get('lexical_label')}, LLM {row.get('llm_label')}")
            if row.get("explanation"):
                parts.append(f"  - Alignment explanation: {row['explanation'][:500]}")
        return "\n".join(parts)

    if intent == Intent.MATERIAL:
        cards = r["most_material_changes"].rows
        total = _total(r.get("material_counts_by_run"))
        if not cards:
            return f"No changes assessed as material were found for {scope}."
        return (f"**{total}** alignment(s) were assessed as material for {scope}. "
                f"The top {len(cards)} by materiality score:\n\n" + "\n".join(_card(c, why=True) for c in cards))

    if intent in (Intent.COUNT, Intent.LIST):
        counts = r["counts_by_run"].rows
        total = _total(r["counts_by_run"])
        cards = (r.get("examples") or r.get("matching_changes")).rows
        breakdown = "; ".join(f"{_company(c['company'])} {_period(c)}: {c['n']}" for c in counts)
        head = f"**{total}** alignment(s) match {scope}" + (f" ({breakdown})." if len(counts) > 1 else ".")
        if not total:
            return head.replace("match", "matched") + _pending_note(r)
        why = "why" in question.lower() or intent == Intent.LIST
        body = "\n".join(_card(c, why=why) for c in cards)
        shown = f"\n\nShowing {len(cards)} ranked by materiality score:\n\n" if cards else ""
        return head + shown + body + _pending_note(r)

    if intent == Intent.DISTRIBUTION:
        dist = r["change_type_distribution"].rows
        columns = (["item"] if plan.group_by_item else []) + ["change_type", "n", "pct", "material_yes", "avg_materiality"]
        top = next((d for d in dist if d["change_type"] not in ("Unchanged", "Not analysed (needs review)")), None)
        lead = (f"Across {scope}, the most common label is **{dist[0]['change_type']}** ({dist[0]['n']}, {dist[0]['pct']}%)"
                if dist else f"No alignments found for {scope}.")
        if top and dist and top is not dist[0]:
            lead += f"; the most common actual change is **{top['change_type']}** ({top['n']}, {top['pct']}%)"
        examples = r["most_material_examples"].rows
        return (lead + ".\n\n" + _table(dist, columns)
                + ("\n\nMost material examples:\n\n" + "\n".join(_card(c) for c in examples) if examples else ""))

    if intent == Intent.COMPARE:
        prof = r["change_profile_by_run"].rows
        cols = ["company", "previous_year", "current_year", "alignments", "unchanged", "modified", "reworded",
                "expanded", "reduced", "new", "removed", "needs_review", "material_yes", "pct_changed_of_analysed",
                "avg_materiality"]
        tops = r["top_material_per_run"].rows
        return (f"Change profile for {scope}:\n\n" + _table(prof, cols)
                + ("\n\nTop material changes per company/period:\n\n" + "\n".join(_card(c) for c in tops) if tops else ""))

    if intent == Intent.LOW_CONFIDENCE:
        cards = r["lowest_confidence"].rows
        total = _total(r["low_confidence_by_run"])
        threshold = plan.filters.max_confidence if plan.filters.max_confidence is not None else 0.8
        return (f"**{total}** materiality assessment(s) have confidence ≤ {threshold:g} for {scope}. "
                f"Lowest-confidence first:\n\n" + "\n".join(_card(c, why=True) for c in cards))

    if intent == Intent.DISAGREEMENTS:
        agree = r["agreement_rate"].rows[0] if r["agreement_rate"].rows else {}
        pairs = r["disagreement_pairs"].rows
        cards = r["disagreement_examples"].rows
        lead = (f"Semantic and lexical labels disagree on **{agree.get('disagree', 0)}** of "
                f"{agree.get('compared', 0)} analysed alignments for {scope}.")
        return (lead + "\n\n" + _table(pairs[:15], ["semantic_label", "lexical_label", "final_label", "n"])
                + "\n\nMost material disagreements:\n\n"
                + "\n".join(_card(c) + f"\n  - semantic {c.get('semantic_label')} vs lexical {c.get('lexical_label')}"
                            for c in cards))

    if intent == Intent.SUMMARY:
        dist = r["change_type_by_run"].rows
        parts = [f"Summary for {scope}:\n", _table(dist, ["company", "previous_year", "current_year", "change_type", "n",
                                                          "material_yes"])]
        items = r["changes_by_item"].rows
        if items:
            parts.append("\nBy item:\n\n" + _table(items, ["item", "changed", "material_yes", "total"]))
        for name, title in (("top_material_changes", "Most material changes"), ("top_new_disclosures", "Notable new disclosures"),
                            ("top_removed_disclosures", "Notable removed disclosures")):
            rows = r[name].rows
            if rows:
                parts.append(f"\n**{title}**\n\n" + "\n".join(_card(c) for c in rows))
        return "\n".join(parts)

    if intent == Intent.TOPIC:
        rows = r["topic_matches"].rows if "topic_matches" in r else []
        if not rows:
            return f"I found no alignments about “{plan.topic}” for {replace(plan.filters, disclosure_taxonomies=[]).describe()}."
        companies = sorted({row["company"] for row in rows})
        topic_filters = replace(plan.filters, disclosure_taxonomies=[])  # topic search does not filter by category
        head = (f"Found **{len(rows)}** relevant alignment(s) about “{plan.topic}” for {topic_filters.describe()} "
                f"({', '.join(_company(c) for c in companies)}), most relevant first:\n\n")
        return head + "\n".join(_card(c, why=True) for c in rows[:12])

    # custom SQL results
    if results:
        res = results[-1]
        return (f"Query returned {len(res.rows)} row(s):\n\n" + _table(res.rows[:25], res.columns[:10]))
    return "I could not route this question to a query. Try naming a company, year pair, item or change type."
