"""Content and styling for the home page. Kept apart from components.py so the pages stay independent.

Colours are fixed hex values with transparent fills, so they read well on both light and dark themes.
Meaning is never carried by colour alone: every coloured item also has a text label.
"""
from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st

PAGES_DIR = Path(__file__).resolve().parents[1] / "pages"

BLUE, TEAL, PURPLE, AMBER, GREEN, ROSE, INDIGO, GREY = (
    "#2563eb", "#0d9488", "#7c3aed", "#d97706", "#16a34a", "#e11d48", "#4f46e5", "#64748b")

# One entry per page the home page can point to. Pages that do not exist on this branch are skipped.
FEATURES = [
    {"key": "run", "cta": "Run pipeline", "file": "0_Run_Pipeline.py", "icon": ":material/play_circle:", "color": BLUE,
     "title": "Run the pipeline", "tagline": "Add a company or a new year",
     "bullets": ["Pick a company and fiscal years", "Watch each of the six steps run",
                 "Resume or retry if something fails"]},
    {"key": "explorer", "cta": "Open the explorer", "file": "1_Change_Explorer.py", "icon": ":material/table_rows:", "color": TEAL,
     "title": "Explore changes", "tagline": "Find what changed between two years",
     "bullets": ["Filter by section, topic or change", "See which changes are material",
                 "Download the results as CSV or JSON"]},
    {"key": "side", "cta": "Open side by side", "file": "2_Side_by_Side.py", "icon": ":material/compare:", "color": PURPLE,
     "title": "Compare side by side", "tagline": "Read both years' text together",
     "bullets": ["Added words green, removed words red", "Sentence IDs and SEC links",
                 "Clear note when a year has no match"]},
    {"key": "synthesis", "cta": "Open the report", "file": "3_Synthesis_Report.py", "icon": ":material/summarize:", "color": AMBER,
     "title": "Read the synthesis report", "tagline": "The multi-year story for one company",
     "bullets": ["Key findings and financial figures", "Every finding links to its evidence",
                 "Download the report"]},
    {"key": "peer", "cta": "Open peer comparison", "file": "4_Peer_Comparison.py", "icon": ":material/groups:", "color": GREEN,
     "title": "Compare with peers", "tagline": "How competitors disclose a topic",
     "bullets": ["Same year and same SEC Item only", "Cited sentences from both sides",
                 "No match does not mean silence"]},
    {"key": "review", "cta": "Start reviewing", "file": "5_Manual_Review.py", "icon": ":material/fact_check:", "color": ROSE,
     "title": "Review the AI's work", "tagline": "Check and correct labels",
     "bullets": ["Start with matches the AI doubted", "Change a category or change label",
                 "Your decision updates the reports"]},
    {"key": "chat", "cta": "Open the chatbot", "file": "6_Chatbot.py", "icon": ":material/forum:", "color": INDIGO,
     "title": "Ask the chatbot", "tagline": "Questions in plain English",
     "bullets": ["Answers cite their source match", "Open any citation side by side",
                 "Says so when evidence is thin"]},
]

CHANGE_LABEL_INFO = {
    "New": (GREEN, "Only in the later filing. The company added this disclosure."),
    "Removed": ("#dc2626", "Only in the earlier filing. The company dropped this disclosure."),
    "Expanded": (BLUE, "Same points, with more detail added."),
    "Reduced": (AMBER, "Same points, with detail taken out."),
    "Modified": (PURPLE, "Facts, numbers, dates or risks changed."),
    "Reworded": (TEAL, "Different words, same meaning."),
    "Unchanged": (GREY, "Same text, apart from spacing or bullets."),
    "Not labelled": ("#94a3b8", "No change label yet, usually because the match still needs review."),
}

STATUS_INFO = {
    "ai_verified": (GREEN, "AI verified", "The AI checked the match and the sentence evidence. A person has not."),
    "auto_matched": (GREEN, "Auto matched", "The text is identical, so no AI check was needed."),
    "unmatched": ("#dc2626", "Unmatched", "The AI found no counterpart in the other year."),
    "needs_review": (AMBER, "Needs review", "The match could not be confirmed. Worth a human look."),
}

CSS = """
.hero { padding: 1.4rem 1.6rem; border-radius: 14px; margin-bottom: 1rem;
        border: 1px solid rgba(128,128,128,0.28);
        background: linear-gradient(135deg, rgba(37,99,235,0.13), rgba(124,58,237,0.10) 55%, rgba(13,148,136,0.10)); }
.hero h1 { margin: 0 0 0.3rem 0; padding: 0; font-size: 2.1rem; line-height: 1.2; }
.hero p { margin: 0; font-size: 1.05rem; opacity: 0.9; }
.steps { display: flex; flex-wrap: wrap; gap: 0.6rem; margin-top: 1rem; }
.step { display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.8rem 0.35rem 0.4rem; border-radius: 999px;
        background: rgba(128,128,128,0.12); font-size: 0.92rem; }
.step b { display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem;
          border-radius: 50%; color: #fff; font-size: 0.85rem; }
.dotlist { list-style: none; margin: 0.4rem 0 0.2rem 0; padding: 0; }
.dotlist li { position: relative; padding-left: 1.15rem; margin: 0.28rem 0; line-height: 1.4; font-size: 0.93rem; }
.dotlist li::before { content: ""; position: absolute; left: 0; top: 0.5em; width: 0.55rem; height: 0.55rem;
                      border-radius: 50%; background: var(--dot, #64748b); }
.card-title { font-weight: 650; font-size: 1.08rem; margin: 0; }
.card-tag { opacity: 0.75; font-size: 0.88rem; margin: 0 0 0.2rem 0; }
.kpi-help { font-size: 0.9rem; line-height: 1.45; margin: 0.1rem 0 0.5rem 0; }
.kpi-help b { font-weight: 650; }
.pill { display: inline-block; padding: 0.12rem 0.65rem; margin: 0 0.35rem 0.3rem 0; border-radius: 999px; color: #fff;
        font-size: 0.82rem; font-weight: 600; }
.glossrow { display: flex; gap: 0.6rem; align-items: baseline; margin: 0.35rem 0; font-size: 0.93rem; }
.glossrow .pill { flex: 0 0 6.2rem; text-align: center; margin: 0; }
.note { border-left: 4px solid var(--note, #64748b); padding: 0.4rem 0.8rem; margin: 0.4rem 0; font-size: 0.92rem;
        background: rgba(128,128,128,0.08); border-radius: 0 8px 8px 0; }
"""


def feature_css() -> str:
    rules = [CSS]
    for feature in FEATURES:
        rules.append(f".st-key-card_{feature['key']} {{ border-left: 6px solid {feature['color']} !important; }}")
    # Every card gets the same fixed height. Narrower windows wrap the text more, so they get a taller card.
    rules.append(".st-key-card_run, .st-key-card_explorer, .st-key-card_side, .st-key-card_synthesis, .st-key-card_peer, "
                 ".st-key-card_review, .st-key-card_chat { height: 12.5rem !important; flex: 0 0 auto !important; overflow: hidden; }")
    rules.append("@media (max-width: 1400px) { .st-key-card_run, .st-key-card_explorer, .st-key-card_side, .st-key-card_synthesis, "
                 ".st-key-card_peer, .st-key-card_review, .st-key-card_chat { height: 17.5rem !important; } }")
    rules.append("@media (max-width: 1100px) { .st-key-card_run, .st-key-card_explorer, .st-key-card_side, .st-key-card_synthesis, "
                 ".st-key-card_peer, .st-key-card_review, .st-key-card_chat { height: 21.5rem !important; } }")
    for name, color in (("total", BLUE), ("changed", PURPLE), ("material", AMBER), ("review", ROSE)):
        rules.append(f".st-key-kpi_{name} {{ border-top: 5px solid {color} !important; min-height: 20rem; }}")
    rules.append(".st-key-askbox { border: 1px solid rgba(79,70,229,0.55) !important; background: rgba(79,70,229,0.07); }")
    return "<style>\n" + "\n".join(rules) + "\n</style>"


def inject_css() -> None:
    st.markdown(feature_css(), unsafe_allow_html=True)


def available_features() -> list:
    return [f for f in FEATURES if (PAGES_DIR / f["file"]).is_file()]


def has_page(file_name: str) -> bool:
    return (PAGES_DIR / file_name).is_file()


def dot_list(items, color: str) -> str:
    rows = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f'<ul class="dotlist" style="--dot:{color}">{rows}</ul>'


def pill(text: str, color: str) -> str:
    return f'<span class="pill" style="background:{color}">{escape(text)}</span>'


def note(text: str, color: str = GREY) -> str:
    return f'<div class="note" style="--note:{color}">{text}</div>'


def hero_html(steps) -> str:
    """steps: (colour, short title) pairs shown as a numbered route through the app."""
    chips = "".join(f'<span class="step"><b style="background:{c}">{i}</b>{escape(t)}</span>'
                    for i, (c, t) in enumerate(steps, 1))
    return (
        '<div class="hero"><h1>SEC disclosure comparison</h1>'
        "<p>See what a company changed in its yearly 10-K filing, judge how much it matters, "
        "and check the exact sentences behind every finding.</p>"
        f'<div class="steps">{chips}</div></div>'
    )


def saved_synthesis_count():
    """Number of saved synthesis reports (database first, then files), or None if neither can be read."""
    from . import reports
    for name in ("list_synthesis_reports", "list_synthesis_files"):
        function = getattr(reports, name, None)
        if function is None:
            continue
        try:
            return len(function())
        except Exception:  # noqa: BLE001 - a missing database must not break the home page
            continue
    return None


def saved_peer_count():
    """Number of saved peer comparisons (database first, then files), or None if neither can be read."""
    try:
        from . import peer_runs
        return len(peer_runs.saved_reports())
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import reports
        return len(reports.list_peer_files())
    except Exception:  # noqa: BLE001
        return None
