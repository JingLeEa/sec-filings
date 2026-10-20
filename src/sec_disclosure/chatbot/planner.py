"""Query classifier: decide which retrieval path answers a question.

Rules handle the question shapes analysts ask most (they are fast, free and testable). Anything the
rules cannot place becomes ``Intent.CUSTOM_SQL``, which the engine sends to the LLM SQL generator when
an LLM is configured, or to keyword search otherwise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .entities import (COMPANY_ALIASES, DISCLOSURE_TAXONOMY_PATTERNS, ITEM_PATTERNS, QueryFilters, parse_filters)


class Intent(str, Enum):
    MATCH_DETAIL = "match_detail"            # a specific match_id
    DISAGREEMENTS = "method_disagreements"   # semantic vs lexical labels differ
    COMPARE = "compare_companies"            # change patterns side by side
    LOW_CONFIDENCE = "low_confidence"        # materiality confidence scores
    SUMMARY = "summary"                      # key changes for a company/period
    COUNT = "count"                          # how many ...
    DISTRIBUTION = "distribution"            # most common change types
    MATERIAL = "material_changes"            # most material changes
    TOPIC = "topic_search"                   # what changed about <topic> (semantic retrieval)
    LIST = "list_changes"                    # show me disclosures that were removed/expanded/...
    CUSTOM_SQL = "custom_sql"                # anything else -> LLM writes SQL


@dataclass
class Plan:
    intent: Intent
    filters: QueryFilters
    topic: str | None = None
    group_by_item: bool = False
    exclude_unchanged: bool = False
    notes: list[str] = field(default_factory=list)


TOPIC_RE = re.compile(
    r"\b(?:about|regarding|concerning|related\s+to|relating\s+to|around|on\s+the\s+topic\s+of|mention(?:s|ing)?|"
    r"discuss(?:es|ing)?|involving|on)\s+(?P<topic>.+?)(?=\s+(?:across|between|for|in|from|at|within|over|since|during)\b"
    r"|\s*[?.!]|$)", re.IGNORECASE)
GENERIC_TOPIC_WORDS = {
    "the", "a", "an", "their", "its", "his", "her", "this", "that", "these", "those", "of", "and", "or", "in", "for",
    "disclosure", "disclosures", "change", "changes", "changed", "filing", "filings", "10-k", "10k", "report",
    "risk", "risks", "item", "items", "company", "companies", "all", "three", "both", "each", "them", "it",
    "material", "materiality", "most", "key", "new", "removed", "added", "expanded", "reduced", "reworded",
    "modified", "unchanged", "disclosed", "language", "text", "wording", "section", "sections", "factor", "factors",
    "anything", "something", "things", "what", "how", "was", "were", "is", "are", "they", "we",
}
COMPARE_RE = re.compile(r"\bcompar\w*|\bversus\b|\bvs\.?\b|\bdifferences?\s+between\b|\bside[\s-]by[\s-]side\b|"
                        r"\bcontrast\b|\bhow\s+do\s+\w+\s+and\s+\w+\s+differ\b")
DISAGREE_RE = re.compile(r"\bdisagree\w*|\bconflict\w*|\bdiverg\w*|\bmismatch\w*|\binconsisten\w*|"
                         r"\bsemantic\s+(?:vs\.?|versus|and)\s+lexical\b|\blexical\s+(?:vs\.?|versus|and)\s+semantic\b")


HOW_CHANGED_RE = re.compile(
    r"\bhow\s+(?:did|has|have|do|does|were|was)\s+(?:the\s+)?(?P<topic>.+?)\s+(?:disclosures?\s+|language\s+|"
    r"wording\s+|risk\s+factors?\s+|risks?\s+)?(?:chang|evolv|shift|develop)\w*", re.IGNORECASE)


def extract_topic(question: str) -> str | None:
    """The subject of a 'what changed about X' / 'how did X change' question, minus company/period/item words."""
    for m in list(TOPIC_RE.finditer(question)) + list(HOW_CHANGED_RE.finditer(question)):
        topic = m.group("topic").strip(" ,;:'\"")
        cleaned = _strip_known_terms(topic)
        if cleaned:
            return cleaned
    return None


def _strip_known_terms(text: str) -> str:
    lowered = text.lower()
    for aliases in COMPANY_ALIASES.values():
        for alias in aliases:
            lowered = re.sub(rf"(?<![\w.]){re.escape(alias)}(?:'s)?(?!\w)", " ", lowered)
    lowered = re.sub(r"\b(?:fy\s*)?20\d{2}\b|\b10-?k\b", " ", lowered)
    for _, pattern in ITEM_PATTERNS:
        lowered = re.sub(pattern, " ", lowered)
    words = [w for w in re.findall(r"[a-z0-9&'-]+", lowered) if w not in GENERIC_TOPIC_WORDS]
    return " ".join(words).strip()


def residual_keywords(question: str) -> str:
    """Content words left after removing filters and question scaffolding (keyword-search fallback)."""
    scaffolding = {"what", "which", "who", "how", "why", "when", "where", "show", "me", "list", "tell", "give",
                   "find", "did", "does", "do", "is", "are", "was", "were", "has", "have", "had", "any", "there",
                   "most", "more", "much", "many", "between", "across", "about", "from", "to", "with", "by", "on",
                   "please", "can", "you", "i", "we", "our", "my", "be", "been", "get", "see", "new", "added",
                   "removed", "summarise", "summarize", "describe", "explain"}
    text = _strip_known_terms(question)
    for _, pattern in ITEM_PATTERNS:
        text = re.sub(pattern, " ", text)
    return " ".join(w for w in text.split() if w not in scaffolding and len(w) > 2)


def classify(question: str, filters: QueryFilters) -> Plan:
    text = question.lower()
    plan = Plan(intent=Intent.CUSTOM_SQL, filters=filters)
    plan.group_by_item = bool(re.search(r"\bby\s+item\b|\bper\s+item\b|\beach\s+item\b|\bacross\s+items\b", text))
    asks_about_change = bool(re.search(r"\bchang\w*|\bevolv\w*|\bdifferen\w*|\bshift\w*|\bupdat\w*", text))
    topic = extract_topic(question)

    if filters.match_ids:
        plan.intent = Intent.MATCH_DETAIL
    elif DISAGREE_RE.search(text) or ("semantic" in text and "lexical" in text):
        plan.intent = Intent.DISAGREEMENTS
    elif COMPARE_RE.search(text):
        plan.intent = Intent.COMPARE  # across companies, or across periods when only one company is named
    elif re.search(r"\bconfiden\w*|\bcertain\w*|\bunsure\b", text):
        plan.intent = Intent.LOW_CONFIDENCE
    elif re.search(r"\bsummar\w*|\boverview\b|\bkey\s+changes\b|\bhighlights?\b|\brecap\b|\bmain\s+changes\b|"
                   r"\bwhat\s+changed\s+(?:in|for)\b|\btl;?dr\b", text) and not topic:
        plan.intent = Intent.SUMMARY
    elif re.search(r"\bhow\s+many\b|(?<!word )\bcount\b|\bnumber\s+of\b", text):
        plan.intent = Intent.COUNT
    elif re.search(r"\bmost\s+common\b|\bdistribution\b|\bbreakdown\b|\bbreak\s+down\b|\btypes?\s+of\s+changes?\b|"
                   r"\bkinds?\s+of\s+changes?\b|\bpatterns?\b|\bfrequen\w*", text):
        plan.intent = Intent.DISTRIBUTION
    elif re.search(r"\bmaterial\w*|\bsignificant\w*|\bimportant\b|\bbiggest\b|\bmajor\b|\bimpactful\b|\bnotable\b", text) \
            and not topic:
        plan.intent = Intent.MATERIAL
    elif topic:
        plan.intent = Intent.TOPIC
        plan.topic = topic
        plan.exclude_unchanged = asks_about_change
    elif filters.change_types or filters.statuses or filters.relationships or filters.disclosure_taxonomies \
            or filters.items:
        plan.intent = Intent.LIST
        plan.exclude_unchanged = asks_about_change
    # Otherwise CUSTOM_SQL: the LLM writes the query (offline: keyword search on the question).

    # Topic words inside other intents (e.g. "most material changes about climate") narrow those intents too.
    if plan.intent in (Intent.MATERIAL, Intent.SUMMARY) and topic:
        plan.topic = topic
    # A taxonomy category named as the topic is applied as a filter instead of a free-text search.
    if plan.intent == Intent.TOPIC and topic and filters.disclosure_taxonomies:
        category_only = all(any(re.fullmatch(p, w) for _, p in DISCLOSURE_TAXONOMY_PATTERNS) for w in topic.split())
        if category_only:
            plan.notes.append("topic matches a taxonomy category; searching text and category")
    return plan


def plan_question(question: str, runs=()) -> Plan:
    return classify(question, parse_filters(question, runs))


FOLLOW_UP_RE = re.compile(r"^\s*(?:and|what\s+about|how\s+about|same\s+for|now|also|ok(?:ay)?,?|then)\b", re.IGNORECASE)


def inherit_context(plan: Plan, previous: Plan | None, question: str) -> Plan:
    """Carry company/period/item filters over from the previous turn for short follow-ups ("what about JPM?")."""
    if previous is None:
        return plan
    is_follow_up = bool(FOLLOW_UP_RE.search(question)) or len(question.split()) <= 4
    if not is_follow_up:
        return plan
    f, p = plan.filters, previous.filters
    if not f.companies and p.companies:
        f.companies = list(p.companies)
    if f.previous_year is None and f.current_year is None:
        f.previous_year, f.current_year = p.previous_year, p.current_year
    if not f.items and p.items:
        f.items = list(p.items)
    if plan.intent in (Intent.CUSTOM_SQL, Intent.LIST) and not (f.change_types or f.statuses) \
            and previous.intent not in (Intent.MATCH_DETAIL,):
        plan.intent = previous.intent
        plan.topic = plan.topic or previous.topic
        plan.exclude_unchanged = previous.exclude_unchanged
        if not f.change_types:
            f.change_types = list(p.change_types)
    plan.notes.append("follow-up: inherited filters from the previous question")
    return plan
