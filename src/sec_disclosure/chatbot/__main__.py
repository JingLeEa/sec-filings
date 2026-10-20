"""Command-line chatbot.

    python -m sec_disclosure.chatbot "What are the most material changes in Bank of America's 2024 10-K?"
    python -m sec_disclosure.chatbot              # interactive; 'exit' to quit, 'reset' to clear context
    python -m sec_disclosure.chatbot --offline --show-sql "Show me the semantic vs lexical disagreements"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import DatabaseError
from .engine import Chatbot


def _print(reply, show_sql: bool, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"answer": reply.answer, "intent": reply.intent, "filters": reply.filters, "mode": reply.mode,
                          "retrieval": reply.retrieval_method, "citations": reply.citations[:50],
                          "warnings": reply.warnings, "notes": reply.notes}, indent=2, default=str))
        return
    print(reply.answer)
    meta = f"[{reply.intent} · {reply.mode}" + (f" · {reply.retrieval_method}" if reply.retrieval_method else "") + "]"
    print("\n" + meta)
    for warning in reply.warnings:
        print(f"warning: {warning}")
    if show_sql:
        for name, sql, params in reply.queries:
            print(f"\n-- {name}  params={params}\n{sql.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="*", help="Question to ask (omit for interactive mode)")
    parser.add_argument("--offline", action="store_true", help="Do not call an LLM; templated answers only")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip pgvector search (full-text search only)")
    parser.add_argument("--show-sql", action="store_true", help="Print the SQL behind each answer")
    parser.add_argument("--json", action="store_true", help="Print a JSON response")
    parser.add_argument("--env-file", type=Path, help="Settings file (default: repository .env)")
    parser.add_argument("--check", action="store_true", help="Send one small SoCLaaS request and report the result")
    args = parser.parse_args(argv)
    try:
        bot = Chatbot(env_file=args.env_file, use_llm=not args.offline, use_embeddings=not args.no_embeddings)
    except (DatabaseError, ValueError) as error:
        print(f"Startup error: {error}", file=sys.stderr)
        return 1
    if args.check:
        from ..llm.client import LLMError
        try:
            print(bot.check_llm())
            return 0
        except LLMError as error:
            where = f" [{bot.llm_description}]" if bot.llm_enabled else ""
            print(f"SoCLaaS check failed: {error}{where}", file=sys.stderr)
            return 1
    if args.question:
        try:
            _print(bot.ask(" ".join(args.question)), args.show_sql, args.json)
        except DatabaseError as error:
            print(f"Database error: {error}", file=sys.stderr)
            return 1
        return 0
    print(f"SEC disclosure chatbot · answers: {bot.llm_description}. Type 'exit' to quit, 'reset' to clear context.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in {"exit", "quit", ":q"}:
            return 0
        if question.lower() == "reset":
            bot.reset()
            print("Context cleared.")
            continue
        if question:
            try:
                _print(bot.ask(question), args.show_sql, args.json)
            except DatabaseError as error:
                print(f"Database error: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
