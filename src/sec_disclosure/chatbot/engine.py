"""Chatbot orchestration: classify -> (SQL | semantic retrieval | LLM-written SQL) -> synthesise -> ground."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..llm.client import LLMError, request_completion
from ..llm.config import LLMConfig
from .config import load_chat_config, load_embedding_config
from .db import Database, DatabaseError, QueryResult
from .grounding import GroundingReport, check_answer
from .planner import Intent, Plan, inherit_context, plan_question, residual_keywords
from .queries import build_queries, topic_cards
from .retrieval import Retriever
from .sql_guard import UnsafeSQL, validate_sql
from .synthesis import SYSTEM_PROMPT, evidence_pack, offline_answer, user_prompt

Completer = Callable[[str, str, bool, int], str]   # (system, prompt, json_mode, max_tokens) -> text

# Generous budgets: reasoning models (Qwen3, DeepSeek-R1, ...) may spend tokens thinking before they answer.
SQL_MAX_TOKENS = 3000
ANSWER_MAX_TOKENS = 6000
DEFAULT_TIMEOUT = 180.0
THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks some models put in the reply; an unclosed block means no answer yet."""
    text = THINK_RE.sub("", text or "")
    if re.search(r"<think(?:ing)?>", text, re.IGNORECASE):
        text = re.split(r"<think(?:ing)?>", text, flags=re.IGNORECASE)[0]
    return text.strip()

SCHEMA_DOC = """View alignment_changes (one row per alignment decision; use this first):
  run_id bigint, company text ('bac','jpm','intc'), previous_year int, current_year int, match_id text,
  status text ('auto_matched','ai_verified','unmatched','needs_review'),
  relationship text ('one_to_one','one_to_many','many_to_one','many_to_many','previous_only','current_only'),
  match_method text, source_file text, item text ('1','1A','7','8','15'), section text,
  disclosure_taxonomy text ('Strategy & Business Model','Operations & Capacity','Technology & AI',
     'Cybersecurity & Data','Supply Chain & Third Parties','Regulation, Legal & Compliance',
     'Financial & Capital Resources','Human Capital & Organization','Other / Unclassified'),
  analysis_status text ('completed','not_started'),
  change_type text ('Unchanged','Modified','Reworded','Expanded','Reduced','New','Removed'; NULL if needs_review),
  lexical_label text, semantic_label text, llm_label text, semantic_score float, lexical_cosine float,
  sequence_match_ratio float, length_ratio float, word_count_delta int, semantic_rationale text,
  material text ('Yes','No'), materiality_score float (0-1), confidence_score float (0-1),
  materiality_reason text, key_change text, explanation text, review_reasons text[], verifier_review_reason text,
  unmatched_type text, previous_ids text[], current_ids text[], previous_summary text, current_summary text,
  previous_text text, current_text text  (original filing sentences; long - use left(x, 400))
Tables also available: alignment_runs, alignments (JSONB change_analysis, extra->'materiality_analysis'),
  alignment_members(run_id, match_id, side, disclosure_id, item, section, taxonomy, summary),
  disclosures, filings (may be empty in development)."""

SQL_SYSTEM = f"""You write one PostgreSQL SELECT query that answers an analyst's question about SEC 10-K
disclosure changes. Return JSON only: {{"sql": "...", "reason": "one sentence"}}.
{SCHEMA_DOC}
Rules: read-only SELECT/WITH only; no semicolons; no double-quoted identifiers; always include match_id in
row-level results so answers can cite it; prefer alignment_changes; use ILIKE for text matching; years are
fiscal years (a '2024 10-K' is current_year = 2024); keep results under 50 rows with ORDER BY and LIMIT."""


@dataclass
class ChatResponse:
    question: str
    answer: str
    intent: str
    filters: dict
    mode: str                                    # 'llm' or 'offline'
    results: list[QueryResult] = field(default_factory=list)
    retrieval_method: str | None = None
    citations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    grounding: GroundingReport | None = None

    @property
    def queries(self) -> list[tuple[str, str, object]]:
        return [(r.name, r.sql, r.params) for r in self.results]


def _default_completer(config: LLMConfig, timeout: float) -> Completer:
    def complete(system: str, prompt: str, json_mode: bool, max_tokens: int) -> str:
        try:
            result = request_completion(prompt, config=config, system_prompt=system, json_mode=json_mode,
                                        max_tokens=max_tokens, timeout=timeout)
        except LLMError as error:
            if json_mode and error.status_code == 400:  # endpoint without response_format support
                result = request_completion(prompt, config=config, system_prompt=system, max_tokens=max_tokens,
                                            timeout=timeout)
            else:
                raise
        text = strip_reasoning(result.text)
        if not text:
            raise LLMError("The model returned no answer text (it may have used its token budget on reasoning).")
        return text
    return complete


def _parse_json(text: str) -> dict:
    """First JSON object in the reply that has an "sql" key; tolerates code fences and surrounding prose."""
    text = strip_reasoning(text)
    decoder = json.JSONDecoder()
    fallback = None
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            if "sql" in value:
                return value
            fallback = fallback or value
    if fallback is not None:
        return fallback
    raise ValueError("no JSON object in model reply")


class Chatbot:
    """Answer natural-language questions about disclosure changes stored in PostgreSQL.

    >>> bot = Chatbot()                       # uses SOCLAAS_* settings if present, else offline
    >>> reply = bot.ask("What are the most material changes in Bank of America's 2024 10-K?")
    >>> print(reply.answer)
    """

    def __init__(self, *, db: Database | None = None, env_file: Path | None = None, use_llm: bool = True,
                 completer: Completer | None = None, embedder=None, use_embeddings: bool = True,
                 llm_timeout: float = DEFAULT_TIMEOUT, ensure_schema: bool = True):
        self.db = db or Database(env_file)
        if ensure_schema:
            self.db.ensure_schema()
        self.llm_description = "offline (disabled)"
        self.completer: Completer | None = None
        if completer is not None:
            self.completer, self.llm_description = completer, "custom completer"
        elif use_llm:
            config, self.llm_description = load_chat_config(env_file)
            if config is not None:
                self.completer = _default_completer(config, llm_timeout)
        if embedder is None and use_embeddings:
            embed_config = load_embedding_config(env_file)
            if embed_config is not None:
                from ..llm.embeddings import make_embedder
                embedder = make_embedder(config=embed_config)
        self.retriever = Retriever(self.db, embedder)
        self.last_plan: Plan | None = None

    def check_llm(self) -> str:
        """Send one tiny request to SoCLaaS; returns a status line (raises LLMError on failure)."""
        if self.completer is None:
            raise LLMError(f"SoCLaaS is not configured: {self.llm_description.removeprefix('offline ')}")
        reply = self.completer("Reply with exactly: OK", "Connection check.", False, 400)
        return f"SoCLaaS OK ({self.llm_description}); reply: {reply[:40]!r}"

    @property
    def llm_enabled(self) -> bool:
        return self.completer is not None

    def reset(self) -> None:
        self.last_plan = None

    def plan(self, question: str) -> Plan:
        runs = [(r.company, r.previous_year, r.current_year) for r in self.db.runs()]
        return inherit_context(plan_question(question, runs), self.last_plan, question)

    def ask(self, question: str) -> ChatResponse:
        question = question.strip()
        if not question:
            raise ValueError("Ask a question.")
        plan = self.plan(question)
        notes = list(plan.notes)
        warnings: list[str] = []
        results: list[QueryResult] = []
        retrieval_method = None

        if plan.intent == Intent.CUSTOM_SQL:
            if self.llm_enabled:
                custom = self._llm_sql(question, notes)
                if custom is not None:
                    results.append(custom)
            if not results:  # offline, or the LLM query failed: fall back to keyword search on the question
                keywords = residual_keywords(question)
                if keywords:
                    plan.intent, plan.topic = Intent.TOPIC, keywords
                    notes.append(f"no specific query type recognised; searched for: {keywords}")
                else:
                    plan.intent = Intent.SUMMARY
                    notes.append("no specific query type recognised; showing a summary")

        if plan.intent == Intent.TOPIC:
            retrieval = self.retriever.search(plan.topic or question, plan.filters, k=max(plan.filters.limit or 0, 25),
                                              exclude_unchanged=plan.exclude_unchanged)
            retrieval_method = retrieval.method
            notes.extend(retrieval.notes)
            results.append(self._run_topic(retrieval, plan))
        elif plan.intent != Intent.CUSTOM_SQL:
            if plan.topic and plan.intent in (Intent.MATERIAL, Intent.SUMMARY):
                notes.append(f"topic '{plan.topic}' noted; results are not restricted to it")
            for query in build_queries(plan):
                results.append(self.db.query(query.name, query.sql, query.params))

        answer, mode, report = self._synthesise(question, plan, results, notes, warnings)
        self.last_plan = plan
        citations = list(dict.fromkeys(row["match_id"] for res in results for row in res.rows
                                       if isinstance(row.get("match_id"), str)))
        if report:
            warnings.extend(report.warnings())
        return ChatResponse(question=question, answer=answer, intent=plan.intent.value, filters=plan.filters.as_dict(),
                            mode=mode, results=results, retrieval_method=retrieval_method, citations=citations,
                            warnings=warnings, notes=notes, grounding=report)

    def _run_topic(self, retrieval, plan: Plan) -> QueryResult:
        if not retrieval.hits:
            return QueryResult(name="topic_matches", sql="-- no retrieval hits", params=None)
        query = topic_cards(retrieval.hits, plan.filters, exclude_unchanged=plan.exclude_unchanged)
        result = self.db.query(query.name, query.sql, query.params)
        limit = plan.filters.limit or 15
        result.rows = result.rows[:limit]
        return result

    def _llm_sql(self, question: str, notes: list[str]) -> QueryResult | None:
        runs = ", ".join(f"{r.company} {r.previous_year}->{r.current_year}" for r in self.db.runs())
        prompt = f"Loaded comparison runs: {runs}\nQuestion: {question}"
        error_note = ""
        for attempt in range(2):
            try:
                reply = _parse_json(self.completer(SQL_SYSTEM, prompt + error_note, True, SQL_MAX_TOKENS))
                safe_sql = validate_sql(str(reply.get("sql", "")))
                result = self.db.query("llm_sql", safe_sql, None)
                notes.append(f"LLM-generated SQL: {reply.get('reason', '').strip()}")
                return result
            except (UnsafeSQL, DatabaseError, ValueError) as error:
                error_note = f"\n\nYour previous query was rejected: {error}. Write a corrected query."
                notes.append(f"LLM SQL attempt {attempt + 1} rejected: {error}")
            except LLMError as error:
                notes.append(f"LLM SQL generation failed: {error}")
                return None
        return None

    def _synthesise(self, question, plan, results, notes, warnings) -> tuple[str, str, GroundingReport | None]:
        fallback = offline_answer(question, plan, results, notes)
        if not self.llm_enabled:
            return fallback, "offline", None
        pack = evidence_pack(results)
        try:
            text = strip_reasoning(self.completer(SYSTEM_PROMPT, user_prompt(question, plan, pack, notes), False,
                                                  ANSWER_MAX_TOKENS))
        except LLMError as error:
            warnings.append(f"LLM answer unavailable ({error}) [{self.llm_description}]; showing the templated answer.")
            return fallback, "offline", None
        if not text:
            warnings.append("LLM returned no text; showing the templated answer.")
            return fallback, "offline", None
        rows = [row for res in results for row in res.rows]
        answer, report = check_answer(text, rows, self.db.existing_match_ids)
        return answer, "llm", report
