"""Year-to-year change labels in the Alignment review section."""
import json

import streamlit as st

from sec_ui import components, config, data, reports, review


def render(scope):
    st.subheader("Alignment review")
    st.caption("Inspect AI-approved change labels or manually label groups that need review.")
    if message := st.session_state.pop("_manual_review_message", None):
        st.success(message)

    source, company, previous, current = components.comparison_picker()
    comparison = components.load_selected(source, company, previous, current)
    try:
        saved_reviews = review.load_reviews(company, previous, current)
    except data.DataError as error:
        st.error(str(error))
        st.stop()
    all_records = comparison["records"]
    records = [r for r in all_records if (review.ai_approved_change(r) if scope == "AI approved" else
               not review.ai_approved_change(r) and (review.requires_change_review(r)
                                                    or review.current_review(r, saved_reviews)))]
    if not records:
        st.info("No AI-approved change labels are available for these filing years." if scope == "AI approved" else
                "No alignment groups need review or change analysis for these filing years. Pick another comparison.")
        st.stop()

    current_reviews = {r["match_id"]: saved for r in records if (saved := review.current_review(r, saved_reviews))}
    total, done = len(records), len(current_reviews)
    m1, m2, m3 = st.columns(3)
    m1.metric("Review queue", f"{total:,}")
    m2.metric("Reviewed", f"{done:,}")
    m3.metric("Pending", f"{total - done:,}")
    st.progress(done / total, text=f"{done:,} of {total:,} alignment groups reviewed by a person")

    st.sidebar.header("Review filters")
    status = st.sidebar.selectbox("Review status", ["All", "Pending", "Reviewed"], key="manual_status")
    items = sorted({r["item"] for r in records}, key=components._item_order)
    components._clean("manual_items", items)
    sel_items = st.sidebar.multiselect("SEC Item", items, key="manual_items",
                                      format_func=lambda i: config.ITEM_TITLES.get(i, f"Item {i}"))
    needle = st.sidebar.text_input("Search text", key="manual_text", placeholder="word or phrase")
    filtered = data.filter_records(records, items=sel_items, text=needle)
    if status != "All":
        filtered = [r for r in filtered if (r["match_id"] in current_reviews) == (status == "Reviewed")]


    def downloads():
        st.divider()
        st.subheader("Download final results")
        rows = review.export_rows(all_records, saved_reviews, include_unreviewed=True)
        base = f"{company}_{previous}_{current}_manual_reviews"
        payload = {"company": company, "previous_year": previous, "current_year": current, "reviews": rows}
        j, c = st.columns(2)
        j.download_button("Reviews (JSON)", json.dumps(payload, indent=2, ensure_ascii=False),
                          file_name=f"{base}.json", mime="application/json", width="stretch", disabled=not rows)
        c.download_button("Reviews (CSV)", reports.rows_to_csv(rows), file_name=f"{base}.csv",
                          mime="text/csv", width="stretch", disabled=not rows)
        st.caption(f"Choices and notes are saved to {review.review_path(company, previous, current)}. "
                   "Downloads include every result in this comparison, using AI labels unless a manual label was saved.")


    if not filtered:
        if status == "Pending" and done == total:
            st.success("All alignment groups in this comparison have been reviewed.")
        else:
            st.info("No disclosures match these review filters. Change a filter in the sidebar to see more.")
        downloads()
        st.stop()

    ids = [r["match_id"] for r in filtered]
    by_id = {r["match_id"]: r for r in filtered}
    if jump := st.session_state.pop("_manual_review_jump", None):
        st.session_state["manual_selected"] = jump
    if st.session_state.get("manual_selected") not in by_id:
        st.session_state["manual_selected"] = ids[0]


    def label(match_id):
        record = by_id[match_id]
        ai_label = record["methods"].get("llm") or data.NOT_LABELLED
        return f"{match_id.rsplit('_', 1)[-1]} | Item {record['item']} | AI label: {ai_label}"


    def step(delta):
        position = ids.index(st.session_state["manual_selected"]) + delta
        st.session_state["manual_selected"] = ids[max(0, min(len(ids) - 1, position))]


    top, prev_col, next_col = st.columns([6, 1, 1], vertical_alignment="bottom")
    top.selectbox(f"Alignment group ({len(ids):,} match the filters)", ids, key="manual_selected", format_func=label)
    position = ids.index(st.session_state["manual_selected"])
    prev_col.button("Previous", on_click=step, args=(-1,), disabled=position == 0, width="stretch")
    next_col.button("Next", on_click=step, args=(1,), disabled=position == len(ids) - 1, width="stretch")

    record = by_id[st.session_state["manual_selected"]]
    saved = current_reviews.get(record["match_id"])
    components.chips(
        (scope, "ok" if scope == "AI approved" else "warn"),
        ("Human reviewed" if saved else "Not manually reviewed", "ok" if saved else ""),
        (config.ITEM_TITLES.get(record["item"], f"Item {record['item']}"), ""),
        (record["category"], ""),
    )
    llm_label = record["methods"].get("llm")
    if scope == "AI approved":
        st.success(f"AI-approved change label: **{llm_label}**")
    else:
        st.info(f"AI change label: **{llm_label}**" if llm_label else "AI change label: Not labelled")
    reason = record["verifier_review_reason"] or "; ".join(record["review_reasons"])
    if reason:
        st.warning(components.safe("Reason for review: " + reason))
    components.side_by_side(record, previous, current, key="manual_highlight")
    if record["explanation"]:
        with st.expander("Match reasoning"):
            st.write(components.safe(record["explanation"]))

    st.subheader("Final change label")
    st.caption("Manual — choose a label and save to override the current final label.")
    if saved and review.label_source(saved) == "manual":
        st.caption(f"Saved manual label: {saved['change_label']} | Last saved: {saved['reviewed_at']}")
    elif not saved and record["match_id"] in saved_reviews:
        st.warning("The disclosure text or matched members changed since the last review. Review this version and save a new label.")

    context = (company, previous, current, record["match_id"], review.fingerprint(record))
    if (st.session_state.get("_manual_review_context") != context
            or any(key not in st.session_state for key in ("manual_label", "manual_notes"))):
        st.session_state["_manual_review_context"] = context
        st.session_state["_manual_review_expected"] = saved_reviews.get(record["match_id"])
        st.session_state["manual_label"] = saved.get("manual_change_label", saved["change_label"]) if saved else None
        st.session_state["manual_notes"] = saved["notes"] if saved else ""

    missing_text = any(record[f"{side}_ids"] and not record[f"{side}_text"].strip() for side in ("previous", "current"))
    if missing_text:
        st.error("Source text is missing for a disclosure in this match. Load its disclosure text before saving a label.")

    with st.form("manual_review_form"):
        chosen_label = st.selectbox("Change label", config.CHANGE_LABELS, index=None,
                                    placeholder="Choose a change label", key="manual_label")
        notes = st.text_area("Review notes (optional)", key="manual_notes", placeholder="Explain your change label.")
        st.caption("Choose New, Removed, Expanded, Reduced, Modified, Reworded or Unchanged. "
                   "Save your review before moving to another disclosure.")
        save_col, next_save_col = st.columns(2)
        save = save_col.form_submit_button("Save review", disabled=missing_text, width="stretch")
        save_next = next_save_col.form_submit_button("Save and next", type="primary", disabled=missing_text, width="stretch")

    if save or save_next:
        try:
            result = review.save_review(record, chosen_label, notes,
                                        expected=st.session_state["_manual_review_expected"])
        except data.DataError as error:
            st.error(str(error))
        else:
            st.session_state.pop("_manual_review_context", None)
            st.session_state["_manual_review_message"] = f"Saved {result['change_label']} for {record['match_id']} (Manual)."
            if save_next:
                following = ids[position + 1:] + ids[:position]
                next_id = next((i for i in following if i not in current_reviews), None)
                st.session_state["_manual_review_jump"] = next_id or (following[0] if following else record["match_id"])
            st.rerun()

    components.supporting_evidence(record, previous, current)
    downloads()
