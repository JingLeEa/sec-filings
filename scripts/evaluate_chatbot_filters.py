#!/usr/bin/env python3
"""Filter-extraction evaluation: does the chatbot read each question's filters and intent correctly?

    python scripts/evaluate_chatbot_filters.py                 # main set (80 questions)
    python scripts/evaluate_chatbot_filters.py --set holdout   # hold-out set (32 questions)

Runs every question through the rule-based planner (no database, no LLM) against all 11 companies with
2023->2024 and 2024->2025 comparisons loaded, and compares the result with labels written by an independent
model from eval/filter_extraction/LABELING_SPEC.md. Years are compared as the comparison windows actually
searched, so different spellings of the same window count as the same. Intent counts as right when it equals
the label or one of the labeller's acceptable alternatives.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
EVAL = ROOT / "eval" / "filter_extraction"
COMPANIES = ["bac", "jpm", "c", "gs", "ms", "intc", "amd", "nvda", "avgo", "qcom", "mu"]
RUNS = [(c, p, p + 1) for c in COMPANIES for p in (2023, 2024)]
ALL_WINDOWS = [(2023, 2024), (2024, 2025)]
SET_FIELDS = ["companies", "items", "change_types", "taxonomies", "statuses", "relationships", "match_ids"]
NUM_FIELDS = ["min_materiality", "max_confidence", "limit"]
CORE = ["intent", "companies", "windows", "items", "change_types", "statuses", "relationships", "match_ids",
        "min_materiality", "max_confidence", "limit"]


def windows(prev, cur) -> list[str]:
    """The loaded windows a filter selects (as queries.where_clause does); [] = no year restriction."""
    if prev and cur and cur - prev > 1:
        chosen = [w for w in ALL_WINDOWS if w[0] >= prev and w[1] <= cur]
    else:
        chosen = [w for w in ALL_WINDOWS if (prev is None or w[0] == prev) and (cur is None or w[1] == cur)]
    chosen = [f"{a}-{b}" for a, b in chosen]
    return [] if len(chosen) == len(ALL_WINDOWS) else chosen


def norm_windows(values) -> list[str]:
    values = sorted(set(values or []))
    return [] if values == ["2023-2024", "2024-2025"] else values


def evaluate(questions: list[dict], gold: dict[int, dict]) -> list[dict]:
    from sec_disclosure.chatbot.planner import inherit_context, plan_question
    rows = []
    for q in questions:
        g = gold[q["id"]]
        plan = plan_question(q["question"], RUNS)
        if q.get("context"):
            plan = inherit_context(plan, plan_question(q["context"], RUNS), q["question"])
        f = plan.filters
        pred = {"intent": plan.intent.value, "companies": f.companies, "windows": windows(f.previous_year, f.current_year),
                "items": f.items, "change_types": f.change_types, "taxonomies": f.disclosure_taxonomies,
                "statuses": f.statuses, "relationships": f.relationships, "match_ids": f.match_ids,
                "min_materiality": f.min_materiality, "max_confidence": f.max_confidence, "limit": f.limit,
                "topic": plan.topic, "clarifications": f.clarifications}
        ok = {"intent": pred["intent"] == g["intent"] or pred["intent"] in (g.get("intent_alternatives") or []),
              "windows": norm_windows(pred["windows"]) == norm_windows(g["windows"])}
        for k in SET_FIELDS:
            ok[k] = sorted(pred[k]) == sorted(g[k] or [])
        for k in NUM_FIELDS:
            a, b = pred[k], g[k]
            ok[k] = (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-9)
        rows.append({"id": q["id"], "question": q["question"], "context": q.get("context", ""), "gold": g,
                     "pred": pred, "ok": ok})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=("main", "holdout"), default="main")
    parser.add_argument("--out", type=Path, help="write per-question results as JSON")
    args = parser.parse_args(argv)
    prefix = "" if args.set == "main" else "holdout_"
    questions = json.loads((EVAL / f"{prefix}questions.json").read_text(encoding="utf-8"))
    gold = {g["id"]: g for g in json.loads((EVAL / f"{prefix}gold.json").read_text(encoding="utf-8"))}
    rows = evaluate(questions, gold)
    n = len(rows)
    for field in CORE + ["taxonomies"]:
        print(f"{field:16} {sum(r['ok'][field] for r in rows):>3}/{n}")
    print(f"{'all core fields':16} {sum(all(r['ok'][k] for k in CORE) for r in rows):>3}/{n}")
    for r in rows:
        bad = [k for k in CORE + ["taxonomies"] if not r["ok"][k]]
        if bad:
            print(f"\n#{r['id']} {r['question']}" + (f"  (after: {r['context']})" if r["context"] else ""))
            for k in bad:
                print(f"   {k}: expected {r['gold'][k]}  got {r['pred'][k]}")
            for c in r["pred"]["clarifications"]:
                print(f"   flagged to user: {c}")
    if args.out:
        args.out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
