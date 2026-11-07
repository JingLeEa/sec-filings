"""Saved chatbot conversations (db/init/12_chatbot_history.sql).

Every question and its answer are stored as they are asked, so a conversation survives a page reload, an app
restart or a new CLI session. A stored turn keeps what is needed to redraw the answer (text, citations,
evidence rows, SQL, grounding) and the parsed question plan, so a follow-up such as "what about Intel?" still
knows the earlier company and years after a restart.
"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import date, datetime
from decimal import Decimal

from .db import Database, DatabaseError, QueryResult
from .entities import QueryFilters
from .grounding import GroundingReport
from .planner import Intent, Plan

MAX_ROWS_PER_RESULT = 200      # evidence rows kept per search (the page shows them in a table)
TITLE_LENGTH = 80


def _plain(value):
    """JSON-safe copy of a value from a query row or parameters."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def plan_to_dict(plan: Plan | None) -> dict | None:
    if plan is None:
        return None
    return {"intent": plan.intent.value, "filters": _plain(asdict(plan.filters)), "topic": plan.topic,
            "group_by_item": plan.group_by_item, "group_by": list(plan.group_by),
            "exclude_unchanged": plan.exclude_unchanged}


def plan_from_dict(data: dict | None) -> Plan | None:
    if not data:
        return None
    try:
        known = {f.name for f in fields(QueryFilters)}
        filters = QueryFilters(**{k: v for k, v in (data.get("filters") or {}).items() if k in known})
        return Plan(intent=Intent(data["intent"]), filters=filters, topic=data.get("topic"),
                    group_by_item=bool(data.get("group_by_item")), group_by=list(data.get("group_by") or []),
                    exclude_unchanged=bool(data.get("exclude_unchanged")))
    except (KeyError, ValueError, TypeError):
        return None   # saved by an older version: the follow-up simply starts without context


def reply_to_payload(reply, plan: Plan | None = None) -> dict:
    """Everything the chat page shows for one answer, as JSON."""
    results = []
    for r in reply.results:
        params = r.params
        if isinstance(params, dict):
            params = {k: v for k, v in params.items() if k != "qvec"}   # embedding vectors are large and not shown
        results.append({"name": r.name, "sql": r.sql, "params": _plain(params), "columns": list(r.columns),
                        "rows": _plain(r.rows[:MAX_ROWS_PER_RESULT]), "total_rows": len(r.rows),
                        "elapsed_ms": r.elapsed_ms})
    return {"answer": reply.answer, "intent": reply.intent, "filters": _plain(reply.filters), "mode": reply.mode,
            "retrieval_method": reply.retrieval_method, "citations": list(reply.citations),
            "warnings": list(reply.warnings), "notes": list(reply.notes),
            "grounding": asdict(reply.grounding) if reply.grounding is not None else None,
            "elapsed_ms": reply.elapsed_ms, "results": results, "plan": plan_to_dict(plan)}


def reply_from_payload(question: str, payload: dict):
    from .engine import ChatResponse
    grounding = payload.get("grounding")
    return ChatResponse(
        question=question, answer=payload.get("answer") or "", intent=payload.get("intent") or "",
        filters=payload.get("filters") or {}, mode=payload.get("mode") or "offline",
        results=[QueryResult(name=r.get("name", ""), sql=r.get("sql", ""), params=r.get("params"),
                             columns=r.get("columns") or [], rows=r.get("rows") or [], elapsed_ms=r.get("elapsed_ms"))
                 for r in payload.get("results") or []],
        retrieval_method=payload.get("retrieval_method"), citations=payload.get("citations") or [],
        warnings=payload.get("warnings") or [], notes=payload.get("notes") or [],
        grounding=GroundingReport(**{k: v for k, v in grounding.items() if k in {f.name for f in fields(GroundingReport)}})
        if grounding else None,
        elapsed_ms=payload.get("elapsed_ms"))


def title_for(question: str) -> str:
    text = " ".join(question.split())
    return text if len(text) <= TITLE_LENGTH else text[:TITLE_LENGTH - 1].rstrip() + "…"


class ChatHistory:
    """Conversations stored in PostgreSQL. Writes use a short write transaction; reads use the read-only path."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ writing

    def save_turn(self, conversation_id: str | None, question: str, *, reply=None, plan: Plan | None = None,
                  error: str | None = None, source: str = "app") -> str:
        """Append one question (with its reply, or the error it ended in). Returns the conversation's id,
        creating the conversation on its first question."""
        payload = reply_to_payload(reply, plan) if reply is not None else {}
        result: dict = {}

        def write(cur):
            cid = conversation_id
            if cid is not None:
                cur.execute("SELECT 1 FROM chatbot_conversations WHERE conversation_id = %s", (cid,))
                if cur.fetchone() is None:
                    cid = None   # deleted meanwhile: start a new one
            if cid is None:
                cur.execute("INSERT INTO chatbot_conversations (title, source) VALUES (%s, %s) "
                            "RETURNING conversation_id::text", (title_for(question), source))
                cid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO chatbot_turns (conversation_id, position, question, answer, error, intent, payload)
                SELECT %s, coalesce(max(position), 0) + 1, %s, %s, %s, %s, %s::jsonb
                FROM chatbot_turns WHERE conversation_id = %s""",
                        (cid, question, reply.answer if reply is not None else None, error,
                         reply.intent if reply is not None else None, json.dumps(payload, ensure_ascii=False), cid))
            cur.execute("UPDATE chatbot_conversations SET updated_at = now() WHERE conversation_id = %s", (cid,))
            result["id"] = cid

        self.db._write(write, "Could not save this question to the chat history.")
        return result["id"]

    def delete(self, conversation_id: str) -> None:
        self.db._write(lambda cur: cur.execute("DELETE FROM chatbot_conversations WHERE conversation_id = %s",
                                               (conversation_id,)), "Could not delete the conversation.")

    # ------------------------------------------------------------------ reading

    def conversations(self, limit: int = 50, source: str | None = None) -> list[dict]:
        """Newest first: conversation_id, title, source, updated_at, questions."""
        return self.db.query("chat_conversations", """
            SELECT c.conversation_id::text AS conversation_id, c.title, c.source, c.created_at, c.updated_at,
                   (SELECT count(*) FROM chatbot_turns t WHERE t.conversation_id = c.conversation_id) AS questions
            FROM chatbot_conversations c
            WHERE (%(source)s::text IS NULL OR c.source = %(source)s)
            ORDER BY c.updated_at DESC LIMIT %(limit)s""", {"source": source, "limit": limit}).rows

    def latest(self, source: str | None = None) -> str | None:
        rows = self.conversations(limit=1, source=source)
        return rows[0]["conversation_id"] if rows else None

    def turns(self, conversation_id: str) -> list:
        """The conversation's turns in order: ChatResponse objects, or {"question", "error"} for failed ones."""
        rows = self.db.query("chat_turns", """
            SELECT question, answer, error, payload FROM chatbot_turns
            WHERE conversation_id = %s ORDER BY position""", (conversation_id,)).rows
        turns = []
        for row in rows:
            if row["answer"] is None:
                turns.append({"question": row["question"], "error": row["error"] or "This question was not answered."})
            else:
                turns.append(reply_from_payload(row["question"], row["payload"] or {}))
        return turns

    def last_plan(self, conversation_id: str) -> Plan | None:
        """The plan of the last answered question, to continue follow-ups after a restart."""
        rows = self.db.query("chat_last_plan", """
            SELECT payload->'plan' AS plan FROM chatbot_turns
            WHERE conversation_id = %s AND answer IS NOT NULL ORDER BY position DESC LIMIT 1""",
                             (conversation_id,)).rows
        return plan_from_dict(rows[0]["plan"]) if rows else None


__all__ = ["ChatHistory", "DatabaseError", "plan_from_dict", "plan_to_dict", "reply_from_payload", "reply_to_payload"]
