#!/usr/bin/env python3
"""Compare LLM-only change labels with a three-method majority decision."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from evaluate_alignment_predictions import (
    CHANGE_BINARY_GROUP,
    CHANGE_LABELS,
    atomic_json,
    atomic_text,
    change_label,
    class_metrics,
    load_rows,
    substantive_change_metrics,
)


YEAR_PAIR = re.compile(r"\d{4}-\d{4}\Z")


def majority_decision(llm: str, lexical: str | None, semantic: str | None) -> tuple[str, str]:
    if llm not in CHANGE_LABELS:
        raise ValueError(f"Invalid LLM taxonomy: {llm!r}")
    if not lexical or not semantic:
        return llm, "missing_vote_fallback"
    votes = Counter((llm, lexical, semantic))
    winner, count = votes.most_common(1)[0]
    if count < 2:
        return llm, "three_way_tie_fallback"
    if winner not in CHANGE_LABELS:
        return llm, "unsupported_label_fallback"
    if winner == llm:
        return llm, "majority_supports_llm"
    return winner, "majority_overrides_llm"


def empty_matrix() -> dict[str, Counter]:
    return {label: Counter() for label in CHANGE_LABELS}


def score_matrix(matrix: dict[str, Counter]) -> dict:
    compared = sum(sum(row.values()) for row in matrix.values())
    per_label, macro_f1 = class_metrics(matrix, CHANGE_LABELS)
    exact_matches = sum(matrix[label][label] for label in CHANGE_LABELS)
    scored = {
        "compared": compared,
        "exact_matches": exact_matches,
        "exact_agreement_rate": round(exact_matches / compared, 4) if compared else None,
        "exact_macro_f1": macro_f1,
        "exact_per_label": per_label,
        "confusion_matrix": {label: dict(sorted(matrix[label].items())) for label in CHANGE_LABELS},
    }
    scored["substantive_change"] = substantive_change_metrics(scored)
    return scored


def evaluate_pair(reference_path: Path, candidate_path: Path) -> dict:
    reference_doc, reference = load_rows(reference_path)
    candidate_doc, candidate = load_rows(candidate_path)
    identity = ("company", "previous_year", "current_year")
    if any(str(reference_doc.get(key)) != str(candidate_doc.get(key)) for key in identity):
        raise ValueError(f"Company or years differ between {reference_path} and {candidate_path}")
    if reference.keys() != candidate.keys():
        raise ValueError(f"Alignment IDs differ between {reference_path} and {candidate_path}")
    for match_id, left in reference.items():
        right = candidate[match_id]
        if any(left.get(key) != right.get(key) for key in
               ("status", "relationship", "previous_ids", "current_ids", "evidence")):
            raise ValueError(f"Alignment membership changed for {match_id}")

    llm_matrix, majority_matrix = empty_matrix(), empty_matrix()
    vote_paths = Counter()
    paired_exact = Counter()
    paired_binary = Counter()
    overrides = []
    eligible = reference_completed = 0
    missing_llm = []
    for match_id, left in reference.items():
        if left.get("status") != "ai_verified":
            continue
        eligible += 1
        truth = change_label(left)
        if truth is None:
            continue
        reference_completed += 1
        right = candidate[match_id]
        llm = change_label(right)
        if llm is None:
            missing_llm.append(match_id)
            continue
        lexical = change_label(right, "lexical")
        semantic = change_label(right, "semantic")
        chosen, path = majority_decision(llm, lexical, semantic)
        vote_paths[path] += 1
        llm_matrix[truth][llm] += 1
        majority_matrix[truth][chosen] += 1

        before, after = llm == truth, chosen == truth
        paired_exact[(before, after)] += 1
        if truth in CHANGE_BINARY_GROUP:
            before_binary = CHANGE_BINARY_GROUP.get(llm) == CHANGE_BINARY_GROUP[truth]
            after_binary = CHANGE_BINARY_GROUP.get(chosen) == CHANGE_BINARY_GROUP[truth]
            paired_binary[(before_binary, after_binary)] += 1
        if chosen != llm:
            overrides.append({
                "match_id": match_id,
                "reference": truth,
                "llm": llm,
                "lexical": lexical,
                "semantic": semantic,
                "majority": chosen,
                "llm_exact_correct": before,
                "majority_exact_correct": after,
                "llm_binary_correct": (CHANGE_BINARY_GROUP.get(llm) == CHANGE_BINARY_GROUP.get(truth)),
                "majority_binary_correct": (CHANGE_BINARY_GROUP.get(chosen) == CHANGE_BINARY_GROUP.get(truth)),
            })
    return {
        "company": str(reference_doc["company"]).lower(),
        "year_pair": f"{reference_doc['previous_year']}-{reference_doc['current_year']}",
        "reference_file": str(reference_path),
        "candidate_file": str(candidate_path),
        "eligible_rows": eligible,
        "reference_completed": reference_completed,
        "compared": sum(vote_paths.values()),
        "missing_llm_ids": missing_llm,
        "vote_paths": dict(vote_paths),
        "paired_exact": paired_counts(paired_exact),
        "paired_binary": paired_counts(paired_binary),
        "llm_only": score_matrix(llm_matrix),
        "majority": score_matrix(majority_matrix),
        "overrides": overrides,
    }


def paired_counts(counts: Counter) -> dict[str, int]:
    return {
        "both_correct": counts[(True, True)],
        "majority_improved": counts[(False, True)],
        "majority_harmed": counts[(True, False)],
        "both_wrong": counts[(False, False)],
    }


def combine(pairs: list[dict]) -> dict:
    if not pairs:
        raise ValueError("No year-pair evaluations were found")
    matrices = {policy: empty_matrix() for policy in ("llm_only", "majority")}
    vote_paths = Counter()
    paired_exact = Counter()
    paired_binary = Counter()
    for pair in pairs:
        vote_paths.update(pair["vote_paths"])
        paired_exact.update(pair["paired_exact"])
        paired_binary.update(pair["paired_binary"])
        for policy, matrix in matrices.items():
            for label, predictions in pair[policy]["confusion_matrix"].items():
                matrix[label].update(predictions)
    overall = {
        "pair_count": len(pairs),
        "company_count": len({pair["company"] for pair in pairs}),
        "eligible_rows": sum(pair["eligible_rows"] for pair in pairs),
        "reference_completed": sum(pair["reference_completed"] for pair in pairs),
        "compared": sum(pair["compared"] for pair in pairs),
        "vote_paths": dict(sorted(vote_paths.items())),
        "paired_exact": dict(paired_exact),
        "paired_binary": dict(paired_binary),
        "llm_only": score_matrix(matrices["llm_only"]),
        "majority": score_matrix(matrices["majority"]),
    }
    if (overall["llm_only"]["compared"] != overall["majority"]["compared"]
            or overall["llm_only"]["compared"] != overall["compared"]):
        raise ValueError("Policies were not scored on the same rows")
    return overall


def stale_existing_reports(pairs: list[dict], root: Path) -> list[str]:
    stale = []
    for pair in pairs:
        path = root / pair["company"] / pair["year_pair"] / "agreement.json"
        if not path.is_file():
            continue
        existing = json.loads(path.read_text(encoding="utf-8"))
        comparison = existing.get("comparison", {})
        if (comparison.get("reference_file") != pair["reference_file"]
                or comparison.get("candidate_file") != pair["candidate_file"]):
            continue
        old_matrix = existing["change_analysis"]["model_rows"]["confusion_matrix"]
        new_matrix = pair["llm_only"]["confusion_matrix"]
        if any(old_matrix.get(reference, {}).get(candidate, 0)
               != new_matrix.get(reference, {}).get(candidate, 0)
               for reference in CHANGE_LABELS for candidate in CHANGE_LABELS):
            stale.append(f"{pair['company'].upper()} {pair['year_pair']}")
    return stale


def fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "-"


def delta(value: float | None, baseline: float | None) -> str:
    return f"{value - baseline:+.3f}" if value is not None and baseline is not None else "-"


def render_report(overall: dict, pairs: list[dict], stale_reports: list[str]) -> str:
    baseline, majority = overall["llm_only"], overall["majority"]
    paths = overall["vote_paths"]
    overrides = paths.get("majority_overrides_llm", 0)
    override_rate = 100 * overrides / overall["compared"] if overall["compared"] else 0
    lines = [
        "# Change-Analysis Decision Evaluation",
        "",
        f"Evaluated {overall['pair_count']} year pairs across {overall['company_count']} companies "
        f"on the same {overall['compared']:,} reference-labeled `ai_verified` alignments.",
        "",
        "The majority policy selects an exact taxonomy label when at least two of LLM, lexical "
        "and semantic agree. A three-way tie, a missing vote, or a majority label outside the "
        "canonical taxonomy falls back to LLM. The production alignments are not changed.",
        "",
        "The reference is model-generated, not human ground truth. Results measure agreement "
        "with the SoCLaaS benchmark, using the same rows and evidence for both policies. "
        "Input files follow the paths recorded in each existing per-pair evaluation report.",
        "",
        ("Existing per-pair evaluation reports differ from the current alignment files for: "
         + ", ".join(stale_reports) + ". This experiment uses the current alignment files."
         if stale_reports else "Existing per-pair LLM baselines match the current alignment files where checked."),
        "",
        "## Overall Comparison",
        "",
        "| Policy | Exact agreement | Exact macro F1 | Substantive P | Substantive R | Substantive F1 | Non-substantive F1 | Binary macro F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, scored in (("LLM only", baseline), ("Majority, else LLM", majority)):
        binary = scored["substantive_change"]
        positive = binary["per_label"]["Substantive"]
        negative = binary["per_label"]["Non-substantive"]
        lines.append(
            f"| {name} | {fmt(scored['exact_agreement_rate'])} | {fmt(scored['exact_macro_f1'])} | "
            f"{fmt(positive['precision'])} | {fmt(positive['recall'])} | {fmt(positive['f1'])} | "
            f"{fmt(negative['f1'])} | {fmt(binary['macro_f1_vs_reference'])} |"
        )
    lines += [
        "",
        f"Majority minus LLM: exact macro F1 **{delta(majority['exact_macro_f1'], baseline['exact_macro_f1'])}**; "
        f"binary macro F1 **{delta(majority['substantive_change']['macro_f1_vs_reference'], baseline['substantive_change']['macro_f1_vs_reference'])}**; "
        f"exact agreement **{delta(majority['exact_agreement_rate'], baseline['exact_agreement_rate'])}**.",
        "",
        "### Exact Taxonomy By Label",
        "",
        "| Label | Reference n | LLM predicted n | Majority predicted n | LLM F1 | Majority F1 | Delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label in CHANGE_LABELS:
        llm_label = baseline["exact_per_label"][label]
        majority_label = majority["exact_per_label"][label]
        if not llm_label["reference_count"] and not llm_label["candidate_count"] and not majority_label["candidate_count"]:
            continue
        lines.append(
            f"| {label} | {llm_label['reference_count']:,} | {llm_label['candidate_count']:,} | "
            f"{majority_label['candidate_count']:,} | {fmt(llm_label['f1'])} | "
            f"{fmt(majority_label['f1'])} | {delta(majority_label['f1'], llm_label['f1'])} |"
        )
    lines += [
        "",
        "## Decision Impact",
        "",
        f"- Majority changed {overrides:,} LLM labels ({override_rate:.1f}% of compared rows).",
        f"- All three labels differed on {paths.get('three_way_tie_fallback', 0):,} rows; "
        f"{paths.get('missing_vote_fallback', 0):,} had a missing lexical or semantic vote. "
        "Both paths retained LLM.",
        f"- Exact taxonomy: majority fixed {overall['paired_exact']['majority_improved']:,} "
        f"LLM errors and introduced {overall['paired_exact']['majority_harmed']:,} new errors.",
        f"- Binary substantive change: majority fixed {overall['paired_binary']['majority_improved']:,} "
        f"LLM errors and introduced {overall['paired_binary']['majority_harmed']:,} new errors.",
        "",
        "## Decision",
        "",
    ]
    if (majority["exact_macro_f1"] > baseline["exact_macro_f1"]
            and majority["exact_agreement_rate"] < baseline["exact_agreement_rate"]):
        lines.append(
            "There is no unqualified winner: majority improves class-balanced exact taxonomy "
            "F1 but lowers exact-label agreement. Its binary macro F1 change is small. "
            "Keep LLM-only as the conservative default unless broader coverage of less common "
            "taxonomies is the explicit priority; review override examples and human-label a "
            "sample before changing production policy."
        )
    else:
        lines.append(
            "Choose the policy against the project's primary objective: exact-label agreement "
            "for overall correctness, exact macro F1 for class balance, or binary macro F1 "
            "for substantive-change detection. These reference labels are not human ground truth."
        )
    lines += [
        "",
        "## Per Year Pair",
        "",
        "| Company | Years | Rows | Overrides | Exact F1 LLM | Exact F1 majority | Binary F1 LLM | Binary F1 majority | Fixed exact | Harmed exact |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pair in pairs:
        llm, voted = pair["llm_only"], pair["majority"]
        lines.append(
            f"| {pair['company'].upper()} | {pair['year_pair']} | {pair['compared']:,} | "
            f"{pair['vote_paths'].get('majority_overrides_llm', 0):,} | "
            f"{fmt(llm['exact_macro_f1'])} | {fmt(voted['exact_macro_f1'])} | "
            f"{fmt(llm['substantive_change']['macro_f1_vs_reference'])} | "
            f"{fmt(voted['substantive_change']['macro_f1_vs_reference'])} | "
            f"{pair['paired_exact']['majority_improved']:,} | "
            f"{pair['paired_exact']['majority_harmed']:,} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "- Exact macro F1 treats the reference-supported change taxonomies equally. Binary macro F1 "
        "groups Modified, Expanded and Reduced as substantive; Reworded and Unchanged as non-substantive.",
        "- New and Removed are not scored in this matched-row (`ai_verified`) policy comparison. "
        "Deterministic alignment rows are likewise excluded.",
        "- The paired fixed/harmed counts show what the vote changes on identical rows; they do "
        "not establish that the model-generated reference is correct.",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-source", choices=("reports", "files"), default="reports",
                        help="Use existing evaluation reports' recorded input paths (default) or folder roots")
    parser.add_argument("--reference-root", type=Path, default=Path("data/alignments_for_eval_soclaas"))
    parser.add_argument("--candidate-root", type=Path, default=Path("data/alignments"))
    parser.add_argument("--evaluation-root", "--existing-evaluation-root", type=Path,
                        default=Path("data/evaluation_soclaas"),
                        help="Saved per-pair evaluations and their recorded input file paths")
    parser.add_argument("--output-dir", type=Path, default=Path("data/evaluation_soclaas/change_ensemble"))
    parser.add_argument("--report", type=Path, default=Path("docs/JINGLE_change_ensemble_evaluation.md"))
    parser.add_argument("--ticker", action="append", help="Limit to a ticker; repeat for several")
    parser.add_argument("--year-pair", action="append", help="Limit to YYYY-YYYY; repeat for several")
    args = parser.parse_args(argv)
    args.ticker = {ticker.lower() for ticker in args.ticker or []}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ticker) for ticker in args.ticker):
        parser.error("Invalid ticker directory name")
    args.year_pair = set(args.year_pair or [])
    if any(not YEAR_PAIR.fullmatch(year_pair) or year_pair[:4] >= year_pair[5:]
           for year_pair in args.year_pair):
        parser.error("Year pairs must contain increasing four-digit years")
    if args.output_dir.resolve() in {args.reference_root.resolve(), args.candidate_root.resolve()}:
        parser.error("Output directory must differ from either alignment root")
    return args


def input_paths(args: argparse.Namespace) -> list[tuple[Path, Path]]:
    if args.input_source == "reports":
        paths = sorted(args.evaluation_root.glob("*/????-????/agreement.json"))
        paths = [path for path in paths
                 if (not args.ticker or path.parent.parent.name in args.ticker)
                 and (not args.year_pair or path.parent.name in args.year_pair)]
        inputs = []
        for path in paths:
            comparison = json.loads(path.read_text(encoding="utf-8"))["comparison"]
            reference_path = Path(comparison["reference_file"])
            candidate_path = Path(comparison["candidate_file"])
            if (comparison["company"] != path.parent.parent.name
                    or f"{comparison['previous_year']}-{comparison['current_year']}" != path.parent.name):
                raise ValueError(f"Comparison metadata does not match {path}")
            inputs.append((reference_path, candidate_path))
    else:
        paths = sorted(args.reference_root.glob("*/????-????/alignments.json"))
        paths = [path for path in paths
                 if (not args.ticker or path.parent.parent.name in args.ticker)
                 and (not args.year_pair or path.parent.name in args.year_pair)]
        inputs = [(path, args.candidate_root / path.parent.parent.name / path.parent.name / "alignments.json")
                  for path in paths]
    if not inputs:
        raise ValueError("No evaluation inputs matched the requested filters")
    for reference_path, candidate_path in inputs:
        for path in (reference_path, candidate_path):
            if not path.is_file():
                raise ValueError(f"Missing alignment file: {path}")
    return inputs


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pairs = [evaluate_pair(reference_path, candidate_path)
             for reference_path, candidate_path in input_paths(args)]
    overall = combine(pairs)
    stale_reports = stale_existing_reports(pairs, args.evaluation_root)
    summary = {"schema_version": "1", "overall": overall,
               "stale_existing_reports": stale_reports,
               "pairs": [{key: value for key, value in pair.items() if key != "overrides"} for pair in pairs]}
    overrides = [dict(company=pair["company"], year_pair=pair["year_pair"], **case)
                 for pair in pairs for case in pair["overrides"]]
    atomic_json(args.output_dir / "metrics.json", summary)
    atomic_json(args.output_dir / "overrides.json", overrides)
    atomic_text(args.report, render_report(overall, pairs, stale_reports))
    print(f"Evaluated {overall['compared']:,} rows across {len(pairs)} pairs; "
          f"{len(overrides):,} majority overrides. Report: {args.report}")
    if stale_reports:
        print("Existing evaluation reports differ from current alignments: " + ", ".join(stale_reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
