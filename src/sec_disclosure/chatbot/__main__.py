"""Command-line chatbot.

    python -m sec_disclosure.chatbot "What are the most material changes in Bank of America's 2024 10-K?"
    python -m sec_disclosure.chatbot              # interactive; 'exit' to quit, 'reset' to clear context
    python -m sec_disclosure.chatbot --offline --show-sql "Show me the semantic vs lexical disagreements"
    python -m sec_disclosure.chatbot --history    # list saved conversations and their questions

Every question is saved (the same history the Streamlit page shows). Interactive mode continues the most
recent conversation, so follow-ups keep working after a restart; type 'new' to start a fresh one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import DatabaseError
from .engine import Chatbot
from .history import ChatHistory


def _utf8_console() -> None:
    """Windows consoles default to cp1252, which cannot print the arrows and quotes used in answers."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print(reply, show_sql: bool, as_json: bool, show_timing: bool = False) -> None:
    if as_json:
        print(json.dumps({"answer": reply.answer, "intent": reply.intent, "filters": reply.filters, "mode": reply.mode,
                          "retrieval": reply.retrieval_method, "citations": reply.citations[:50],
                          "warnings": reply.warnings, "notes": reply.notes}, indent=2, default=str))
        return
    print(reply.answer)
    query_ms = sum(r.elapsed_ms or 0 for r in reply.results)
    meta = (f"[{reply.intent} · {reply.mode}" + (f" · {reply.retrieval_method}" if reply.retrieval_method else "")
            + f" · {reply.elapsed_ms or 0:.0f} ms total, {query_ms:.0f} ms in {len(reply.results)} queries]")
    print("\n" + meta)
    for warning in reply.warnings:
        print(f"warning: {warning}")
    if show_sql:
        for name, sql, params in reply.queries:
            print(f"\n-- {name}  params={params}\n{sql.strip()}")
    if show_timing:
        for result in reply.results:
            print(f"  {result.elapsed_ms or 0:8.1f} ms  {result.name} ({len(result.rows)} rows)")


class _Session:
    """Asks questions and saves each one to the current saved conversation."""

    def __init__(self, bot: Chatbot, store: ChatHistory | None):
        self.bot, self.store, self.conversation_id = bot, store, None

    def resume(self, show: bool = False) -> None:
        if self.store is None:
            return
        try:
            latest = self.store.latest()
            if latest is None:
                return
            turns = self.store.turns(latest)
            self.conversation_id = latest
            self.bot.last_plan = self.store.last_plan(latest)
        except DatabaseError as error:
            print(f"(Earlier questions could not be loaded: {error})", file=sys.stderr)
            return
        if show and turns:
            print(f"\nContinuing your last conversation ({len(turns)} question{'s' if len(turns) != 1 else ''}):")
            for turn in turns[-5:]:
                question = turn["question"] if isinstance(turn, dict) else turn.question
                print(f"  - {question}")
            if len(turns) > 5:
                print(f"  ... and {len(turns) - 5} earlier (see 'history').")

    def new(self) -> None:
        self.bot.reset()
        self.conversation_id = None

    def ask(self, question: str, args) -> None:
        try:
            reply = self.bot.ask(question)
        except DatabaseError as error:
            self._save(question, error=f"Database error: {error}")
            raise
        _print(reply, args.show_sql, args.json, args.timing)
        self._save(question, reply=reply)

    def _save(self, question: str, reply=None, error: str | None = None) -> None:
        if self.store is None:
            return
        try:
            self.conversation_id = self.store.save_turn(self.conversation_id, question, reply=reply,
                                                        plan=self.bot.last_plan if reply is not None else None,
                                                        error=error, source="cli")
        except DatabaseError as error_saving:
            print(f"(This question was not saved to the history: {error_saving})", file=sys.stderr)


def _list_history(store: ChatHistory | None, limit: int = 20) -> int:
    if store is None:
        print("History is switched off (--no-history).")
        return 0
    try:
        conversations = store.conversations(limit=limit)
    except DatabaseError as error:
        print(f"Database error: {error}", file=sys.stderr)
        return 1
    if not conversations:
        print("No saved conversations yet.")
        return 0
    for conversation in conversations:
        when = conversation["updated_at"].astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"\n{when} · {conversation['source']} · {conversation['questions']} question(s)")
        for turn in store.turns(conversation["conversation_id"]):
            question = turn["question"] if isinstance(turn, dict) else turn.question
            print(f"  - {question}" + ("  [not answered]" if isinstance(turn, dict) else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="*", help="Question to ask (omit for interactive mode)")
    parser.add_argument("--offline", action="store_true", help="Do not call an LLM; templated answers only")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip pgvector search (full-text search only)")
    parser.add_argument("--show-sql", action="store_true", help="Print the SQL behind each answer")
    parser.add_argument("--json", action="store_true", help="Print a JSON response")
    parser.add_argument("--env-file", type=Path, help="Settings file (default: repository .env)")
    parser.add_argument("--timing", action="store_true", help="Print how long each database query took")
    parser.add_argument("--check", action="store_true", help="Send one small SoCLaaS request and report the result")
    parser.add_argument("--history", action="store_true", help="List saved conversations and their questions, then exit")
    parser.add_argument("--new", action="store_true", help="Interactive mode: start a new conversation instead of continuing the last")
    parser.add_argument("--continue", dest="resume", action="store_true",
                        help="One-question mode: add the question to the last conversation (follow-ups keep its scope)")
    parser.add_argument("--no-history", action="store_true", help="Do not save questions or load earlier ones")
    args = parser.parse_args(argv)
    _utf8_console()
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
    store = None if args.no_history else ChatHistory(bot.db)
    if args.history:
        return _list_history(store)
    session = _Session(bot, store)
    if args.question:
        if args.resume:
            session.resume()
        try:
            session.ask(" ".join(args.question), args)
            if args.timing and bot.db.connect_ms is not None:
                print(f"  {bot.db.connect_ms:8.1f} ms  opening the database connection (once per session)")
        except DatabaseError as error:
            print(f"Database error: {error}", file=sys.stderr)
            return 1
        return 0
    print(f"SEC disclosure chatbot · answers: {bot.llm_description}. "
          "Type 'exit' to quit, 'new' for a new conversation, 'history' to see saved ones.")
    if not args.new:
        session.resume(show=True)
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in {"exit", "quit", ":q"}:
            return 0
        if question.lower() in {"reset", "new"}:
            session.new()
            print("New conversation started; the earlier one stays saved.")
            continue
        if question.lower() == "history":
            _list_history(store)
            continue
        if question:
            try:
                session.ask(question, args)
            except DatabaseError as error:
                print(f"Database error: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
