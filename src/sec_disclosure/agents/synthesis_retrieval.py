"""PostgreSQL retrieval for evidence-grounded synthesis."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json
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
                COALESCE(a.change_analysis->>'final_taxonomy', a.change_analysis->>'llm') AS change_taxonomy,
                a.extra->'materiality_analysis' AS materiality_analysis,
                a.extra->'manual_review' AS manual_review,
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
        human = change.get("manual_review") or {}
        evidence = (human.get("source_evidence") if human.get("source_fingerprint") else None)
        for disclosure in evidence or change.get("evidence") or []:
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


def fetch_financial_metrics(conn, company: str, years: list[int], *,
                            diagnostics: list[dict] | None = None) -> list[dict]:
    """Fetch annual company-wide XBRL facts from the loaded XBRL view.

    Instant facts are labeled with the calendar year of ``instant_date``. The
    view has no verified issuer fiscal-year-end date with which to validate it.
    Equal amounts for the same context are deduplicated even when their accuracy
    declarations differ; the selected source's metadata is retained unchanged.
    Differing comparative values use the latest selected source filing year.
    If that latest year is itself ambiguous, omit the context. Diagnostics retain
    every version and the selection/omission reason without changing stored facts.
    """
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
                       status, decimals, precision,
                       decimals IS NULL AS decimals_missing,
                       precision IS NULL AS precision_missing
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
                   dimensions, status, decimals, precision,
                   decimals_missing, precision_missing
            FROM eligible
            ORDER BY source_filing_year, concept
            """,
            (company, years, sorted(concepts)),
        )
        rows = cursor.fetchall()

    grouped: dict[tuple, list[tuple[dict, tuple]]] = defaultdict(list)
    for row in rows:
        (row_company, year, form_type, filing_id, extraction_id, taxonomy_year,
         concept, label, amount, unit, period_type, scope, value_index, value_text,
         period_start, period_end, instant_date, dimensions, status,
         decimals, precision, decimals_missing, precision_missing) = row
        period_date = period_end if period_type == "duration" else instant_date
        if period_date is None:
            raise ValueError(
                f"{concept} from source filing year {year} has no reporting-period date."
            )
        reporting_year = period_date.year
        if period_type == "duration":
            period_key = (concept, period_type, period_start, period_end, unit, scope)
        else:
            period_key = (concept, period_type, instant_date, unit, scope)
        metric = {
            "id": (f"xbrl:{row_company.upper()}:{year}:{filing_id}:{extraction_id}:"
                   f"{concept}:{period_date.isoformat()}:{value_index}"),
            "company": row_company.upper(),
            "concept": concept,
            "label": label,
            "definition": None,
            "unit": unit,
            "year": reporting_year,
            "value": Decimal(str(amount)),
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
            "decimals": decimals,
            "precision": precision,
            "decimals_missing": decimals_missing,
            "precision_missing": precision_missing,
        }
        equivalence_key = (
            Decimal(str(amount)),
            unit,
            json.dumps(dimensions, sort_keys=True, separators=(",", ":")),
            status,
            scope,
        )
        grouped[period_key].append((metric, equivalence_key))

    selected_metrics = []
    for period_key in sorted(grouped, key=lambda key: tuple(map(str, key))):
        records = grouped[period_key]
        ordered = sorted((metric for metric, _ in records), key=lambda metric: (
            metric['source_filing_year'], metric['filing_id'],
            metric['extraction_id'], metric['value_index'], metric['id']))
        # decimals/precision declare accuracy, not a different amount or scale.
        # Preserve existing provenance for equal values. A different amount in
        # a later annual filing is a new version, not an arithmetic/API failure.
        if len({equivalence_key for _, equivalence_key in records}) == 1:
            selected_metrics.append(ordered[0])
            continue
        latest_year = ordered[-1]['source_filing_year']
        latest = [(metric, key) for metric, key in records
                  if metric['source_filing_year'] == latest_year]
        ambiguous = len({key for _, key in latest}) > 1
        chosen = next(metric for metric in ordered if metric['source_filing_year'] == latest_year)
        diagnostic = {
            **{key: chosen[key] for key in ('concept', 'year', 'unit', 'metric_scope',
                'concept_period_type', 'period_start_date', 'period_end_date', 'instant_date')},
            'policy': 'latest_available_source_filing_year',
            'latest_source_filing_year': latest_year,
            'resolution': 'omitted_ambiguous_latest_filing' if ambiguous else 'selected_latest_filing',
            'selected_id': None if ambiguous else chosen['id'],
            'candidates': [{key: metric[key] for key in (
                'id', 'source_filing_year', 'form_type', 'filing_id', 'extraction_id',
                'value', 'value_text', 'decimals', 'precision',
                'decimals_missing', 'precision_missing')} for metric in ordered],
        }
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        if not ambiguous:
            chosen['fact_selection'] = {
                'policy': diagnostic['policy'],
                'differing_source_values': True,
                'alternative_source_ids': [metric['id'] for metric in ordered if metric['id'] != chosen['id']],
            }
            selected_metrics.append(chosen)
    return sorted(
        selected_metrics,
        key=lambda metric: (
            metric["year"], metric["concept"], metric["period_type"],
            metric["period_start_date"] or metric["instant_date"],
            metric["source_filing_year"], metric["id"],
        ),
    )
