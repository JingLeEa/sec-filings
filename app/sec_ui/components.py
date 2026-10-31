"""Shared Streamlit pieces: cached loading, the sidebar comparison picker, filters, badges, styling."""
from __future__ import annotations

import streamlit as st

from . import config, data

PERSISTED_KEYS = [
    "sel_company", "sel_pair", "f_items", "f_categories", "f_changes", "f_materiality",
    "f_statuses", "f_relationships", "f_text", "selected_match",
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
    return data.load_comparison(company, previous, current, cached_source())


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
        return cached_comparison(company, previous, current, mode)
    except data.DataError as error:
        st.error(f"Could not load this comparison. {error}")
        st.stop()
    except Exception as error:  # noqa: BLE001
        st.error(f"Something went wrong while loading this comparison: {error}")
        st.stop()


def filters(records: list, *, location: str = "sidebar"):
    """Shared filters, shown in the sidebar or in a page panel."""
    if location not in {"sidebar", "page"}:
        raise ValueError("Filter location must be 'sidebar' or 'page'.")
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
    area = st.sidebar if location == "sidebar" else st.container(border=True)
    with area:
        if location == "sidebar":
            st.header("Filters")
            columns = [st] * 4
        else:
            st.markdown("**Filters**")
            columns = st.columns(4)
        sel_items = columns[0].multiselect("SEC Item", items, key="f_items",
                                           format_func=lambda i: config.ITEM_TITLES.get(i, f"Item {i}"))
        sel_categories = columns[1].multiselect("Content category", categories, key="f_categories")
        sel_changes = columns[2].multiselect("Change label", changes, key="f_changes")
        sel_materiality = columns[3].multiselect("Materiality", ["Material", "Not material", data.NOT_ASSESSED],
                                                 key="f_materiality")
        with st.expander("More filters"):
            more = st.columns(3) if location == "page" else [st] * 3
            sel_statuses = more[0].multiselect("Match status", statuses, key="f_statuses",
                                                format_func=lambda s: data.STATUS_LABELS[s])
            sel_relationships = more[1].multiselect("Match type", relationships, key="f_relationships",
                                                     format_func=lambda s: data.RELATIONSHIP_LABELS[s])
            text = more[2].text_input("Search text", key="f_text", placeholder="word or phrase")
        st.button("Clear filters", width="stretch" if location == "sidebar" else "content",
                  on_click=_clear_filters)
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
