"""Shared Streamlit pieces: cached loading, the sidebar comparison picker, filters, badges, styling."""
from __future__ import annotations

import streamlit as st

from . import config, data, diff, provenance, review

PERSISTED_KEYS = [
    "sel_company", "sel_pair", "f_items", "f_categories", "f_changes", "f_materiality",
    "f_statuses", "f_relationships", "f_text", "selected_match",
    "manual_section", "review_scope",
]

CSS = """
<style>
:root { --del-bg: rgba(220, 38, 38, 0.18); --ins-bg: rgba(22, 163, 74, 0.20); --chip-bg: rgba(128,128,128,0.16); }
.diffbox { white-space: pre-wrap; line-height: 1.6; padding: 0.75rem 1rem; border: 1px solid rgba(128,128,128,0.35);
           border-radius: 8px; font-size: 0.95rem; overflow-wrap: anywhere; }
.diffbox .del { background: var(--del-bg); text-decoration: line-through; text-decoration-thickness: 1px; border-radius: 3px; }
.diffbox .ins { background: var(--ins-bg); text-decoration: underline; border-radius: 3px; }
.diffbox.empty { font-style: italic; opacity: 0.8; }
.chip { display: inline-block; padding: 0.1rem 0.6rem; margin: 0 0.35rem 0.35rem 0; border-radius: 999px;
        background: var(--chip-bg); font-size: 0.82rem; border: 1px solid rgba(128,128,128,0.3); }
.chip.ok { border-color: rgba(22,163,74,0.7); }
.chip.warn { border-color: rgba(217,119,6,0.8); }
.chip.accent { border-color: rgba(37,99,235,0.8); }
.chip.bad { border-color: rgba(220,38,38,0.7); }
.sidehead { font-weight: 600; margin-bottom: 0.25rem; }
.sourcehead { margin: 0.85rem 0 0.3rem; font-size: 0.82rem; overflow-wrap: anywhere; }
.sourcehead .source-label { opacity: 0.7; }
</style>
"""


def setup(title: str):
    """Call first on every page."""
    st.set_page_config(page_title=f"{title} | SEC Disclosure Comparison", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    persist()


def persist():
    """Streamlit forgets widget values for widgets that are not on the current page.
    Re-assigning them at the top of each page keeps the choices when moving between pages."""
    for key in PERSISTED_KEYS:
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


@st.cache_data(show_spinner="Looking for data...", ttl=300)
def cached_source():
    return data.discover_source()


@st.cache_data(show_spinner="Loading comparison...", ttl=900, max_entries=8)
def cached_comparison(company: str, previous: int, current: int, mode: str):
    return data.load_comparison(company, previous, current, cached_source(), apply_reviews=False)


def safe(text) -> str:
    """Escape dollar signs so Streamlit markdown does not treat $...$ as maths."""
    return str(text or "").replace("$", "\\$")


def chip(text: str, tone: str = "") -> str:
    from html import escape
    return f'<span class="chip {tone}">{escape(str(text))}</span>'


def chips(*items) -> None:
    st.markdown("".join(chip(*i) if isinstance(i, tuple) else chip(i) for i in items), unsafe_allow_html=True)


def tone_for_status(status: str) -> str:
    return {"ai_verified": "ok", "auto_matched": "ok", "needs_review": "warn", "unmatched": "bad"}.get(status, "")


def refresh_button():
    if st.sidebar.button("Reload data", help="Clear cached files and look again", width="stretch"):
        st.cache_data.clear()
        st.session_state.pop("_manual_review_context", None)
        st.session_state.pop("_disclosure_review_context", None)
        st.rerun()


def data_source_notice(source) -> None:
    for note in source.notes:
        st.sidebar.caption(note)


def comparison_picker():
    """Sidebar company and year-pair selector. Returns (source, company, previous, current) or stops the page."""
    st.sidebar.header("Comparison")
    try:
        source = cached_source()
    except Exception as error:  # noqa: BLE001 - show anything unexpected as a message, not a stack trace
        st.error(f"Could not look for data: {error}")
        st.stop()
    if not source.companies:
        st.sidebar.warning("No data found")
        empty_state_no_data(source)
        st.stop()
    companies = source.companies
    if st.session_state.get("sel_company") not in companies:
        st.session_state.pop("sel_company", None)
    company = st.sidebar.selectbox("Company", companies, key="sel_company", format_func=config.company_label)
    pairs = source.pairs(company)
    labels = {f"FY{p} to FY{c}": (p, c) for p, c in pairs}
    if st.session_state.get("sel_pair") not in labels:
        st.session_state.pop("sel_pair", None)
    pair_label = st.sidebar.selectbox("Filing years", list(labels), key="sel_pair", index=len(labels) - 1)
    previous, current = labels[pair_label]
    modes = {source.scopes.get((company, previous, current), "files")}
    st.sidebar.caption("Data from " + ("PostgreSQL" if "postgres" in modes else "JSON files"))
    data_source_notice(source)
    refresh_button()
    return source, company, previous, current


def empty_state_no_data(source=None) -> None:
    st.error("No comparison data was found.")
    st.markdown(
        "The app reads aligned disclosures either from JSON files or from PostgreSQL.\n\n"
        "- **Files:** get the `test-data` branch (`git worktree add ../sec-filings-test-data origin/test-data`) "
        "or set `SEC_UI_ALIGNMENTS_DIR` and `SEC_UI_DISCLOSURES_DIR`.\n"
        "- **PostgreSQL:** start the database (`docker compose up -d`), load an alignment run, and set "
        "`SEC_UI_DATA_SOURCE=postgres`."
    )
    if source is not None:
        for note in source.notes:
            st.caption(note)


def load_selected(source, company, previous, current):
    """Load one comparison with friendly messages. Stops the page on failure."""
    mode = source.scopes.get((company, previous, current), "files")
    try:
        return review.apply_final_choices(cached_comparison(company, previous, current, mode))
    except data.DataError as error:
        st.error(f"Could not load this comparison. {error}")
        st.stop()
    except Exception as error:  # noqa: BLE001
        st.error(f"Something went wrong while loading this comparison: {error}")
        st.stop()


def filters(records: list, prefix_help: bool = True):
    """Sidebar filters shared by the explorer and the side-by-side page."""
    st.sidebar.header("Filters")
    items = sorted({r["item"] for r in records if r["item"]}, key=_item_order)
    categories = [c for c in config.CONTENT_CATEGORIES + [data.NOT_CLASSIFIED]
                  if any(c in (r["categories"] or [data.NOT_CLASSIFIED]) for r in records)]
    changes = [c for c in config.CHANGE_LABELS + [data.NOT_LABELLED] if any(r["change_label"] == c for r in records)]
    statuses = [s for s in data.STATUS_LABELS if any(r["status"] == s for r in records)]
    relationships = [s for s in data.RELATIONSHIP_LABELS if any(r["relationship"] == s for r in records)]
    _clean("f_items", items)
    _clean("f_categories", categories)
    _clean("f_changes", changes)
    _clean("f_statuses", statuses)
    _clean("f_relationships", relationships)
    _clean("f_materiality", ["Material", "Not material", data.NOT_ASSESSED])
    sel_items = st.sidebar.multiselect("SEC Item", items, key="f_items", format_func=lambda i: config.ITEM_TITLES.get(i, f"Item {i}"))
    sel_categories = st.sidebar.multiselect("Content category", categories, key="f_categories")
    sel_changes = st.sidebar.multiselect("Change label", changes, key="f_changes")
    sel_materiality = st.sidebar.multiselect("Materiality", ["Material", "Not material", data.NOT_ASSESSED], key="f_materiality")
    with st.sidebar.expander("More filters"):
        sel_statuses = st.multiselect("Match status", statuses, key="f_statuses", format_func=lambda s: data.STATUS_LABELS[s])
        sel_relationships = st.multiselect("Match type", relationships, key="f_relationships",
                                           format_func=lambda s: data.RELATIONSHIP_LABELS[s])
        text = st.text_input("Search text", key="f_text", placeholder="word or phrase")
    st.sidebar.button("Clear filters", width="stretch", on_click=_clear_filters)
    return data.filter_records(records, items=sel_items, categories=sel_categories, change_labels=sel_changes,
                               materiality=sel_materiality, statuses=sel_statuses, relationships=sel_relationships, text=text)


def _clear_filters():
    for key in ("f_items", "f_categories", "f_changes", "f_materiality", "f_statuses", "f_relationships"):
        st.session_state[key] = []
    st.session_state["f_text"] = ""


def _clean(key, options):
    """Drop remembered choices that do not exist for the current company."""
    if key in st.session_state and isinstance(st.session_state[key], list):
        st.session_state[key] = [v for v in st.session_state[key] if v in options]


def _item_order(item: str):
    digits = "".join(ch for ch in item if ch.isdigit())
    return (int(digits) if digits else 99, item)


def side_by_side(record: dict, previous: int, current: int, *, key: str):
    """Shared year columns and highlights for reading and manual review."""
    from html import escape

    show_marks = st.toggle("Highlight changes", value=True, key=key)
    previous_blocks = provenance.source_blocks(record, "previous")
    current_blocks = provenance.source_blocks(record, "current")
    missing = record["missing_side"]
    if missing == "later":
        prev_html = [diff.whole_side_html(record["previous_text"][b["start"]:b["end"]], "removed") for b in previous_blocks]
        curr_html = []
        stats = "The whole earlier disclosure has no counterpart in the later filing."
    elif missing == "earlier":
        prev_html = []
        curr_html = [diff.whole_side_html(record["current_text"][b["start"]:b["end"]], "added") for b in current_blocks]
        stats = "The whole later disclosure has no counterpart in the earlier filing."
    else:
        result = diff.diff_texts(record["previous_text"], record["current_text"],
                                 previous_ranges=[(b["start"], b["end"]) for b in previous_blocks],
                                 current_ranges=[(b["start"], b["end"]) for b in current_blocks])
        prev_html, curr_html = result["previous_blocks_html"], result["current_blocks_html"]
        stats = ("The wording is the same apart from spacing." if result["identical"]
                 else f"{result['removed_words']:,} words removed, {result['added_words']:,} words added.")
    if not show_marks:
        prev_html = [diff.plain_html(record["previous_text"][b["start"]:b["end"]]) for b in previous_blocks]
        curr_html = [diff.plain_html(record["current_text"][b["start"]:b["end"]]) for b in current_blocks]

    left, right = st.columns(2)
    for column, title, blocks, bodies, empty_msg in (
        (left, f"FY{previous} (earlier)", previous_blocks, prev_html, f"No counterpart found in the FY{previous} filing."),
        (right, f"FY{current} (later)", current_blocks, curr_html, f"No counterpart found in the FY{current} filing."),
    ):
        with column:
            st.markdown(f'<div class="sidehead">{title}</div>', unsafe_allow_html=True)
            if not blocks:
                st.markdown(f'<div class="diffbox empty">{empty_msg}</div>', unsafe_allow_html=True)
            for block, body in zip(blocks, bodies):
                owner = (f'<span class="source-label">Disclosure</span> <code>{escape(block["disclosure_id"])}</code>'
                         if block["disclosure_id"] else "Disclosure attribution unavailable")
                paragraph = (f'<span class="source-label">Paragraph</span> <code>{escape(block["paragraph_id"])}</code>'
                             if block["paragraph_id"] else '<span class="source-label">Paragraph ID unavailable</span>')
                st.markdown(f'<div class="sourcehead">{owner} · {paragraph}</div>', unsafe_allow_html=True)
                if body.strip():
                    st.markdown(f'<div class="diffbox">{body}</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="diffbox empty">Disclosure text is unavailable for this filing.</div>',
                                unsafe_allow_html=True)
    if record["text_source"] == "cited sentences":
        st.warning("The full disclosure text was not found, so this view shows only the sentences cited as evidence. "
                   "The comparison may be partial. Point SEC_UI_DISCLOSURES_DIR at the disclosures folder to see everything.")
    st.caption(f"{stats}  Red with strike-through = removed. Green with underline = added.")
    if missing:
        st.info("No counterpart found means the saved comparison for this pair found none. "
                "It is not proof that the topic is missing from the whole filing.")


def supporting_evidence(record: dict, previous: int, current: int, *, include_citations=True):
    import pandas as pd

    st.subheader("Supporting evidence")
    sentences = provenance.sentence_rows(record)
    if sentences:
        st.caption("Each sentence lists its disclosure and paragraph."
                   + (" Cited marks sentences used in the alignment reasoning." if include_citations else ""))
        columns = ["side", "disclosure_id", "paragraph_id", "sentence_id", "text"]
        frame = pd.DataFrame(sentences)[columns + (["cited"] if include_citations else []) + ["source_url"]]
        frame["side"] = frame["side"].map({"earlier": f"FY{previous}", "later": f"FY{current}"}).fillna(frame["side"])
        st.dataframe(frame, hide_index=True, width="stretch", column_config={
            "side": st.column_config.TextColumn("Filing", width="small"),
            "disclosure_id": st.column_config.TextColumn("Disclosure ID"),
            "paragraph_id": st.column_config.TextColumn("Paragraph ID"),
            "sentence_id": st.column_config.TextColumn("Sentence ID"),
            "text": st.column_config.TextColumn("Sentence", width="large"),
            "cited": st.column_config.CheckboxColumn("Cited", width="small"),
            "source_url": st.column_config.LinkColumn("SEC filing", display_text="Open on sec.gov"),
        })
    else:
        st.info("No sentence-level evidence was saved for this match.")

    with st.expander("Disclosures in this match"):
        for side, label in (("previous_members", f"FY{previous}"), ("current_members", f"FY{current}")):
            for member in record[side]:
                st.markdown(f"**{label}** `{member.get('disclosure_id', '')}` Item {member.get('item', '')}, "
                            f"{member.get('section', '') or 'no section'}")
                st.caption(safe(member.get("summary")) or "No summary saved.")


def disclosure_text(disclosure):
    """One filing's selected text with the same source IDs used in comparisons."""
    from html import escape

    view = {"previous_members": [], "current_members": [disclosure],
            "current_text": disclosure.get("content") or "", "evidence": []}
    for block in provenance.source_blocks(view, "current"):
        paragraph = escape(block["paragraph_id"]) if block["paragraph_id"] else "unavailable"
        st.markdown(f'<div class="sourcehead"><span class="source-label">Disclosure</span> '
                    f'<code>{escape(block["disclosure_id"])}</code> · <span class="source-label">Paragraph</span> '
                    f'<code>{paragraph}</code></div>', unsafe_allow_html=True)
        text = view["current_text"][block["start"]:block["end"]]
        st.markdown(f'<div class="diffbox">{diff.plain_html(text)}</div>', unsafe_allow_html=True)
    with st.expander("Sentence sources"):
        supporting_evidence(view, disclosure["fiscal_year"], disclosure["fiscal_year"], include_citations=False)
