"""SEC Disclosure Comparison: home page.

Run with:  streamlit run app/Home.py
"""
import altair as alt
import pandas as pd
import streamlit as st

from sec_ui import components, config, data, landing

components.setup("Home")
landing.inject_css()

CHAT_PAGE = "2_Chatbot.py"
STEPS = [(landing.BLUE, "Prepare a company"), (landing.TEAL, "Explore the changes"), (landing.PURPLE, "Read the evidence"),
         (landing.AMBER, "Get the summary"), (landing.INDIGO, "Ask questions")]

landing.render_intro()
st.markdown(landing.hero_html(STEPS), unsafe_allow_html=True)


# ---------------------------------------------------------------- what do you want to do?

st.subheader("What would you like to do?")
features = landing.available_features()
for start in range(0, len(features), 3):
    row = st.columns(3)
    for column, feature in zip(row, features[start:start + 3]):
        with column.container(border=True, key=f"card_{feature['key']}"):
            st.markdown(
                f'<p class="card-title">{feature["title"]}</p><p class="card-tag">{feature["tagline"]}</p>'
                + landing.dot_list(feature["bullets"], feature["color"]),
                unsafe_allow_html=True,
            )
            st.page_link(f"pages/{feature['file']}", label=feature["cta"], icon=feature["icon"])


# ---------------------------------------------------------------- need data first?

try:
    source = components.cached_source()
except Exception as error:  # noqa: BLE001 - a broken data folder should not blank the whole home page
    st.error(f"Could not look for data: {error}")
    st.stop()

if not source.companies:
    st.divider()
    st.info("No comparison data was found yet, so there is nothing to summarise here.")
    components.empty_state_no_data(source)
    st.stop()

source, company, previous, current = components.comparison_picker()
comparison = components.load_selected(source, company, previous, current)
records, meta = comparison["records"], comparison["meta"]
company_name = config.COMPANY_NAMES.get(company, (company.upper(), ""))[0]


# ---------------------------------------------------------------- ask the chatbot

def _open_chat(question: str):
    st.session_state["chat_prefill"] = question
    st.switch_page(f"pages/{CHAT_PAGE}")


if landing.has_page(CHAT_PAGE):
    with st.container(border=True, key="askbox"):
        st.markdown("**Not sure where to start? Ask a question.**")
        with st.form("home_ask", border=False):
            asked = st.text_input("Your question", label_visibility="collapsed",
                                  placeholder=f"For example: What are the most material changes in {company_name}'s {current} 10-K?")
            submitted = st.form_submit_button("Ask the chatbot", type="primary")
        st.caption("Or try one of these:")
        examples = [f"What are the most material changes in {company_name}'s {current} 10-K?",
                    f"What did {company_name} add or remove between {previous} and {current}?",
                    f"Summarise the key changes for {company_name} from {previous} to {current}"]
        for column, example in zip(st.columns(3), examples):
            if column.button(example, key=f"ex_{example[:24]}", width="stretch"):
                _open_chat(example)
        if submitted and asked.strip():
            _open_chat(asked.strip())
        elif submitted:
            st.warning("Type a question first.")


# ---------------------------------------------------------------- snapshot with explanations

st.divider()
st.subheader(f"Snapshot: {config.company_label(company)}, FY{previous} to FY{current}")
st.caption("Change the company or the years in the sidebar. Each number below says what it counts and has a button to see the matches. "
           "If you just saved a manual review and the numbers look old, press Reload data in the sidebar.")

if not records:
    st.info("This comparison has no records yet. Pick another company or year pair.")
    st.stop()
if not meta.get("run_complete", True):
    st.warning("This alignment run is not marked complete, so some matches may still change.")

total = len(records)
changed = [r for r in records if r["change_label"] not in ("Unchanged", data.NOT_LABELLED)]
material = [r for r in records if r["materiality"] == "Material"]
review = [r for r in records if r["status"] == "needs_review"]


def _share(count: int) -> str:
    return f"{count / total:.0%}" if total else "0%"


def _see(preset: dict):
    """Open the change explorer with only these filters set."""
    for key in ("f_items", "f_categories", "f_changes", "f_materiality", "f_statuses", "f_relationships"):
        st.session_state[key] = preset.get(key, [])
    st.session_state["f_text"] = ""
    st.switch_page("pages/6_Change_Explorer.py")


kpis = [
    ("total", "Matched groups", f"{total:,}",
     "<b>What it counts:</b> sets of related disclosures that the system paired across the two years. "
     "One group can hold several paragraphs. This is the base for the other numbers.",
     "See all matches", {}),
    ("changed", "With a change", f"{len(changed):,}",
     f"<b>What it counts:</b> groups whose text is different, including new and removed ones. "
     f"That is <b>{_share(len(changed))}</b> of all groups. Unchanged groups and groups without a label are left out.",
     "See what changed", {"f_changes": [c for c in config.CHANGE_LABELS if c != "Unchanged"]}),
    ("material", "Material changes", f"{len(material):,}",
     f"<b>What it counts:</b> changes the AI judged likely to matter to an investor (<b>{_share(len(material))}</b> of groups). "
     "Most changes are routine, so start here.",
     "See material changes", {"f_materiality": ["Material"]}),
    ("review", "Need review", f"{len(review):,}",
     "<b>What it counts:</b> matches the AI could not confirm. They have no change label yet, so treat them with care and check the text.",
     "See matches to review", {"f_statuses": ["needs_review"]}),
]
for column, (key, label, value, helptext, button, preset) in zip(st.columns(4), kpis):
    with column.container(border=True, key=f"kpi_{key}"):
        st.metric(label, value)
        st.markdown(f'<div class="kpi-help">{helptext}</div>', unsafe_allow_html=True)
        if st.button(button, key=f"see_{key}", width="stretch"):
            _see(preset)


# ---------------------------------------------------------------- charts

def _bars(order, counts, colors=None, title=""):
    frame = pd.DataFrame({"Label": order, "Matches": [counts[k] for k in order]})
    color = (alt.Color("Label:N", scale=alt.Scale(domain=order, range=[colors[k] for k in order]), legend=None)
             if colors else alt.value(landing.BLUE))
    chart = (alt.Chart(frame).mark_bar(cornerRadiusEnd=3)
             .encode(y=alt.Y("Label:N", sort=order, title=None, axis=alt.Axis(labelLimit=260)),
                     x=alt.X("Matches:Q", title="Matches"), color=color, tooltip=["Label", "Matches"])
             .properties(height=max(120, 32 * len(order))))
    st.altair_chart(chart, width="stretch")


st.markdown("#### What changed, and where")
left, right = st.columns(2)
with left:
    st.markdown("**Type of change**")
    counts = data.count_by(records, "change_label")
    order = [c for c in config.CHANGE_LABELS + [data.NOT_LABELLED] if c in counts]
    _bars(order, counts, {k: landing.CHANGE_LABEL_INFO[k][0] for k in order})
    st.caption("Each colour is one change label. The key terms below explain them.")
with right:
    st.markdown("**Part of the filing**")
    counts = data.count_by(records, "item")
    order = sorted(counts, key=components._item_order)
    names = {i: config.ITEM_TITLES.get(i, f"Item {i}") for i in order}
    _bars([names[i] for i in order], {names[i]: counts[i] for i in order})
    st.caption("Risk factors (Item 1A) and financial statements (Item 8) usually hold the most text.")

st.markdown("**Topic**")
counts = data.count_by(records, "category")
order = [c for c in config.CONTENT_CATEGORIES + [data.NOT_CLASSIFIED] if c in counts]
_bars(order, counts)
st.caption("The AI sorts every disclosure into one of nine topics. Open the explorer to filter by topic.")


# ---------------------------------------------------------------- key terms

st.markdown("#### Key terms")
tab_labels, tab_status, tab_items = st.tabs(["Change labels", "Match status", "SEC items"])
with tab_labels:
    for label, (color, text) in landing.CHANGE_LABEL_INFO.items():
        st.markdown(f'<div class="glossrow">{landing.pill(label, color)}<span>{text}</span></div>', unsafe_allow_html=True)
with tab_status:
    for color, label, text in landing.STATUS_INFO.values():
        st.markdown(f'<div class="glossrow">{landing.pill(label, color)}<span>{text}</span></div>', unsafe_allow_html=True)
with tab_items:
    descriptions = {"1": "What the company does and how it makes money.", "1A": "The risks the company says it faces.",
                    "7": "Management's own explanation of the results (MD&A).", "8": "The financial statements and notes.",
                    "15": "Exhibits and the index of documents."}
    for item, title in config.ITEM_TITLES.items():
        st.markdown(f'<div class="glossrow">{landing.pill(title.split(" ", 2)[0] + " " + item, landing.GREY)}'
                    f"<span><b>{title.split(' ', 2)[2]}.</b> {descriptions.get(item, '')}</span></div>", unsafe_allow_html=True)


# ---------------------------------------------------------------- what is loaded

st.markdown("#### What is loaded")
pair_count = sum(len(source.pairs(c)) for c in source.companies)
loaded = [f"{len(source.companies)} companies", f"{pair_count} year-to-year comparisons"]
synthesis_count, peer_count = landing.saved_synthesis_count(), landing.saved_peer_count()
if synthesis_count is not None:
    loaded.append(f"{synthesis_count} synthesis reports")
if peer_count is not None:
    loaded.append(f"{peer_count} peer comparisons")
st.markdown("".join(landing.pill(text, color) for text, color in zip(loaded, (landing.BLUE, landing.TEAL, landing.AMBER, landing.GREEN))),
            unsafe_allow_html=True)
st.caption("Companies: " + ", ".join(config.company_label(c) for c in source.companies))


# ---------------------------------------------------------------- good to know

st.markdown("#### Good to know")
st.markdown(
    landing.note("<b>Labels come from an AI.</b> Change labels, materiality and summaries are model output. "
                 "\"AI verified\" is not the same as checked by a person.", landing.AMBER)
    + landing.note("<b>\"No counterpart found\" is not proof.</b> It means the saved comparison found no match. "
                   "The topic may still appear elsewhere in the filing.", landing.ROSE)
    + landing.note("<b>Years are fiscal years.</b> \"FY2024 to FY2025\" compares the 10-K for fiscal 2024 with the one for fiscal 2025.",
                   landing.BLUE),
    unsafe_allow_html=True,
)
with st.expander("Method note saved with this comparison"):
    st.markdown(components.safe(meta.get("note") or "No method note was saved with this run."))
