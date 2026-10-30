"""Unit tests for the chatbot that need no database or model: routing, filters, SQL safety, grounding, config."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from sec_disclosure.chatbot.engine import _parse_json, strip_reasoning
from sec_disclosure.chatbot.entities import normalise_match_id, parse_filters, resolve_years
from sec_disclosure.chatbot.grounding import check_answer
from sec_disclosure.chatbot.planner import Intent, inherit_context, plan_question
from sec_disclosure.chatbot.queries import build_queries, where_clause
from sec_disclosure.chatbot.retrieval import expand_terms, reciprocal_rank_fusion, tsquery_text
from sec_disclosure.chatbot.sql_guard import UnsafeSQL, validate_sql

ROOT = Path(__file__).resolve().parents[1]
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
        self.assertEqual(resolve_years("changes since 2024", bac), (2023, 2025))   # the 2024 and 2025 filings
        self.assertEqual(resolve_years("changes since 2023", bac), (2023, 2025))
        self.assertEqual(resolve_years("changes from 2024", bac), (2024, None))
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


class FilterEvalFixTests(unittest.TestCase):
    """Fixes from the 80-question filter evaluation, plus questions whose reading must not change."""

    def plan(self, question):
        return plan_question(question, RUNS)

    def windows(self, question):
        f = self.plan(question).filters
        return f.previous_year, f.current_year

    def test_year_phrasing(self):
        self.assertEqual(self.windows("tl;dr of Micron's latest filing changes"), (None, 2025))
        self.assertEqual(self.windows("BAC's most recent 10-K"), (None, 2025))
        self.assertEqual(self.windows("How has Micron's operating cash flow evolved since 2023?"), (2023, 2025))
        self.assertEqual(self.windows("Micron's material changes, 2024 onward"), (2023, 2025))
        self.assertEqual(self.windows("Compare Intel's 2024 and 2025 filing changes"), (2023, 2025))
        self.assertEqual(self.windows("Was Morgan Stanley's 2025 10-K more heavily revised than its 2024 one?"),
                         (2023, 2025))
        # unchanged readings
        self.assertEqual(self.windows("What changed between JPMorgan's 2024 and 2025 10-Ks?"), (2024, 2025))
        self.assertEqual(self.windows("new disclosures in Intel's 2025 filing versus its 2024 one"), (None, 2025))
        self.assertEqual(self.windows("Summarise BAC from 2023 to 2025"), (2023, 2025))
        self.assertEqual(self.windows("What is AMD's current ratio?"), (None, None))

    def test_ambiguous_year_pair_is_flagged(self):
        plan = self.plan("Compare the 2024 and 2025 10-Ks for BAC")
        self.assertEqual((plan.filters.previous_year, plan.filters.current_year), (2024, 2025))
        self.assertTrue(any("2024 → 2025" in c for c in plan.filters.clarifications))
        self.assertEqual(self.plan("changes between 2024 and 2025 for BAC").filters.clarifications, [])

    def test_lowercase_short_ticker_is_flagged_not_matched(self):
        plan = self.plan("how many changes did ms have in 2024")
        self.assertEqual(plan.filters.companies, [])
        self.assertTrue(any("Morgan Stanley" in c for c in plan.filters.clarifications))
        self.assertEqual(self.plan("query took 5 ms for BAC").filters.clarifications, [])

    def test_revised_is_not_the_modified_label(self):
        self.assertEqual(self.plan("Which BAC disclosures were revised in 2024?").filters.change_types, [])
        self.assertEqual(self.plan("Which BAC disclosures were modified in 2024?").filters.change_types, ["Modified"])

    def test_rewrote_is_flagged_not_filtered(self):
        plan = self.plan("Compare how much AMD and Nvidia rewrote their Item 1A in 2024")
        self.assertEqual(plan.intent, Intent.COMPARE)
        self.assertEqual(plan.filters.change_types, [])
        self.assertTrue(any("reworded" in c for c in plan.filters.clarifications))

    def test_intent_fixes(self):
        cases = {
            "What were the highlights of Intel's 2025 10-K compared to the year before?": Intent.SUMMARY,
            "How did JPMorgan describe its liquidity risk in the 2024 risk factors?": Intent.TOPIC,
            "What changed in Nvidia's export control disclosures in 2024?": Intent.TOPIC,
            "Has AMD updated its cybersecurity disclosures?": Intent.TOPIC,
            "How does Qualcomm's AI disclosure stack up against Broadcom's?": Intent.PEER_COMPARE,
            "Which bank changed its 10-K the most in 2025: BAC, JPM, C, GS or MS?": Intent.COMPARE,
            "To what extent did Qualcomm revise its Item 1A in 2024?": Intent.COMPARE,
            "Which JPM alignments did the two methods label differently?": Intent.DISAGREEMENTS,
            "In Goldman's 2024 risk factors, where don't the embedding and text-diff classifications agree?":
                Intent.DISAGREEMENTS,
            "What changed between JPMorgan's 2024 and 2025 10-Ks?": Intent.SUMMARY,
            "Walk me through Micron's changes between 2024 and 2025": Intent.SUMMARY,
            "How many reworded disclosures did Broadcom have across both comparison windows?": Intent.COUNT,
            # must not change
            "Summarise the key changes for BAC 2023 to 2024": Intent.SUMMARY,
            "What changed in BAC's 2024 10-K?": Intent.SUMMARY,
            "Show me the split disclosures (one-to-many) for Micron 2024": Intent.LIST,
            "List the new disclosures in AMD's 2025 business section": Intent.LIST,
            "Compare NVIDIA and AMD on supply chain risk in 2024": Intent.PEER_COMPARE,
            "Compare the change patterns between BAC and JPM for 2024-2025": Intent.COMPARE,
            "Which bank made the most material changes in 2025?": Intent.MATERIAL,
            "Show me the semantic vs lexical disagreements": Intent.DISAGREEMENTS,
        }
        for question, intent in cases.items():
            with self.subTest(question=question):
                self.assertEqual(self.plan(question).intent, intent)

    def test_topics(self):
        self.assertEqual(self.plan("What changed in Nvidia's export control disclosures in 2024?").topic,
                         "export control")
        self.assertEqual(self.plan("How did JPMorgan describe its liquidity risk in 2024?").topic, "liquidity")
        self.assertEqual(self.plan("How does Qualcomm's AI disclosure stack up against Broadcom's?").topic, "ai")

    def test_sector_words_and_limits(self):
        self.assertEqual(self.plan("Which chipmaker revised its 10-K the least?").filters.companies,
                         ["intc", "amd", "nvda", "avgo", "qcom", "mu"])
        self.assertEqual(self.plan("Which bank changed the most?").filters.companies, ["bac", "jpm", "c", "gs", "ms"])
        self.assertEqual(self.plan("Bank of America's top changes").filters.companies, ["bac"])
        self.assertIsNone(self.plan("where the two change-detection methods differ").filters.limit)
        self.assertEqual(self.plan("top 5 changes for AMD").filters.limit, 5)


class SqlAccuracyFixTests(unittest.TestCase):
    """Fixes from the SQL accuracy evaluation (eval/sql_accuracy)."""

    def plan(self, question):
        return plan_question(question, RUNS)

    def test_material_filter(self):
        self.assertEqual(self.plan("How many material changes did Bank of America have in 2024?").filters.material, "Yes")
        self.assertEqual(self.plan("Show non-material changes for BAC").filters.material, "No")
        self.assertIsNone(self.plan("What are the 5 most material changes for BAC?").filters.material)
        self.assertIsNone(self.plan("changes with materiality above 0.8").filters.material)
        where, params = where_clause(self.plan("How many material changes did BAC have?").filters)
        self.assertIn("material = %(material)s", where)
        self.assertEqual(params["material"], "Yes")

    def test_threshold_operators(self):
        f = self.plan("How many changes in Nvidia's 2025 10-K have a materiality score above 0.8?").filters
        self.assertTrue(f.min_materiality_strict)
        self.assertIn("materiality_score > %(min_materiality)s", where_clause(f)[0])
        f = self.plan("changes with materiality of at least 0.9").filters
        self.assertIn("materiality_score >= %(min_materiality)s", where_clause(f)[0])
        f = self.plan("Which Goldman Sachs disclosures have a confidence score below 0.7?").filters
        self.assertIn("confidence_score < %(max_confidence)s", where_clause(f)[0])
        f = self.plan("changes with confidence of at most 80%").filters
        self.assertAlmostEqual(f.max_confidence, 0.8)
        self.assertIn("confidence_score <= %(max_confidence)s", where_clause(f)[0])
        self.assertTrue(self.plan("changes with materiality below 0.3").filters.clarifications)

    def test_changed_count_excludes_unchanged_and_unanalysed(self):
        plan = self.plan("How many cybersecurity disclosures changed for JPMorgan in 2025?")
        self.assertTrue(plan.exclude_unchanged)
        sql = build_queries(plan)[0].sql
        self.assertIn("change_type IS NOT NULL AND change_type <> 'Unchanged'", sql)
        self.assertFalse(self.plan("How many changes did BAC have in 2024?").exclude_unchanged)

    def test_numbers_are_not_topics(self):
        plan = self.plan("What are Nvidia's 3 most material new disclosures in 2025?")
        self.assertEqual(plan.intent, Intent.MATERIAL)
        self.assertIsNone(plan.topic)
        self.assertEqual(plan.filters.limit, 3)

    def test_breakdown_dimensions(self):
        plan = self.plan("Break down Bank of America's 2024 changes by change type and item")
        self.assertEqual((plan.intent, plan.group_by), (Intent.DISTRIBUTION, ["change_type", "item"]))
        sql = build_queries(plan)[0].sql
        self.assertIn("GROUP BY coalesce(change_type, 'Not analysed (needs review)'), item", sql)
        self.assertEqual(self.plan("What's the status breakdown for Goldman Sachs in 2024?").group_by, ["status"])
        self.assertEqual(self.plan("What are the most common types of changes in Item 7?").group_by, [])
        self.assertEqual(self.plan("How many alignments does each company have in 2025?").intent, Intent.COUNT)

    def test_calculations_go_to_llm_sql_with_rule_fallback(self):
        for question in ("What is the average materiality score of Morgan Stanley's 2025 changes?",
                         "Which Item had the highest share of removed disclosures for JPMorgan in 2025?",
                         "What is the median materiality score of new disclosures in 2025?",
                         "For each company, what percentage of its 2024 alignments needs review?",
                         "List the companies where removed disclosures outnumber new disclosures in 2024",
                         "Which 3 sections of JPMorgan's 2024 MD&A have the most changed disclosures?"):
            with self.subTest(question=question):
                plan = self.plan(question)
                self.assertEqual(plan.intent, Intent.CUSTOM_SQL)
                self.assertIsNotNone(plan.fallback_intent)
        for question in ("Show material changes where the model's confidence is below 60% for GS",
                         "What are the 5 most material changes in Bank of America's 2024 10-K?",
                         "What was AMD's gross margin in 2024?",
                         "How many material changes did each chipmaker have in 2025?"):
            with self.subTest(question=question):
                self.assertNotEqual(self.plan(question).intent, Intent.CUSTOM_SQL)


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

PEER_RUNS = [(c, p, p + 1) for c in ("bac", "jpm", "c", "gs", "ms", "intc", "amd", "nvda", "avgo", "qcom", "mu")
             for p in (2023, 2024)]


class PeerRoutingTests(unittest.TestCase):
    def test_company_names_short_tickers_and_order(self):
        from sec_disclosure.chatbot.entities import find_companies
        self.assertEqual(find_companies("Compare NVIDIA and AMD on export controls"), ["nvda", "amd"])
        self.assertEqual(find_companies("How do C and MS describe credit risk?"), ["c", "ms"])
        self.assertEqual(find_companies("Goldman vs Morgan Stanley vs Citi"), ["gs", "ms", "c"])
        self.assertEqual(find_companies("JPMorgan Chase vs Bank of America"), ["jpm", "bac"])
        self.assertEqual(find_companies("ms excel, item c, mu of the model"), [])  # lower-case words are not tickers
        self.assertEqual(find_companies("Item 1A(c) of the filing"), [])

    def test_peer_questions_route_to_peer_comparison(self):
        cases = [
            ("Compare NVIDIA and AMD on export controls in 2024", ["nvda", "amd"], "export controls", 2024),
            ("How do C and MS describe credit risk?", ["c", "ms"], "credit", None),
            ("What do NVDA and Intel say about supply chain risk?", ["nvda", "intc"], "supply chain", None),
            ("How does Nvidia compare to its peers on customer concentration?", ["nvda"], "customer concentration", None),
        ]
        for question, companies, topic, year in cases:
            with self.subTest(question=question):
                plan = plan_question(question, PEER_RUNS)
                self.assertEqual(plan.intent, Intent.PEER_COMPARE)
                self.assertEqual(plan.filters.companies, companies)
                self.assertEqual(plan.topic, topic)
                self.assertEqual(plan.filters.current_year, year)

    def test_change_statistics_and_other_intents_are_not_peer_questions(self):
        self.assertEqual(plan_question("Compare the change patterns between BAC and JPM for 2024-2025", PEER_RUNS).intent,
                         Intent.COMPARE)
        self.assertEqual(plan_question("Most material changes for NVDA and AMD", PEER_RUNS).intent, Intent.MATERIAL)
        self.assertEqual(plan_question("How many new risk factors did NVDA and AMD add in 2025?", PEER_RUNS).intent,
                         Intent.COUNT)

    def test_peer_topic_query_needs_every_word(self):
        from sec_disclosure.chatbot.peers import topic_tsquery
        self.assertEqual(topic_tsquery("export controls"), "(export <-> controls) | ((export) & (controls))")
        self.assertIn("(supply | suppliers | sourcing) & (chain)", topic_tsquery("supply chain"))
        self.assertIn("emissions", topic_tsquery("climate"))
        self.assertEqual(topic_tsquery("'); drop table x; --"), "(drop <-> table <-> x) | ((drop) & (table) & (x))")

    def test_pairs_for_one_company_come_from_loaded_runs(self):
        from sec_disclosure.chatbot.peers import fiscal_year, wanted_pairs
        runs = [{"target": "nvda", "peers": ["amd", "intc"]}, {"target": "avgo", "peers": ["nvda"]}]
        self.assertEqual(wanted_pairs(["nvda"], runs), [("nvda", "amd"), ("nvda", "intc"), ("nvda", "avgo")])
        self.assertEqual(wanted_pairs(["nvda", "amd", "intc"], runs),
                         [("nvda", "amd"), ("nvda", "intc"), ("amd", "intc")])
        self.assertEqual(fiscal_year(parse_filters("between 2023 and 2024", RUNS)), 2024)

    def test_grounding_checks_sentence_ids(self):
        rows = [{"disclosure_a": "nvda_2024_1A_D002", "sentence_ids_a": ["nvda_2024_1A_P003_S001"]}]
        cleaned, report = check_answer("Demand risk (nvda_2024_1A_P003_S001; nvda_2024_1A_P099_S009).", rows,
                                       lambda ids: set())
        self.assertIn("nvda_2024_1A_P003_S001", cleaned)
        self.assertNotIn("P099_S009", cleaned)
        self.assertEqual(report.removed_ids, ["nvda_2024_1A_P099_S009"])


class PeerLoaderValidationTests(unittest.TestCase):
    FIXTURE = ROOT / "test_data" / "peer_comparison" / "nvda" / "2024_amd_material_pilot" / "peer_comparison.json"

    def mutate(self, change):
        import json
        import tempfile
        report = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        change(report)
        folder = Path(tempfile.mkdtemp())
        (folder / "peer_comparison.json").write_text(json.dumps(report), encoding="utf-8")
        return folder

    def test_fixture_is_valid(self):
        from sec_disclosure.storage.peer_comparisons import read_report, rows_for
        report, _ = read_report(self.FIXTURE.parent)
        targets, results, comparisons = rows_for(report)
        self.assertEqual(len(targets), report["jobs_completed"])
        self.assertEqual(len(results), len(targets) * len(report["peers"]))
        self.assertTrue(all({e["side"] for e in ev} == {"target", "peer"} for _, ev in comparisons))

    def test_invalid_reports_are_rejected_before_any_database_work(self):
        from sec_disclosure.storage.peer_comparisons import read_report

        def wrong_peer_prefix(r):
            first = next(res for job in r["comparisons"] for res in job["peer_results"] if res["comparisons"])
            first["comparisons"][0]["peer_disclosure_id"] = "zzz_2024_1A_D001"

        def status_without_comparisons(r):
            first = next(res for job in r["comparisons"] for res in job["peer_results"] if res["comparisons"])
            first["comparisons"] = []

        def missing_peer(r):
            r["comparisons"][0]["peer_results"] = []

        def count_mismatch(r):
            r["jobs_completed"] += 1

        def old_schema(r):
            r["schema_version"] = "1"

        for change in (wrong_peer_prefix, status_without_comparisons, missing_peer, count_mismatch, old_schema):
            with self.subTest(change=change.__name__), self.assertRaises(ValueError):
                read_report(self.mutate(change))



class SynthesisRoutingTests(unittest.TestCase):
    def test_financial_figure_questions_route_to_trends(self):
        for question in ("How did AMD gross margin change from 2023 to 2025?", "What was AMD revenue growth in 2024?",
                         "AMD financial trends", "Compare AMD and NVDA revenue growth", "How did AMD's debt change?"):
            with self.subTest(question=question):
                self.assertEqual(plan_question(question, PEER_RUNS).intent, Intent.FINANCIAL)

    def test_disclosure_questions_with_financial_words_are_not_financial(self):
        self.assertEqual(plan_question("What do JPM and BAC say about liquidity risk?", PEER_RUNS).intent,
                         Intent.PEER_COMPARE)
        self.assertEqual(plan_question("Which revenue disclosures were expanded for AMD?", PEER_RUNS).intent,
                         Intent.LIST)

    def test_summary_keeps_its_topic_and_span(self):
        plan = plan_question("Summarise AMD changes about data center", PEER_RUNS)
        self.assertEqual((plan.intent, plan.topic), (Intent.SUMMARY, "data center"))
        plan = plan_question("Summarise the key changes for AMD from 2023 to 2025", PEER_RUNS)
        self.assertEqual((plan.filters.previous_year, plan.filters.current_year), (2023, 2025))

    def test_metric_patterns(self):
        from sec_disclosure.chatbot.reports import wanted_metrics
        self.assertEqual(wanted_metrics("How did gross margin change?"), ["gross margin%"])
        self.assertEqual(wanted_metrics("AMD margins"), ["gross margin%", "operating margin%", "net margin%"])
        self.assertEqual(wanted_metrics("operating margin and net margin"), ["operating margin%", "net margin%"])
        self.assertIn("revenue%", wanted_metrics("revenue growth"))
        self.assertEqual(wanted_metrics("AMD financial trends"), [])

    def test_report_span(self):
        from sec_disclosure.chatbot.reports import span_for
        self.assertEqual(span_for(parse_filters("AMD 2023 to 2025", RUNS)), (2023, 2025))
        self.assertEqual(span_for(parse_filters("AMD's 2024 10-K", RUNS)), (2023, 2024))
        self.assertEqual(span_for(parse_filters("AMD", RUNS)), (None, None))

    def test_multi_year_span_filters_every_window_inside_it(self):
        where, params = where_clause(parse_filters("AMD 2023 to 2025", RUNS))
        self.assertIn("previous_year >= %(previous_year)s", where)
        self.assertIn("current_year <= %(current_year)s", where)
        where, _ = where_clause(parse_filters("AMD 2023 to 2024", RUNS))
        self.assertIn("previous_year = %(previous_year)s", where)

    def test_schema_files_rerun_when_their_version_is_old(self):
        from sec_disclosure.storage.chatbot_schema import SCHEMAS, outdated
        current = {key: version for _, key, version in SCHEMAS}
        self.assertEqual(outdated(current), [])
        old_peer = {**current, "peer_comparison_schema_version": "1"}
        self.assertEqual([p.name for p in outdated(old_peer)], ["07_peer_comparisons.sql"])
        for path, _, _ in SCHEMAS:
            self.assertTrue(path.is_file(), path)


class SynthesisLoaderValidationTests(unittest.TestCase):
    FIXTURE = ROOT / "test_data" / "synthesis" / "amd_2023_2025_synthesis_output.json"
    SAMPLES = sorted((ROOT / "test_data" / "synthesis").glob("*_synthesis_output.json"))

    def mutate(self, change):
        import json
        import tempfile
        report = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        change(report)
        path = Path(tempfile.mkdtemp()) / "X_synthesis_output.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_agent_samples_are_valid(self):
        from sec_disclosure.storage.synthesis_reports import read_report
        self.assertTrue(self.SAMPLES)
        for path in self.SAMPLES:
            report, _ = read_report(path)
            company = report["company"].lower()
            ids = [i for f in report["report"]["key_findings"] for i in f["evidence_ids"] if "_M" in i]
            self.assertTrue(ids, path.name)
            self.assertTrue(all(i.startswith(company + "_") for i in ids), path.name)

    def test_report_key(self):
        from sec_disclosure.storage.synthesis_reports import report_key
        self.assertEqual(report_key("AMD", [2023, 2024, 2025]), "amd_2023_2024_2025")

    def test_old_synthesis_schema_is_upgraded(self):
        from sec_disclosure.storage.chatbot_schema import SCHEMAS, outdated
        current = {key: version for _, key, version in SCHEMAS}
        self.assertEqual([p.name for p in outdated({**current, "synthesis_schema_version": "1"})],
                         ["08_synthesis_reports.sql"])

    def test_schema_matches_the_agents_table(self):
        sql = (ROOT / "db" / "init" / "08_synthesis_reports.sql").read_text(encoding="utf-8")
        for line in ("company       TEXT NOT NULL CHECK (company = upper(company))",
                     "years         INTEGER[] NOT NULL CHECK (cardinality(years) >= 2)",
                     "report        JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object')",
                     "PRIMARY KEY (company, years)"):
            self.assertIn(line, sql)

    def test_numbers_written_as_strings_are_accepted(self):
        from sec_disclosure.storage.synthesis_reports import read_report

        def stringify(r):
            for t in r["financial_trends"]:
                for key in ("old_value", "new_value", "value", "growth_pct"):
                    if t.get(key) is not None:
                        t[key] = str(t[key])
        report, _ = read_report(self.mutate(stringify))
        self.assertTrue(report["financial_trends"])

    def test_invalid_reports_are_rejected(self):
        from sec_disclosure.storage.synthesis_reports import read_report

        def other_company(r):
            r["report"]["key_findings"][0]["evidence_ids"] = ["nvda_2023_2024_M0001"]

        def no_summary(r):
            r["report"]["executive_summary"] = " "

        def bad_trend(r):
            r["financial_trends"][0]["type"] = "guess"

        def bad_number(r):
            r["financial_trends"][0]["growth_pct"] = "lots"

        def one_year(r):
            r["years"] = [2024]

        def finding_without_ids(r):
            del r["report"]["key_findings"][0]["evidence_ids"]

        for change in (other_company, no_summary, bad_trend, bad_number, one_year, finding_without_ids):
            with self.subTest(change=change.__name__), self.assertRaises(ValueError):
                read_report(self.mutate(change))


if __name__ == "__main__":
    unittest.main()
