"""Integration tests: synthesis-report loader, summary-first and financial-trend answers in the chatbot.

Uses tests/fixtures/synthesis/AMD_2023_2025_synthesis_output.json (real agent output; see its README). The
report is loaded for the tests and removed afterwards unless it was already loaded. Skipped when the database
is unreachable, when AMD's alignments are not loaded, or when a different AMD 2023-2025 report is loaded (so a
real report is never replaced). Expected values come from the fixture JSON. No model is called.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "synthesis" / "AMD_2023_2025_synthesis_output.json"
NEEDED_ALIGNMENTS = [("amd", 2023, 2024), ("amd", 2024, 2025)]
PRELOADED = False


def _setup():
    global PRELOADED
    try:
        from sec_disclosure.chatbot.db import Database
        db = Database()
        loaded = {(r.company, r.previous_year, r.current_year) for r in db.runs()}
        db.ensure_schema()
    except Exception as error:  # noqa: BLE001
        return None, f"database unavailable ({type(error).__name__})"
    missing = [r for r in NEEDED_ALIGNMENTS if r not in loaded]
    if missing:
        return None, f"alignment runs not loaded: {missing}"
    existing = db.query("t", "SELECT source_sha256 FROM synthesis_reports WHERE company = 'amd' "
                             "AND start_year = 2023 AND end_year = 2025").rows
    if existing and existing[0]["source_sha256"] != hashlib.sha256(FIXTURE.read_bytes()).hexdigest():
        return None, "a different AMD 2023-2025 synthesis report is loaded; not replacing it with the fixture"
    PRELOADED = bool(existing)
    return db, ""


DB, SKIP_REASON = _setup()


@unittest.skipIf(DB is None, SKIP_REASON)
class SynthesisChatbotIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sec_disclosure.chatbot import Chatbot
        from sec_disclosure.storage.synthesis_reports import store_synthesis_report
        cls.report = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.result = store_synthesis_report(FIXTURE)
        cls.bot = Chatbot(db=DB, use_llm=False, use_embeddings=False)
        cls.finding_ids = {i for f in cls.report["report"]["key_findings"] for i in f["evidence_ids"]}

    @classmethod
    def tearDownClass(cls):
        if PRELOADED:
            return
        import psycopg
        from sec_disclosure.storage.disclosures import connection_arguments
        with psycopg.connect(**connection_arguments()) as conn:
            conn.execute("DELETE FROM synthesis_reports WHERE report_id = %s", (cls.result.report_id,))

    def setUp(self):
        self.bot.reset()

    def trend(self, kind, metric, **where):
        return [t for t in self.report["financial_trends"] if t["type"] == kind
                and (t.get("label") or t.get("name")) == metric and all(t.get(k) == v for k, v in where.items())]

    # ------------------------------------------------------------------ loader

    def test_loader_counts_and_reload(self):
        from sec_disclosure.storage.synthesis_reports import store_synthesis_report
        self.assertEqual(self.result.findings, len(self.report["report"]["key_findings"]))
        self.assertEqual(self.result.trends, len(self.report["financial_trends"]))
        type(self).result = store_synthesis_report(FIXTURE)
        rows = DB.query("t", "SELECT count(*) AS n FROM synthesis_reports WHERE company = 'amd'").rows
        self.assertEqual(rows[0]["n"], 1)

    # ------------------------------------------------------------------ summary-first

    def test_summary_answers_from_the_report_and_cites_its_findings(self):
        reply = self.bot.ask("Summarise the key changes for AMD from 2023 to 2025")
        self.assertEqual(reply.intent, "summary")
        self.assertEqual(reply.retrieval_method, "synthesis_report")
        self.assertIn(self.report["report"]["executive_summary"][:80], reply.answer)
        cited = set(re.findall(r"\bamd_\d{4}_\d{4}_M\d{4}\b", reply.answer))
        self.assertTrue(cited)
        self.assertLessEqual(cited, self.finding_ids)
        evidence = next(r for r in reply.results if r.name == "synthesis_finding_evidence").rows
        self.assertEqual({r["match_id"] for r in evidence} & cited, cited)   # every cited ID resolved to an alignment

    def test_summary_without_years_uses_the_report_too(self):
        self.assertEqual(self.bot.ask("Summarise the key changes for AMD").retrieval_method, "synthesis_report")

    def test_topic_narrows_the_findings(self):
        reply = self.bot.ask("Summarise AMD changes about data center")
        findings = next(r for r in reply.results if r.name == "synthesis_findings").rows
        self.assertTrue(findings)
        self.assertTrue(all("data center" in (f["title"] + f["analysis"]).lower() for f in findings))
        self.assertLess(len(findings), len(self.report["report"]["key_findings"]))

    def test_company_without_report_falls_back_to_alignment_summary(self):
        reply = self.bot.ask("Summarise the key changes for BAC 2023 to 2024")
        self.assertEqual(reply.retrieval_method, "alignment summary (no synthesis report)")
        self.assertTrue(any("no synthesis report loaded for BAC" in n for n in reply.notes))
        self.assertTrue(next(r for r in reply.results if r.name == "change_type_by_run").rows)

    def test_mixed_companies_use_report_and_fallback(self):
        reply = self.bot.ask("Summarise the key changes for AMD and BAC")
        self.assertEqual(reply.retrieval_method, "synthesis_report + fallback: alignment summary")
        fallback = next(r for r in reply.results if r.name == "fallback_change_type_by_run").rows
        self.assertEqual({r["company"] for r in fallback}, {"bac"})

    def test_years_outside_the_report_fall_back(self):
        from sec_disclosure.chatbot.entities import parse_filters
        lookup = self.bot.reports.lookup(parse_filters("AMD 2021 to 2022"))
        self.assertFalse(lookup.found)

    def test_stale_report_is_flagged(self):
        import psycopg
        from sec_disclosure.storage.disclosures import connection_arguments
        with psycopg.connect(**connection_arguments()) as conn:
            original = conn.execute("SELECT loaded_at FROM synthesis_reports WHERE report_id = %s",
                                    (self.result.report_id,)).fetchone()[0]
            conn.execute("UPDATE synthesis_reports SET loaded_at = '2000-01-01' WHERE report_id = %s",
                         (self.result.report_id,))
        try:
            reply = self.bot.ask("Summarise the key changes for AMD")
            self.assertTrue(any("may be out of date" in n for n in reply.notes))
        finally:
            with psycopg.connect(**connection_arguments()) as conn:
                conn.execute("UPDATE synthesis_reports SET loaded_at = %s WHERE report_id = %s",
                             (original, self.result.report_id))

    # ------------------------------------------------------------------ financial trends

    def test_gross_margin_matches_the_computed_trends(self):
        reply = self.bot.ask("How did AMD gross margin change from 2023 to 2025?")
        self.assertEqual(reply.intent, "financial_trends")
        rows = next(r for r in reply.results if r.name == "synthesis_trends").rows
        self.assertEqual({r["metric"] for r in rows}, {"Gross margin"})
        for row in rows:
            expected = self.trend("calculated_metric", "Gross margin", year=row["year"])[0]["value"]
            self.assertAlmostEqual(float(row["value"]), float(expected), places=4)
            self.assertIn(f"{float(expected):.1f}%", reply.answer)

    def test_revenue_growth_matches_the_computed_trends(self):
        reply = self.bot.ask("What was AMD revenue growth?")
        rows = next(r for r in reply.results if r.name == "synthesis_trends").rows
        self.assertEqual({r["metric"] for r in rows}, {"Revenue"})
        for row in rows:
            expected = self.trend("year_over_year_change", "Revenue", from_year=row["from_year"])[0]["growth_pct"]
            self.assertIn(f"{float(expected):+.1f}%", reply.answer)

    def test_financial_question_without_report_says_so_and_shows_disclosures(self):
        reply = self.bot.ask("How did Intel revenue change?")
        self.assertEqual(reply.intent, "financial_trends")
        self.assertIn("No computed financial figures", reply.answer)
        self.assertTrue(reply.retrieval_method.startswith("fallback"))

    # ------------------------------------------------------------------ LLM grounding

    def test_llm_summary_keeps_real_ids_and_removes_invented_ones(self):
        from sec_disclosure.chatbot import Chatbot

        def completer(system, prompt, json_mode, max_tokens):
            real = re.search(r"amd_\d{4}_\d{4}_M\d{4}", prompt).group(0)
            return f"AMD pivoted to data center AI [{real}] and opened a new fab [amd_2024_2025_M9999]."

        bot = Chatbot(db=DB, completer=completer, use_embeddings=False)
        reply = bot.ask("Summarise the key changes for AMD from 2023 to 2025")
        self.assertEqual(reply.mode, "llm")
        self.assertNotIn("M9999", reply.answer)
        self.assertEqual(reply.grounding.removed_ids, ["amd_2024_2025_M9999"])


if __name__ == "__main__":
    unittest.main()
