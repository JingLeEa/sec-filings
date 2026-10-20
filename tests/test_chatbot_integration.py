"""Integration tests: the chatbot against PostgreSQL with the test-data alignment fixtures loaded.

Skipped unless the database is reachable and all six fixture runs are loaded:

    docker compose up -d
    for c in bac intc jpm; do for p in 2023-2024 2024-2025; do
      python scripts/load_alignments_to_postgres.py --company $c --previous-year ${p%-*} --current-year ${p#*-} \
          --dir test_data/alignments/$c/$p; done; done
    python -m pytest tests/test_chatbot_integration.py

Expected numbers are computed from the fixture JSON files directly, independently of the chatbot's SQL.
No model is called: the LLM paths use a scripted completer and the vector path a deterministic toy embedder.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "test_data" / "alignments"
PERIODS = [(c, p, p + 1) for c in ("bac", "intc", "jpm") for p in (2023, 2024)]


def _database():
    try:
        from sec_disclosure.chatbot.db import Database
        db = Database()
        loaded = {(r.company, r.previous_year, r.current_year) for r in db.runs()}
    except Exception as error:  # noqa: BLE001 - any failure means "no database here"
        return None, f"database unavailable ({type(error).__name__})"
    missing = [p for p in PERIODS if p not in loaded]
    if missing:
        return None, f"fixture runs not loaded: {missing}"
    if not FIXTURES.is_dir():
        return None, "test_data/alignments missing (check out the test-data branch)"
    db.ensure_schema()
    return db, ""


DB, SKIP_REASON = _database()


def fixture_records() -> list[dict]:
    records = []
    for company, previous, current in PERIODS:
        for name in ("alignments", "needs_review"):
            report = json.loads((FIXTURES / company / f"{previous}-{current}" / f"{name}.json").read_text(encoding="utf-8"))
            for record in report["alignments"]:
                members = (record.get("previous_disclosures") or []) + (record.get("current_disclosures") or [])
                items = sorted({str(m.get("item")) for m in members if m.get("item")})
                analysis = record.get("change_analysis") or {}
                materiality = record.get("materiality_analysis") or {}
                records.append({
                    "company": company, "previous_year": previous, "current_year": current,
                    "match_id": record["match_id"], "status": record["status"], "relationship": record["relationship"],
                    "item": items[0] if items else None, "change_type": analysis.get("final_taxonomy"),
                    "semantic": (analysis.get("semantic") or {}).get("label"),
                    "lexical": (analysis.get("lexical") or {}).get("change_taxonomy"),
                    "analysis_status": analysis.get("status"),
                    "material": materiality.get("materiality"), "materiality_score": materiality.get("materiality_score"),
                    "confidence": materiality.get("confidence_score"),
                })
    return records


def toy_embedding(text: str, dim: int = 1024) -> list[float]:
    """Hashed bag of words: texts sharing words get similar vectors. Deterministic, no API."""
    vector = [0.0] * dim
    for word in re.findall(r"[a-z]{3,}", text.lower()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@unittest.skipIf(DB is None, SKIP_REASON)
class ChatbotIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sec_disclosure.chatbot import Chatbot
        cls.records = fixture_records()
        cls.all_ids = {r["match_id"] for r in cls.records}
        cls.bot = Chatbot(db=DB, use_llm=False, use_embeddings=False)

    def setUp(self):
        self.bot.reset()

    def ask(self, question):
        return self.bot.ask(question)

    def cited(self, answer: str) -> set[str]:
        return set(re.findall(r"\b[a-z]+_\d{4}_\d{4}_M\d{4}\b", answer))

    # ------------------------------------------------------------------ data model

    def test_view_has_one_row_per_fixture_alignment(self):
        rows = DB.query("t", "SELECT count(*) AS n, count(DISTINCT (run_id, match_id)) AS d FROM alignment_changes").rows[0]
        self.assertEqual(rows["n"], len(self.records))
        self.assertEqual(rows["d"], len(self.records))

    def test_view_labels_match_fixture_json(self):
        db_counts = Counter((r["company"], r["previous_year"], r["change_type"]) for r in DB.query(
            "t", "SELECT company, previous_year, change_type FROM alignment_changes").rows)
        json_counts = Counter((r["company"], r["previous_year"], r["change_type"]) for r in self.records)
        self.assertEqual(db_counts, json_counts)

    def test_queries_are_read_only(self):
        from sec_disclosure.chatbot.db import DatabaseError
        with self.assertRaises(DatabaseError):
            DB.query("write_attempt", "DELETE FROM alignments WHERE false")

    # ------------------------------------------------------------------ the ten example questions

    def test_every_example_answers_and_cites_only_real_alignments(self):
        from test_chatbot import EXAMPLES
        for question, intent, *_ in EXAMPLES:
            with self.subTest(question=question):
                self.bot.reset()
                reply = self.ask(question)
                self.assertEqual(reply.intent, intent.value)
                self.assertTrue(reply.answer.strip())
                cited = self.cited(reply.answer)
                self.assertTrue(cited, "answer should cite at least one match_id")
                self.assertLessEqual(cited, self.all_ids)
                self.assertLessEqual(cited, set(reply.citations), "cited IDs must come from the query results")

    def test_most_material_bac_2024(self):
        reply = self.ask("What are the most material changes in Bank of America's 2024 10-K?")
        expected = [r for r in self.records if r["company"] == "bac" and r["current_year"] == 2024 and r["material"] == "Yes"]
        counts = {res.name: res for res in reply.results}
        self.assertEqual(sum(r["n"] for r in counts["material_counts_by_run"].rows), len(expected))
        top = counts["most_material_changes"].rows
        scores = [r["materiality_score"] for r in top]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(scores[0], max(r["materiality_score"] for r in expected))
        self.assertIn(f"**{len(expected)}**", reply.answer)

    def test_new_intel_risk_factors_2025(self):
        reply = self.ask("How many new risk factors did Intel add in 2025?")
        expected = [r for r in self.records if r["company"] == "intc" and r["current_year"] == 2025
                    and r["item"] == "1A" and r["change_type"] == "New"]
        pending = [r for r in self.records if r["company"] == "intc" and r["current_year"] == 2025
                   and r["item"] == "1A" and r["status"] == "needs_review" and r["relationship"] == "current_only"]
        self.assertIn(f"**{len(expected)}**", reply.answer)
        self.assertEqual(self.cited(reply.answer), {r["match_id"] for r in expected})
        if pending:
            self.assertIn(f"{len(pending)} further one-sided candidate", reply.answer)

    def test_removed_jpm_item_1a_2023_2024(self):
        reply = self.ask("Show me disclosures that were removed from JPMorgan's Item 1A between 2023 and 2024")
        expected = {r["match_id"] for r in self.records if r["company"] == "jpm" and r["previous_year"] == 2023
                    and r["item"] == "1A" and r["change_type"] == "Removed"}
        self.assertEqual(self.cited(reply.answer), expected)
        for row in reply.results[1].rows:
            self.assertTrue(row["previous_excerpt"], "removed disclosures should quote the previous-year text")

    def test_item_7_distribution(self):
        reply = self.ask("What are the most common types of changes in Item 7?")
        dist = {r["change_type"]: r["n"] for r in reply.results[0].rows}
        expected = Counter(r["change_type"] or "Not analysed (needs review)" for r in self.records if r["item"] == "7")
        self.assertEqual(dist, dict(expected))

    def test_semantic_lexical_disagreements(self):
        reply = self.ask("Show me the semantic vs lexical disagreements")
        expected = [r for r in self.records if r["analysis_status"] == "completed" and r["semantic"] and r["lexical"]
                    and r["semantic"] != r["lexical"]]
        agreement = next(res for res in reply.results if res.name == "agreement_rate").rows[0]
        self.assertEqual(agreement["disagree"], len(expected))

    def test_low_confidence(self):
        reply = self.ask("Which disclosures have low confidence scores in their materiality assessment?")
        expected = [r for r in self.records if r["confidence"] is not None and r["confidence"] <= 0.8]
        by_run = next(res for res in reply.results if res.name == "low_confidence_by_run")
        self.assertEqual(sum(r["n"] for r in by_run.rows), len(expected))
        confidences = [r["confidence_score"] for r in next(res for res in reply.results if res.name == "lowest_confidence").rows]
        self.assertEqual(confidences, sorted(confidences))

    def test_compare_bac_jpm(self):
        reply = self.ask("Compare the change patterns between BAC and JPM for 2024-2025")
        profile = {r["company"]: r for r in reply.results[0].rows}
        self.assertEqual(set(profile), {"bac", "jpm"})
        for company in ("bac", "jpm"):
            rows = [r for r in self.records if r["company"] == company and r["previous_year"] == 2024]
            self.assertEqual(profile[company]["alignments"], len(rows))
            self.assertEqual(profile[company]["new"], sum(r["change_type"] == "New" for r in rows))
            self.assertEqual(profile[company]["needs_review"], sum(r["status"] == "needs_review" for r in rows))

    def test_climate_topic_uses_full_text_and_finds_climate_text(self):
        reply = self.ask("What changed about climate risk across all three companies?")
        self.assertEqual(reply.retrieval_method, "full_text")
        rows = reply.results[-1].rows
        self.assertGreaterEqual(len({r["company"] for r in rows}), 2)
        vocabulary = re.compile(r"climat|environment|sustainab|emission|carbon|net-zero|transition risk|physical risk|weather",
                                re.IGNORECASE)
        for row in rows:
            text = " ".join(str(row.get(k) or "") for k in ("key_change", "current_summary", "previous_summary",
                                                             "explanation", "materiality_reason", "section",
                                                             "previous_excerpt", "current_excerpt"))
            self.assertRegex(text, vocabulary, row["match_id"])
            self.assertNotEqual(row["change_type"], "Unchanged")

    def test_match_detail_and_unknown_match(self):
        reply = self.ask("Tell me about bac_2023_2024_M0004")
        self.assertIn("bac_2023_2024_M0004", reply.answer)
        reply = self.ask("Tell me about bac_2023_2024_M9999")
        self.assertIn("No alignment", reply.answer)

    # ------------------------------------------------------------------ pgvector path

    def test_vector_search_with_evidence_embeddings(self):
        from sec_disclosure.chatbot import Chatbot
        rows = DB.query("t", """SELECT run_id, match_id, coalesce(current_text, previous_text) AS text
                                FROM alignment_changes WHERE company = 'jpm' AND item = '1A'""").rows
        import psycopg
        from sec_disclosure.storage.disclosures import connection_arguments
        from sec_disclosure.chatbot.retrieval import vector_literal
        with psycopg.connect(**connection_arguments()) as conn, conn.cursor() as cur:
            cur.execute("SELECT model_id FROM embedding_models WHERE name = 'bge-m3'")
            model_id = cur.fetchone()[0]
            cur.executemany("""INSERT INTO chatbot_evidence_embeddings (run_id, match_id, side, model_id, text_hash, embedding)
                               VALUES (%s, %s, 'current', %s, 'test', %s::vector) ON CONFLICT DO NOTHING""",
                            [(r["run_id"], r["match_id"], model_id, vector_literal(toy_embedding(r["text"] or "")))
                             for r in rows if r["text"]])
        try:
            bot = Chatbot(db=DB, use_llm=False, embedder=lambda texts: [toy_embedding(t) for t in texts])
            reply = bot.ask("What changed about climate change physical risks for JPMorgan?")
            self.assertIn("pgvector:evidence", reply.retrieval_method)
            self.assertIn("full_text", reply.retrieval_method)
            top = reply.results[-1].rows[:3]
            self.assertTrue(any(re.search("climate", (r["current_excerpt"] or "") + (r["previous_excerpt"] or ""), re.I)
                                for r in top))
        finally:
            with psycopg.connect(**connection_arguments()) as conn:
                conn.execute("DELETE FROM chatbot_evidence_embeddings WHERE text_hash = 'test'")

    # ------------------------------------------------------------------ LLM paths (scripted completer)

    def test_llm_answer_is_grounded_and_hallucinated_ids_removed(self):
        from sec_disclosure.chatbot import Chatbot

        def completer(system, prompt, json_mode, max_tokens):
            real = re.search(r"intc_2024_2025_M\d{4}", prompt).group(0)
            return f"Intel added risk factors [{real}] and also [intc_2024_2025_M8888]."

        bot = Chatbot(db=DB, completer=completer, use_embeddings=False)
        reply = bot.ask("How many new risk factors did Intel add in 2025?")
        self.assertEqual(reply.mode, "llm")
        self.assertNotIn("M8888", reply.answer)
        self.assertEqual(reply.grounding.removed_ids, ["intc_2024_2025_M8888"])
        self.assertTrue(reply.warnings)

    def test_llm_sql_path_rejects_unsafe_sql_then_runs_corrected_query(self):
        from sec_disclosure.chatbot import Chatbot
        calls = []

        def completer(system, prompt, json_mode, max_tokens):
            calls.append(prompt)
            if json_mode and len(calls) == 1:
                return json.dumps({"sql": "DELETE FROM alignments", "reason": "oops"})
            if json_mode:
                return json.dumps({"sql": "SELECT match_id, section FROM alignment_changes WHERE company = 'bac' "
                                          "AND section ILIKE '%deposit%' ORDER BY match_id LIMIT 5",
                                   "reason": "sections mentioning deposits"})
            return "Answer based on " + ", ".join(sorted(set(re.findall(r"bac_\d{4}_\d{4}_M\d{4}", prompt)))[:2])

        bot = Chatbot(db=DB, completer=completer, use_embeddings=False)
        reply = bot.ask("Which sections had the largest increase in word count?")
        self.assertEqual(reply.intent, "custom_sql")
        self.assertTrue(any("rejected" in n for n in reply.notes))
        self.assertEqual(reply.results[0].name, "llm_sql")
        self.assertTrue(reply.results[0].rows)
        self.assertTrue(all("deposit" in r["section"].lower() for r in reply.results[0].rows))
        self.assertEqual(DB.query("t", "SELECT count(*) AS n FROM alignments").rows[0]["n"], len(self.records))

    def test_reasoning_block_is_hidden_from_the_answer(self):
        from sec_disclosure.chatbot import Chatbot

        def completer(system, prompt, json_mode, max_tokens):
            return "<think>Maybe cite jpm_2023_2024_M9999?</think>\nFirst Republic risk was removed [jpm_2023_2024_M0005]."

        bot = Chatbot(db=DB, completer=completer, use_embeddings=False)
        reply = bot.ask("Show me disclosures that were removed from JPMorgan's Item 1A between 2023 and 2024")
        self.assertEqual(reply.mode, "llm")
        self.assertNotIn("think", reply.answer)
        self.assertEqual(reply.grounding.removed_ids, [])

    def test_llm_failure_falls_back_to_template(self):
        from sec_disclosure.chatbot import Chatbot
        from sec_disclosure.llm.client import LLMError

        def completer(*args):
            raise LLMError("SoCLaaS returned HTTP 429.")

        bot = Chatbot(db=DB, completer=completer, use_embeddings=False)
        reply = bot.ask("Summarise the key changes for BAC 2023 to 2024")
        self.assertEqual(reply.mode, "offline")
        self.assertIn("Summary for Bank of America", reply.answer)


if __name__ == "__main__":
    unittest.main()
