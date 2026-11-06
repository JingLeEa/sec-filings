"""Saved chatbot conversations (src/sec_disclosure/chatbot/history.py, db/init/12_chatbot_history.sql).

The first tests need no database. The rest run against the local PostgreSQL and are skipped without it; they
create their own conversations and delete them again.
"""
from __future__ import annotations

import json
import unittest
from datetime import date
from decimal import Decimal

from sec_disclosure.chatbot.db import QueryResult
from sec_disclosure.chatbot.engine import ChatResponse
from sec_disclosure.chatbot.entities import QueryFilters
from sec_disclosure.chatbot.grounding import GroundingReport
from sec_disclosure.chatbot.history import (MAX_ROWS_PER_RESULT, plan_from_dict, plan_to_dict, reply_from_payload,
                                            reply_to_payload, title_for)
from sec_disclosure.chatbot.planner import Intent, Plan


def _reply(rows: int = 3) -> ChatResponse:
    result = QueryResult(name="changes", sql="SELECT 1", params={"company": "intc", "qvec": [0.1] * 1024},
                         columns=["match_id", "score", "filed"],
                         rows=[{"match_id": f"intc_2024_2025_M{i:04d}", "score": Decimal("0.75"), "filed": date(2025, 1, 31)}
                               for i in range(rows)], elapsed_ms=4.2)
    return ChatResponse(question="How many new risk factors did Intel add in 2025?", answer="**3** new [intc_2024_2025_M0001]",
                        intent="count", filters={"companies": ["intc"], "current_year": 2025}, mode="offline",
                        results=[result], citations=["intc_2024_2025_M0001"], warnings=["w"], notes=["n"],
                        grounding=GroundingReport(cited_match_ids=["intc_2024_2025_M0001"]), elapsed_ms=12.0)


class SerializationTests(unittest.TestCase):
    def test_reply_round_trips_through_json(self):
        plan = Plan(intent=Intent.COUNT, filters=QueryFilters(companies=["intc"], current_year=2025, items=["1A"]))
        payload = json.loads(json.dumps(reply_to_payload(_reply(), plan)))
        restored = reply_from_payload("How many new risk factors did Intel add in 2025?", payload)
        self.assertEqual(restored.answer, "**3** new [intc_2024_2025_M0001]")
        self.assertEqual(restored.citations, ["intc_2024_2025_M0001"])
        self.assertEqual(restored.results[0].rows[0]["score"], 0.75)
        self.assertEqual(restored.results[0].rows[0]["filed"], "2025-01-31")
        self.assertNotIn("qvec", restored.results[0].params)   # embedding vectors are not stored
        self.assertTrue(restored.grounding.grounded)
        self.assertEqual(len(restored.queries), 1)

    def test_plan_round_trips(self):
        plan = Plan(intent=Intent.COUNT, filters=QueryFilters(companies=["intc"], current_year=2025, items=["1A"]),
                    topic="export controls", group_by=["company"], exclude_unchanged=True)
        restored = plan_from_dict(json.loads(json.dumps(plan_to_dict(plan))))
        self.assertEqual(restored.intent, Intent.COUNT)
        self.assertEqual(restored.filters, plan.filters)
        self.assertEqual((restored.topic, restored.group_by, restored.exclude_unchanged), ("export controls", ["company"], True))

    def test_unknown_or_old_plans_are_ignored(self):
        self.assertIsNone(plan_from_dict(None))
        self.assertIsNone(plan_from_dict({"intent": "no_such_intent", "filters": {}}))
        restored = plan_from_dict({"intent": Intent.COUNT.value, "filters": {"companies": ["amd"], "gone_field": 1}})
        self.assertEqual(restored.filters.companies, ["amd"])

    def test_large_results_are_capped(self):
        payload = reply_to_payload(_reply(rows=MAX_ROWS_PER_RESULT + 50))
        self.assertEqual(len(payload["results"][0]["rows"]), MAX_ROWS_PER_RESULT)
        self.assertEqual(payload["results"][0]["total_rows"], MAX_ROWS_PER_RESULT + 50)

    def test_titles_are_short(self):
        self.assertEqual(title_for("  What   changed? "), "What changed?")
        self.assertLessEqual(len(title_for("x" * 300)), 80)


def _database():
    try:
        from sec_disclosure.chatbot.db import Database
        db = Database()
        db.ensure_schema()
        db.query("history_table", "SELECT 1 FROM chatbot_turns LIMIT 1")
        return db, ""
    except Exception as error:  # noqa: BLE001
        return None, f"database unavailable ({type(error).__name__})"


DB, SKIP_REASON = _database()


@unittest.skipIf(DB is None, SKIP_REASON)
class SavedConversationTests(unittest.TestCase):
    def setUp(self):
        from sec_disclosure.chatbot.history import ChatHistory
        self.store = ChatHistory(DB)
        self.created: list[str] = []

    def tearDown(self):
        for conversation_id in self.created:
            self.store.delete(conversation_id)

    def test_questions_survive_a_new_session(self):
        reply = _reply()
        plan = Plan(intent=Intent.COUNT, filters=QueryFilters(companies=["intc"], current_year=2025, items=["1A"]))
        cid = self.store.save_turn(None, reply.question, reply=reply, plan=plan)
        self.created.append(cid)
        self.store.save_turn(cid, "What about AMD?", error="Sorry, that question could not be answered.")
        self.assertEqual(self.store.save_turn(cid, "And 2024?", reply=reply, plan=plan), cid)

        from sec_disclosure.chatbot.history import ChatHistory
        fresh = ChatHistory(DB)                       # as after an app or CLI restart
        self.assertEqual(fresh.latest(), cid)
        turns = fresh.turns(cid)
        self.assertEqual([t["question"] if isinstance(t, dict) else t.question for t in turns],
                         [reply.question, "What about AMD?", "And 2024?"])
        self.assertIsInstance(turns[1], dict)
        self.assertEqual(turns[0].answer, reply.answer)
        self.assertEqual(fresh.last_plan(cid).filters.companies, ["intc"])
        listed = next(c for c in fresh.conversations() if c["conversation_id"] == cid)
        self.assertEqual((listed["title"], listed["questions"]), (reply.question, 3))

    def test_delete_removes_the_conversation_and_its_questions(self):
        cid = self.store.save_turn(None, "Temporary question", reply=_reply())
        self.store.delete(cid)
        self.assertEqual(self.store.turns(cid), [])
        self.assertNotIn(cid, [c["conversation_id"] for c in self.store.conversations()])

    def test_saving_to_a_deleted_conversation_starts_a_new_one(self):
        cid = self.store.save_turn(None, "First", reply=_reply())
        self.store.delete(cid)
        new_id = self.store.save_turn(cid, "Second", reply=_reply())
        self.created.append(new_id)
        self.assertNotEqual(new_id, cid)

    def test_follow_up_after_restart_keeps_the_scope(self):
        from sec_disclosure.chatbot import Chatbot
        try:
            first = Chatbot(db=DB, use_llm=False, use_embeddings=False, ensure_schema=False)
        except Exception as error:  # noqa: BLE001
            self.skipTest(f"chatbot unavailable ({type(error).__name__})")
        if not {"intc", "amd"} <= {r.company for r in DB.runs()}:
            self.skipTest("Intel and AMD runs not loaded")
        reply = first.ask("How many new risk factors did Intel add in 2025?")
        cid = self.store.save_turn(None, reply.question, reply=reply, plan=first.last_plan)
        self.created.append(cid)

        restarted = Chatbot(db=DB, use_llm=False, use_embeddings=False, ensure_schema=False)
        restarted.last_plan = self.store.last_plan(cid)
        follow_up = restarted.ask("What about AMD?")
        self.assertEqual(follow_up.filters.get("companies"), ["amd"])
        self.assertEqual(follow_up.filters.get("current_year"), 2025)
        self.assertEqual(follow_up.filters.get("items"), ["1A"])


if __name__ == "__main__":
    unittest.main()
