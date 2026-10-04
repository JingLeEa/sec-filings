"""Embedding-backed retrieval, drop-in for the TF-IDF DisclosureIndex.

Same public surface as indexing.disclosure_retrieval.DisclosureIndex -
`search(...)` and `candidates(...)` with the same arguments and the same hit
shape - so agents/disclosure_alignment.py picks it up by swapping the
constructor and nothing else.

Every scoring decision already encoded in the TF-IDF index is mirrored
deliberately: the 0.35 summary / 0.65 content blend, the 0.02 same-taxonomy and
0.03 section bonuses applied only when a base score exists, Item-scoped search,
and forward plus reverse retrieval for split/merge coverage. Holding those
constant means any later comparison between the two indexes measures the
similarity function and nothing else.

What changes is the similarity itself: cosine over dense embeddings instead of
over sparse term weights, so two disclosures that share meaning but little
vocabulary score as related. That gap is the reason this module exists.
"""

from __future__ import annotations

from typing import Callable, Protocol

import numpy as np

Embedder = Callable[[list[str]], list[list[float]]]

SUMMARY_WEIGHT = 0.35
CONTENT_WEIGHT = 0.65
TAXONOMY_BONUS = 0.02
SECTION_BONUS = 0.03


class _HasSearch(Protocol):
    def search(self, query: str, year: str, **kwargs) -> list[dict]: ...


def _matrix(vectors: list[list[float]]) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(1, -1) if array.size else np.zeros((len(vectors), 0), dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    # A zero vector (an absent summary) must stay zero, not become NaN.
    norms[norms == 0] = 1.0
    return array / norms


class SemanticIndex:
    """Dense-vector retrieval over disclosure records."""

    def __init__(self, records: dict[str, dict], *, embedder: Embedder):
        self.records = records
        self.keys = list(records)
        self.position = {key: index for index, key in enumerate(self.keys)}

        summaries = [str(records[key].get("summary", "") or "") for key in self.keys]
        contents = [str(records[key]["content"]) for key in self.keys]
        sections = [str(records[key].get("section", "") or "") for key in self.keys]

        # One embedder call for everything: the cache dedupes, and section
        # strings repeat heavily across a filing so they cost almost nothing.
        combined = embedder(summaries + contents + sections) if self.keys else []
        size = len(self.keys)
        self.summary = _matrix(combined[:size]) if size else np.zeros((0, 0), np.float32)
        self.content = _matrix(combined[size:2 * size]) if size else np.zeros((0, 0), np.float32)
        self.section = _matrix(combined[2 * size:]) if size else np.zeros((0, 0), np.float32)
        self._embedder = embedder

    # -- vectors for an arbitrary query or anchor ----------------------------

    def _vectors_for(self, anchor: dict | None, query: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if anchor is not None:
            key = anchor.get("disclosure_id")
            if key in self.position:
                index = self.position[key]
                return (self.summary[index], self.content[index], self.section[index])
            texts = [str(anchor.get("summary", "") or ""), str(anchor["content"]),
                     str(anchor.get("section", "") or "")]
        else:
            texts = [query, query, ""]
        summary, content, section = _matrix(self._embedder(texts))
        return summary, content, section

    # -- same API as DisclosureIndex -----------------------------------------

    def search(self, query: str, year: str, *, item: str | None = None, anchor: dict | None = None,
               limit: int = 5, offset: int = 0) -> list[dict]:
        if anchor is not None:
            if item is not None and item != anchor["item"]:
                raise ValueError("Search Item must match the anchor's Item.")
            item = anchor["item"]
        if not isinstance(item, str) or not item:
            raise ValueError("Search requires a specific SEC Item.")
        if not self.keys:
            return []

        summary, content, section = self._vectors_for(anchor, query)
        if anchor is None:
            section = np.zeros_like(section)

        summary_scores = self.summary @ summary
        content_scores = self.content @ content
        section_scores = self.section @ section
        base_scores = SUMMARY_WEIGHT * summary_scores + CONTENT_WEIGHT * content_scores

        hits: list[dict] = []
        for index, key in enumerate(self.keys):
            record = self.records[key]
            if record["fiscal_year"] != year or record["item"] != item:
                continue
            base = float(base_scores[index])
            bonus = 0.0
            if anchor and base > 0:
                bonus = (TAXONOMY_BONUS * (anchor.get("taxonomy") == record.get("taxonomy"))
                         + SECTION_BONUS * float(section_scores[index]))
            hits.append({"disclosure_id": key, "score": round(base + bonus, 6),
                         "text_score": round(base, 6), "same_item": True})
        return sorted(hits, key=lambda hit: (-hit["score"], hit["disclosure_id"]))[offset:offset + limit]

    def candidates(self, previous_year: str, current_year: str, top_k: int = 5) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        for key, record in self.records.items():
            if record["fiscal_year"] != current_year:
                continue
            hits = self.search("", previous_year, anchor={**record, "disclosure_id": key},
                               limit=len(self.records))
            result[key] = hits[:top_k]
        # Reverse retrieval improves split/merge coverage without extra API calls.
        for key, record in self.records.items():
            if record["fiscal_year"] != previous_year:
                continue
            for hit in self.search("", current_year, anchor={**record, "disclosure_id": key}, limit=2):
                bucket = result.get(hit["disclosure_id"])
                if bucket is None:
                    continue
                if hit["text_score"] > 0 and not any(h["disclosure_id"] == key for h in bucket):
                    bucket.append({**hit, "disclosure_id": key, "retrieval_direction": "reverse"})
        return result
