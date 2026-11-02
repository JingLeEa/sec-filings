"""Integration tests: synthesis reports in the chatbot (summary-first and financial-trend answers).

The synthesis agent saves each report to synthesis_reports(company, years, report jsonb) (its
db/init/06_synthesis_reports.sql and storage/synthesis.py); the chatbot reads that table through the
chatbot_synthesis_* views in db/init/11_chatbot_synthesis.sql.

Uses the agent's sample output in test_data/synthesis/amd_2023_2025_synthesis_output.json. The report is upserted
for the tests and removed afterwards unless it was already in the table. Skipped when the database is
unreachable, when AMD's alignments are not loaded, or when a different AMD 2023-2025 report is stored (so a
real report is never replaced). Expected values come from the sample JSON. No model is called.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "test_data" / "synthesis" / "amd_2023_2025_synthesis_output.json"
NEEDED_ALIGNMENTS = [("amd", 2023, 2024), ("amd", 2024, 2025)]
KEY = "company = 'AMD' AND years = ARRAY[2023, 2024, 2025]"
PRELOADED = False

# The synthesis agent's table, verbatim from db/init/06_synthesis_reports.sql on the synthesis-agent branch.
AGENT_DDL = """
CREATE TABLE IF NOT EXISTS synthesis_reports (
    company       TEXT NOT NULL CHECK (company = upper(company)),
    years         INTEGER[] NOT NULL CHECK (cardinality(years) >= 2),
    report        JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company, years)
);"""
AGENT_UPSERT = """
INSERT INTO synthesis_reports (company, years, report) VALUES (%s, %s, %s::jsonb)
ON CONFLICT (company, years) DO UPDATE SET report = EXCLUDED.report, generated_at = now()"""


def _connect():
    import psycopg
    from sec_disclosure.storage.disclosures import connection_arguments
    return psycopg.connect(**connection_arguments())


def _setup():
    global PRELOADED
    if not SAMPLE.exists():
        return None, f"sample report missing: {SAMPLE.relative_to(ROOT)}"
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
    existing = db.query("t", f"SELECT report FROM synthesis_reports WHERE {KEY}").rows
    if existing and existing[0]["report"] != json.loads(SAMPLE.read_text(encoding="utf-8")):
        return None, "a different AMD 2023-2025 synthesis report is stored; not replacing it with the sample"
    PRELOADED = bool(existing)
    return db, ""


DB, SKIP_REASON = _setup()


@unittest.skipIf(DB is None, SKIP_REASON)
class SynthesisChatbotIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sec_disclosure.chatbot import Chatbot
        from sec_disclosure.storage.synthesis_reports import store_synthesis_report
        cls.report = json.loads(SAMPLE.read_text(encoding="utf-8"))
        cls.result = store_synthesis_report(SAMPLE)
        cls.bot = Chatbot(db=DB, use_llm=False, use_embeddings=False)
        cls.finding_ids = {i for f in cls.report["report"]["key_findings"] for i in f["evidence_ids"]}

    @classmethod
    def tearDownClass(cls):
        if PRELOADED:
            return
        with _connect() as conn:
            conn.execute(f"DELETE FROM synthesis_reports WHERE {KEY}")

    def setUp(self):
        self.bot.reset()

    def trend(self, kind, metric, **where):
        return [t for t in self.report["financial_trends"] if t["type"] == kind
                and (t.get("label") or t.get("name")) == metric and all(t.get(k) == v for k, v in where.items())]

    # ------------------------------------------------------------------ storage / views

    def test_loader_writes_the_agents_table_and_reload_replaces(self):
        from sec_disclosure.storage.synthesis_reports import store_synthesis_report
        self.assertEqual(self.result.report_key, "amd_2023_2024_2025")
        self.assertEqual(self.result.findings, len(self.report["report"]["key_findings"]))
        type(self).result = store_synthesis_report(SAMPLE)
        self.assertEqual(DB.query("t", f"SELECT count(*) AS n FROM synthesis_reports WHERE {KEY}").rows[0]["n"], 1)

    def test_views_expose_every_finding_and_trend(self):
        q = "SELECT count(*) AS n FROM {} WHERE report_key = 'amd_2023_2024_2025'"
        self.assertEqual(DB.query("t", q.format("chatbot_synthesis_findings")).rows[0]["n"],
                         len(self.report["report"]["key_findings"]))
        self.assertEqual(DB.query("t", q.format("chatbot_synthesis_trends")).rows[0]["n"],
                         len(self.report["financial_trends"]))
        row = DB.query("t", "SELECT * FROM chatbot_synthesis_reports WHERE report_key = 'amd_2023_2024_2025'").rows[0]
        self.assertEqual((row["company"], row["start_year"], row["end_year"]), ("amd", 2023, 2025))
        self.assertEqual(row["executive_summary"], self.report["report"]["executive_summary"])

    def test_agent_ddl_and_upsert_work_after_the_chatbot_schema(self):
        """The agent's own DDL is a no-op on our table, its upsert lands in it, and the views see the update."""
        with _connect() as conn:
            conn.execute(AGENT_DDL)
            conn.execute(AGENT_UPSERT, ("AMD", [2023, 2024, 2025], SAMPLE.read_text(encoding="utf-8")))
            n = conn.execute("SELECT count(*) FROM chatbot_synthesis_reports "
                             "WHERE report_key = 'amd_2023_2024_2025'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_schema_file_is_idempotent(self):
        sql = (ROOT / "db" / "init" / "11_chatbot_synthesis.sql").read_text(encoding="utf-8")
        with _connect() as conn:
            conn.execute(sql)
            n = conn.execute(f"SELECT count(*) FROM synthesis_reports WHERE {KEY}").fetchone()[0]
        self.assertEqual(n, 1)   # rerunning the file keeps the agent's rows

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
        reply = self.bot.ask("Summarise AMD changes about export controls")
        findings = next(r for r in reply.results if r.name.startswith("synthesis_findings")).rows
        self.assertTrue(findings)
        if next(r for r in reply.results if r.name.startswith("synthesis_findings")).name == "synthesis_findings":
            self.assertLess(len(findings), len(self.report["report"]["key_findings"]))

    def uncovered_run(self):
        """A loaded alignment run that no synthesis report covers (reports exist for most companies now)."""
        covered = DB.query("t", "SELECT company, start_year, end_year FROM chatbot_synthesis_reports").rows
        for run in DB.runs():
            if not any(r["company"] == run.company and r["start_year"] <= run.previous_year
                       and r["end_year"] >= run.current_year for r in covered):
                return run
        self.skipTest("every loaded alignment run is covered by a synthesis report")

    def test_company_without_report_falls_back_to_alignment_summary(self):
        run = self.uncovered_run()
        reply = self.bot.ask(f"Summarise the key changes for {run.company.upper()} {run.previous_year} to {run.current_year}")
        self.assertEqual(reply.retrieval_method, "alignment summary (no synthesis report)")
        self.assertTrue(any(f"no synthesis report loaded for {run.company.upper()}" in n for n in reply.notes))
        self.assertTrue(next(r for r in reply.results if r.name == "change_type_by_run").rows)

    def test_mixed_companies_use_report_and_fallback(self):
        run = self.uncovered_run()
        if run.company == "amd" or (run.previous_year, run.current_year) not in ((2023, 2024), (2024, 2025)):
            self.skipTest("needs an uncovered company other than AMD in a window AMD's report covers")
        reply = self.bot.ask(f"Summarise the key changes for AMD and {run.company.upper()} "
                             f"{run.previous_year} to {run.current_year}")
        self.assertEqual(reply.retrieval_method, "synthesis_report + fallback: alignment summary")
        fallback = next(r for r in reply.results if r.name == "fallback_change_type_by_run").rows
        self.assertEqual({r["company"] for r in fallback}, {run.company})

    def test_years_outside_the_report_fall_back(self):
        from sec_disclosure.chatbot.entities import parse_filters
        self.assertFalse(self.bot.reports.lookup(parse_filters("AMD 2021 to 2022")).found)

    def test_stale_report_is_flagged(self):
        with _connect() as conn:
            original = conn.execute(f"SELECT generated_at FROM synthesis_reports WHERE {KEY}").fetchone()[0]
            conn.execute(f"UPDATE synthesis_reports SET generated_at = '2000-01-01' WHERE {KEY}")
        try:
            reply = self.bot.ask("Summarise the key changes for AMD")
            self.assertTrue(any("may be out of date" in n for n in reply.notes))
        finally:
            with _connect() as conn:
                conn.execute(f"UPDATE synthesis_reports SET generated_at = %s WHERE {KEY}", (original,))

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
        self.assertTrue(rows)
        self.assertTrue(all(r["metric"].lower().startswith("revenue") for r in rows))
        for row in rows:
            expected = self.trend("year_over_year_change", row["metric"], from_year=row["from_year"])[0]["growth_pct"]
            self.assertIn(f"{float(expected):+.1f}%", reply.answer)

    def test_metric_words_match_the_agents_xbrl_labels(self):
        for question, label in (("AMD operating cash flow", "Net Cash Provided by (Used in) Operating Activities"),
                                ("AMD capex", "Payments to Acquire Property, Plant, and Equipment"),
                                ("AMD diluted EPS", "Earnings Per Share, Diluted"),
                                ("AMD net income", "Net Income (Loss) Attributable to Parent")):
            with self.subTest(question=question):
                rows = next(r for r in self.bot.ask(question).results if r.name == "synthesis_trends").rows
                self.assertIn(label, {r["metric"] for r in rows})

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
