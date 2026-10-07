"""Lexical metrics and a lightweight taxonomy for aligned disclosures."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


TOKEN_RE = re.compile(r"\b[\w]+(?:['’][\w]+)*\b", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Return case-folded word tokens, preserving numbers and contractions."""
    return [token.casefold() for token in TOKEN_RE.findall(text or "")]


def classify_change(sequence_match_ratio: float, cosine: float, length_ratio: float) -> str:
    """Apply the requested ordered lexical taxonomy heuristic."""
    if sequence_match_ratio >= 0.95 or cosine >= 0.95 or (
        sequence_match_ratio >= 0.85 and 0.75 < length_ratio < 1.25
    ):
        return "Reworded"
    if length_ratio >= 1.25:
        return "Expanded"
    if length_ratio <= 0.75:
        return "Reduced"
    return "Modified"


def compute_lexical_metrics(prev_text: str, curr_text: str) -> dict:
    """Compare two texts and return lexical scores plus bounded token samples."""
    prev_tokens = tokenize(prev_text)
    curr_tokens = tokenize(curr_text)
    
    len_prev = len(prev_tokens)
    len_curr = len(curr_tokens)

    # 1. Edge Case: Brand new disclosure (added)
    if not prev_tokens and curr_tokens:
        return {
            "method": "tfidf_difflib_hybrid",
            "change_taxonomy": "New",
            "cosine_similarity": 0.0,
            "sequence_match_ratio": 0.0,
            "length_ratio": 0.0,
            "word_count_delta": len_curr,
            "added_tokens_sample": curr_tokens[:20],
            "removed_tokens_sample": [],
        }

    # 2. Edge Case: Completely excised disclosure (removed)
    if prev_tokens and not curr_tokens:
        return {
            "method": "tfidf_difflib_hybrid",
            "change_taxonomy": "Removed",
            "cosine_similarity": 0.0,
            "sequence_match_ratio": 0.0,
            "length_ratio": 0.0,
            "word_count_delta": -len_prev,
            "added_tokens_sample": [],
            "removed_tokens_sample": prev_tokens[:20],
        }