"""Human review of disclosure types and year-to-year change labels."""
import streamlit as st

from sec_ui import components, alignment_review, disclosure_review

components.setup("Manual review")
st.title("Manual review")

# Carry the active section's existing choice into one shared scope.
if "review_scope" not in st.session_state:
    old_key = "disclosure_scope" if st.session_state.get("manual_section") == "Disclosure" else "manual_scope"
    old_scope = st.session_state.get(old_key, "Needs review")
    st.session_state["review_scope"] = "AI approved" if old_scope == "All results" else old_scope

for key, choices, default in (
    ("review_scope", ["Needs review", "AI approved"], "Needs review"),
    ("manual_section", ["Disclosure", "Alignment"], "Alignment"),
):
    if st.session_state.get(key) not in choices:
        st.session_state[key] = default
    st.session_state[f"_{key}_selection"] = st.session_state[key]


def retain_selection(key):
    # Clicking the selected segment must not clear the active scope or section.
    if st.session_state[key] is None:
        st.session_state[key] = st.session_state[f"_{key}_selection"]


with st.container(border=True, key="manual_review_controls"):
    scope = st.segmented_control("Review scope", ["Needs review", "AI approved"], key="review_scope",
                                 width="stretch", on_change=retain_selection, args=("review_scope",))
    st.caption("Choose which records to view. **Needs review** shows items flagged for a person to check. "
               "**AI approved** shows items accepted by the AI; you can still review and change their labels.")
    section = st.segmented_control("Review section", ["Disclosure", "Alignment"], key="manual_section",
                                   width="stretch", on_change=retain_selection, args=("manual_section",))
    st.caption("Choose what to label. **Disclosure** lets you check the disclosure type within one filing. "
               "**Alignment** lets you check how disclosures changed between two filing years.")

if section == "Disclosure":
    disclosure_review.render(scope)
else:
    alignment_review.render(scope)
