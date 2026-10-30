"""Query classifier: decide which retrieval path answers a question.

Rules handle the question shapes analysts ask most (they are fast, free and testable). Anything the
rules cannot place becomes ``Intent.CUSTOM_SQL``, which the engine sends to the LLM SQL generator when
an LLM is configured, or to keyword search otherwise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .entities import (COMPANY_ALIASES, DISCLOSURE_TAXONOMY_PATTERNS, ITEM_PATTERNS, RELATIONSHIP_PATTERNS,
                       STATUS_PATTERNS, QueryFilters, parse_filters)
from .reports import FINANCIAL_RE


class Intent(str, Enum):
    MATCH_DETAIL = "match_detail"            # a specific match_id
    DISAGREEMENTS = "method_disagreements"   # semantic vs lexical labels differ
    FINANCIAL = "financial_trends"           # revenue, margins, EPS ... from synthesis-report trends (XBRL)
    PEER_COMPARE = "peer_comparison"         # how two+ companies disclose the same topic (peer agent output first)
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
    r"discuss(?:es|ing|ed)?|describe[sd]?|describing|involving|on)\s+(?P<topic>.+?)(?=\s+(?:across|between|for|in|from|at|within|over|since|during)\b"
    r"|\s*[?.!]|$)", re.IGNORECASE)
GENERIC_TOPIC_WORDS = {
    "the", "a", "an", "their", "its", "his", "her", "this", "that", "these", "those", "of", "and", "or", "in", "for",
    "disclosure", "disclosures", "change", "changes", "changed", "filing", "filings", "10-k", "10k", "report",
    "risk", "risks", "item", "items", "company", "companies", "all", "three", "both", "each", "them", "it",
    "material", "materiality", "most", "key", "new", "removed", "added", "expanded", "reduced", "reworded",
    "modified", "unchanged", "disclosed", "language", "text", "wording", "section", "sections", "factor", "factors",
    "anything", "something", "things", "what", "how", "was", "were", "is", "are", "they", "we",
    "rewrote", "rewritten", "rewrite", "revised", "revision", "revisions", "updated", "update", "updates",
}
# How much / which company changed the most: questions about the amount of change (change statistics).
AMOUNT_OF_CHANGE = (r"\b(?:how\s+much|to\s+what\s+extent|how\s+(?:heavily|extensively|substantially))\b.{0,60}?"
                    r"\b(?:chang|revis|rewr|updat|edit)\w*|"
                    r"\b(?:chang|revis|rewr|updat|edit|add)\w*\b.{0,40}?\bthe\s+(?:most|least|fewest)\b"
                    r"(?!\s+(?:material|significant|important|notable|common|frequent))|"
                    r"\b(?:more|less)\s+(?:heavily\s+|substantially\s+|significantly\s+)?"
                    r"(?:revised|changed|rewritten|updated|edited)\b")
# "compared to the year before" inside a single-filing question is not a comparison between companies or periods.
YEAR_BEFORE_RE = re.compile(r"\b(?:compared\s+(?:to|with)|vs\.?|versus|relative\s+to|against|over)\s+"
                            r"(?:the\s+|its\s+|their\s+)?(?:year\s+before|prior\s+year|previous\s+year|"
                            r"preceding\s+year|last\s+year|(?:fy\s*)?20\d{2}\s+(?=one|filing|10-?k|report))(?:'s)?"
                            r"(?:\s+(?:one|filing|10-?k|report))?|\bcomparison\s+(?:windows?|periods?|years?)\b")
# "<subject> disclosures": "its tariff disclosures", "in Nvidia's export control disclosures".
DISCLOSURE_TOPIC_RE = re.compile(r"(?:\b(?:its|their|the|in|on|to)\s+|'s\s+)(?P<topic>(?:[\w&-]+\s+){0,3}?[\w&-]+)\s+"
                                 r"disclosures?\b", re.IGNORECASE)
COMPARE_RE = re.compile(r"\bcompar\w*|\bversus\b|\bvs\.?\b|\bdifferences?\s+between\b|\bside[\s-]by[\s-]side\b|"
                        r"\bcontrast\b|\bhow\s+do\s+\w+\s+and\s+\w+\s+differ\b|" + AMOUNT_OF_CHANGE)
DISAGREE_RE = re.compile(r"\bdisagree\w*|\bconflict\w*|\bdiverg\w*|\bmismatch\w*|\binconsisten\w*|"
                         r"\blabel(?:l?ed|s|ing)?\s+(?:\w+\s+){0,2}differently\b|\b(?:two|both)\s+(?:labell?ing\s+)?methods\b|"
                         r"\bmethods?\s+(?:disagree|differ)\w*|(?:n['o]t|\bnot)\b.{0,60}?\bagree\b|"
                         r"\bdifferent\s+(?:verdicts|labels|conclusions|classifications)\b|"
                         r"\bsemantic\s+(?:vs\.?|versus|and)\s+lexical\b|\blexical\s+(?:vs\.?|versus|and)\s+semantic\b")


HOW_CHANGED_RE = re.compile(
    r"\bhow\s+(?:did|has|have|do|does|were|was)\s+(?:the\s+)?(?P<topic>.+?)\s+(?:disclosures?\s+|language\s+|"
    r"wording\s+|risk\s+factors?\s+|risks?\s+)?(?:chang|evolv|shift|develop)\w*", re.IGNORECASE)


def extract_topic(question: str) -> str | None:
    """The subject of a 'what changed about X' / 'how did X change' question, minus company/period/item words."""
    for m in (list(TOPIC_RE.finditer(question)) + list(HOW_CHANGED_RE.finditer(question))
              + list(DISCLOSURE_TOPIC_RE.finditer(question))):
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
    for _, pattern in ITEM_PATTERNS + STATUS_PATTERNS + RELATIONSHIP_PATTERNS:   # filters, not subjects
        lowered = re.sub(pattern, " ", lowered)
    words = [w for w in re.findall(r"[a-z0-9&'-]+", lowered) if w not in GENERIC_TOPIC_WORDS]
    return " ".join(words).strip()


SHORT_TERMS = {"ai", "ip", "hr", "fx", "rd"}


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
    return " ".join(w for w in text.split() if w not in scaffolding and (len(w) > 2 or w in SHORT_TERMS))


# Financial-figure questions ("how did AMD's gross margin change") vs. disclosure questions that merely mention
# a financial word ("what do they say about liquidity risk", "which revenue disclosures were expanded").
NOT_FINANCIAL_RE = re.compile(r"\bdisclos\w*|\brisks?\b|\blanguage\b|\bwording\b|\bsections?\b|\bmention\w*|"
                              r"\b(?:say|says|said|describe[sd]?|discuss(?:es|ed)?)\b|\bitem\s*\d")
PEER_LANGUAGE_RE = re.compile(
    r"\bcompar\w*|\bversus\b|\bvs\.?(?=\s|$)|\bdiffer\w*|\bsimilar\w*|\bcontrast\b|\bboth\b|\bside[\s-]by[\s-]side\b|"
    r"\b(?:say|says|said|describe[sd]?|disclose[sd]?|discuss(?:es|ed)?|talk|treat|frame|position|approach)\b|"
    r"\bstacks?\s+up\b|\bstacked\s+up\b|\bmeasures?\s+up\b|\brelative\s+to\b|\bfares?\s+against\b|\bagainst\b")
PEER_SET_RE = re.compile(r"\bpeers?\b|\bcompetitors?\b|\brivals?\b|\bother\s+(?:banks|companies|chipmakers|firms)\b")
CHANGE_STATS_RE = re.compile(
    r"\bchange\s+(?:patterns?|profiles?|statistics|stats|rates?)\b|\btypes?\s+of\s+changes?\b|\bhow\s+many\b|"
    r"\bnumber\s+of\b|\bdistribution\b|\bbreakdown\b|\bstatistic\w*|\bchange\s+labels?\b|" + AMOUNT_OF_CHANGE)
PEER_FILLER = {"compare", "comparing", "comparison", "versus", "differ", "different", "difference", "differences",
               "similar", "similarly", "contrast", "both", "say", "says", "said", "describe", "describes", "disclose",
               "discloses", "discuss", "discusses", "talk", "treat", "frame", "position", "approach", "peers", "peer",
               "competitors", "rivals", "their", "they", "them", "its", "vs", "and", "with", "stack", "stacks",
               "stacked", "measure", "measures", "fare", "fares", "against", "relative", "disclosure", "disclosures",
               "up"}


def peer_topic(question: str) -> str | None:
    topic = extract_topic(question)
    if not topic:
        topic = " ".join(w for w in residual_keywords(question).split() if w not in PEER_FILLER) or None
    return topic


def classify(question: str, filters: QueryFilters) -> Plan:
    text = YEAR_BEFORE_RE.sub(" ", question.lower())
    plan = Plan(intent=Intent.CUSTOM_SQL, filters=filters)
    plan.group_by_item = bool(re.search(r"\bby\s+item\b|\bper\s+item\b|\beach\s+item\b|\bacross\s+items\b", text))
    asks_about_change = bool(re.search(r"\bchang\w*|\bevolv\w*|\bdifferen\w*|\bshift\w*|\bupdat\w*", text))
    topic = extract_topic(question)

    if filters.match_ids:
        plan.intent = Intent.MATCH_DETAIL
    elif DISAGREE_RE.search(text) or ("semantic" in text and "lexical" in text):
        plan.intent = Intent.DISAGREEMENTS
    elif FINANCIAL_RE.search(text) and not NOT_FINANCIAL_RE.search(text) and not filters.change_types:
        plan.intent = Intent.FINANCIAL      # computed financial trends from synthesis reports
    elif not CHANGE_STATS_RE.search(text) and (
            (len(filters.companies) >= 2 and PEER_LANGUAGE_RE.search(text))
            or (len(filters.companies) >= 1 and PEER_SET_RE.search(text))):
        plan.intent = Intent.PEER_COMPARE   # precomputed peer-agent comparisons first, alignments as fallback
        plan.topic = peer_topic(question)
    elif COMPARE_RE.search(text):
        plan.intent = Intent.COMPARE  # across companies, or across periods when only one company is named
    elif re.search(r"\bconfiden\w*|\bcertain\w*|\bunsure\b", text):
        plan.intent = Intent.LOW_CONFIDENCE
    elif re.search(r"\bsummar\w*|\boverview\b|\brecap\b|\btl;?dr\b", text) or (
            re.search(r"\bkey\s+changes\b|\bhighlights?\b|\bmain\s+changes\b|\bwhat\s+changed\s+(?:in|for|between)\b|"
                      r"\bwalk\s+(?:me\s+|us\s+)?through\b", text)
            and not topic):
        plan.intent = Intent.SUMMARY        # synthesis report first (narrowed to the topic, if any), SQL summary otherwise
        plan.topic = topic
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

    # "rewrote" usually means changed in general, not the pipeline's Reworded label (same content, new wording).
    if re.search(r"\brewr(?:o|i)te\w*|\brewritten\b", text) and "Reworded" not in filters.change_types:
        filters.clarifications.append("Showing all changes. Ask for \"reworded\" disclosures to see only wording "
                                      "changes (same content, new wording).")
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
