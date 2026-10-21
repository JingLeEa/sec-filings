#!/usr/bin/env python3
"""Pool completed alignment evaluation reports across companies and year pairs."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from evaluate_alignment_predictions import (
    CHANGE_BINARY_LABELS,
    CHANGE_LABELS,
    MATERIALITY_LABELS,
    atomic_json,
    atomic_text,
    class_metrics,
    substantive_change_metrics,
    yes_no_macro_f1,
)


CHANGE_METHODS = {
    "llm": "model_rows",
    "lexical": "lexical_model_rows",
    "semantic": "semantic_model_rows",
}


def read_pairs(root: Path) -> list[dict]:
    paths = sorted(root.glob("*/????-????/agreement.json"))
    if not paths:
        raise ValueError(f"No comparison reports found under {root}")
    pairs = []
    for path in paths:
        metrics_path = path.with_name("metrics.json")
        report_path = path.with_name("report.txt")
        if not metrics_path.is_file() or not report_path.is_file():
            raise ValueError(f"Incomplete evaluation output beside {path}")
        agreement = json.loads(path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        comparison = agreement["comparison"]
        if (comparison != metrics["comparison"]
                or comparison["company"] != path.parent.parent.name
                or f"{comparison['previous_year']}-{comparison['current_year']}" != path.parent.name):
            raise ValueError(f"Comparison metadata does not match at {path}")
        for method, key in CHANGE_METHODS.items():
            group = agreement["change_analysis"][key]
            if group["compared"] != metrics["change_analysis"][method]["predictions_compared"]:
                raise ValueError(f"Change comparison counts disagree at {path}")
        if (agreement["materiality_analysis"]["model_rows"]["compared"]
                != metrics["materiality_analysis"]["predictions_compared"]):
            raise ValueError(f"Materiality comparison counts disagree at {path}")
        pairs.append({"comparison": comparison, "agreement": agreement, "metrics": metrics,
                      "source": str(path)})
    return pairs


def pooled_group(pairs: list[dict], section: str, key: str, labels: tuple[str, ...]) -> dict:
    matrix = {label: Counter() for label in labels}
    eligible = completed = compared = 0
    reference_pending = missing_candidate_rows = missing_candidate_predictions = 0
    for pair in pairs:
        group = pair["agreement"][section][key]
        eligible += group["eligible_rows"]
        completed += group["reference_completed"]
        compared += group["compared"]
        reference_pending += group["reference_pending"]
        missing_candidate_rows += len(group["missing_candidate_rows"])
        missing_candidate_predictions += len(group["missing_candidate_predictions"])
        for reference, predictions in group["confusion_matrix"].items():
            if reference not in matrix:
                raise ValueError(f"Unknown reference label {reference!r} in {pair['source']}")
            matrix[reference].update(predictions)
    if (eligible - completed != reference_pending
            or completed - compared != missing_candidate_rows + missing_candidate_predictions):
        raise ValueError(f"Coverage counts do not reconcile in {section}.{key}")
    if sum(sum(counts.values()) for counts in matrix.values()) != compared:
        raise ValueError(f"Confusion counts do not match compared rows in {section}.{key}")
    per_label, macro_f1 = class_metrics(matrix, labels)
    matches = sum(matrix[label][label] for label in labels)
    result = {
        "eligible_rows": eligible,
        "reference_completed": completed,
        "reference_pending": reference_pending,
        "missing_candidate_rows": missing_candidate_rows,
        "missing_candidate_predictions": missing_candidate_predictions,
        "compared": compared,
        "coverage_rate": round(compared / eligible, 4) if eligible else None,
        "matches": matches,
        "agreement_rate": round(matches / compared, 4) if compared else None,
        "confusion_matrix": {label: dict(sorted(matrix[label].items())) for label in labels},
        "per_label": per_label,
        "macro_f1_vs_reference": macro_f1,
    }
    if section == "change_analysis":
        result["substantive_change"] = substantive_change_metrics(result)
    else:
        result["yes_no_macro_f1_vs_reference"] = yes_no_macro_f1(matrix)
    return result


def summarize(pairs: list[dict]) -> dict:
    models = Counter(pair["comparison"]["reference_model"] for pair in pairs)
    candidates = sorted({pair["comparison"]["candidate_model"] for pair in pairs})
    return {
        "comparison_count": len(pairs),
        "company_count": len({pair["comparison"]["company"] for pair in pairs}),
        "reference_models": dict(sorted(models.items())),
        "candidate_models": candidates,
        "change_analysis": {
            method: pooled_group(pairs, "change_analysis", key, CHANGE_LABELS)
            for method, key in CHANGE_METHODS.items()
        },
        "materiality_analysis": pooled_group(
            pairs, "materiality_analysis", "model_rows", MATERIALITY_LABELS
        ),
    }


def missing_pairs(pairs: list[dict]) -> list[str]:
    years = sorted({f"{pair['comparison']['previous_year']}-{pair['comparison']['current_year']}"
                    for pair in pairs})
    by_company = defaultdict(set)
    for pair in pairs:
        comparison = pair["comparison"]
        by_company[comparison["company"]].add(
            f"{comparison['previous_year']}-{comparison['current_year']}"
        )
    return [f"{company.upper()} {year_pair}" for company, available in sorted(by_company.items())
            for year_pair in years if year_pair not in available]


def fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "-"


def percent(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "-"


def detailed_change_table(change: dict, *, binary: bool) -> list[str]:
    labels = (CHANGE_BINARY_LABELS if binary else tuple(
        label for label in CHANGE_LABELS
        if any(change[method]["per_label"][label]["reference_count"]
               or change[method]["per_label"][label]["candidate_count"]
               for method in CHANGE_METHODS)
    ))
    lines = [
        "| Method | Label | Reference n | Candidate n | Precision | Recall | F1 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method in CHANGE_METHODS:
        group = change[method]["substantive_change"] if binary else change[method]
        for label in labels:
            entry = group["per_label"][label]
            lines.append(
                f"| {method.upper()} | {label} | {entry['reference_count']:,} | "
                f"{entry['candidate_count']:,} | {fmt(entry['precision'])} | "
                f"{fmt(entry['recall'])} | {fmt(entry['f1'])} |"
            )
        lines.append(f"| {method.upper()} | **Macro F1** | - | - | - | - | "
                     f"**{fmt(group['macro_f1_vs_reference'])}** |")
    return lines


def format_report(pairs: list[dict], summary: dict, root: Path) -> str:
    overall = summary["overall"]
    change = overall["change_analysis"]
    materiality = overall["materiality_analysis"]
    llm_binary = change["llm"]["substantive_change"]
    lexical_binary = change["lexical"]["substantive_change"]
    semantic_binary = change["semantic"]["substantive_change"]
    materiality_matrix = materiality["confusion_matrix"]
    false_yes = materiality_matrix["No"].get("Yes", 0)
    missed_yes = materiality_matrix["Yes"].get("No", 0)
    candidate_yes = materiality["per_label"]["Yes"]["candidate_count"]
    reference_yes = materiality["per_label"]["Yes"]["reference_count"]
    lines = [
        "# Consolidated Alignment Evaluation",
        "",
        f"Source: `{root}`; {overall['comparison_count']} year-pair comparisons across "
        f"{overall['company_count']} companies. Candidate: {', '.join(overall['candidate_models'])}.",
        "",
        "These are pooled agreement scores against model-generated references, not accuracy "
        "against human ground truth. Reference names are kept as recorded in each comparison: "
        + ", ".join(f"{model} ({count} pairs)" for model, count in overall["reference_models"].items())
        + ". The model labels should not be assumed interchangeable without checking run provenance.",
        "",
        "Missing company-year reports among observed year pairs: "
        + (", ".join(summary["missing_pairs"]) if summary["missing_pairs"] else "none") + ".",
        "",
        "## Key Findings",
        "",
        f"- Pooled substantive-change macro F1 is {fmt(llm_binary['macro_f1_vs_reference'])} "
        f"for LLM, {fmt(lexical_binary['macro_f1_vs_reference'])} for lexical and "
        f"{fmt(semantic_binary['macro_f1_vs_reference'])} for semantic. Exact-taxonomy "
        f"macro F1 is {fmt(change['llm']['macro_f1_vs_reference'])} for LLM and "
        f"{fmt(change['lexical']['macro_f1_vs_reference'])} for lexical; the ranking "
        "depends on the classification task.",
        f"- Materiality Yes precision is {fmt(materiality['per_label']['Yes']['precision'])} "
        f"and recall is {fmt(materiality['per_label']['Yes']['recall'])}. Qwen predicted "
        f"Yes on {materiality['per_label']['Yes']['candidate_count']:,} rows versus "
        f"{materiality['per_label']['Yes']['reference_count']:,} reference Yes rows; "
        f"{false_yes:,} reference-No rows were predicted Yes.",
        f"- Semantic produced unmapped candidate taxonomies on "
        f"{semantic_binary['total_compared'] - semantic_binary['compared']:,} of "
        f"{semantic_binary['total_compared']:,} compared change rows; its binary scores "
        "apply only to the mapped subset.",
        "",
        "## Change Analysis",
        "",
        "Binary substantive change: Modified, Expanded and Reduced are substantive; "
        "Reworded and Unchanged are non-substantive. New and Removed are outside this matched-row metric.",
        "",
        "| Method | Compared / eligible | Binary coverage | Substantive P | R | F1 | Non-substantive F1 | Binary macro F1 | Exact taxonomy macro F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method, group in change.items():
        binary = group["substantive_change"]
        positive = binary["per_label"]["Substantive"]
        negative = binary["per_label"]["Non-substantive"]
        lines.append(
            f"| {method.upper()} | {group['compared']:,} / {group['eligible_rows']:,} | "
            f"{fmt(binary['prediction_coverage_rate'])} | {fmt(positive['precision'])} | "
            f"{fmt(positive['recall'])} | {fmt(positive['f1'])} | {fmt(negative['f1'])} | "
            f"{fmt(binary['macro_f1_vs_reference'])} | {fmt(group['macro_f1_vs_reference'])} |"
        )
    lines += [
        "",
        "Binary coverage is the fraction of compared rows with a mappable candidate taxonomy. "
        "Unmapped labels such as Relocated are excluded from binary P/R/F1, but count against coverage.",
        "",
        "### Detailed Binary Metrics",
        "",
        "Per-label precision, recall and F1 for the substantive/non-substantive task. "
        "Macro F1 is the unweighted mean of the two reference-supported class F1 scores.",
        "",
        *detailed_change_table(change, binary=True),
        "",
        "### Detailed Taxonomy Metrics",
        "",
        "Exact-label scores for the matched-row change taxonomy. New and Removed have no "
        "reference support in these model rows and are omitted from the table and macro average. "
        "Unlike the binary semantic score, exact-taxonomy recall counts unmapped candidate labels "
        "such as Relocated as mismatches.",
        "",
        *detailed_change_table(change, binary=False),
        "",
        "### Error Rates",
        "",
        "False substantive rate divides false substantive calls by all candidate substantive calls. "
        "Missed substantive rate divides missed substantive cases by all reference substantive changes. "
        "These are pooled rates, not averages of the company-year rates.",
        "",
        "| Method | False substantive | Of candidate substantive | Missed substantive | Of reference substantive |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for method, group in change.items():
        binary = group["substantive_change"]
        positive = binary["per_label"]["Substantive"]
        lines.append(
            f"| {method.upper()} | {binary['false_substantive']:,} | "
            f"{percent(binary['false_substantive'], positive['candidate_count'])} "
            f"({binary['false_substantive']:,}/{positive['candidate_count']:,}) | "
            f"{binary['missed_substantive']:,} | "
            f"{percent(binary['missed_substantive'], positive['reference_count'])} "
            f"({binary['missed_substantive']:,}/{positive['reference_count']:,}) |"
        )
    lines += [
        "",
        "For semantic, the denominators include only rows with a mappable candidate label.",
        "",
        "## Materiality",
        "",
        f"Model rows compared: **{materiality['compared']:,} / {materiality['eligible_rows']:,}** "
        f"(coverage {fmt(materiality['coverage_rate'])}).",
        "",
        "| Label | Reference n | Candidate n | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label in MATERIALITY_LABELS:
        entry = materiality["per_label"][label]
        lines.append(f"| {label} | {entry['reference_count']:,} | {entry['candidate_count']:,} | "
                     f"{fmt(entry['precision'])} | {fmt(entry['recall'])} | {fmt(entry['f1'])} |")
    lines += [
        "",
        f"Primary Yes/No macro F1: **{fmt(materiality['yes_no_macro_f1_vs_reference'])}**. "
        f"Three-label macro F1: {fmt(materiality['macro_f1_vs_reference'])}. "
        "The primary score gives Uncertain no separate class weight; its predictions can still "
        "affect Yes/No precision.",
        f"False Yes against reference No: **{false_yes:,}/{candidate_yes:,} "
        f"({percent(false_yes, candidate_yes)})** of candidate Yes calls. "
        f"Missed reference Yes: **{missed_yes:,}/{reference_yes:,} "
        f"({percent(missed_yes, reference_yes)})** of reference Yes cases. "
        f"Another {materiality_matrix['Uncertain'].get('Yes', 0):,} candidate Yes calls "
        "had an Uncertain reference, so the false-Yes percentage alone is not exactly 1 minus precision.",
        "",
        "## Coverage",
        "",
        "Eligible means reference alignment rows in the task's evaluation scope: "
        "`ai_verified` for change analysis; `ai_verified` plus `unmatched` for materiality. "
        "Compared means eligible rows with both a valid reference label and a candidate label "
        "for the same `match_id`. Deterministic `auto_matched` rows are excluded here.",
        "",
        "| Task | Eligible | Reference label pending | Candidate row missing | Candidate label missing | Compared |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for title, group in (("LLM change", change["llm"]), ("Materiality", materiality)):
        lines.append(
            f"| {title} | {group['eligible_rows']:,} | {group['reference_pending']:,} | "
            f"{group['missing_candidate_rows']:,} | {group['missing_candidate_predictions']:,} | "
            f"{group['compared']:,} |"
        )
    lines += [
        "",
        "Reference-label gaps by company-year:",
    ]
    for pair in pairs:
        comparison = pair["comparison"]
        change_pending = pair["agreement"]["change_analysis"]["model_rows"]["reference_pending"]
        materiality_pending = pair["agreement"]["materiality_analysis"]["model_rows"]["reference_pending"]
        if change_pending or materiality_pending:
            lines.append(
                f"- {comparison['company'].upper()} {comparison['previous_year']}-{comparison['current_year']}: "
                f"{change_pending} change, {materiality_pending} materiality."
            )
    if not any(pair["agreement"]["change_analysis"]["model_rows"]["reference_pending"]
               or pair["agreement"]["materiality_analysis"]["model_rows"]["reference_pending"]
               for pair in pairs):
        lines.append("- None.")
    lines += [
        "",
        "## Reference Breakdown",
        "",
        "| Recorded reference | Pairs | LLM binary macro F1 | Lexical binary macro F1 | Semantic binary macro F1 | Materiality Yes F1 | Materiality Yes/No macro F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, group in summary["by_reference_model"].items():
        lines.append(f"| {model} | {group['comparison_count']} | "
                     f"{fmt(group['change_analysis']['llm']['substantive_change']['macro_f1_vs_reference'])} | "
                     f"{fmt(group['change_analysis']['lexical']['substantive_change']['macro_f1_vs_reference'])} | "
                     f"{fmt(group['change_analysis']['semantic']['substantive_change']['macro_f1_vs_reference'])} | "
                     f"{fmt(group['materiality_analysis']['per_label']['Yes']['f1'])} | "
                     f"{fmt(group['materiality_analysis']['yes_no_macro_f1_vs_reference'])} |")
    lines += [
        "",
        "## Company-Year Results",
        "",
        "Each score below is calculated within its own year pair; the pooled scores above use "
        "summed confusion counts rather than averaging pair scores.",
        "",
        "| Company | Years | Reference | Change n | LLM binary macro F1 | Lexical | Semantic | Materiality n | Yes P | Yes R | Yes F1 | Yes/No macro F1 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pair in pairs:
        comparison = pair["comparison"]
        metrics = pair["metrics"]
        methods = metrics["change_analysis"]
        mat = metrics["materiality_analysis"]
        yes = mat["per_label"]["Yes"]
        lines.append(
            f"| {comparison['company'].upper()} | {comparison['previous_year']}-{comparison['current_year']} | "
            f"{comparison['reference_model']} | {methods['llm']['substantive_change']['compared']:,} | "
            f"{fmt(methods['llm']['substantive_change']['macro_f1_vs_reference'])} | "
            f"{fmt(methods['lexical']['substantive_change']['macro_f1_vs_reference'])} | "
            f"{fmt(methods['semantic']['substantive_change']['macro_f1_vs_reference'])} | "
            f"{mat['predictions_compared']:,} | {fmt(yes['precision'])} | {fmt(yes['recall'])} | "
            f"{fmt(yes['f1'])} | {fmt(mat['yes_no_macro_f1_vs_reference'])} |"
        )
    lines += [
        "",
        "## Reading The Results",
        "",
        "- Scores cover only eligible model-classified rows with predictions on both sides; "
        "deterministic exact-text cases are outside these model-row tables.",
        "- Semantic binary scores can exclude unmapped taxonomies. Check binary coverage before comparing methods.",
        "- Class prevalence varies by company and year. Inspect per-label precision and recall, "
        "not only macro F1.",
        "- Alignment quality and reference-label correctness are not measured here. "
        "Review disagreement examples before treating these values as performance claims.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("data/evaluation_soclaas"))
    parser.add_argument("--report", type=Path, default=Path("docs/JINGLE_evaluation_results.md"))
    parser.add_argument("--summary-json", type=Path,
                        default=Path("data/evaluation_soclaas/consolidated_metrics.json"))
    args = parser.parse_args(argv)
    try:
        pairs = read_pairs(args.input_root)
        by_model = defaultdict(list)
        for pair in pairs:
            by_model[pair["comparison"]["reference_model"]].append(pair)
        summary = {
            "schema_version": "1",
            "source_root": str(args.input_root),
            "overall": summarize(pairs),
            "missing_pairs": missing_pairs(pairs),
            "by_reference_model": {model: summarize(group) for model, group in sorted(by_model.items())},
            "pairs": [{"comparison": pair["comparison"], "source": pair["source"],
                       "metrics": pair["metrics"]} for pair in pairs],
        }
        report = format_report(pairs, summary, args.input_root)
        atomic_json(args.summary_json, summary)
        atomic_text(args.report, report)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(2, f"Evaluation consolidation stopped: {error}\n")
    print(f"Consolidated {len(pairs)} comparisons into {args.report} and {args.summary_json}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
