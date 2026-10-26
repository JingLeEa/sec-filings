"""PostgreSQL retrieval for evidence-grounded synthesis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _rows_as_dicts(cursor) -> list[dict[str, Any]]:
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_change_evidence(conn, company: str, previous_year: int,
                         current_year: int, limit: int = 500) -> list[dict]:
    """Load materiality-ranked alignments with members and source sentences."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                ar.run_id, ar.company, ar.previous_year, ar.current_year,
                a.match_id, a.status AS alignment_status, a.relationship,
                a.change_analysis->>'final_taxonomy' AS change_taxonomy,
                a.extra->'materiality_analysis' AS materiality_analysis,
                a.evidence
            FROM alignments AS a
            JOIN alignment_runs AS ar ON ar.run_id = a.run_id
            WHERE LOWER(ar.company) = LOWER(%s)
              AND ar.previous_year = %s
              AND ar.current_year = %s
              AND ar.run_complete = TRUE
            ORDER BY
                (a.extra->'materiality_analysis'
                    ->>'materiality_score')::numeric DESC NULLS LAST,
                a.match_id
            LIMIT %s
            """,
            (company, previous_year, current_year, limit),
        )
        changes = _rows_as_dicts(cursor)
    if not changes:
        return []

    run_ids = list({change["run_id"] for change in changes})
    match_ids = list({change["match_id"] for change in changes})
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT am.run_id, am.match_id, am.side, am.disclosure_id,
                   am.item, am.section, am.taxonomy, am.summary,
                   am.extraction_status
            FROM alignment_members AS am
            WHERE am.run_id = ANY(%s) AND am.match_id = ANY(%s)
            ORDER BY am.match_id, am.side, am.disclosure_id
            """,
            (run_ids, match_ids),
        )
        members = _rows_as_dicts(cursor)

    members_by_key: dict[tuple, list[dict]] = {}
    for member in members:
        key = (member["run_id"], member["match_id"])
        members_by_key.setdefault(key, []).append({
            "side": member["side"],
            "disclosure_id": member["disclosure_id"],
            "item": member["item"],
            "section": member["section"],
            "taxonomy": member["taxonomy"],
            "summary": member["summary"],
            "extraction_status": member["extraction_status"],
        })

    results = []
    for change in changes:
        source_evidence = []
        for disclosure in change.get("evidence") or []:
            if not isinstance(disclosure, dict):
                continue
            for sentence in disclosure.get("sentences") or []:
                if isinstance(sentence, dict):
                    source_evidence.append({
                        "disclosure_id": disclosure.get("disclosure_id"),
                        "sentence_id": sentence.get("sentence_id"),
                        "paragraph_id": sentence.get("paragraph_id"),
                        "text": sentence.get("text"),
                        "source_url": sentence.get("source_url"),
                    })
        materiality = change.get("materiality_analysis")
        if not isinstance(materiality, dict):
            materiality = None
        results.append({
            "company": change["company"],
            "previous_year": change["previous_year"],
            "current_year": change["current_year"],
            "match_id": change["match_id"],
            "alignment_status": change["alignment_status"],
            "relationship": change["relationship"],
            "taxonomy": change["change_taxonomy"],
            "materiality": materiality,
            "members": members_by_key.get((change["run_id"], change["match_id"]), []),
            "source_evidence": source_evidence,
            "evidence_count": len(source_evidence),
        })
    return results


def fetch_financial_metrics(conn, company: str, years: list[int]) -> list[dict]:
    """Fetch annual company-wide XBRL facts from the loaded XBRL view."""
    if not years:
        return []
    concepts = {
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:GrossProfit",
        "us-gaap:OperatingIncomeLoss",
        "us-gaap:NetIncomeLoss",
        "us-gaap:EarningsPerShareDiluted",
        "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap:Assets",
        "us-gaap:AssetsCurrent",
        "us-gaap:LiabilitiesCurrent",
        "us-gaap:StockholdersEquity",
        "us-gaap:DebtLongtermAndShorttermCombinedAmount",
    }
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH eligible AS (
                SELECT company, source_filing_year, form_type, filing_id,
                       extraction_id, taxonomy_year, concept, label, amount, unit,
                       concept_period_type, metric_scope, value_index, value_text,
                       period_start_date, period_end_date, instant_date, dimensions,
                       status,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_filing_year, concept
                           ORDER BY COALESCE(period_end_date, instant_date) DESC,
                                    value_index DESC
                       ) AS period_rank
                FROM xbrl_value_context
                WHERE UPPER(company) = UPPER(%s)
                  AND source_filing_year = ANY(%s)
                  AND concept = ANY(%s)
                  AND dimensions = '[]'::jsonb
                  AND status = 'reported'
                  AND amount IS NOT NULL
                  AND metric_scope = 'company_wide'
                  AND (
                      (concept_period_type = 'duration'
                       AND period_start_date IS NOT NULL
                       AND period_end_date IS NOT NULL
                       AND period_end_date - period_start_date BETWEEN 300 AND 380)
                      OR
                      (concept_period_type = 'instant' AND instant_date IS NOT NULL)
                  )
            )
            SELECT company, source_filing_year, form_type, filing_id,
                   extraction_id, taxonomy_year, concept, label, amount, unit,
                   concept_period_type, metric_scope, value_index, value_text,
                   period_start_date, period_end_date, instant_date,
                   dimensions, status
            FROM eligible WHERE period_rank = 1
            ORDER BY source_filing_year, concept
            """,
            (company, years, sorted(concepts)),
        )
        rows = cursor.fetchall()

    metrics = []
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        (row_company, year, form_type, filing_id, extraction_id, taxonomy_year,
         concept, label, amount, unit, period_type, scope, value_index, value_text,
         period_start, period_end, instant_date, dimensions, status) = row
        period_date = period_end or instant_date
        metric = {
            "id": (f"xbrl:{row_company.upper()}:{year}:{filing_id}:{extraction_id}:"
                   f"{concept}:{period_date.isoformat()}:{value_index}"),
            "company": row_company.upper(),
            "concept": concept,
            "label": label,
            "definition": None,
            "unit": unit,
            "year": int(year),
            "value": float(amount),
            "period_type": "FY" if period_type == "duration" else "instant",
            "concept_period_type": period_type,
            "period_start_date": period_start.isoformat() if period_start else None,
            "period_end_date": period_end.isoformat() if period_end else None,
            "instant_date": instant_date.isoformat() if instant_date else None,
            "source_filing_year": int(year),
            "form_type": form_type,
            "filing_id": int(filing_id),
            "extraction_id": int(extraction_id),
            "taxonomy_year": taxonomy_year,
            "value_index": value_index,
            "value_text": value_text,
            "dimensions": dimensions,
            "metric_scope": scope,
            "status": status,
        }
        grouped[(year, concept)].append(metric)
        metrics.append(metric)

    duplicates = {
        key: [record["id"] for record in records]
        for key, records in grouped.items() if len(records) > 1
    }
    if duplicates:
        raise ValueError(
            "Ambiguous financial facts remain after annual period selection: "
            f"{duplicates}"
        )
    return metrics
