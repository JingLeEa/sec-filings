"""Single-filing disclosure-type labelling with the existing dashboard controls."""
import json

import streamlit as st

from . import components, config, data, disclosure_data, reports, review


@st.cache_data(ttl=300, show_spinner="Looking for disclosure inputs...")
def cached_source():
    return disclosure_data.discover_source()


@st.cache_data(ttl=900, max_entries=8, show_spinner="Loading disclosures...")
def cached_disclosures(company, year, mode):
    return disclosure_data.load_disclosures(company, year, disclosure_data.Source({(company, year): mode}), include_all=True)


def render(scope):
    st.subheader("Disclosure review")
    st.caption("Inspect AI-approved disclosure types or manually label disclosures that need review.")
    if message := st.session_state.pop("_disclosure_review_message", None):
        st.success(message)
    source = cached_source()
    st.sidebar.header("Disclosure")
    components.data_source_notice(source)
    if not source.companies:
        st.info("No extracted disclosure data was found. Load extraction results or set SEC_UI_DISCLOSURES_DIR.")
        components.refresh_button()
        return
    if st.session_state.get("sel_company") not in source.companies:
        st.session_state.pop("sel_company", None)
    company = st.sidebar.selectbox("Company", source.companies, key="sel_company", format_func=config.company_label)
    years = source.years(company)
    if st.session_state.get("sel_disclosure_year") not in years:
        st.session_state.pop("sel_disclosure_year", None)
    year = st.sidebar.selectbox("Filing year", years, index=len(years)-1, key="sel_disclosure_year",
                                format_func=lambda value: f"FY{value}")
    mode = source.scopes[(company, year)]
    st.sidebar.caption("Data from " + ("PostgreSQL" if mode == "postgres" else "JSON files"))
    components.refresh_button()
    try:
        all_records = cached_disclosures(company, year, mode)
        saved_reviews = review.load_disclosure_reviews(company, year)
    except data.DataError as error:
        st.error(str(error))
        return
    records = [row for row in all_records if review.ai_approved_disclosure(row) == (scope == "AI approved")]
    if not records:
        st.info("No AI-approved disclosure types are available for this company and filing year." if scope == "AI approved"
                else "No disclosures need type review for this company and filing year.")
        return
    current = {row["disclosure_id"]: saved for row in records
               if (saved := review.current_disclosure_review(row, saved_reviews))}
    total, done = len(records), len(current)
    m1, m2, m3 = st.columns(3)
    m1.metric("Review queue", f"{total:,}")
    m2.metric("Reviewed", f"{done:,}")
    m3.metric("Pending", f"{total-done:,}")
    st.progress(done/total, text=f"{done:,} of {total:,} disclosures reviewed by a person")
    st.sidebar.header("Review filters")
    status = st.sidebar.selectbox("Review status", ["All", "Pending", "Reviewed"], key="disclosure_status")
    items = sorted({row["item"] for row in records}, key=components._item_order)
    components._clean("disclosure_items", items)
    selected_items = st.sidebar.multiselect("SEC Item", items, key="disclosure_items",
                                           format_func=lambda item: config.ITEM_TITLES.get(item, f"Item {item}"))
    text = st.sidebar.text_input("Search text", key="disclosure_text", placeholder="word or phrase").strip().lower()
    filtered = [row for row in records if (not selected_items or row["item"] in selected_items)
                and (not text or text in " ".join(str(row.get(field) or "") for field in
                     ("summary", "content", "section", "taxonomy")).lower())
                and (status == "All" or (row["disclosure_id"] in current) == (status == "Reviewed"))]

    def downloads():
        st.divider()
        st.subheader("Download final disclosure results")
        rows = review.export_disclosure_rows(all_records, saved_reviews, include_unreviewed=True)
        base = f"{company}_{year}_disclosure_reviews"
        j, c = st.columns(2)
        j.download_button("Disclosure reviews (JSON)", json.dumps({"company": company, "fiscal_year": year,
                          "reviews": rows}, indent=2, ensure_ascii=False), file_name=base+".json",
                          mime="application/json", disabled=not rows, width="stretch")
        c.download_button("Disclosure reviews (CSV)", reports.rows_to_csv(rows), file_name=base+".csv",
                          mime="text/csv", disabled=not rows, width="stretch")
        st.caption(f"Choices and notes are saved to {review.disclosure_review_path(company, year)}. "
                   "Downloads include every disclosure in this filing, using AI types unless a manual type was saved.")

    if not filtered:
        if status == "Pending" and done == total:
            st.success("All disclosures in this filing have been reviewed.")
        else:
            st.info("No disclosures match these review filters.")
        downloads()
        return
    ids = [row["disclosure_id"] for row in filtered]
    by_id = {row["disclosure_id"]: row for row in filtered}
    if jump := st.session_state.pop("_disclosure_review_jump", None):
        st.session_state["disclosure_selected"] = jump
    if st.session_state.get("disclosure_selected") not in by_id:
        st.session_state["disclosure_selected"] = ids[0]

    def step(delta):
        position = ids.index(st.session_state["disclosure_selected"])+delta
        st.session_state["disclosure_selected"] = ids[max(0, min(len(ids)-1, position))]

    def label(disclosure_id):
        row = by_id[disclosure_id]
        return f"{disclosure_id.rsplit('_', 1)[-1]} | Item {row['item']} | AI type: {row.get('taxonomy') or data.NOT_CLASSIFIED}"

    top, previous_col, next_col = st.columns([6, 1, 1], vertical_alignment="bottom")
    top.selectbox(f"Disclosure ({len(ids):,} match the filters)", ids, key="disclosure_selected", format_func=label)
    position = ids.index(st.session_state["disclosure_selected"])
    previous_col.button("Previous", on_click=step, args=(-1,), disabled=position == 0, width="stretch")
    next_col.button("Next", on_click=step, args=(1,), disabled=position == len(ids)-1, width="stretch")
    row = by_id[st.session_state["disclosure_selected"]]
    saved = current.get(row["disclosure_id"])
    components.chips((scope, "ok" if scope == "AI approved" else "warn"),
                     ("Human reviewed" if saved else "Not manually reviewed", "ok" if saved else ""),
                     (config.ITEM_TITLES.get(row["item"], f"Item {row['item']}"), ""), (row.get("taxonomy", ""), ""))
    if scope == "AI approved":
        st.success(components.safe(f"AI-approved disclosure type: **{row['taxonomy']}**"))
    else:
        st.info(components.safe(f"AI disclosure type: **{row.get('taxonomy') or data.NOT_CLASSIFIED}**"))
    verification = row.get("verification") or {}
    reason = verification.get("model_review_reason") or "; ".join(verification.get("review_reasons", []))
    if reason:
        st.warning(components.safe("Reason for review: " + reason))
    if row.get("summary"):
        st.write(components.safe(row["summary"]))
    components.disclosure_text(row)

    st.subheader("Final disclosure type")
    st.caption("Manual — choose a type and save to override the current final type.")
    context = (company, year, row["disclosure_id"], review.disclosure_fingerprint(row))
    if (st.session_state.get("_disclosure_review_context") != context
            or any(key not in st.session_state for key in ("disclosure_type", "disclosure_notes"))):
        st.session_state["_disclosure_review_context"] = context
        st.session_state["_disclosure_review_expected"] = saved_reviews.get(row["disclosure_id"])
        st.session_state["disclosure_type"] = saved.get("manual_disclosure_type", saved["disclosure_type"]) if saved else None
        st.session_state["disclosure_notes"] = saved["notes"] if saved else ""
    if saved and review.label_source(saved) == "manual":
        st.caption(f"Saved manual type: {saved['disclosure_type']} | Last saved: {saved['reviewed_at']}")
    elif not saved and row["disclosure_id"] in saved_reviews:
        st.warning("The selected source text changed since the last review. Review this version and save a new type.")
    if not (row.get("content") or "").strip():
        st.error("Source text is missing for this disclosure. Load its text before saving a type.")
    with st.form("disclosure_review_form"):
        chosen = st.selectbox("Disclosure type", config.CONTENT_CATEGORIES, index=None,
                              placeholder="Choose a disclosure type", key="disclosure_type")
        notes = st.text_area("Review notes (optional)", key="disclosure_notes")
        st.caption("Save your review before moving to another disclosure.")
        left, right = st.columns(2)
        missing = not (row.get("content") or "").strip()
        save = left.form_submit_button("Save review", disabled=missing, width="stretch")
        save_next = right.form_submit_button("Save and next", type="primary", disabled=missing, width="stretch")
    if save or save_next:
        try:
            result = review.save_disclosure_review(row, chosen, notes,
                                                   expected=st.session_state["_disclosure_review_expected"])
        except data.DataError as error:
            st.error(str(error))
        else:
            st.session_state.pop("_disclosure_review_context", None)
            st.session_state["_disclosure_review_message"] = f"Saved {result['disclosure_type']} for {row['disclosure_id']} (Manual)."
            if save_next:
                following = ids[position+1:]+ids[:position]
                st.session_state["_disclosure_review_jump"] = next((value for value in following if value not in current),
                                                                  following[0] if following else row["disclosure_id"])
            st.rerun()
    downloads()
