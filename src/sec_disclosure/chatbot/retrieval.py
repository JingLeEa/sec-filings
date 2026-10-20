"""Topic retrieval: pgvector cosine search when vectors exist, PostgreSQL full-text search always.

Sources, in order of preference, fused with reciprocal rank fusion (RRF):
  1. ``chatbot_evidence_embeddings``  - bge-m3 vectors of each alignment's evidence (scripts/embed_alignment_evidence.py)
  2. ``disclosure_embeddings``        - bge-m3 vectors of full disclosures (pipeline's semantic-retrieval stage),
                                        mapped to alignments through ``alignment_members``
  3. full-text search over evidence sentences, summaries, explanations and key changes (needs no API)

Vector search needs the question embedded with the same model, so it only runs when SoCLaaS embedding
settings are available. Without them the chatbot still answers topic questions with full-text search and
says so in ``RetrievalResult.method``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .db import Database, DatabaseError
from .entities import QueryFilters
from .queries import SqlQuery, where_clause

Embedder = Callable[[list[str]], list[list[float]]]
RRF_K = 60

# Small domain thesaurus so keyword search finds the vocabulary filings actually use.
SYNONYMS: dict[str, list[str]] = {
    "climate": ["climate", "environmental", "sustainability", "emissions", "carbon", "net-zero", "transition risk",
                "physical risk", "weather"],
    "ai": ["artificial intelligence", "AI", "machine learning", "generative", "model risk"],
    "cyber": ["cybersecurity", "cyber", "cyberattack", "information security", "data breach", "ransomware"],
    "cybersecurity": ["cybersecurity", "cyber", "cyberattack", "information security", "data breach", "ransomware"],
    "geopolitical": ["geopolitical", "war", "conflict", "sanctions", "Ukraine", "Middle East", "tariffs"],
    "interest": ["interest rate", "rates", "net interest income", "monetary policy"],
    "rates": ["interest rate", "rates", "net interest income", "monetary policy"],
    "china": ["China", "Chinese", "export controls", "export restrictions"],
    "tariffs": ["tariffs", "trade policy", "trade restrictions", "export controls"],
    "capital": ["capital", "Basel", "capital requirements", "CET1", "stress capital buffer"],
    "credit": ["credit risk", "credit losses", "allowance", "delinquencies", "charge-offs"],
    "liquidity": ["liquidity", "funding", "deposits", "LCR"],
    "workforce": ["employees", "workforce", "headcount", "talent", "restructuring"],
    "layoffs": ["restructuring", "workforce reduction", "headcount", "severance"],
    "supply": ["supply chain", "suppliers", "third parties", "sourcing"],
    "foundry": ["foundry", "Intel Foundry", "manufacturing", "fabs", "process technology"],
    "regulation": ["regulation", "regulatory", "supervisory", "compliance", "rulemaking"],
    "litigation": ["litigation", "lawsuit", "legal proceedings", "settlement", "enforcement"],
}

FTS_DOCUMENT = """
    setweight(to_tsvector('english', coalesce(key_change, '') || ' ' || coalesce(current_summary, '') || ' ' ||
                                     coalesce(previous_summary, '') || ' ' || coalesce(section, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(explanation, '') || ' ' || coalesce(materiality_reason, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(current_text, '') || ' ' || coalesce(previous_text, '')), 'C')"""


@dataclass
class RetrievalResult:
    method: str
    hits: list[tuple[int, str, float]] = field(default_factory=list)   # (run_id, match_id, fused score)
    queries: list[SqlQuery] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def expand_terms(topic: str) -> list[str]:
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9&'-]+", topic.lower()):
        for term in SYNONYMS.get(word, [word]):
            if term.lower() not in (t.lower() for t in terms):
                terms.append(term)
    phrase = topic.strip()
    if " " in phrase and phrase.lower() not in (t.lower() for t in terms):
        terms.insert(0, phrase)
    return terms


def tsquery_text(terms: list[str]) -> str:
    """OR of terms; multi-word terms become phrase matches. Only letters/digits reach to_tsquery."""
    parts = []
    for term in terms:
        words = re.findall(r"[a-z0-9]+", term.lower())
        if not words:
            continue
        parts.append(" <-> ".join(words) if len(words) > 1 else words[0])
    return " | ".join(f"({p})" for p in parts)


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


class Retriever:
    def __init__(self, db: Database, embedder: Embedder | None = None, *, model_name: str = "bge-m3"):
        self.db = db
        self.embedder = embedder
        self.model_name = model_name

    def search(self, topic: str, filters: QueryFilters, *, k: int = 25, exclude_unchanged: bool = False) -> RetrievalResult:
        where, params = where_clause(filters, alias="v", skip=("disclosure_taxonomies", "match_ids"))
        if exclude_unchanged and not filters.change_types:
            where += " AND v.change_type IS DISTINCT FROM 'Unchanged'"
        ranked_lists: list[list[tuple[int, str, float]]] = []
        methods: list[str] = []
        result = RetrievalResult(method="")

        vector = self._embed(topic, result)
        if vector is not None:
            for source, query in self._vector_queries(vector, where, params, k):
                try:
                    rows = self.db.query(query.name, query.sql, query.params).rows
                except DatabaseError as error:
                    result.notes.append(f"{source} vector search skipped: {error}")
                    continue
                result.queries.append(query)
                if rows:
                    ranked_lists.append([(r["run_id"], r["match_id"], float(r["score"])) for r in rows])
                    methods.append(f"pgvector:{source}")

        terms = expand_terms(topic)
        tsq = tsquery_text(terms)
        if tsq:
            fts = SqlQuery("topic_fulltext", f"""
                SELECT v.run_id, v.match_id, ts_rank_cd({FTS_DOCUMENT.replace('coalesce(', 'coalesce(v.')},
                                                        to_tsquery('english', %(tsq)s), 32) AS score
                FROM alignment_changes v
                WHERE {where} AND ({FTS_DOCUMENT.replace('coalesce(', 'coalesce(v.')}) @@ to_tsquery('english', %(tsq)s)
                ORDER BY score DESC, v.match_id
                LIMIT {int(k)}""", {**params, "tsq": tsq})
            rows = self.db.query(fts.name, fts.sql, fts.params).rows
            result.queries.append(fts)
            if rows:
                ranked_lists.append([(r["run_id"], r["match_id"], float(r["score"])) for r in rows])
                methods.append("full_text")
            result.notes.append("search terms: " + ", ".join(terms))

        result.hits = reciprocal_rank_fusion(ranked_lists)[:k]
        result.method = " + ".join(methods) if methods else "no matches"
        return result

    def _embed(self, topic: str, result: RetrievalResult) -> list[float] | None:
        if self.embedder is None:
            result.notes.append("vector search off: no embedding endpoint configured (SOCLAAS_*), using full-text search")
            return None
        try:
            sources = self.db.embedding_sources()
        except DatabaseError:
            sources = {}
        if not any(sources.values()):
            result.notes.append("vector search off: no vectors in chatbot_evidence_embeddings or disclosure_embeddings "
                                "(run scripts/embed_alignment_evidence.py)")
            return None
        try:
            vector = self.embedder([topic])[0]
        except Exception as error:  # LLMError messages are already credential-free
            result.notes.append(f"vector search off: embedding the question failed ({type(error).__name__})")
            return None
        return vector if vector and any(vector) else None

    def _vector_queries(self, vector: list[float], where: str, params: dict, k: int):
        base = {**params, "qvec": vector_literal(vector), "model": self.model_name}
        yield "evidence", SqlQuery("topic_vector_evidence", f"""
            SELECT e.run_id, e.match_id, max(1 - (e.embedding <=> %(qvec)s::vector)) AS score
            FROM chatbot_evidence_embeddings e
            JOIN embedding_models m ON m.model_id = e.model_id AND m.name = %(model)s
            JOIN alignment_changes v ON v.run_id = e.run_id AND v.match_id = e.match_id
            WHERE {where}
            GROUP BY 1, 2 ORDER BY score DESC LIMIT {int(k)}""", base)
        yield "disclosures", SqlQuery("topic_vector_disclosures", f"""
            SELECT am.run_id, am.match_id, max(1 - (de.embedding <=> %(qvec)s::vector)) AS score
            FROM disclosure_embeddings de
            JOIN embedding_models m ON m.model_id = de.model_id AND m.name = %(model)s
            JOIN alignment_members am ON am.disclosure_id = de.disclosure_id
            JOIN alignment_changes v ON v.run_id = am.run_id AND v.match_id = am.match_id
            WHERE de.field = 'content' AND {where}
            GROUP BY 1, 2 ORDER BY score DESC LIMIT {int(k)}""", base)


def reciprocal_rank_fusion(lists: list[list[tuple[int, str, float]]]) -> list[tuple[int, str, float]]:
    if len(lists) == 1:
        return lists[0]
    scores: dict[tuple[int, str], float] = {}
    for ranked in lists:
        for rank, (run_id, match_id, _) in enumerate(ranked, start=1):
            scores[(run_id, match_id)] = scores.get((run_id, match_id), 0.0) + 1.0 / (RRF_K + rank)
    return [(r, m, s) for (r, m), s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][1]))]
