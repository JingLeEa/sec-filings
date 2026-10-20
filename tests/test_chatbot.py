"""Unit tests for the chatbot that need no database or model: routing, filters, SQL safety, grounding, config."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sec_disclosure.chatbot.engine import _parse_json, strip_reasoning
from sec_disclosure.chatbot.entities import normalise_match_id, parse_filters, resolve_years
from sec_disclosure.chatbot.grounding import check_answer
from sec_disclosure.chatbot.planner import Intent, inherit_context, plan_question
from sec_disclosure.chatbot.queries import build_queries, where_clause
from sec_disclosure.chatbot.retrieval import expand_terms, reciprocal_rank_fusion, tsquery_text
from sec_disclosure.chatbot.sql_guard import UnsafeSQL, validate_sql

RUNS = [("bac", 2023, 2024), ("bac", 2024, 2025), ("intc", 2023, 2024), ("intc", 2024, 2025),
        ("jpm", 2023, 2024), ("jpm", 2024, 2025)]

# (question, intent, expected filter subset, topic)
EXAMPLES = [
    ("What are the most material changes in Bank of America's 2024 10-K?", Intent.MATERIAL,
     {"companies": ["bac"], "current_year": 2024}, None),
    ("How many new risk factors did Intel add in 2025?", Intent.COUNT,
     {"companies": ["intc"], "current_year": 2025, "items": ["1A"], "change_types": ["New"]}, None),
    ("Show me disclosures that were removed from JPMorgan's Item 1A between 2023 and 2024", Intent.LIST,
     {"companies": ["jpm"], "previous_year": 2023, "current_year": 2024, "items": ["1A"], "change_types": ["Removed"]}, None),
    ("What changed about climate risk across all three companies?", Intent.TOPIC, {}, "climate"),
    ("Which disclosures have low confidence scores in their materiality assessment?", Intent.LOW_CONFIDENCE, {}, None),
    ("Compare the change patterns between BAC and JPM for 2024-2025", Intent.COMPARE,
     {"companies": ["bac", "jpm"], "previous_year": 2024, "current_year": 2025}, None),
    ("What are the most common types of changes in Item 7?", Intent.DISTRIBUTION, {"items": ["7"]}, None),
    ("Show me the semantic vs lexical disagreements", Intent.DISAGREEMENTS, {}, None),
    ("Which disclosures were expanded and why?", Intent.LIST, {"change_types": ["Expanded"]}, None),
    ("Summarise the key changes for BAC 2023 to 2024", Intent.SUMMARY,
     {"companies": ["bac"], "previous_year": 2023, "current_year": 2024}, None),
]


class RoutingTests(unittest.TestCase):
    def test_example_questions_route_with_expected_filters(self):
        for question, intent, expected, topic in EXAMPLES:
            with self.subTest(question=question):
                plan = plan_question(question, RUNS)
                self.assertEqual(plan.intent, intent)
                filters = plan.filters.as_dict()
                for key, value in expected.items():
                    self.assertEqual(filters.get(key), value, key)
                if topic:
                    self.assertEqual(plan.topic, topic)

    def test_example_questions_have_no_unexpected_filters(self):
        plan = plan_question("What changed about climate risk across all three companies?", RUNS)
        self.assertEqual(plan.filters.as_dict(), {})

    def test_match_id_routes_to_detail_and_is_normalised(self):
        plan = plan_question("Explain BAC_2023_2024_m4 please", RUNS)
        self.assertEqual(plan.intent, Intent.MATCH_DETAIL)
        self.assertEqual(plan.filters.match_ids, ["bac_2023_2024_M0004"])
        self.assertEqual(normalise_match_id("jpm_2024_2025_M01116"), "jpm_2024_2025_M1116")

    def test_how_did_x_change_is_a_topic_question(self):
        plan = plan_question("How did AI disclosures change at Intel?", RUNS)
        self.assertEqual(plan.intent, Intent.TOPIC)
        self.assertEqual(plan.topic, "ai")
        self.assertTrue(plan.exclude_unchanged)

    def test_unfiltered_open_question_goes_to_llm_sql(self):
        self.assertEqual(plan_question("Which sections had the largest increase in word count?", RUNS).intent,
                         Intent.CUSTOM_SQL)

    def test_ai_verified_status_is_not_the_ai_taxonomy(self):
        filters = parse_filters("list ai verified alignments for jpm", RUNS)
        self.assertEqual(filters.statuses, ["ai_verified"])
        self.assertEqual(filters.disclosure_taxonomies, [])

    def test_human_capital_is_not_financial_capital(self):
        filters = parse_filters("human capital disclosures at intel", RUNS)
        self.assertEqual(filters.disclosure_taxonomies, ["Human Capital & Organization"])

    def test_limits_ignore_item_numbers_years_and_10k(self):
        self.assertEqual(parse_filters("top 5 material changes in Item 7 for 2024", RUNS).limit, 5)
        self.assertIsNone(parse_filters("Item 7 disclosures in the 2024 10-K", RUNS).limit)
        self.assertEqual(parse_filters("show three examples", RUNS).limit, 3)

    def test_thresholds(self):
        filters = parse_filters("changes with materiality above 0.7 and confidence below 80%", RUNS)
        self.assertEqual(filters.min_materiality, 0.7)
        self.assertAlmostEqual(filters.max_confidence, 0.8)

    def test_year_resolution(self):
        bac = [(2023, 2024), (2024, 2025)]
        self.assertEqual(resolve_years("2024-2025", bac), (2024, 2025))
        self.assertEqual(resolve_years("between 2025 and 2024", bac), (2024, 2025))
        self.assertEqual(resolve_years("the 2024 10-K", bac), (None, 2024))
        self.assertEqual(resolve_years("changes since 2024", bac), (2024, None))
        self.assertEqual(resolve_years("BAC 2023", bac), (2023, None))  # no run ends in 2023
        self.assertEqual(resolve_years("no year here", bac), (None, None))

    def test_follow_up_inherits_scope(self):
        first = plan_question("How many new risk factors did Intel add in 2025?", RUNS)
        follow = inherit_context(plan_question("what about JPM?", RUNS), first, "what about JPM?")
        self.assertEqual(follow.intent, Intent.COUNT)
        self.assertEqual(follow.filters.companies, ["jpm"])
        self.assertEqual(follow.filters.current_year, 2025)
        self.assertEqual(follow.filters.items, ["1A"])
        self.assertEqual(follow.filters.change_types, ["New"])

    def test_full_question_does_not_inherit(self):
        first = plan_question("Summarise the key changes for BAC 2023 to 2024", RUNS)
        question = "Which disclosures have low confidence scores in their materiality assessment overall?"
        plan = inherit_context(plan_question(question, RUNS), first, question)
        self.assertEqual(plan.filters.companies, [])


class QueryBuilderTests(unittest.TestCase):
    def test_values_are_bound_never_interpolated(self):
        filters = parse_filters("bac", RUNS)
        filters.companies = ["bac'; DROP TABLE alignments; --"]
        where, params = where_clause(filters)
        self.assertNotIn("DROP", where)
        self.assertEqual(params["companies"], ["bac'; DROP TABLE alignments; --"])

    def test_every_intent_builds_queries_with_named_params_only(self):
        for question, *_ in EXAMPLES:
            plan = plan_question(question, RUNS)
            if plan.intent == Intent.TOPIC:
                continue
            for query in build_queries(plan):
                with self.subTest(question=question, query=query.name):
                    self.assertTrue(query.sql.lstrip().upper().startswith(("SELECT", "WITH")))
                    for name in query.params:
                        self.assertIn(f"%({name})s", query.sql)

    def test_new_and_removed_also_count_pending_review_candidates(self):
        plan = plan_question("How many new risk factors did Intel add in 2025?", RUNS)
        names = [q.name for q in build_queries(plan)]
        self.assertIn("pending_review_candidates", names)


class SqlGuardTests(unittest.TestCase):
    def test_accepts_read_only_queries_and_wraps_a_limit(self):
        sql = validate_sql("SELECT company, count(*) AS \"Total\" FROM alignment_changes WHERE explanation ILIKE '%delete%' "
                           "GROUP BY 1;")
        self.assertTrue(sql.startswith("SELECT * FROM ("))
        self.assertIn("LIMIT 200", sql)
        validate_sql("WITH x AS (SELECT * FROM alignments) SELECT match_id FROM x "
                     "CROSS JOIN LATERAL jsonb_array_elements(x.evidence) e")

    def test_rejects_writes_and_escapes(self):
        bad = [
            "DELETE FROM alignments",
            "SELECT 1; DROP TABLE alignments",
            "SELECT * FROM alignments; ",  # fine on its own...
            "SELECT pg_sleep(10)",
            "SELECT * FROM pg_catalog.pg_user",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM users",
            "SELECT * INTO copy_table FROM alignments",
            "WITH d AS (DELETE FROM alignments RETURNING *) SELECT * FROM d",
            "SELECT set_config('statement_timeout', '0', false)",
            "SELECT * FROM \"alignments\"",
            "SELECT 1 /* ; */ ; SELECT 2",
            "SELECT * FROM alignments, secrets",
        ]
        for sql in bad:
            with self.subTest(sql=sql):
                if sql == "SELECT * FROM alignments; ":
                    validate_sql(sql)
                    continue
                with self.assertRaises(UnsafeSQL):
                    validate_sql(sql)


class GroundingTests(unittest.TestCase):
    ROWS = [{"match_id": "bac_2023_2024_M0107", "previous_ids": ["bac_2023_1A_D020"],
             "current_excerpt": "U.S. federal banking agencies may require increased capital and liquidity levels, "
                                "which could adversely impact the Corporation."}]

    def test_unknown_ids_are_removed_and_known_kept(self):
        answer = ("Capital rules tightened [bac_2023_2024_M0107] and a made-up change [bac_2023_2024_M9999] "
                  "based on bac_2023_1A_D020.")
        cleaned, report = check_answer(answer, self.ROWS, lambda ids: set())
        self.assertIn("bac_2023_2024_M0107", cleaned)
        self.assertNotIn("M9999", cleaned)
        self.assertEqual(report.removed_ids, ["bac_2023_2024_M9999"])
        self.assertFalse(report.grounded)

    def test_ids_that_exist_in_database_are_kept_with_a_warning(self):
        cleaned, report = check_answer("See [jpm_2023_2024_M0005].", self.ROWS, lambda ids: set(ids))
        self.assertIn("jpm_2023_2024_M0005", cleaned)
        self.assertEqual(report.removed_ids, [])
        self.assertTrue(any("not in the retrieved evidence" in w for w in report.warnings()))

    def test_quotes_must_appear_in_evidence(self):
        good = 'The filing says "U.S. federal banking agencies may require increased capital … could adversely impact".'
        _, report = check_answer(good, self.ROWS)
        self.assertEqual(report.checked_quotes, 1)
        self.assertEqual(report.unverified_quotes, [])
        bad = 'The filing says "the Corporation will exit all commercial real estate lending by 2026 entirely".'
        _, report = check_answer(bad, self.ROWS)
        self.assertEqual(len(report.unverified_quotes), 1)


class ReasoningModelTests(unittest.TestCase):
    def test_think_blocks_are_removed(self):
        self.assertEqual(strip_reasoning("<think>count rows {x}</think>\n\n**3** changes"), "**3** changes")
        self.assertEqual(strip_reasoning("<think>still thinking when tokens ran out"), "")
        self.assertEqual(strip_reasoning("plain answer"), "plain answer")

    def test_sql_json_found_after_reasoning_and_code_fences(self):
        reply = ('<think>Maybe {"draft": 1}? Use alignment_changes.</think>\nHere you go:\n```json\n'
                 '{"sql": "SELECT match_id FROM alignment_changes LIMIT 5", "reason": "r"}\n```')
        self.assertEqual(_parse_json(reply)["sql"], "SELECT match_id FROM alignment_changes LIMIT 5")
        with self.assertRaises(ValueError):
            _parse_json("<think>{\"sql\": \"x\"}</think> sorry, no query")


class RetrievalHelperTests(unittest.TestCase):
    def test_terms_expand_and_tsquery_is_sanitised(self):
        terms = expand_terms("climate")
        self.assertIn("emissions", terms)
        self.assertEqual(tsquery_text(["transition risk", "x'); drop table t; --"]),
                         "(transition <-> risk) | (x <-> drop <-> table <-> t)")

    def test_rrf_rewards_agreement(self):
        fused = reciprocal_rank_fusion([[(1, "a", 0.9), (1, "b", 0.8)], [(1, "b", 5.0), (1, "c", 4.0)]])
        self.assertEqual(fused[0][1], "b")


class ConfigTests(unittest.TestCase):
    """SoCLaaS is the only LLM service; precedence is environment > repository .env > ~/.config file."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.folder = Path(tempfile.mkdtemp())
        self.repo_env = self.folder / ".env"
        self.home_env = self.folder / "soclaas.env"
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("SOCLAAS_", "LLM_"))}
        patcher = patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, **values):
        path.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()), encoding="utf-8")

    def load(self):
        from sec_disclosure.chatbot.config import load_soclaas
        return load_soclaas(self.repo_env, home_file=self.home_env)

    def test_repo_env_and_key_never_shown(self):
        self.write(self.repo_env, SOCLAAS_BASE_URL="https://soclaas.example/v1", SOCLAAS_API_KEY="sk-secret-123",
                   SOCLAAS_MODEL="default")
        settings = self.load()
        self.assertEqual(settings.config.model, "default")
        self.assertNotIn("sk-secret-123", repr(settings.config))
        self.assertNotIn("sk-secret-123", settings.description)
        self.assertIn(str(self.repo_env), settings.description)

    def test_repo_env_beats_home_file_and_export_beats_both(self):
        self.write(self.home_env, SOCLAAS_BASE_URL="https://soclaas.example/v1", SOCLAAS_API_KEY="old-home-key",
                   SOCLAAS_MODEL="default")
        self.write(self.repo_env, SOCLAAS_API_KEY="new-repo-key")
        settings = self.load()
        self.assertEqual(settings.config.api_key, "new-repo-key")
        self.assertEqual(settings.key_source, str(self.repo_env))
        with patch.dict(os.environ, {"SOCLAAS_API_KEY": "exported-key"}):
            settings = self.load()
        self.assertEqual(settings.config.api_key, "exported-key")
        self.assertEqual(settings.key_source, "environment variable")

    def test_chatbot_model_overrides_pipeline_model(self):
        self.write(self.repo_env, SOCLAAS_BASE_URL="https://soclaas.example/v1", SOCLAAS_API_KEY="k",
                   SOCLAAS_MODEL="default", SOCLAAS_CHATBOT_MODEL="qwen3.6:35b")
        self.assertEqual(self.load().config.model, "qwen3.6:35b")

    def test_llm_star_settings_are_ignored(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://other.example/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}):
            settings = self.load()
        self.assertIsNone(settings.config)
        self.assertIn("SOCLAAS_BASE_URL", settings.description)

    def test_incomplete_or_insecure_settings_are_offline(self):
        self.write(self.repo_env, SOCLAAS_BASE_URL="http://soclaas.example/v1", SOCLAAS_API_KEY="k", SOCLAAS_MODEL="m")
        self.assertIsNone(self.load().config)
        self.write(self.repo_env, SOCLAAS_BASE_URL="https://soclaas.example/v1", SOCLAAS_MODEL="m")
        settings = self.load()
        self.assertIsNone(settings.config)
        self.assertIn("SOCLAAS_API_KEY", settings.description)

if __name__ == "__main__":
    unittest.main()
