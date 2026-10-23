from financial_retrieval import fetch_financial_metrics_from_db


metrics = fetch_financial_metrics_from_db(
    company="AMD",
    years=[2023, 2024, 2025],
)

print(f"Retrieved {len(metrics)} annual metrics")

for metric in metrics:
    print(
        metric["year"],
        metric["label"],
        f'${metric["value"]:,.0f}',
        metric["unit"],
        metric["id"],
        metric["period_start_date"],
        metric["period_end_date"],
    )

assert len(metrics) == 9, (
    f"Expected 9 metrics (3 concepts × 3 years), "
    f"got {len(metrics)}"
)

assert len({metric["id"] for metric in metrics}) == 9

print("PASS: annual metrics retrieved without duplicate IDs.")
