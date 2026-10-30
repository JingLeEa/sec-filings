#!/usr/bin/env python3
"""Score a run of scripts/evaluate_chatbot_sql.py: does the chatbot return the reference result, and say it?

    python scripts/score_chatbot_sql.py eval/sql_accuracy/runs/offline.json [--out scored.json]

Two checks per question:
- data: the rows the chatbot fetched contain the reference result. How this is compared depends on answer_type:
  count (number equal; a per-run breakdown that sums to it counts), ids (no wrong IDs, and either all IDs or a
  total equal to the reference count), topn (top N match, ties in the ranking score allowed in any order),
  groups (the same value for every group), value (equal within rounding), table (every reference column found in
  one chatbot result; for "which ... highest/most" questions the top answer is enough).
- text: the written answer states the reference number(s) / top answer / only reference IDs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ID_RE = re.compile(r"\b[a-z]{1,6}_\d{4}_\d{4}_M\d{4}\b")
NUM_RE = re.compile(r"(?<![\w.])[-+−]?\d[\d,]*(?:\.\d+)?")
CHANGE_TYPES = ["unchanged", "modified", "reworded", "expanded", "reduced", "new", "removed"]
TOP_WORDS = re.compile(r"\b(?:which|what)\b.*\b(?:highest|most|largest|biggest|lowest|least)\b", re.I)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def close(a, b, rel=0.005, abs_=0.011):
    return abs(a - b) <= max(abs_, rel * abs(b))


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM_RE.findall(text or ""):
        try:
            out.append(float(m.replace(",", "").replace("−", "-")))
        except ValueError:
            pass
    return out


def text_has(text, value) -> bool:
    if is_num(value):
        return any(close(x, value, rel=0.006, abs_=0.051) for x in numbers_in(text))
    return str(value).lower() in (text or "").lower()


def value_cols(result):
    return [c for c in result["columns"] if "year" not in c and any(is_num(r.get(c)) for r in result["rows"])]


def norm_key(v):
    if v is None:
        return "null"
    if is_num(v):
        return f"{float(v):g}"
    return str(v).strip().lower()


# ---------------------------------------------------------------- per answer type

COUNT_COL = re.compile(r"^(?:n|count|total|value|alignments|.*count.*|n_.*|num.*|total_.*)$")


def score_count(q, gold, results):
    n = gold["rows"][0][gold["columns"][0]] if gold["rows"] else 0
    data = False
    for r in results:
        for c in (c for c in value_cols(r) if COUNT_COL.match(c)):
            vals = [row[c] for row in r["rows"] if is_num(row.get(c))]
            if (len(r["rows"]) == 1 and vals and close(vals[0], n, abs_=0.5)) or (vals and close(sum(vals), n, abs_=0.5)):
                data = True
    return {"expected": n, "data": data, "text": text_has(q.get("answer", ""), n), "detail": f"expected {n}"}


def score_ids(q, gold, results):
    g = {row["match_id"] for row in gold["rows"]}
    c = {row["match_id"] for r in results for row in r["rows"] if isinstance(row.get("match_id"), str)}
    hit = c & g
    precision = len(hit) / len(c) if c else (1.0 if not g else 0.0)
    recall = len(hit) / len(g) if g else (1.0 if not c else 0.0)
    totals = [sum(row[col] for row in r["rows"] if is_num(row.get(col))) for r in results for col in value_cols(r)
              if col in ("n", "count", "total", "alignments")]
    count_ok = any(close(t, len(g), abs_=0.5) for t in totals)
    data = precision == 1.0 and (recall == 1.0 or count_ok)
    cited = set(ID_RE.findall(q.get("answer", "")))
    text = (cited <= g) and (bool(cited) or not g)
    return {"expected": len(g), "data": data, "text": text, "precision": round(precision, 3),
            "recall": round(recall, 3), "count_ok": count_ok,
            "detail": f"{len(g)} expected; chatbot fetched {len(c)}, {len(hit)} correct"
                      + ("; total count matches" if count_ok else "")}


def score_topn(q, gold, results):
    n, rows = q["n"], gold["rows"]
    if not rows:
        return {"expected": 0, "data": not any(r["rows"] for r in results), "text": True, "detail": "no rows expected"}
    k = min(n, len(rows))
    edge = rows[k - 1]["score"]
    better = (lambda s: s < edge) if q.get("order") == "asc" else (lambda s: s > edge)
    must = {r["match_id"] for r in rows if r["score"] is not None and better(r["score"])}
    allowed = must | {r["match_id"] for r in rows if r["score"] == edge}
    ranked = next((r for r in results if "match_id" in r["columns"] and r["rows"]), None)
    top = [row["match_id"] for row in ranked["rows"][:k]] if ranked else []
    overlap = len(set(top) & allowed) / k
    data = len(top) == k and set(top) <= allowed and must <= set(top)
    cited = set(ID_RE.findall(q.get("answer", "")))
    text = must <= cited and len(cited & allowed) >= k
    return {"expected": k, "data": data, "text": text, "overlap": round(overlap, 3),
            "detail": f"top {k}: {len(set(top) & allowed)}/{k} acceptable" + (f" ({len(allowed) - len(must)} tied at the cut-off)"
                                                                                if len(allowed) > k else "")}


def grouped(result, group_cols):
    """Candidate {group: value} dicts from one chatbot result (long or wide format)."""
    out = []
    cols = result["columns"]
    if all(g in cols for g in group_cols):
        for vc in value_cols(result):
            if vc in group_cols:
                continue
            d = defaultdict(float)
            for row in result["rows"]:
                if is_num(row.get(vc)):
                    d[tuple(norm_key(row.get(g)) for g in group_cols)] += row[vc]
            out.append(dict(d))
    if "change_type" in group_cols:   # wide: one column per change type
        others = [g for g in group_cols if g != "change_type"]
        wide = [c for c in cols if c.lower() in CHANGE_TYPES]
        if wide and all(o in cols for o in others):
            d = defaultdict(float)
            for row in result["rows"]:
                for c in wide:
                    if is_num(row.get(c)):
                        key = tuple(norm_key(row.get(g)) if g != "change_type" else c.lower() for g in group_cols)
                        d[key] += row[c]
            out.append(dict(d))
    return out


def score_groups(q, gold, results):
    measure = "n" if "n" in gold["columns"] else value_cols(gold)[-1]
    group_cols = [c for c in gold["columns"] if c != measure]
    expected = {tuple(norm_key(row[g]) for g in group_cols): row[measure] for row in gold["rows"]}
    best = 0.0
    for r in results:
        for d in grouped(r, group_cols):
            hits = sum(1 for k, v in expected.items() if k in d and close(d[k], v, abs_=0.5))
            best = max(best, hits / len(expected) if expected else 1.0)
    text_hits = sum(text_has(q.get("answer", ""), v) for v in expected.values())
    return {"expected": len(expected), "data": best == 1.0, "groups_matched": round(best, 3),
            "text": bool(expected) and text_hits / len(expected) >= 0.8,
            "detail": f"{len(expected)} groups by {', '.join(group_cols)}; best match {best:.0%}"}


def score_value(q, gold, results):
    v = gold["rows"][0][gold["columns"][0]] if gold["rows"] else None
    if v is None:
        return {"expected": None, "data": False, "text": False, "detail": "reference returned no value"}
    cells = [row[c] for r in results for row in r["rows"] for c in r["columns"] if is_num(row.get(c))]
    data = any(close(x, v) or close(x * 100, v) or close(x / 100, v) for x in cells)
    return {"expected": v, "data": data, "text": text_has(q.get("answer", ""), v), "detail": f"expected {v:g}"}


def score_table(q, gold, results):
    gcols = gold["columns"]
    if not gold["rows"]:
        return {"expected": 0, "data": not any(r["rows"] for r in results), "text": True, "detail": "no rows expected"}

    def cell(v):
        return f"{round(float(v), 2):g}" if is_num(v) else norm_key(v)

    def contains(r):
        """Every reference row appears in this result (extra rows/columns allowed, e.g. more years shown)."""
        candidates = []
        for gc in gcols:
            need = Counter(cell(row.get(gc)) for row in gold["rows"])
            cols = [cc for cc in r["columns"] if not need - Counter(cell(row.get(cc)) for row in r["rows"])]
            if not cols:
                return False
            candidates.append(cols[:3])
        from itertools import product
        for combo in product(*candidates):
            have = Counter(tuple(cell(row.get(c)) for c in combo) for row in r["rows"])
            if not Counter(tuple(cell(row.get(gc)) for gc in gcols) for row in gold["rows"]) - have:
                return True
        return False
    full = any(contains(r) for r in results if r["rows"])
    top_row = gold["rows"][0]
    label_col = next((c for c in gcols if not is_num(top_row.get(c))), None)
    top_label = top_row.get(label_col) if label_col else None
    top_ok = False
    if TOP_WORDS.search(q["question"]) and top_label is not None:
        for r in results:   # an aggregated result (not row-level alignments) whose first row is the top answer
            if r["rows"] and "match_id" not in r["columns"] and \
                    any(norm_key(v) == norm_key(top_label) for v in r["rows"][0].values()):
                top_ok = True
    data = full or top_ok
    answer = q.get("answer", "")
    if top_label is not None:
        text = text_has(answer, top_label)
    else:
        nums = [v for v in top_row.values() if is_num(v)]
        text = bool(nums) and all(text_has(answer, x) for x in nums)
    return {"expected": len(gold["rows"]), "data": data, "text": text, "full_table": full, "top_answer": top_ok,
            "detail": f"{len(gold['rows'])} reference rows; top: {top_label if top_label is not None else top_row}"}


SCORERS = {"count": score_count, "ids": score_ids, "topn": score_topn, "groups": score_groups, "value": score_value,
           "table": score_table}


def verdict(q, s) -> str:
    if q.get("chatbot_error"):
        return "Chatbot error"
    if q.get("gold_error"):
        return "Reference SQL error"
    if s["data"] and s["text"]:
        return "Correct"
    if s["data"]:
        return "Right data, answer text wrong"
    if not any(r["rows"] for r in q.get("results", [])):
        return "No data returned"
    return "Wrong data"


def score_run(run: dict) -> list[dict]:
    scored = []
    for q in run["questions"]:
        s = {"data": False, "text": False, "detail": ""}
        if "gold" in q and "results" in q:
            s = SCORERS[q["answer_type"]](q, q["gold"], q["results"])
        s["verdict"] = verdict(q, s)
        scored.append({"id": q["id"], "set": q["set"], "answer_type": q["answer_type"], "question": q["question"],
                       "intent": q.get("intent"), **s})
    return scored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, help="write per-question scores as JSON")
    args = parser.parse_args(argv)
    run = json.loads(args.run.read_text(encoding="utf-8"))
    scored = score_run(run)
    print(f"Mode: {run['mode']} ({run.get('llm')})")
    for part in ("rules", "hard", None):
        rows = [s for s in scored if part is None or s["set"] == part]
        label = {"rules": "Rule questions", "hard": "Hard (LLM-SQL) questions", None: "All"}[part]
        print(f"{label:28} data {sum(s['data'] for s in rows):>2}/{len(rows)}   "
              f"text {sum(s['text'] for s in rows):>2}/{len(rows)}   "
              f"correct {sum(s['verdict'] == 'Correct' for s in rows):>2}/{len(rows)}")
    print("By answer type:")
    for t in SCORERS:
        rows = [s for s in scored if s["answer_type"] == t]
        if rows:
            print(f"  {t:8} data {sum(s['data'] for s in rows)}/{len(rows)}")
    for s in scored:
        if s["verdict"] != "Correct":
            print(f"  #{s['id']:<3} {s['verdict']:<30} [{s['intent']}] {s['question'][:60]} | {s['detail']}")
    if args.out:
        args.out.write_text(json.dumps(scored, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
