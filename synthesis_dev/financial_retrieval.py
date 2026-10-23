"""Retrieve validated financial facts from PostgreSQL for synthesis."""

from collections import defaultdict

from sec_disclosure.storage.disclosures import connection_arguments


# Use stable, standard concepts first. Avoid interchangeable COGS concepts
# because their definitions and reported amounts may differ.
DURATION_CONCEPTS = {
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:GrossProfit",
    "us-gaap:OperatingIncomeLoss",
    "us-gaap:NetIncomeLoss",
    "us-gaap:EarningsPerShareDiluted",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
}

INSTANT_CONCEPTS = {
    "us-gaap:Assets",
    "us-gaap:AssetsCurrent",
    "us-gaap:LiabilitiesCurrent",
    "us-gaap:StockholdersEquity",
    "us-gaap:DebtLongtermAndShorttermCombinedAmount",
}

FINANCIAL_CONCEPTS = DURATION_CONCEPTS | INSTANT_CONCEPTS


def fetch_financial_metrics(
    conn,
    company: str,
    years: list[int],
) -> list[dict]:
    """Fetch current annual-period facts from each requested filing year.

    Duration facts must span approximately a full fiscal year.
    Instant facts are selected using their actual instant_date.
    Comparative periods are not mistaken for the current period.
    Ambiguous records raise an error instead of being silently selected.
    """
    if not years:
        return []

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH eligible AS (
                SELECT
                    company,
                    source_filing_year,
                    form_type,
                    filing_id,
                    extraction_id,
                    taxonomy_year,
                    concept,
                    label,
                    amount,
                    unit,
                    concept_period_type,
                    metric_scope,
                    value_index,
                    value_text,
                    period_start_date,
                    period_end_date,
                    instant_date,
                    dimensions,
                    status,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_filing_year, concept
                        ORDER BY
                            COALESCE(period_end_date, instant_date) DESC,
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
                      (
                          concept_period_type = 'duration'
                          AND period_start_date IS NOT NULL
                          AND period_end_date IS NOT NULL
                          AND period_end_date - period_start_date
                              BETWEEN 300 AND 380
                      )
                      OR
                      (
                          concept_period_type = 'instant'
                          AND instant_date IS NOT NULL
                      )
                  )
            )
            SELECT
                company,
                source_filing_year,
                form_type,
                filing_id,
                extraction_id,
                taxonomy_year,
                concept,
                label,
                amount,
                unit,
                concept_period_type,
                metric_scope,
                value_index,
                value_text,
                period_start_date,
                period_end_date,
                instant_date,
                dimensions,
                status
            FROM eligible
            WHERE period_rank = 1
            ORDER BY source_filing_year, concept
            """,
            (company, years, sorted(FINANCIAL_CONCEPTS)),
        )
        rows = cur.fetchall()

    metrics = []
    grouped = defaultdict(list)

    for row in rows:
        (
            row_company,
            source_year,
            form_type,
            filing_id,
            extraction_id,
            taxonomy_year,
            concept,
            label,
            amount,
            unit,
            concept_period_type,
            metric_scope,
            value_index,
            value_text,
            period_start,
            period_end,
            instant_date,
            dimensions,
            status,
        ) = row

        date_key = (
            period_end.isoformat()
            if period_end
            else instant_date.isoformat()
        )

        # Include the fact's date and index so that source IDs remain
        # distinguishable if the underlying filing data changes.
        metric_id = (
            f"xbrl:{row_company.upper()}:{source_year}:"
            f"{filing_id}:{extraction_id}:{concept}:"
            f"{date_key}:{value_index}"
        )

        metric = {
            "id": metric_id,
            "company": row_company.upper(),
            "concept": concept,
            "label": label,
            "definition": None,
            "unit": unit,
            "year": int(source_year),
            "value": float(amount),
            "period_type": (
                "FY" if concept_period_type == "duration"
                else "instant"
            ),
            "concept_period_type": concept_period_type,
            "period_start_date": (
                period_start.isoformat() if period_start else None
            ),
            "period_end_date": (
                period_end.isoformat() if period_end else None
            ),
            "instant_date": (
                instant_date.isoformat() if instant_date else None
            ),
            "source_filing_year": int(source_year),
            "form_type": form_type,
            "filing_id": int(filing_id),
            "extraction_id": int(extraction_id),
            "taxonomy_year": taxonomy_year,
            "value_index": value_index,
            "value_text": value_text,
            "dimensions": dimensions,
            "metric_scope": metric_scope,
            "status": status,
        }

        grouped[(source_year, concept)].append(metric)
        metrics.append(metric)

    duplicates = {
        key: [record["id"] for record in records]
        for key, records in grouped.items()
        if len(records) > 1
    }
    if duplicates:
        raise ValueError(
            "Ambiguous financial facts remain after period selection. "
            f"Inspect these filing-year/concept groups: {duplicates}"
        )

    return metrics


def fetch_financial_metrics_from_db(
    company: str,
    years: list[int],
) -> list[dict]:
    """Open the configured database connection and fetch financial facts."""
    import psycopg

    with psycopg.connect(**connection_arguments()) as conn:
        return fetch_financial_metrics(conn, company, years)
