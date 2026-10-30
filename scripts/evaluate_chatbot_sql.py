#!/usr/bin/env python3
"""Run the SQL-accuracy evaluation: each question's reference SQL and the chatbot's answer, saved for scoring.

    python scripts/evaluate_chatbot_sql.py --mode offline --out eval/sql_accuracy/runs/offline.json
    python scripts/evaluate_chatbot_sql.py --mode llm --out eval/sql_accuracy/runs/llm_1.json
    python scripts/score_chatbot_sql.py eval/sql_accuracy/runs/offline.json

Questions: eval/sql_accuracy/questions.json. Reference SQL: eval/sql_accuracy/gold_sql.json (written from
eval/sql_accuracy/DATA_DICTIONARY.md without looking at the chatbot's queries). The reference SQL runs on the
same database as the chatbot, so reloaded data never leaves stale expected answers. Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
EVAL = ROOT / "eval" / "sql_accuracy"
LONG_TEXT = {"previous_text", "current_text", "previous_excerpt", "current_excerpt", "materiality_reason",
             "explanation", "semantic_rationale", "previous_summary", "current_summary", "analysis",
             "executive_summary", "longitudinal_analysis", "financial_analysis"}
MAX_ROWS = 1000


def plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def records(result) -> dict:
    columns = [c for c in result.columns if c not in LONG_TEXT]
    return {"name": result.name, "sql": result.sql, "columns": columns,
            "rows": [{c: plain(row.get(c)) for c in columns} for row in result.rows[:MAX_ROWS]],
            "total_rows": len(result.rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("offline", "llm"), default="offline",
                        help="offline = templated answers and rule SQL only; llm = SoCLaaS writes SQL and answers")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--questions", type=Path, default=EVAL / "questions.json")
    parser.add_argument("--gold", type=Path, default=EVAL / "gold_sql.json")
    parser.add_argument("--ids", type=int, nargs="*", help="only these question ids")
    parser.add_argument("--no-embeddings", action="store_true", help="keyword search only (no pgvector)")
    args = parser.parse_args(argv)

    from sec_disclosure.chatbot import Chatbot
    from sec_disclosure.chatbot.db import Database, DatabaseError

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    gold = {g["id"]: g for g in json.loads(args.gold.read_text(encoding="utf-8"))}
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]
    db = Database()
    bot = Chatbot(db=db, use_llm=args.mode == "llm", use_embeddings=not args.no_embeddings)
    out = {"mode": args.mode, "llm": bot.llm_description, "started": datetime.now().isoformat(timespec="seconds"),
           "questions": []}
    for q in questions:
        entry = {**q, "gold_sql": gold[q["id"]]["sql"], "gold_interpretation": gold[q["id"]].get("interpretation", "")}
        try:
            entry["gold"] = records(db.query("gold", gold[q["id"]]["sql"]))
        except DatabaseError as error:
            entry["gold_error"] = str(error)
        bot.reset()
        started = time.perf_counter()
        try:
            reply = bot.ask(q["question"])
            entry.update(intent=reply.intent, mode=reply.mode, retrieval_method=reply.retrieval_method,
                         filters=reply.filters, notes=reply.notes, warnings=reply.warnings, answer=reply.answer,
                         results=[records(r) for r in reply.results])
        except Exception as error:  # noqa: BLE001 - record and keep going
            entry["chatbot_error"] = f"{type(error).__name__}: {error}"
        entry["seconds"] = round(time.perf_counter() - started, 2)
        out["questions"].append(entry)
        print(f"[{q['id']:>2}] {entry.get('intent', 'ERROR'):<20} {entry['seconds']:>6.1f}s  {q['question'][:70]}",
              flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"Saved {len(out['questions'])} answers to {args.out}. Score with: "
          f"python scripts/score_chatbot_sql.py {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
