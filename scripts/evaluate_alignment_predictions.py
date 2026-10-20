#!/usr/bin/env python3
"""Compare production change and materiality predictions with reference eval rows."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


CHANGE_LABELS = ("New", "Removed", "Expanded", "Reduced", "Modified", "Reworded", "Unchanged")
CHANGE_BINARY_LABELS = ("Substantive", "Non-substantive")
CHANGE_BINARY_GROUP = {
    "Modified": "Substantive", "Expanded": "Substantive", "Reduced": "Substantive",
    "Reworded": "Non-substantive", "Unchanged": "Non-substantive",
}
MATERIALITY_LABELS = ("Yes", "No", "Uncertain")
MODEL_CHANGE_STATUSES = {"ai_verified"}
LOGIC_CHANGE_STATUSES = {"auto_matched", "unmatched"}
MODEL_MATERIALITY_STATUSES = {"ai_verified", "unmatched"}
LOGIC_MATERIALITY_STATUSES = {"auto_matched"}
TICKER = re.compile(r"[a-z0-9][a-z0-9.-]*\Z")
YEAR_PAIR = re.compile(r"\d{4}-\d{4}\Z")


def load_rows(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("run_complete") is not True:
        raise ValueError(f"Expected a completed alignment report: {path}")
    rows = document.get("alignments")
    if not isinstance(rows, list):
        raise ValueError(f"Missing alignments list: {path}")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("match_id"), str) or not row["match_id"]:
            raise ValueError(f"Invalid alignment row in {path}")
        if row["match_id"] in indexed:
            raise ValueError(f"Duplicate match_id {row['match_id']} in {path}")
        indexed[row["match_id"]] = row
    return document, indexed


def change_label(row, method="llm"):
    analysis = row.get("change_analysis")
    if not isinstance(analysis, dict):
        return None
    if method == "llm":
        label = analysis.get("llm")
        return label if analysis.get("status") == "completed" and label in CHANGE_LABELS else None
    if method not in {"lexical", "semantic"}:
        raise ValueError(f"Unknown change method: {method}")
    result = analysis.get(method)
    if not isinstance(result, dict):
        return None
    label = result.get("change_taxonomy" if method == "lexical" else "label")
    return label if isinstance(label, str) and label else None


def materiality_label(row):
    analysis = row.get("materiality_analysis")
    if not isinstance(analysis, dict):
        return None
    label = analysis.get("materiality")
    return label if label in MATERIALITY_LABELS else None


def valid_score(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1


def class_metrics(confusion, labels):
    metrics = {}
    active_f1 = []
    for label in labels:
        support = sum(confusion[label].values())
        predicted = sum(confusion[reference][label] for reference in labels)
        correct = confusion[label][label]
        precision = correct / predicted if predicted else None
        recall = correct / support if support else None
        f1 = 2 * correct / (support + predicted) if support + predicted else None
        metrics[label] = {"reference_count": support, "candidate_count": predicted,
                          "precision": round(precision, 4) if precision is not None else None,
                          "recall": round(recall, 4) if recall is not None else None,
                          "f1": round(f1, 4) if f1 is not None else None}
        if support:
            active_f1.append(f1 or 0.0)
    return metrics, round(sum(active_f1) / len(active_f1), 4) if active_f1 else None


def yes_no_macro_f1(confusion):
    scores = []
    for label in ("Yes", "No"):
        support = sum(confusion[label].values())
        if not support:
            continue
        predicted = sum(confusion[reference][label] for reference in MATERIALITY_LABELS)
        scores.append(2 * confusion[label][label] / (support + predicted))
    return round(sum(scores) / len(scores), 4) if scores else None


def label_comparison(confusion, labels, candidate_labels):
    totals = {
        "reference": {label: sum(confusion[label].values()) for label in labels},
        "candidate": {label: sum(confusion[other][label] for other in labels)
                      for label in candidate_labels},
    }
    differences = [
        {"candidate": candidate, "reference": reference, "count": confusion[reference][candidate]}
        for reference in labels for candidate in candidate_labels
        if reference != candidate and confusion[reference][candidate]
    ]
    differences.sort(key=lambda entry: (-entry["count"], entry["candidate"], entry["reference"]))
    return totals, differences


def substantive_change_metrics(model_report):
    confusion = {label: Counter() for label in CHANGE_BINARY_LABELS}
    unmapped = Counter()
    for reference, predictions in model_report["confusion_matrix"].items():
        for candidate, count in predictions.items():
            if not count:
                continue
            if reference not in CHANGE_BINARY_GROUP:
                raise ValueError("Reference change rows must have matched-group taxonomy labels.")
            if candidate not in CHANGE_BINARY_GROUP:
                unmapped[candidate] += count
                continue
            confusion[CHANGE_BINARY_GROUP[reference]][CHANGE_BINARY_GROUP[candidate]] += count

    substantive, nonsubstantive = CHANGE_BINARY_LABELS
    total_compared = model_report["compared"]
    compared = total_compared - sum(unmapped.values())
    matches = sum(confusion[label][label] for label in CHANGE_BINARY_LABELS)
    per_label, macro_f1 = class_metrics(confusion, CHANGE_BINARY_LABELS)
    return {
        "label_mapping": {label: CHANGE_BINARY_GROUP[label] for label in CHANGE_LABELS
                          if label in CHANGE_BINARY_GROUP},
        "compared": compared,
        "total_compared": total_compared,
        "prediction_coverage_rate": round(compared / total_compared, 4) if total_compared else None,
        "unmapped_prediction_counts": dict(sorted(unmapped.items())),
        "matches": matches,
        "agreement_rate": round(matches / compared, 4) if compared else None,
        "confusion_matrix": {label: {other: confusion[label][other] for other in CHANGE_BINARY_LABELS}
                             for label in CHANGE_BINARY_LABELS},
        "per_label": per_label,
        "macro_f1_vs_reference": macro_f1,
        "missed_substantive": confusion[substantive][nonsubstantive],
        "false_substantive": confusion[nonsubstantive][substantive],
    }


def evaluate_group(reference, candidate, *, statuses, task, labels, candidate_method="llm"):
    get_label = change_label if task == "change_analysis" else materiality_label
    eligible = [row for row in reference.values() if row.get("status") in statuses]
    confusion = {label: Counter() for label in labels}
    observed_candidate_labels = set()
    reference_completed = candidate_completed = matches = 0
    missing_candidate_rows = []
    missing_candidate_predictions = []
    disagreements = []
    score_differences = {"materiality_score": [], "confidence_score": []}
    by_status = {}

    for row in eligible:
        match_id = row["match_id"]
        reference_label = get_label(row)
        if reference_label is None:
            continue
        reference_completed += 1
        other = candidate.get(match_id)
        if other is None:
            missing_candidate_rows.append(match_id)
            continue
        candidate_label = (change_label(other, candidate_method) if task == "change_analysis"
                           else get_label(other))
        if candidate_label is None:
            missing_candidate_predictions.append(match_id)
            continue
        candidate_completed += 1
        observed_candidate_labels.add(candidate_label)
        confusion[reference_label][candidate_label] += 1
        status = row["status"]
        by_status.setdefault(status, {"compared": 0, "matches": 0})
        by_status[status]["compared"] += 1
        if reference_label == candidate_label:
            matches += 1
            by_status[status]["matches"] += 1
        else:
            example = {"match_id": match_id, "status": status,
                       "reference": reference_label, "candidate": candidate_label}
            if task == "materiality_analysis":
                example["reference_reason"] = row["materiality_analysis"].get("reason")
                example["candidate_reason"] = other["materiality_analysis"].get("reason")
            disagreements.append(example)
        if task == "materiality_analysis":
            for field in score_differences:
                left = row["materiality_analysis"].get(field)
                right = other["materiality_analysis"].get(field)
                if valid_score(left) and valid_score(right):
                    score_differences[field].append(abs(left - right))

    for counts in by_status.values():
        counts["agreement_rate"] = round(counts["matches"] / counts["compared"], 4)
    per_label, macro_f1 = class_metrics(confusion, labels)
    candidate_labels = (*labels, *sorted(observed_candidate_labels - set(labels)))
    label_counts, difference_counts = label_comparison(confusion, labels, candidate_labels)
    compared = candidate_completed
    report = {
        "eligible_rows": len(eligible),
        "reference_completed": reference_completed,
        "reference_pending": len(eligible) - reference_completed,
        "compared": compared,
        "matches": matches,
        "disagreements_count": compared - matches,
        "agreement_rate": round(matches / compared, 4) if compared else None,
        "reference_coverage_rate": round(reference_completed / len(eligible), 4) if eligible else None,
        "missing_candidate_rows": missing_candidate_rows,
        "missing_candidate_predictions": missing_candidate_predictions,
        "by_status": by_status,
        "confusion_matrix": {label: {other: confusion[label][other] for other in candidate_labels}
                             for label in labels},
        "label_counts": label_counts,
        "difference_counts": difference_counts,
        "per_label": per_label,
        "macro_f1_vs_reference": macro_f1,
        "disagreements": disagreements,
    }
    if task == "materiality_analysis":
        report["yes_no_macro_f1_vs_reference"] = yes_no_macro_f1(confusion)
        report["score_mae"] = {
            field: {"compared": len(values),
                    "mean_absolute_error": round(sum(values) / len(values), 4) if values else None}
            for field, values in score_differences.items()
        }
    return report


def evaluate_pair(reference_path, candidate_path, *, reference_model="OpenAI"):
    reference_document, reference = load_rows(reference_path)
    candidate_document, candidate = load_rows(candidate_path)
    identity_fields = ("company", "previous_year", "current_year")
    if any(str(reference_document.get(key)) != str(candidate_document.get(key))
           for key in identity_fields):
        raise ValueError("Reference and candidate company/year identities differ.")
    common_ids = reference.keys() & candidate.keys()
    for match_id in common_ids:
        left, right = reference[match_id], candidate[match_id]
        if any(left.get(key) != right.get(key) for key in
               ("status", "relationship", "previous_ids", "current_ids", "evidence")):
            raise ValueError(f"Alignment membership changed for {match_id}; results are not comparable.")
    change_model = evaluate_group(reference, candidate, statuses=MODEL_CHANGE_STATUSES,
                                  task="change_analysis", labels=CHANGE_LABELS)
    change_model["substantive_change"] = substantive_change_metrics(change_model)
    change_methods = {}
    for method in ("lexical", "semantic"):
        method_report = evaluate_group(reference, candidate, statuses=MODEL_CHANGE_STATUSES,
                                       task="change_analysis", labels=CHANGE_LABELS,
                                       candidate_method=method)
        method_report["substantive_change"] = substantive_change_metrics(method_report)
        change_methods[f"{method}_model_rows"] = method_report
    return {
        "schema_version": "1",
        "comparison": {
            "company": reference_document["company"],
            "previous_year": str(reference_document["previous_year"]),
            "current_year": str(reference_document["current_year"]),
            "reference_file": str(reference_path),
            "candidate_file": str(candidate_path),
            "reference_model": reference_model,
            "candidate_model": "Qwen",
            "interpretation": (f"Agreement with {reference_model} model outputs, "
                               "not accuracy against human ground truth."),
        },
        "alignment_rows": {"reference": len(reference), "candidate": len(candidate),
                           "shared_ids": len(common_ids),
                           "reference_only_ids": sorted(reference.keys() - candidate.keys()),
                           "candidate_only_ids": sorted(candidate.keys() - reference.keys())},
        "change_analysis": {
            "model_rows": change_model,
            **change_methods,
            "deterministic_rows": evaluate_group(reference, candidate, statuses=LOGIC_CHANGE_STATUSES,
                                                 task="change_analysis", labels=CHANGE_LABELS),
        },
        "materiality_analysis": {
            "model_rows": evaluate_group(reference, candidate, statuses=MODEL_MATERIALITY_STATUSES,
                                         task="materiality_analysis", labels=MATERIALITY_LABELS),
            "deterministic_rows": evaluate_group(reference, candidate, statuses=LOGIC_MATERIALITY_STATUSES,
                                                 task="materiality_analysis", labels=MATERIALITY_LABELS),
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", required=True,
                        help="Ticker to evaluate; repeat for several companies.")
    parser.add_argument("--year-pair", action="append",
                        help="YYYY-YYYY comparison; repeat or omit to discover available pairs.")
    parser.add_argument("--reference-root", type=Path,
                        default=Path("data/alignments_for_eval_soclaas"))
    parser.add_argument("--candidate-root", type=Path, default=Path("data/alignments"))
    parser.add_argument("--output-root", type=Path, default=Path("data/evaluation_soclaas"))
    parser.add_argument("--model", default="DeepSeek",
                        help="Name of the reference model shown in reports and metrics (default: DeepSeek).")
    args = parser.parse_args(argv)
    args.model = args.model.strip()
    if not args.model:
        parser.error("--model must not be empty.")
    args.ticker = list(dict.fromkeys(ticker.lower() for ticker in args.ticker))
    if any(not TICKER.fullmatch(ticker) for ticker in args.ticker):
        parser.error("Tickers must be safe company directory names.")
    if args.year_pair and any(not YEAR_PAIR.fullmatch(pair) or pair[:4] >= pair[5:]
                              for pair in args.year_pair):
        parser.error("Year pairs must contain increasing four-digit years.")
    if args.output_root.resolve() in {args.reference_root.resolve(), args.candidate_root.resolve()}:
        parser.error("Output root must differ from the alignment roots.")
    return args


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def ranking_metrics(report):
    change = report["change_analysis"]
    methods = {}
    for method, key in (("llm", "model_rows"), ("lexical", "lexical_model_rows"),
                        ("semantic", "semantic_model_rows")):
        group = change[key]
        binary = group["substantive_change"]
        methods[method] = {
            "reference_completed": group["reference_completed"],
            "predictions_compared": group["compared"],
            "substantive_change": {
                "compared": binary["compared"],
                "total_compared": binary["total_compared"],
                "prediction_coverage_rate": binary["prediction_coverage_rate"],
                "unmapped_prediction_counts": binary["unmapped_prediction_counts"],
                "per_label": binary["per_label"],
                "macro_f1_vs_reference": binary["macro_f1_vs_reference"],
            },
        }
    materiality = report["materiality_analysis"]["model_rows"]
    return {
        "schema_version": "1",
        "comparison": report["comparison"],
        "change_analysis": methods,
        "materiality_analysis": {
            "reference_completed": materiality["reference_completed"],
            "predictions_compared": materiality["compared"],
            "per_label": materiality["per_label"],
            "yes_no_macro_f1_vs_reference": materiality["yes_no_macro_f1_vs_reference"],
            "macro_f1_vs_reference": materiality["macro_f1_vs_reference"],
        },
    }


def print_comparison_table(title, group, labels, reference_model="OpenAI"):
    counts = group["label_counts"]
    candidate_model = "Qwen"
    width = max(14, len(reference_model), len(candidate_model))
    print(f"\n{title} label counts (compared model rows only; n={group['compared']})")
    print(f"{'Label':<{width}} {candidate_model:>8} {reference_model:>8}")
    for label in labels:
        print(f"{label:<{width}} {counts['candidate'][label]:>8} {counts['reference'][label]:>8}")
    for label in sorted(counts["candidate"].keys() - counts["reference"].keys()):
        print(f"{label:<{width}} {counts['candidate'][label]:>8} {0:>8}")
    print(f"\n{title} disagreements ({candidate_model} -> {reference_model})")
    if not group["difference_counts"]:
        print("None")
        return
    print(f"{candidate_model:<{width}} {reference_model:<{width}} {'Count':>8}")
    for entry in group["difference_counts"]:
        print(f"{entry['candidate']:<{width}} {entry['reference']:<{width}} {entry['count']:>8}")


def print_materiality_metrics(group, reference_model="OpenAI"):
    def display(value):
        return "-" if value is None else f"{value:.3f}"

    print(f"\nMateriality precision / recall / F1 ({reference_model} reference; compared model rows only)")
    print(f"{'Label':<14} {reference_model + ' n':>8} {'Qwen n':>8} {'Precision':>10} {'Recall':>8} {'F1':>8}")
    for label in MATERIALITY_LABELS:
        metrics = group["per_label"][label]
        print(f"{label:<14} {metrics['reference_count']:>8} {metrics['candidate_count']:>8} "
              f"{display(metrics['precision']):>10} {display(metrics['recall']):>8} "
              f"{display(metrics['f1']):>8}")
    print(f"Yes/No macro F1 (primary; labels with {reference_model} support): "
          f"{display(group['yes_no_macro_f1_vs_reference'])}")
    print(f"Three-label macro F1 (Yes/No/Uncertain): {display(group['macro_f1_vs_reference'])}")


def print_change_metrics(title, group, reference_model="OpenAI"):
    def display(value):
        return "-" if value is None else f"{value:.3f}"

    print(f"\n{title} exact-taxonomy precision / recall / F1 "
          f"({reference_model} reference; n={group['compared']}/{group['reference_completed']} reference rows)")
    print(f"{'Label':<14} {reference_model + ' n':>8} {'Qwen n':>8} {'Precision':>10} {'Recall':>8} {'F1':>8}")
    for label in CHANGE_LABELS:
        metrics = group["per_label"][label]
        print(f"{label:<14} {metrics['reference_count']:>8} {metrics['candidate_count']:>8} "
              f"{display(metrics['precision']):>10} {display(metrics['recall']):>8} "
              f"{display(metrics['f1']):>8}")
    print(f"Macro F1 (labels with {reference_model} support): {display(group['macro_f1_vs_reference'])}")
    extra = {label: count for label, count in group["label_counts"]["candidate"].items()
             if label not in CHANGE_LABELS and count}
    if extra:
        print(f"Candidate-only labels (counted as exact mismatches): {extra}")
    if group["missing_candidate_rows"] or group["missing_candidate_predictions"]:
        print(f"Missing candidate rows: {len(group['missing_candidate_rows'])}; "
              f"missing predictions: {len(group['missing_candidate_predictions'])}")


def print_substantive_change_metrics(group, method="LLM", reference_model="OpenAI"):
    def display(value):
        return "-" if value is None else f"{value:.3f}"

    print(f"\nSubstantive change detection: {method} "
          f"({reference_model} reference; n={group['compared']}/{group['total_compared']} mapped rows)")
    print(f"{'Label':<18} {reference_model + ' n':>8} {'Qwen n':>8} {'Precision':>10} {'Recall':>8} {'F1':>8}")
    for label in CHANGE_BINARY_LABELS:
        metrics = group["per_label"][label]
        print(f"{label:<18} {metrics['reference_count']:>8} {metrics['candidate_count']:>8} "
              f"{display(metrics['precision']):>10} {display(metrics['recall']):>8} "
              f"{display(metrics['f1']):>8}")
    print(f"Agreement: {display(group['agreement_rate'])}; "
          f"macro F1: {display(group['macro_f1_vs_reference'])}; "
          f"missed substantive: {group['missed_substantive']}; "
          f"false substantive: {group['false_substantive']}")
    if group["unmapped_prediction_counts"]:
        print(f"Unmapped predictions excluded from binary metrics: "
              f"{group['unmapped_prediction_counts']}")


def print_report(report, target):
    change = report["change_analysis"]["model_rows"]
    materiality = report["materiality_analysis"]["model_rows"]
    comparison = report["comparison"]
    reference_model = comparison.get("reference_model", "OpenAI")
    print(f"{comparison['company'].upper()} {comparison['previous_year']}-{comparison['current_year']}: "
          f"change {change['matches']}/{change['compared']} "
          f"({change['reference_completed']}/{change['eligible_rows']} reference complete); "
          f"materiality {materiality['matches']}/{materiality['compared']} "
          f"({materiality['reference_completed']}/{materiality['eligible_rows']} reference complete). "
          f"Saved {target}")
    for method, key in (("LLM", "model_rows"), ("Lexical", "lexical_model_rows"),
                        ("Semantic", "semantic_model_rows")):
        group = report["change_analysis"][key]
        print_comparison_table("Change taxonomy" if method == "LLM"
                               else f"{method} change taxonomy", group, CHANGE_LABELS, reference_model)
        print_change_metrics(method, group, reference_model)
        print_substantive_change_metrics(group["substantive_change"], method, reference_model)
    print_comparison_table("Materiality", materiality, MATERIALITY_LABELS, reference_model)
    print_materiality_metrics(materiality, reference_model)


def main(argv=None):
    args = parse_args(argv)
    for ticker in args.ticker:
        directory = args.reference_root / ticker
        if args.year_pair:
            pairs = list(dict.fromkeys(args.year_pair))
        elif directory.is_dir():
            pairs = sorted(path.name for path in directory.iterdir()
                           if path.is_dir() and YEAR_PAIR.fullmatch(path.name))
        else:
            pairs = []
        if not pairs:
            raise ValueError(f"No evaluation year-pairs found for {ticker} in {directory}")
        for pair in pairs:
            reference_path = directory / pair / "alignments.json"
            candidate_path = args.candidate_root / ticker / pair / "alignments.json"
            report = evaluate_pair(reference_path, candidate_path, reference_model=args.model)
            target = args.output_root / ticker / pair / "agreement.json"
            atomic_json(target, report)
            atomic_json(target.with_name("metrics.json"), ranking_metrics(report))
            output = StringIO()
            with redirect_stdout(output):
                print_report(report, target)
            report_text = output.getvalue()
            atomic_text(target.with_name("report.txt"), report_text)
            print(report_text, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Evaluation comparison stopped: {error}") from error
