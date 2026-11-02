"""Integration tests: peer-comparison loader and peer questions in the chatbot, against PostgreSQL.

Uses the real pilot run on the test-data branch (test_data/peer_comparison/nvda/2024_amd_material_pilot: NVDA
vs AMD, fiscal 2024, NVDA disclosures marked material, 105 of 105 compared). The loader saves it to
peer_comparison_reports (the pipeline's table) and syncs the chatbot's copy. A database that already holds it is
left as it was; otherwise it is removed afterwards. Other peer runs (e.g. the 2024 industry runs) may be loaded
too, so answers are checked against every loaded run. Skipped when the database is unreachable, when NVDA/AMD
2023-2024 alignments are not loaded, or when a different NVDA 2024 vs AMD report is saved (never replaced).
No model is called.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "test_data" / "peer_comparison" / "nvda" / "2024_amd_material_pilot" / "peer_comparison.json"
NEEDED_ALIGNMENTS = [("nvda", 2023, 2024), ("amd", 2023, 2024)]
PILOT_KEY = "target_ticker = 'NVDA' AND fiscal_year = 2024 AND peer_tickers = ARRAY['AMD']"
DISCLOSURE_ID = re.compile(r"\b[a-z]+_2024_[0-9A-Z]{1,2}_D\d{3}\b")


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
    existing = db.query("t", f"SELECT report FROM peer_comparison_reports WHERE {PILOT_KEY}").rows
    if existing and existing[0]["report"] != json.loads(FIXTURE.read_text(encoding="utf-8")):
        return None, "a different NVDA 2024 vs AMD peer report is saved; not replacing it with the pilot"
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
            conn.execute(f"DELETE FROM peer_comparison_reports WHERE {PILOT_KEY}")
            conn.execute("DELETE FROM peer_comparison_runs WHERE peer_run_id = %s", (cls.result.peer_run_id,))

    def loaded_ids(self) -> set[str]:
        rows = DB.query("t", "SELECT target_disclosure_id AS a, peer_disclosure_id AS b FROM peer_comparisons "
                             "UNION SELECT target_disclosure_id, NULL FROM peer_comparison_targets").rows
        return {r["a"] for r in rows} | {r["b"] for r in rows if r["b"]}

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
        rows = DB.query("t", "SELECT count(*) AS n FROM peer_comparison_runs WHERE target = 'nvda' AND fiscal_year = 2024 "
                             "AND peers = ARRAY['amd']").rows
        self.assertEqual(rows[0]["n"], 1)
        saved = DB.query("t", f"SELECT count(*) AS n FROM peer_comparison_reports WHERE {PILOT_KEY}").rows
        self.assertEqual(saved[0]["n"], 1)

    def test_report_saved_by_the_pipeline_is_synced(self):
        """A report the pipeline saves (main's storage.peer_comparison) reaches the chatbot's tables on start-up."""
        from sec_disclosure.chatbot.db import Database
        from sec_disclosure.storage.peer_comparison import store_peer_report
        store_peer_report(self.report)
        db = Database()
        db.ensure_schema()
        self.assertTrue(db.peer_sync and any(s.startswith("NVDA 2024 vs AMD") for s in db.peer_sync.synced))
        run = DB.query("t", "SELECT peer_run_id FROM peer_comparison_runs WHERE target = 'nvda' AND fiscal_year = 2024 "
                            "AND peers = ARRAY['amd']").rows
        self.assertEqual(len(run), 1)
        type(self).result = type(self.result)(**{**self.result.__dict__, "peer_run_id": run[0]["peer_run_id"]})
        db.ensure_schema()
        self.assertIsNone(db.peer_sync)   # nothing new to sync

    # ------------------------------------------------------------------ peer-first answers

    def test_peer_question_answers_from_peer_output_with_real_quotes(self):
        reply = self.bot.ask("Compare NVIDIA and AMD on export controls in 2024")
        self.assertEqual(reply.intent, "peer_comparison")
        self.assertEqual(reply.retrieval_method, "peer_agent")
        rows = next(r for r in reply.results if r.name == "peer_comparisons").rows
        self.assertTrue(rows)
        evidence = {r["sentence_id"]: r["text"] for r in DB.query(
            "t", "SELECT sentence_id, text FROM peer_comparison_evidence WHERE sentence_id = ANY(%s)",
            ([sid for row in rows for sid in (row["sentence_ids_a"] or []) + (row["sentence_ids_b"] or [])],)).rows}
        self.assertEqual(len({(r["disclosure_a"], r["disclosure_b"]) for r in rows}), len(rows))   # no duplicates
        for row in rows:
            self.assertEqual({row["company_a"], row["company_b"]}, {"nvda", "amd"})
            for sid in (row["sentence_ids_a"] or []) + (row["sentence_ids_b"] or []):
                self.assertIn(evidence[sid], row["quote_a"] + " " + row["quote_b"])
        self.assertIn("marked material", reply.answer)  # the runs compared only the target's material disclosures
        self.assertTrue(any("export" in (r["comparison"] + r["quote_a"]).lower() for r in rows))
        # Every disclosure ID in the answer must come from a loaded run (pairs, no-match rows or the agent's text).
        cited = set(DISCLOSURE_ID.findall(reply.answer))
        self.assertTrue(cited)
        agent_text = " ".join(r["comparison"] + " " + (r["summary_a"] or "") + " " + (r["summary_b"] or "") for r in rows)
        self.assertLessEqual(cited, self.loaded_ids() | set(DISCLOSURE_ID.findall(agent_text)))

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
        self.assertEqual(expected, 8)   # in the pilot alone
        rows = next((r for r in lookup.results if r.name == "peer_no_match"), None)
        self.assertTrue(rows and rows.rows)
        self.assertTrue(all(r["status"] != "compared" and {r["company_a"], r["company_b"]} == {"nvda", "amd"}
                            for r in rows.rows))

    # ------------------------------------------------------------------ fallback

    def test_pair_without_peer_run_falls_back_to_alignments(self):
        reply = self.bot.ask("Compare NVDA and JPMorgan on cybersecurity")   # peer runs stay within an industry
        self.assertTrue(reply.retrieval_method.startswith("fallback"))
        self.assertTrue(any("no peer-agent run loaded for NVDA–JPM" in n for n in reply.notes))
        fallback = next(r for r in reply.results if r.name == "fallback_topic_matches")
        self.assertTrue(fallback.rows)
        self.assertTrue({r["company"] for r in fallback.rows} <= {"nvda", "jpm"})

    def test_topic_not_in_peer_output_falls_back(self):
        reply = self.bot.ask("Compare NVIDIA and AMD on aquaculture in 2024")
        self.assertTrue(reply.retrieval_method.startswith("fallback"))
        self.assertTrue(any("no precomputed comparison on 'aquaculture'" in n for n in reply.notes))

    def test_pair_without_topic_and_without_run_shows_change_profiles(self):
        reply = self.bot.ask("Compare NVDA and Bank of America disclosures")
        self.assertEqual(reply.retrieval_method, "fallback: alignment change profiles")
        profile = next(r for r in reply.results if r.name == "fallback_change_profile_by_run")
        self.assertTrue({r["company"] for r in profile.rows} <= {"nvda", "bac"})

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
