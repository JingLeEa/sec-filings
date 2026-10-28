"""Integration tests: peer-comparison loader and peer questions in the chatbot, against PostgreSQL.

Uses the real pilot run on the test-data branch (test_data/peer_comparison/nvda/2024_amd_material_pilot: NVDA
vs AMD, fiscal 2024, NVDA disclosures marked material, 105 of 105 compared). It is loaded for the tests and
reloaded unchanged, so a database that already holds it is left as it was. Skipped when the database is
unreachable, when NVDA/AMD 2023-2024 alignments are not loaded, or when a different NVDA 2024 vs AMD run is
loaded (so another real run is never replaced).
Expected values come from the fixture JSON, independently of the chatbot's SQL. No model is called.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "test_data" / "peer_comparison" / "nvda" / "2024_amd_material_pilot" / "peer_comparison.json"
NEEDED_ALIGNMENTS = [("nvda", 2023, 2024), ("amd", 2023, 2024)]


def _setup():
    try:
        from sec_disclosure.chatbot.db import Database
        db = Database()
        loaded = {(r.company, r.previous_year, r.current_year) for r in db.runs()}
        db.ensure_schema()
    except Exception as error:  # noqa: BLE001
        return None, f"database unavailable ({type(error).__name__})"
    if not FIXTURE.is_file():
        return None, "test_data/peer_comparison pilot missing (merge the latest test-data branch)"
    missing = [r for r in NEEDED_ALIGNMENTS if r not in loaded]
    if missing:
        return None, f"alignment runs not loaded: {missing}"
    existing = db.query("t", "SELECT source_sha256 FROM peer_comparison_runs WHERE target = 'nvda' AND fiscal_year = 2024 "
                             "AND peers = ARRAY['amd']").rows
    fixture_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    if existing and existing[0]["source_sha256"] != fixture_sha:
        return None, "a different NVDA 2024 vs AMD peer run is loaded; not replacing it with the pilot"
    global PRELOADED
    PRELOADED = bool(existing)
    return db, ""


PRELOADED = False   # the pilot was already loaded before the tests: leave it loaded afterwards
DB, SKIP_REASON = _setup()


@unittest.skipIf(DB is None, SKIP_REASON)
class PeerChatbotIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sec_disclosure.chatbot import Chatbot
        from sec_disclosure.storage.peer_comparisons import store_peer_comparison
        cls.report = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.result = store_peer_comparison(FIXTURE.parent)
        cls.bot = Chatbot(db=DB, use_llm=False, use_embeddings=False)
        cls.sentences = {s["sentence_id"]: s["text"] for job in cls.report["comparisons"] for res in job["peer_results"]
                         for c in res["comparisons"] for ev in c["evidence"] for s in ev["sentences"]}
        cls.pairs = {(job["target_disclosure_id"], c["peer_disclosure_id"]) for job in cls.report["comparisons"]
                     for res in job["peer_results"] for c in res["comparisons"]}

    @classmethod
    def tearDownClass(cls):
        if PRELOADED:
            return
        import psycopg
        from sec_disclosure.storage.disclosures import connection_arguments
        with psycopg.connect(**connection_arguments()) as conn:
            conn.execute("DELETE FROM peer_comparison_runs WHERE peer_run_id = %s", (cls.result.peer_run_id,))

    def setUp(self):
        self.bot.reset()

    # ------------------------------------------------------------------ loader

    def test_loader_counts_match_fixture(self):
        jobs = self.report["comparisons"]
        compared = [c for job in jobs for res in job["peer_results"] for c in res["comparisons"]]
        self.assertEqual(self.result.targets, len(jobs))
        self.assertEqual(self.result.results, len(jobs))           # one peer (AMD)
        self.assertEqual(self.result.comparisons, len(compared))
        self.assertTrue(self.result.run_complete)
        selection = DB.query("t", "SELECT target_selection FROM peer_comparison_runs WHERE peer_run_id = %s",
                             (self.result.peer_run_id,)).rows[0]["target_selection"]
        self.assertEqual(selection, "material_changes")
        rows = DB.query("t", "SELECT count(*) AS n FROM peer_comparison_pairs WHERE peer_run_id = %s",
                        (self.result.peer_run_id,)).rows
        self.assertEqual(rows[0]["n"], len(compared))

    def test_reloading_replaces_the_run(self):
        from sec_disclosure.storage.peer_comparisons import store_peer_comparison
        again = store_peer_comparison(FIXTURE.parent)
        type(self).result = again
        rows = DB.query("t", "SELECT count(*) AS n FROM peer_comparison_runs WHERE target = 'nvda' AND fiscal_year = 2024").rows
        self.assertEqual(rows[0]["n"], 1)

    # ------------------------------------------------------------------ peer-first answers

    def test_peer_question_answers_from_peer_output_with_real_quotes(self):
        reply = self.bot.ask("Compare NVIDIA and AMD on export controls in 2024")
        self.assertEqual(reply.intent, "peer_comparison")
        self.assertEqual(reply.retrieval_method, "peer_agent")
        rows = next(r for r in reply.results if r.name == "peer_comparisons").rows
        self.assertTrue(rows)
        for row in rows:
            self.assertIn((row["disclosure_a"], row["disclosure_b"]), self.pairs)
            self.assertEqual({row["company_a"], row["company_b"]}, {"nvda", "amd"})
            for sid in (row["sentence_ids_a"] or []) + (row["sentence_ids_b"] or []):
                self.assertIn(self.sentences[sid], row["quote_a"] + " " + row["quote_b"])
        self.assertIn("marked material", reply.answer)  # the pilot compared only NVDA's material disclosures
        self.assertTrue(any("export" in (r["comparison"] + r["quote_a"]).lower() for r in rows))
        # Every disclosure ID in the answer must come from the pilot (pairs, no-match rows or the agent's own text).
        cited = set(re.findall(r"\b[a-z]+_2024_[0-9A-Z]{1,2}_D\d{3}\b", reply.answer))
        self.assertTrue(cited)
        self.assertLessEqual(cited, set(re.findall(r"\b[a-z]+_2024_[0-9A-Z]{1,2}_D\d{3}\b", FIXTURE.read_text("utf-8"))))

    def test_year_over_year_labels_come_from_alignments(self):
        reply = self.bot.ask("NVDA vs AMD risk factors")
        yoy = next(r for r in reply.results if r.name == "peer_year_over_year").rows
        self.assertTrue(yoy)
        check = DB.query("t", "SELECT match_id, change_type FROM alignment_changes WHERE match_id = ANY(%s)",
                         ([r["match_id"] for r in yoy],)).rows
        labels = {r["match_id"]: r["change_type"] for r in check}
        for row in yoy:
            self.assertEqual(row["change_type"], labels[row["match_id"]])

    def test_one_company_with_peers_uses_every_loaded_pair(self):
        reply = self.bot.ask("How does Nvidia compare to its peers on supply chain?")
        self.assertEqual(reply.intent, "peer_comparison")
        rows = next(r for r in reply.results if r.name == "peer_comparisons").rows
        self.assertTrue(all("nvda" in (r["company_a"], r["company_b"]) for r in rows))

    def test_no_match_decisions_are_reported(self):
        from sec_disclosure.chatbot.entities import parse_filters
        lookup = self.bot.peers.lookup(parse_filters("NVDA and AMD 2024"), None)
        expected = sum(1 for job in self.report["comparisons"] for res in job["peer_results"]
                       if res["peer_ticker"] == "AMD" and res["status"] != "compared")
        self.assertEqual(expected, 8)
        rows = next((r for r in lookup.results if r.name == "peer_no_match"), None)
        self.assertEqual(len(rows.rows) if rows else 0, min(expected, 5))

    # ------------------------------------------------------------------ fallback

    def test_pair_without_peer_run_falls_back_to_alignments(self):
        reply = self.bot.ask("Compare NVDA and Broadcom on supply chain")
        self.assertTrue(reply.retrieval_method.startswith("fallback"))
        self.assertTrue(any("no peer-agent run loaded for NVDA–AVGO" in n for n in reply.notes))
        fallback = next(r for r in reply.results if r.name == "fallback_topic_matches")
        self.assertTrue(fallback.rows)
        self.assertTrue({r["company"] for r in fallback.rows} <= {"nvda", "avgo"})

    def test_topic_not_in_peer_output_falls_back(self):
        reply = self.bot.ask("Compare NVIDIA and AMD on goodwill in 2024")
        self.assertTrue(reply.retrieval_method.startswith("fallback"))
        self.assertTrue(any("no precomputed comparison on 'goodwill'" in n for n in reply.notes))

    def test_pair_without_topic_and_without_run_shows_change_profiles(self):
        reply = self.bot.ask("Compare NVDA and Qualcomm disclosures")
        self.assertEqual(reply.retrieval_method, "fallback: alignment change profiles")
        profile = next(r for r in reply.results if r.name == "fallback_change_profile_by_run")
        self.assertTrue({r["company"] for r in profile.rows} <= {"nvda", "qcom"})

    # ------------------------------------------------------------------ LLM grounding on peer answers

    def test_llm_peer_answer_keeps_real_ids_and_removes_invented_sentence_ids(self):
        from sec_disclosure.chatbot import Chatbot

        def completer(system, prompt, json_mode, max_tokens):
            real = re.search(r"nvda_2024_[0-9A-Z]{1,2}_P\d{3}_S\d{3}", prompt).group(0)
            return f"Both discuss export controls ({real}); NVIDIA adds detail (nvda_2024_1A_P777_S777)."

        bot = Chatbot(db=DB, completer=completer, use_embeddings=False)
        reply = bot.ask("Compare NVIDIA and AMD on export controls in 2024")
        self.assertEqual(reply.mode, "llm")
        self.assertNotIn("P777_S777", reply.answer)
        self.assertEqual(reply.grounding.removed_ids, ["nvda_2024_1A_P777_S777"])


if __name__ == "__main__":
    unittest.main()
