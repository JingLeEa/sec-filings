# ZIYANG: Manual review

**Maintainer:** ZIYANG — contact ZIYANG for questions about manual review.

## 1. Purpose and implementation status

The **Manual Review** page appears below **Peer Comparison** in the Streamlit
sidebar and follows the existing dashboard's styling. It has two sections:

| Section | Scope | Human label |
| --- | --- | --- |
| **Disclosure** | One company's single filing year | Final disclosure type (content category) |
| **Alignment** | One company's pair of filing years | Final change label for the year-to-year comparison |

**AI labels are the default final results in both sections.** The review scopes
are **AI approved** and **Needs review**. Approved records display their AI label
in a prominent banner and in the record selector. The **Final change label** and
**Final disclosure type** forms are manual-only: choose a label and save to
override the current final result. There is no LLM/Manual source selector.
Existing manual reviews retain their saved labels.

Both sections support source evidence, review reasons, notes, saved progress,
All/Pending/Reviewed filters, Previous/Next navigation, Save review, Save and next,
and JSON/CSV downloads. Alignment reuses the existing side-by-side comparison
with removed words in red and added words in green.

Saving records a human annotation and automatically queues a background pipeline
refresh through **materiality**, the current pipeline's final runnable stage.
Disclosure reviews apply the human type to extraction outputs, then rerun affected
year comparisons, change analysis, lexical/semantic analysis and materiality.
Alignment reviews apply the human change label to the saved group and continue
through change analysis and materiality, preserving the human label.
Overview, Change Explorer and Side by Side use the resulting labels in views,
filters and exports. Original model labels and source evidence remain available.
Synthesis/peer reports are not regenerated; their agent runners are absent from
this checkout. User accounts and authenticated reviewer identity are not implemented.

## 2. Code and workflow

| File | Responsibility |
| --- | --- |
| [`app/pages/5_Manual_Review.py`](../app/pages/5_Manual_Review.py) | Page entry point and Disclosure/Alignment section switch; defaults to Alignment |
| [`app/sec_ui/disclosure_review.py`](../app/sec_ui/disclosure_review.py) | Single-filing disclosure-type review form and navigation |
| [`app/sec_ui/disclosure_data.py`](../app/sec_ui/disclosure_data.py) | Extraction review queue, independent of alignment runs |
| [`app/sec_ui/alignment_review.py`](../app/sec_ui/alignment_review.py) | Year-to-year change-label review form and navigation |
| [`app/sec_ui/review.py`](../app/sec_ui/review.py) | Final-label resolution, separate annotation files, source fingerprints, validation, atomic saves and exports |
| [`app/sec_ui/provenance.py`](../app/sec_ui/provenance.py) | Disclosure/paragraph labels and sentence provenance |
| [`app/sec_ui/components.py`](../app/sec_ui/components.py), [`diff.py`](../app/sec_ui/diff.py) | Shared dashboard styling, evidence display and word highlighting |
| [`app/sec_ui/config.py`](../app/sec_ui/config.py) | Data folders, allowed labels and annotation storage configuration |
| [`app/sec_ui/refresh.py`](../app/sec_ui/refresh.py) | Save-triggered scheduling, live status, retry controls and cache refresh |
| [`pipelines/review_jobs.py`](../src/sec_disclosure/pipelines/review_jobs.py) | Durable queue, separate worker process and interrupted-job recovery |
| [`pipelines/review_refresh.py`](../src/sec_disclosure/pipelines/review_refresh.py) | Dependency planning, applying corrections, affected comparison reruns and publication |
| [`review_overrides.py`](../src/sec_disclosure/review_overrides.py) | Source-bound human labels, original-label retention and match-ID renumbering |

The workflow reads saved pipeline results, shows their original evidence and
model concerns, then saves a manual label and optional notes. Existing matching
annotations determine human review progress and populate the
Reviewed filter. Pipeline data is cached; final-label choices are read again on
each page run so saving a choice does not require clearing that cache.

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
| `SEC_UI_PIPELINE_DATA_DIR` | Pipeline raw-data/embedding-cache root; defaults to `data` in PostgreSQL mode or the extraction folder's parent in file mode |
| `SEC_UI_PIPELINE_ENV_FILE` | Optional backend database/model configuration file; otherwise the existing defaults apply |
| `SEC_UI_EMBEDDING_MODEL` | Refresh embedding model; defaults to `bge-m3` |
| `SEC_UI_REFRESH_AUTOSTART` | Defaults to `1`; set `0` only to use a separately managed worker or offline UI tests |

The review page reads PostgreSQL or JSON through the existing data layer. Human
annotations are saved as local JSON files in either mode.
Automatic refreshes use the configured model API and need the same backend
dependencies and configuration as the full pipeline. File mode requires the raw
paragraph/sentence files and complete extraction metadata, not just UI fixtures.

## 4. Inputs and review scope

**Review scope** appears above the Disclosure/Alignment selector in a bordered
control panel. Both selectors use segmented buttons with the dashboard's theme.
The scope defaults to **Needs review** and stays selected when switching sections
or visiting another dashboard page. **AI approved**
shows completed, accepted AI results with valid labels and no active review flag.
**Needs review** shows records flagged for human attention. The scopes are
separate: an approved record does not join Needs review merely because a user
saved an override. All/Pending/Reviewed describes human review progress; an
AI-approved record can already have a final label without a human review.

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
Finalized groups with completed labels appear under **AI approved**. Unstarted
automatic stages alone do not create human review tasks or AI-approved labels.

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
2. Select **AI approved** to inspect completed labels, or keep **Needs review**
   for flagged records. Use the other filters to select a record. The record
   selector shows only the disclosure/match ID, SEC Item and original AI
   type/label in both sections. Human review status stays in the review filters
   and progress indicators.
3. Read the model's concern and the original evidence. In Alignment, compare
   the earlier and later filing text side by side.
4. Read the AI label in the banner. To make a correction, choose your own label
   in the manual **Final change label** or **Final disclosure type** form.
   Optionally add notes, then click **Save review** or **Save and next**.
   The AI result remains the default until a manual override is saved.
5. Use **Reviewed** to reopen and edit saved decisions. Download JSON or CSV to
   export final results for the whole selected filing or year pair, including
   unreviewed records using the AI default.

After either Save action, **Pipeline updates** shows the queued/running stage,
completion or a failure with **Retry pipeline refresh**. The review remains saved
if a model, database or worker error stops the refresh. A completed refresh clears
the dashboard cache and reloads current results automatically. **Refresh downstream
results** can resubmit an existing saved decision without editing its label.

A disclosure review affects adjacent available filing years and any existing
comparison containing the reviewed year. For example, AMD FY2024 refreshes
2023–2024 and 2024–2025; other companies and unrelated comparisons are unchanged.
The human decision supplies the extraction-stage result: the model does not
re-extract or replace the reviewed type. A resolved `model_requested_review` flag
is retained in the audit metadata; unrelated source warnings remain active.

An alignment review preserves the selected group's membership and original AI
label, sets the human label as `final_taxonomy`, and recalculates downstream
results. It does not ask the matching agent to undo the human decision. A group
with unresolved correspondence warnings remains in `needs_review.json`; its human
change label and materiality can be saved without falsely claiming AI approval.
Materiality receives full selected source sentences for these human-reviewed groups.

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
If no AI label is available, no final label is invented. The manual form lets a
reviewer supply one. The original AI label remains visible after an override.

## 6. Saved outputs and downloads

The default annotation paths are separate for the two sections:

```text
data/manual_reviews/disclosures/<company>/<year>/manual_reviews.json
data/manual_reviews/<company>/<previous>-<current>/manual_reviews.json
```

Both use `schema_version: 1` and a `reviews` object keyed by disclosure ID or
match ID. Writes use a temporary file and atomic replacement.
Refresh jobs, stage workspaces, source backups and the worker log are stored under
`<manual-reviews-root>/pipeline_refresh/`. One worker owns that queue at a time.
Newer human decisions supersede older queued work; input revisions are checked
before publishing so a background run cannot replace newer inputs silently.
Valid reviews follow unchanged source text/membership if alignment renumbers IDs;
changed source evidence requires a new review. Reused IDs retain earlier review
versions for audit rather than deleting decisions for a different source group.

To drain pending jobs using a separately managed worker or after an app restart:

```bash
.venv/bin/python scripts/process_review_refresh.py --queue-dir data/manual_reviews/pipeline_refresh
```

| Scope | Envelope fields in addition to `schema_version` and `reviews` | Saved review fields |
| --- | --- | --- |
| Disclosure | `stage: disclosure`, `company`, `fiscal_year` | `disclosure_id`, `disclosure_type`, `label_source`, `llm_disclosure_type`, `manual_disclosure_type`, `original_disclosure_type`, `notes`, `reviewed_at`, `source_fingerprint` |
| Alignment | `company`, `previous_year`, `current_year` | `match_id`, `change_label`, `label_source`, `llm_change_label`, `manual_change_label`, `original_change_label`, `notes`, `reviewed_at`, `source_fingerprint`, `source_file`, `previous_ids`, `current_ids` |

The internal `label_source` is `llm` for AI defaults or older saved AI choices,
and `manual` for human overrides. New saves from the review forms always use
`manual`; `disclosure_type`/`change_label` records that final value. Legacy
entries without `label_source` are treated as manual. AI labels and manual
labels remain separate for comparison and export.

`reviewed_at` is an ISO timestamp in UTC. A source fingerprint binds a saved
decision to the selected text and source membership, so reused IDs do not
silently inherit a decision for different evidence.

Downloads include every current source record for the selected company/year
scope, regardless of review filters. Both exports include `final_label_source`.
Disclosure exports contain `llm_disclosure_type`, `manual_disclosure_type` and
`final_disclosure_type`. Alignment exports contain `llm_label`,
`manual_change_label`, `final_change_label` and `change_label` (the effective
final label). Original pipeline labels, notes, UTC timestamps, fingerprints,
selected text, IDs and SEC URLs remain available. Unreviewed records have no
review timestamp or manual label. An unavailable final label is null in the
`final_*` field; the dashboard displays Not labelled/Not classified.

## 7. Example data and downstream usage

Alignment fixtures are under [`tests/fixtures/alignments/amd`](../tests/fixtures/alignments/amd).
Review tests create temporary source records and annotation files; their labels
are synthetic test decisions. No real human decisions are committed as fixtures.

Downloaded results retain final human choices alongside original model labels.
The refresh publishes updated normalized PostgreSQL rows and pipeline snapshots,
or updated canonical JSON reports in file mode. Backend synthesis integration is
outside the current runnable pipeline.

## 8. Validation and limitations

Run the offline UI checks from the repository root:

```bash
.venv/bin/python -m pytest tests/test_ui_data.py tests/test_ui_pages.py tests/test_ui_review.py tests/test_ui_provenance.py tests/test_disclosure_manual_review.py -q
.venv/bin/python -m pytest tests/test_review_refresh.py -q
```

Coverage includes both sections, independent type/change labels and scopes,
queue membership, table-warning retirement with model-review retention,
AI defaults, separate AI-approved and flagged scopes, clear AI label display,
manual-only forms, overriding completed results, final labels
across dashboard pages and exports, legacy annotation compatibility,
provenance for split/merged disclosures, safe highlighting, saved progress,
editing and navigation, reloads, source changes, conflicting edits, atomic save
failures and exports. Optional checks depend on local fixture data or PostgreSQL.
Both review sections have also been checked visually against live PostgreSQL data.

Annotations have no authenticated reviewer identity. The conflict lock is shared
within one app process; separate Streamlit processes should not share an
annotation directory for concurrent writes. Missing evidence must be corrected
upstream before it can be reviewed. Saved choices affect dashboard final results;
original source evidence is retained while affected pipeline results are refreshed.
