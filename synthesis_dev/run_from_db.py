
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import psycopg

from sec_disclosure.storage.disclosures import connection_arguments
from synthesis_dev.retrieval import fetch_change_evidence
from synthesis_dev.agent import build_synthesis_graph
from synthesis_dev.financial_retrieval import fetch_financial_metrics_from_db

def materiality_decision(value):
    """Return True/False for an explicit materiality decision; None if unclear."""
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "material", "true"}:
            return True
        if normalized in {"no", "not material", "false"}:
            return False
        return None

    if not isinstance(value, dict):
        return None

    decision_keys = (
        "decision",
        "is_material",
        "materiality",
        "materiality_decision",
        "classification",
        "label",
        "result",
    )

    for key in decision_keys:
        if key in value:
            result = materiality_decision(value[key])
            if result is not None:
                return result

    # Support decisions nested inside the stored assessment.
    for nested in value.values():
        if isinstance(nested, dict):
            result = materiality_decision(nested)
            if result is not None:
                return result

    return None


def make_agent_change(item):
    """Adapt a retrieved database alignment to the agent's input schema."""
    return {
        "id": item["match_id"],
        "match_id": item["match_id"],
        "taxonomy": item["taxonomy"],
        "alignment_status": item["alignment_status"],
        "relationship": item["relationship"],
        "materiality": item["materiality"],
        "members": item["members"],
        "source_evidence": item["source_evidence"],
        "evidence_count": item["evidence_count"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run disclosure synthesis from completed PostgreSQL pipelines."
    )
    parser.add_argument(
        "--company",
        required=True,
        help="Company ticker stored in the database, e.g. AMD.",
    )
    parser.add_argument(
        "--years",
        required=True,
        nargs="+",
        type=int,
        help="Fiscal years to analyse, e.g. --years 2023 2024 2025.",
    )
    parser.add_argument(
        "--limit-per-pair",
        type=int,
        default=500,
        help="Maximum alignments retrieved for each year pair (default: 500).",
    )
    parser.add_argument(
        "--max-changes",
        type=int,
        default=50,
        help="Maximum material changes sent to the agent (default: 50).",
    )
    parser.add_argument(
        "--finding-target", type=int, default=None,
        help="Minimum detailed findings requested; defaults to selected changes.",
    )
    parser.add_argument(
        "--finding-batch-size", type=int, default=10,
        help="Material changes per synthesis request (default: 10).",
    )
    args = parser.parse_args()

    company = args.company.strip().upper()
    years = sorted(set(args.years))

    if len(years) < 2:
        parser.error("Provide at least two distinct years.")
    if args.limit_per_pair < 1 or args.max_changes < 1 or args.finding_batch_size < 1:
        parser.error("--limit-per-pair and --max-changes must be positive.")

    year_pairs = list(zip(years[:-1], years[1:]))

    # Retrieve completed alignments for every requested consecutive year pair.
    retrieved = []
    with psycopg.connect(**connection_arguments()) as conn:
        for previous_year, current_year in year_pairs:
            pair_changes = fetch_change_evidence(
                conn,
                company=company,
                previous_year=previous_year,
                current_year=current_year,
                limit=args.limit_per_pair,
            )
            retrieved.extend(pair_changes)
            print(
                f"{company} {previous_year}-{current_year}: "
                f"retrieved {len(pair_changes)} alignments"
            )
    
    # Explicit policy: only a positive, explicit materiality assessment qualifies.
    selected = []
    excluded_no = 0
    excluded_unclear = 0

    for item in retrieved:
        decision = materiality_decision(item.get("materiality"))

        if decision is True:
            selected.append(item)
        elif decision is False:
            excluded_no += 1
        else:
            excluded_unclear += 1
    
    # Report explicitly material changes available for each year pair.
    print("\nExplicitly material changes available by year pair:")
    for previous_year, current_year in year_pairs:
        pair_count = sum(
            1
            for item in selected
            if item.get("previous_year") == previous_year
            and item.get("current_year") == current_year
        )
        print(f"  {previous_year}-{current_year}: {pair_count}")

    # Allocate the overall evidence budget across year pairs first.
    # Retrieval already orders each pair by materiality score.
    selected_by_pair = {
        (previous_year, current_year): [
            item
            for item in selected
            if item.get("previous_year") == previous_year
            and item.get("current_year") == current_year
        ]
        for previous_year, current_year in year_pairs
    }

    pair_count = len(year_pairs)
    base_quota, remainder = divmod(args.max_changes, pair_count)

    selected = []
    for index, year_pair in enumerate(year_pairs):
        quota = base_quota + (1 if index < remainder else 0)
        selected.extend(selected_by_pair[year_pair][:quota])

    # Redistribute unused quota if a year pair has fewer material changes.
    if len(selected) < args.max_changes:
        selected_ids = {item["match_id"] for item in selected}
        remaining = [
            item
            for item in retrieved
            if materiality_decision(item.get("materiality")) is True
            and item["match_id"] not in selected_ids
        ]
        selected.extend(remaining[:args.max_changes - len(selected)])

    print("\nMaterial changes selected by year pair:")
    for previous_year, current_year in year_pairs:
        pair_count = sum(
            1
            for item in selected
            if item.get("previous_year") == previous_year
            and item.get("current_year") == current_year
        )
        print(f"  {previous_year}-{current_year}: {pair_count}")


    if not selected:
        raise RuntimeError(
            "No explicitly material changes were found. "
            "Check the stored materiality assessments and company/year inputs."
        )

    material_changes = [make_agent_change(item) for item in selected]

    # Retrieve financial metrics for exactly the requested years.
    financial_metrics = fetch_financial_metrics_from_db(
        company=company,
        years=years,
    )

    request = {
        "company": company,
        "years": years,
        "material_changes": material_changes,
        "financial_metrics": financial_metrics,
        "finding_target": (
            args.finding_target if args.finding_target is not None
            else len(material_changes)
        ),
        "finding_batch_size": args.finding_batch_size,
    }

    print(f"\nCompany: {company}")
    print(f"Requested years: {years}")
    print(f"Completed year-pair queries: {len(year_pairs)}")
    print(f"Alignments retrieved: {len(retrieved)}")
    print(f"Explicitly material: {len(selected)} sent to agent")
    print(f"Excluded as not material: {excluded_no}")
    print(f"Excluded as missing/unclear: {excluded_unclear}")
    print(f"Financial metrics retrieved: {len(financial_metrics)}")
    print("\nRunning synthesis agent...")

    graph = build_synthesis_graph()
    result = graph.invoke({"request": request})

    source_catalog = {
        item["match_id"]: {
            "company": item["company"],
            "previous_year": item["previous_year"],
            "current_year": item["current_year"],
            "taxonomy": item["taxonomy"],
            "alignment_status": item["alignment_status"],
            "relationship": item["relationship"],
            "materiality": item["materiality"],
            "sources": [
                {
                    "disclosure_id": sentence["disclosure_id"],
                    "sentence_id": sentence["sentence_id"],
                    "text": sentence["text"],
                    "source_url": sentence["source_url"],
                }
                for sentence in item["source_evidence"]
            ],
        }
        for item in selected
    }

    # Deterministic coverage reporting.
    # A material change is counted as covered when at least one
    # key finding cites its evidence ID.
    selected_ids = {
        str(change["match_id"])
        for change in selected
    }

    cited_ids = {
        str(evidence_id)
        for finding in result["report"].get("key_findings", [])
        for evidence_id in finding.get("evidence_ids", [])
    }

    covered_ids = selected_ids & cited_ids
    uncited_ids = selected_ids - cited_ids

    coverage_summary = {
        "selected_material_changes": len(selected_ids),
        "cited_material_changes": len(covered_ids),
        "uncited_material_changes": len(uncited_ids),
        "citation_coverage_pct": round(
            len(covered_ids) / len(selected_ids) * 100, 2
        ) if selected_ids else 0.0,
    }

    coverage_gaps = []

    for change_id in sorted(uncited_ids):
        source = source_catalog[change_id]

        disclosure_ids = sorted({
            str(item["disclosure_id"])
            for item in source["sources"]
            if item.get("disclosure_id") is not None
        })

        coverage_gaps.append({
            "match_id": change_id,
            "previous_year": source["previous_year"],
            "current_year": source["current_year"],
            "taxonomy": source["taxonomy"],
            "alignment_status": source["alignment_status"],
            "relationship": source["relationship"],
            "materiality": source["materiality"],
            "source_disclosure_ids": disclosure_ids,
            "reason": "Not cited by any key finding",
        })

    output = {
        "company": company,
        "years": years,
        "materiality_policy": (
            "Include only changes with an explicit positive materiality "
            "assessment. Exclude negative and missing/unclear assessments."
        ),
        "input_summary": {
            "year_pairs_queried": year_pairs,
            "database_alignments_retrieved": len(retrieved),
            "material_changes_selected": len(selected),
            "excluded_not_material": excluded_no,
            "excluded_missing_or_unclear_materiality": excluded_unclear,
            "financial_metrics_supplied": len(financial_metrics),
        },
        "financial_metrics": financial_metrics,
        "financial_trends": result["trends"],
        "report": result["report"],
        "validation": result["validation"],
        "coverage_summary": coverage_summary,
        "coverage_gaps": coverage_gaps,
        "source_catalog": source_catalog,
        "limitations": [
            "This runner uses existing completed alignment and materiality results "
            "in PostgreSQL; it does not launch upstream extraction or alignment.",
            "Only changes with explicit positive materiality assessments are selected.",
            "Agent validation checks structure and evidence IDs, not independent "
            "factual entailment.",
            "Citation coverage measures which selected material changes are "
            "referenced by key findings; it does not independently establish "
            "that each cited change was fully or accurately analyzed.",
        ],
    }

    safe_company = re.sub(r"[^A-Za-z0-9_-]+", "_", company)
    output_path = (
        ROOT
        / "synthesis_dev"
        / f"{safe_company}_{years[0]}_{years[-1]}_synthesis_output.json"
    )
    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    print("\n=== EXECUTIVE SUMMARY ===")
    print(result["report"]["executive_summary"])
    print("\n=== VALIDATION ===")
    print(json.dumps(result["validation"], indent=2))
    print("\n=== DISCLOSURE COVERAGE ===")
    print(json.dumps(coverage_summary, indent=2))
    print(f"\nSaved report: {output_path}")


if __name__ == "__main__":
    main()
