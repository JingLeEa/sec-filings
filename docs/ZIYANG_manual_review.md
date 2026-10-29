# ZIYANG: Manual review

**Maintainer:** ZIYANG — contact ZIYANG for questions about manual review.

## 1. Purpose and implementation status

The **Manual Review** page appears below **Peer Comparison** in the Streamlit
sidebar and follows the existing dashboard's styling. It has two sections:

| Section | Scope | Human label |
| --- | --- | --- |
| **Disclosure** | One company's single filing year | Disclosure type (content category) |
| **Alignment** | One company's pair of filing years | Change label for the year-to-year comparison |

Both sections support source evidence, review reasons, notes, saved progress,
All/Pending/Reviewed filters, Previous/Next navigation, Save review, Save and next,
and JSON/CSV downloads. Alignment reuses the existing side-by-side comparison
with removed words in red and added words in green.

The page makes no model calls. Saving records a local human annotation; it does
not update pipeline labels or rerun extraction, alignment, change analysis or
materiality. Other dashboard pages continue to show the saved pipeline results.
User accounts, reviewer identity and automatic application of human labels to
downstream stages are not implemented.

## 2. Code and workflow

| File | Responsibility |
| --- | --- |
| [`app/pages/5_Manual_Review.py`](../app/pages/5_Manual_Review.py) | Page entry point and Disclosure/Alignment section switch; defaults to Alignment |
| [`app/sec_ui/disclosure_review.py`](../app/sec_ui/disclosure_review.py) | Single-filing disclosure-type review form and navigation |
| [`app/sec_ui/disclosure_data.py`](../app/sec_ui/disclosure_data.py) | Extraction review queue, independent of alignment runs |
| [`app/sec_ui/alignment_review.py`](../app/sec_ui/alignment_review.py) | Year-to-year change-label review form and navigation |
| [`app/sec_ui/review.py`](../app/sec_ui/review.py) | Separate annotation files, source fingerprints, validation, atomic saves and exports |
| [`app/sec_ui/provenance.py`](../app/sec_ui/provenance.py) | Disclosure/paragraph labels and sentence provenance |
| [`app/sec_ui/components.py`](../app/sec_ui/components.py), [`diff.py`](../app/sec_ui/diff.py) | Shared dashboard styling, evidence display and word highlighting |
| [`app/sec_ui/config.py`](../app/sec_ui/config.py) | Data folders, allowed labels and annotation storage configuration |

The workflow reads saved pipeline results, selects records requiring review,
shows their original evidence and model concerns, then saves a human label and
notes alongside those results. Existing matching annotations determine progress
and populate the Reviewed filter.

## 3. Setup

Use the existing [dashboard setup](YUVIKA_ui_dashboard.md). From the repository
root, with dependencies installed in `.venv`:

```bash
.venv/bin/python -m streamlit run app/Home.py
```

Open the address printed by Streamlit, then select **Manual Review**. No model
API key is needed. PostgreSQL mode needs a configured, running database; see
[database setup](YUVIKA_database_docker_setup.md).

| Environment variable | Purpose |
| --- | --- |
| `SEC_UI_DATA_SOURCE` | `auto` (default), `files` or `postgres` |
| `SEC_UI_DISCLOSURES_DIR` | Extraction JSON root: `<company>/<year>/` |
| `SEC_UI_ALIGNMENTS_DIR` | Alignment JSON root: `<company>/<previous>-<current>/` |
| `SEC_UI_MANUAL_REVIEWS_DIR` | Writable annotation root; defaults to `data/manual_reviews` |

The review page reads PostgreSQL or JSON through the existing data layer. Human
annotations are saved as local JSON files in either mode.

## 4. Inputs and queue membership

**Disclosure** reads `disclosures.json` and `review_candidates.json` from
[disclosure extraction](ZIYANG_disclosure_extraction.md). PostgreSQL discovery
requires a completed extraction snapshot for the company/year. An alignment run
is not required. File mode reads the available extraction reports in the
configured folder. Records enter the queue when their effective verification
still needs review.

Table inspection is outside the narrative extraction stage. The pipeline no
longer generates `possible_missing_table_context`, and the shared source policy
removes that legacy flag from active review reasons when older outputs are read.
A table-only warning therefore does not create a human review task.
`model_requested_review` and other active concerns remain. The original selected
narrative evidence is retained; dropping a warning does not delete the disclosure.

**Alignment** reads [alignment outputs](ZIYANG_disclosure_alignment.md): groups
from `needs_review.json`, plus groups with explicit
`change_analysis.status: needs_review`. An automatic stage that has merely not
started does not create a review task. A group with a matching saved human review
remains accessible if the pipeline later finalizes it.

Source labels identify each displayed paragraph's disclosure ID and original
paragraph ID. The evidence table lists available selected sentences with their
disclosure, paragraph and sentence IDs and SEC links. Alignment's **Cited** column
marks the subset used in its reasoning. Missing metadata is shown as unavailable.
When full disclosure text is unavailable, alignment falls back to saved cited
sentences and indicates that limitation. Split/merged groups retain individual
source ownership; source labels do not affect the whole-group text diff. The
same provenance display is shared with the **Side by Side** page.

## 5. Review and rerun

1. Choose **Disclosure** or **Alignment**, then select the company and filing
   year or year pair in the sidebar.
2. Use the review-status filter and other available filters to select a record.
3. Read the model's concern and the original evidence. In Alignment, compare
   the earlier and later filing text side by side.
4. Choose a label, optionally add notes, then click **Save review** or
   **Save and next**.
5. Use **Reviewed** to reopen and edit saved decisions. Download JSON or CSV to
   export the current saved reviews for the selected scope.

Disclosure types are Strategy & Business Model; Operations & Capacity;
Technology & AI; Cybersecurity & Data; Supply Chain & Third Parties;
Regulation, Legal & Compliance; Financial & Capital Resources;
Human Capital & Organization; and Other / Unclassified.

Alignment change labels are New, Removed, Expanded, Reduced, Modified,
Reworded and Unchanged.

Save before navigating: unsaved changes do not transfer to another record.
Saved reviews survive app restarts. **Reload data** refreshes pipeline inputs and
resets the review form to its saved values. If selected text or source IDs change,
the previous annotation does not count as reviewed for the new version. Saving
without source text is disabled. Conflicting edits produce an error asking the
reviewer to reload before saving again.

## 6. Saved outputs and downloads

The default annotation paths are separate for the two sections:

```text
data/manual_reviews/disclosures/<company>/<year>/manual_reviews.json
data/manual_reviews/<company>/<previous>-<current>/manual_reviews.json
```

Both use `schema_version: 1` and a `reviews` object keyed by disclosure ID or
match ID. Writes use a temporary file and atomic replacement.

| Scope | Envelope fields in addition to `schema_version` and `reviews` | Saved review fields |
| --- | --- | --- |
| Disclosure | `stage: disclosure`, `company`, `fiscal_year` | `disclosure_id`, `disclosure_type`, `original_disclosure_type`, `notes`, `reviewed_at`, `source_fingerprint` |
| Alignment | `company`, `previous_year`, `current_year` | `match_id`, `change_label`, `original_change_label`, `notes`, `reviewed_at`, `source_fingerprint`, `source_file`, `previous_ids`, `current_ids` |

`reviewed_at` is an ISO timestamp in UTC. A source fingerprint binds a saved
decision to the selected text and source membership, so reused IDs do not
silently inherit a decision for different evidence.

Downloads include saved reviews matching the current source records for the
selected company/year scope, regardless of the current review filters.
Disclosure exports contain `original_disclosure_type`, `manual_disclosure_type`,
notes, timestamps, fingerprint, selected text, IDs and SEC URLs. Alignment exports
retain the original `change_label` and evidence and add `manual_change_label`,
`manual_notes`, `reviewed_at` and `source_fingerprint`.

## 7. Example data and downstream usage

Alignment fixtures are under [`tests/fixtures/alignments/amd`](../tests/fixtures/alignments/amd).
Review tests create temporary source records and annotation files; their labels
are synthetic test decisions. No real human decisions are committed as fixtures.

Downloaded reviews can be used to inspect human decisions alongside the original
model labels. Applying them to pipeline outputs or consuming them automatically
in synthesis requires a separate integration; clicking Save does not do this.

## 8. Validation and limitations

Run the offline UI checks from the repository root:

```bash
.venv/bin/python -m pytest tests/test_ui_data.py tests/test_ui_pages.py tests/test_ui_review.py tests/test_ui_provenance.py tests/test_disclosure_manual_review.py -q
```

Coverage includes both sections, independent type/change labels and scopes,
queue membership, table-warning retirement with model-review retention,
provenance for split/merged disclosures, safe highlighting, saved progress,
editing and navigation, reloads, source changes, conflicting edits, atomic save
failures and exports. Optional checks depend on local fixture data or PostgreSQL.
Both review sections have also been checked visually against live PostgreSQL data.

Annotations have no authenticated reviewer identity. The conflict lock is shared
within one app process; separate Streamlit processes should not share an
annotation directory for concurrent writes. Missing evidence must be corrected
upstream before it can be reviewed. Human labels remain separate from pipeline
results until an explicit downstream integration is added.
