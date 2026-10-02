"""Dependency-free TF-IDF retrieval; scores are rankings, not match probabilities."""

from __future__ import annotations

import math
import re
from collections import Counter


STOP = set("a an and are as at be been being by can could did do does for from had has have in into is it its may of on or our should that the their these they this those to was we were which will with would you your".split())


def terms(text: str) -> Counter:
    words = [w for w in re.findall(r"[a-z][a-z0-9]+", text.lower()) if w not in STOP]
    return Counter(words + [a + " " + b for a, b in zip(words, words[1:])])


class Tfidf:
    def __init__(self, documents: dict[str, str]):
        counts = {key: terms(text) for key, text in documents.items()}
        frequencies = Counter(term for count in counts.values() for term in count)
        self.idf = {term: math.log((1 + len(counts)) / (1 + count)) + 1
                    for term, count in frequencies.items()}
        self.vectors = {key: self.vector(count) for key, count in counts.items()}

    def vector(self, text: str | Counter) -> dict[str, float]:
        count = terms(text) if isinstance(text, str) else text
        values = {term: (1 + math.log(n)) * self.idf[term]
                  for term, n in count.items() if term in self.idf}
        norm = math.sqrt(sum(value * value for value in values.values()))
        return {term: value / norm for term, value in values.items()} if norm else {}

    @staticmethod
    def cosine(left: dict, right: dict) -> float:
        if len(left) > len(right):
            left, right = right, left
        return sum(value * right.get(term, 0) for term, value in left.items())


class DisclosureIndex:
    def __init__(self, records: dict[str, dict]):
        self.records = records
        self.summary = Tfidf({key: d.get("summary", "") for key, d in records.items()})
        self.content = Tfidf({key: d["content"] for key, d in records.items()})
        self.section = Tfidf({key: str(d.get("section", "")) for key, d in records.items()})

    def search(self, query: str, year: str, *, item: str | None = None, anchor: dict | None = None,
               limit: int = 5, offset: int = 0) -> list[dict]:
        if anchor is not None:
            if item is not None and item != anchor["item"]:
                raise ValueError("Search Item must match the anchor's Item.")
            item = anchor["item"]
        if not isinstance(item, str) or not item:
            raise ValueError("Search requires a specific SEC Item.")
        if anchor:
            summary = self.summary.vector(anchor.get("summary", ""))
            content = self.content.vector(anchor["content"])
            section = self.section.vector(str(anchor.get("section", "")))
        else:
            summary, content, section = (self.summary.vector(query), self.content.vector(query), {})
        hits = []
        for key, record in self.records.items():
            if record["fiscal_year"] != year or record["item"] != item:
                continue
            s = self.summary.cosine(summary, self.summary.vectors[key])
            c = self.content.cosine(content, self.content.vectors[key])
            base = .35 * s + .65 * c
            # Metadata may reorder relevant results, but cannot create relevance.
            bonus = 0.0
            if anchor and base > 0:
                bonus = (.02 * (anchor.get("taxonomy") == record.get("taxonomy"))
                         + .03 * self.section.cosine(section, self.section.vectors[key]))
            hits.append({"disclosure_id": key, "score": round(base + bonus, 6),
                         "text_score": round(base, 6), "same_item": True})
        return sorted(hits, key=lambda hit: (-hit["score"], hit["disclosure_id"]))[offset:offset + limit]

    def candidates(self, previous_year: str, current_year: str, top_k: int = 5) -> dict[str, list[dict]]:
        result = {}
        for key, record in self.records.items():
            if record["fiscal_year"] != current_year:
                continue
            hits = self.search("", previous_year, anchor=record, limit=len(self.records))
            selected = hits[:top_k]
            result[key] = selected
        # Reverse retrieval improves split/merge coverage without extra API calls.
        for key, record in self.records.items():
            if record["fiscal_year"] != previous_year:
                continue
            for hit in self.search("", current_year, anchor=record, limit=2):
                candidates = result[hit["disclosure_id"]]
                if hit["text_score"] > 0 and not any(h["disclosure_id"] == key for h in candidates):
                    candidates.append({**hit, "disclosure_id": key, "retrieval_direction": "reverse"})
        return result
