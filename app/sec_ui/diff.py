"""Word-level text comparison used by the side-by-side page. No Streamlit imports.

Removed words are shown with a red background and a strike-through, added words with a green
background and an underline, so the change is clear without relying on colour alone.
"""
from __future__ import annotations

import html
import re
from difflib import SequenceMatcher

# Words (keeping inner punctuation such as $6.5, 1,000 or co-operate), then single punctuation marks, then spaces.
_TOKEN = re.compile(r"\s+|[\w$%][\w$%'\u2019\-./,]*[\w%]|\w|[^\s\w]")


def _tokens(text: str) -> list:
    return _TOKEN.findall(text or "")


def _esc(text: str) -> str:
    """HTML-escape, and write dollar signs as entities so markdown never reads $...$ as maths."""
    return html.escape(text or "").replace("$", "&#36;")


def _wrap(text: str, css: str) -> str:
    return f'<span class="{css}">{_esc(text)}</span>' if text else ""


_BULLETS = "\u25e6\u2022\u25aa\u25cf\u2023\u2043-*\u2013\u2014"


def _key(token: str):
    """Comparison form of a word. Leading bullet marks are ignored and bullet-only tokens are skipped."""
    stripped = token.lstrip(_BULLETS)
    return stripped or None


def _flags(prev_tokens: list, curr_tokens: list, mark: str, first: bool) -> list:
    """One flag per non-space token of one side: the mark for words that differ, otherwise None."""
    prev_words = [t for t in prev_tokens if not t.isspace()]
    curr_words = [t for t in curr_tokens if not t.isspace()]
    prev_idx = [i for i, t in enumerate(prev_words) if _key(t)]
    curr_idx = [j for j, t in enumerate(curr_words) if _key(t)]
    matcher = SequenceMatcher(None, [_key(prev_words[i]) for i in prev_idx], [_key(curr_words[j]) for j in curr_idx],
                              autojunk=False)
    flags = [None] * (len(prev_words) if first else len(curr_words))
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if first and tag in ("delete", "replace"):
            for i in range(i1, i2):
                flags[prev_idx[i]] = mark
        if not first and tag in ("insert", "replace"):
            for j in range(j1, j2):
                flags[curr_idx[j]] = mark
    return flags


def diff_texts(previous: str, current: str) -> dict:
    """Return html for both sides plus counts of added and removed words.

    Whitespace-only differences are not highlighted. The html is escaped, so filing text can
    never inject markup.
    """
    prev_tokens, curr_tokens = _tokens(previous), _tokens(current)
    prev_flags = _flags(prev_tokens, curr_tokens, "del", first=True)
    curr_flags = _flags(prev_tokens, curr_tokens, "ins", first=False)
    return {
        "previous_html": _render(prev_tokens, prev_flags),
        "current_html": _render(curr_tokens, curr_flags),
        "removed_words": sum(1 for f in prev_flags if f),
        "added_words": sum(1 for f in curr_flags if f),
        "identical": not any(prev_flags) and not any(curr_flags),
    }


def _render(tokens: list, flags: list) -> str:
    parts, word_index = [], -1
    pending, pending_flag = [], None

    def flush():
        nonlocal pending, pending_flag
        if pending:
            text = "".join(pending)
            parts.append(_wrap(text, pending_flag) if pending_flag else _esc(text))
        pending, pending_flag = [], None

    for token in tokens:
        if token.isspace():
            # Keep a space inside a highlighted run only if the run continues after it.
            pending.append(token)
            continue
        word_index += 1
        flag = flags[word_index]
        if flag != pending_flag:
            # Move trailing whitespace out of the previous run so highlights do not bleed.
            trailing = ""
            while pending and pending[-1].isspace():
                trailing = pending.pop() + trailing
            flush()
            if trailing:
                parts.append(_esc(trailing))
            pending_flag = flag
        pending.append(token)
    trailing = ""
    while pending and pending[-1].isspace():
        trailing = pending.pop() + trailing
    flush()
    if trailing:
        parts.append(_esc(trailing))
    return "".join(parts)


def plain_html(text: str) -> str:
    return _esc(text)


def whole_side_html(text: str, kind: str) -> str:
    """Highlight an entire side, used when the other side has no counterpart."""
    css = "del" if kind == "removed" else "ins"
    return _wrap(text, css)
