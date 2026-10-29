"""Human review of disclosure types and year-to-year change labels."""
import streamlit as st

from sec_ui import components, alignment_review, disclosure_review

components.setup("Manual review")
st.title("Manual review")
section = st.radio("Review section", ["Disclosure", "Alignment"], index=1,
                   horizontal=True, key="manual_section")
if section == "Disclosure":
    disclosure_review.render()
else:
    alignment_review.render()
