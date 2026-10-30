# Chatbot accuracy evaluation

How the 10-K disclosure-change chatbot was tested for accuracy, what the tests found, and what was changed as a
result. Two tests, each measuring one stage of answering a question:

1. **Filter extraction**: does the chatbot understand the question? That covers its intent, companies, years,
   Items, change types, statuses, thresholds and limits.
2. **SQL accuracy**: does the chatbot fetch the right data, and does the written answer report it correctly?

Both tests are in the repository and can be rerun after any change (see [Rerunning](#rerunning)).

## Summary

| Test | Before | After |
|---|---|---|
| Filter extraction, main set (80): questions with no real error | 67 (84%) | 80 (100%) |
| Filter extraction, hold-out (32): every filter right | 6 | 21 after the first round of fixes, 32 after the second ¹ |
| SQL accuracy, rule questions (45), offline: fully correct | 33 (73%) | 43 (96%) |
| SQL accuracy, all 60, LLM mode (Qwen): fully correct | — | 51 and 56 (two runs) |

¹ After the first round, the hold-out misses were used to make a second round of fixes, so the 32/32 is no
longer an unseen-data score. 21/32 is the honest measure of how well the first-round fixes generalised.

## Why independent reference answers

In both tests the reference answers were written by a separate Claude Opus agent. It worked only from a written
spec (`eval/filter_extraction/LABELING_SPEC.md`, `eval/sql_accuracy/DATA_DICTIONARY.md`) and never saw the
chatbot's code or queries. If the chatbot's own logic had been used to write the references, the test would only
check the code against itself.

---

## 1. Filter extraction

### Method

- **Questions:** 80 questions (`eval/filter_extraction/questions.json`). They cover every intent, all 11
  companies and many ways of naming years (FY24, "since", "latest", "between"). They also include Items, change
  types, thresholds, typos, lowercase tickers and 3 follow-up questions.
- **Labels:** the independent agent labelled the ideal reading of each question (`gold.json`). It also listed
  any other intent that would be a reasonable reading.
- **Run:** each question goes through the rule-based planner (`plan_question`, plus `inherit_context` for
  follow-ups) with 22 comparison runs loaded. No database and no LLM are used.
- **Scoring:**
  - Years are compared as the comparison windows actually searched (2023→2024, 2024→2025), so different ways of
    naming the same window count as the same.
  - Intent counts as right if it matches the label or one of the acceptable alternatives.
  - Every other field must match exactly.
- **Verdicts:** each question with a difference was then reviewed and given one of four verdicts:
  - **Real error:** the chatbot would search the wrong rows or answer the wrong kind of question.
  - **Flagged to user:** the question is ambiguous, and the answer now says how it was read.
  - **Harmless:** differs from the label but gives the same answer.
  - **Labeller judgement:** only the inferred topic category differs.
- **Hold-out:** 32 new questions (`holdout_*.json`) were written and labelled by the same agent before any fix,
  using different wording for the same kinds of problem.

### What it found

The 13 real errors on the main set:

| Area | Question examples | Problem |
|---|---|---|
| Years | #3, #15, #27, #30 | "latest" was not read as a year; "since 2023" dropped the 2024→2025 comparison; "the 2024 and 2025 filings" was read as one comparison |
| Topic | #16, #51, #54 | "describe its liquidity risk", "export control disclosures" and "updated its cybersecurity disclosures" were not read as topic searches |
| Missing phrases | #21, #26, #28, #74 | "stack up against", "changed the most", "the two methods label differently" and "rewrote" matched no rule |
| Wrong match | #5, #30 | "compared to the year before" was read as a company comparison; "revised" became the Modified label |
| Company | #36 | lowercase "ms" was not recognised as Morgan Stanley |

### Changes made (commit "fix question-parsing gaps found by the filter evaluation")

| Change | File |
|---|---|
| "latest" / "most recent" = the newest loaded filing | `entities.py` |
| "since 2023" / "2024 onward" = every comparison from that filing on | `entities.py` |
| "changes in the 2024 and 2025 filings" and "its 2025 10-K … than its 2024 one" cover both filings | `entities.py` |
| "revised" no longer means the Modified label | `entities.py` |
| "which bank" / "chipmakers" / "semiconductor" expand to that sector's companies | `entities.py` |
| "two change-detection methods" is no longer read as a limit of 2 | `entities.py` |
| "compared to the year before" and "versus its 2024 one" are not a company comparison | `planner.py` |
| Topic taken after describe / discuss and from "<subject> disclosures"; filter words such as "split" are not topics | `planner.py` |
| Peer-comparison phrases: stack up, measure up, relative to, fares against | `planner.py` |
| Amount of change ("how much", "to what extent", "changed the most") counts as change statistics | `planner.py` |
| Disagreement phrases: label differently, two methods, don't agree, different verdicts | `planner.py` |
| "what changed between …" and "walk me through" are summaries; "comparison windows" is not a comparison | `planner.py` |

**Flagged to the user instead of guessed.** The answer starts with one line saying how the question was read:

- **Two years without "between"** ("compare the 2024 and 2025 10-Ks"): read as the 2024 → 2025 comparison, with a
  hint on how to ask for each filing instead.
- **Lowercase ms / gs / mu:** not matched (they also match ordinary text such as "5 ms"); the answer asks
  "Did you mean Morgan Stanley?"
- **"Rewrote":** not filtered to the Reworded label, because most people mean any change; the answer says how to
  ask for wording-only changes.

**Deliberately not changed:**

- **Match IDs:** the company and year are not copied out of a match ID, because the ID lookup already gives the
  same answer.
- **Topic categories:** a category is not inferred from the subject (e.g. export controls → Regulation), because
  a wrong category would filter out the right disclosures.

### Results

| | Main 80 before | Main 80 after | Hold-out before | Hold-out after |
|---|---|---|---|---|
| Intent | 89% | 100% | 38% | 100% |
| Year windows | 91% | 96% | 75% | 100% |
| Companies | 95% | 95% | 94% | 100% |
| No real error | 84% | 100% | 19% | 100% |

The main-set questions that still differ from the label are the 2 flagged cases, the 3 harmless match-ID cases
and 5 inferred categories.

---

## 2. SQL accuracy

### Method

- **Questions:** 60 questions (`eval/sql_accuracy/questions.json`), each with one correct answer in the database:
  - **45 the rules should handle:** counts, filtered lists, top-N, breakdowns, comparisons, thresholds and
    financial figures.
  - **15 harder questions** that need a calculation the templates don't do (average, median, share, per-section
    ranking), which the LLM writes SQL for.
- **Reference SQL** (`gold_sql.json`): written by the independent agent from `DATA_DICTIONARY.md`. The dictionary
  fixes the definitions that can be read two ways: what "material" means, how to treat not-analysed rows, what
  "the 2024 10-K" means, and whether "above" includes the boundary.
- **Run** (`scripts/evaluate_chatbot_sql.py`): runs the reference SQL and the chatbot on the same database, and
  saves the chatbot's intent, SQL, rows and answer. Offline mode uses rules and templates only; LLM mode uses
  SoCLaaS (`qwen3.6:35b`).
- **Scoring** (`scripts/score_chatbot_sql.py`, tested in `tests/test_score_chatbot_sql.py`) makes two checks per
  question:
  - **Data:** do the rows the chatbot fetched contain the reference result? How this is compared depends on the
    answer type:

    | Answer type | Passes when |
    |---|---|
    | Count | equal; a per-run breakdown that sums to the reference counts |
    | List of IDs | no wrong IDs, and either every ID or a total equal to the reference count |
    | Top-N | the right N; ties in the ranking score allowed in any order |
    | Breakdown | the same value for every group; long or wide tables |
    | Single value | equal within rounding |
    | Table | the labels and the asked measure match; for "which … highest" questions the top answer is enough |

  - **Answer text:** does the written answer state the reference number(s), the top answer, or only reference IDs?

  A question is **fully correct** when both checks pass.

### Offline findings and changes (commit "fixes from the SQL accuracy evaluation")

| Problem | Questions | Change |
|---|---|---|
| Calculation questions never reached the LLM: keyword rules caught them first ("average materiality" → material changes) | all 15 hard | Questions asking for an average, median, share, percentage, "outnumber", "increase in the number" or "which sections/items have the most" go to the LLM SQL writer. Offline, the closest built-in answer is shown with a note. The SQL prompt now lists all 11 tickers and the data definitions. |
| "Material" ignored in counts and confidence lists (972 instead of 146) | #6, #30, #38 | "material" filters to material = 'Yes'; "most material" only ranks; "non-material" = 'No' |
| "Above" / "below" included the boundary (21 instead of 4) | #4, #21 | The operator is kept: above / below are strict, at least / at most inclusive. A question asking for a maximum materiality, which isn't supported, now says so. |
| "How many … changed" counted Unchanged rows | #11 | Counts only analysed rows with an actual change |
| A number became the search topic ("3 most material") | #28 | Numbers and rank words are not topics |
| No breakdown by status, or by two dimensions | #34, #36 | Breakdowns by change type, item, status, company, relationship or category, and two-way |
| Per-company disagreement counts not in the answer | #40 | Added to the answer |

By design: #19 (a category treated as a text search) and #39 (the flagged "2024 and 2025 filings" reading).

### Results

| Fully correct | Offline before | Offline after | LLM run 1 | LLM run 2 |
|---|---|---|---|---|
| Rule questions (45) | 33 | 43 | 39 | 41 |
| Hard questions (15) | 0 | 0 ² | 12 | 15 |
| All (60) | 33 | 43 | 51 | 56 |

² Offline the hard questions can't pass: they need the LLM to write SQL.

**LLM mode observations (runs 1 and 2, the current code):**

- **Run 1 SQL misses** on the hard questions:
  - **#46:** counted removals per Item instead of each Item's share. The question can also be read as each
    Item's share of all removals, under which the answer is reasonable.
  - **#51:** added a filter not asked for (material only), giving a median of 0.8 instead of 0.65.
  - **#56:** used the wrong year and grouped by alignment instead of by section.

  Run 2 got all three right.
- **Consistency:** none of the 15 generated SQL queries was the same in both runs, so a hard question can get a
  different answer from one run to the next.
- **Rule questions:** the right data came back for 43 of 45 in both runs, but Qwen sometimes misreported it.
  It said a count wasn't available though it was (#5), added numbers up wrong (#12: 655 instead of 825), missed
  a per-company breakdown (#40), and once cited an ID range (#22).
- **Quotes:** in 9–14 answers per run, the grounding check flagged a quotation that was not word for word in
  the filing text.

### Tried and reverted

Two further changes were tested in LLM run 3 and then reverted; the code is back to the run-2 version.

1. **SQL drafts and hints:**
   - Two SQL drafts with a third as tie-breaker.
   - Hints to the SQL writer with the filters the rules had parsed.
   - Extra SQL rules.
2. **Clearer evidence tables:** each table labelled with what it holds, and totals computed in code, so Qwen
   never adds up numbers itself.

**Run 3: 53/60** (rule questions 41, hard 12).

- **What worked:** the answer-writing change fixed #5, #12 and #40.
- **What went wrong:** the hints passed conditions from the question into the SQL. "Rated material" became
  `WHERE material = 'Yes'`, so every share came out 100% (#48). "Between the 2024 and 2025 filings" pinned the
  query to one comparison (#58). All three drafts made the same mistake, so the tie-breaker did not help.
- **Lesson:** only pass the SQL writer the companies resolved from the question. Items, change types and
  "material" are often the very thing a calculation measures, so they must not become WHERE conditions.

The answer-writing change can be reapplied on its own, since it is separate from the SQL hints.

### Remaining issues

- **SQL varies between runs** for calculation questions; generating more than one draft only helps when the
  drafts make different mistakes.
- **Answer text** can misreport correct data, mainly counts and sums (see the reverted answer-writing change).
- **#46** ("highest share of removed disclosures") is genuinely ambiguous and should be reworded.
- **Quotations:** the grounding check catches quotes that are not word for word in the filing, and the answer
  carries a warning, but the model still writes them.

---

## Limitations of these tests

- **Who wrote them:** we wrote the questions and a model wrote the references. Ten of the reference SQL queries,
  the ones whose wording can be read more than one way, are listed for a human check in the results workbook.
  A wrong reference makes the chatbot look wrong when it isn't.
- **Data:** results are for one snapshot of the data: 10 companies, 2023→2024 and 2024→2025; Citigroup is not
  loaded in the test database. The SQL reference results are recomputed from the reference SQL on whatever data
  is loaded.
- **Samples:** 60–80 questions per test and two LLM runs are small samples; a difference of one or two
  questions is within run-to-run noise in LLM mode.
- **Scoring:** the scorer's answer-text check looks for the reference numbers and IDs in the answer. It can miss
  a correct answer written in a different form, and it can pass an answer that happens to mention the number.
  Verdicts on failures were reviewed by hand.

## Rerunning

```bash
# filter extraction (no database needed)
python scripts/evaluate_chatbot_filters.py
python scripts/evaluate_chatbot_filters.py --set holdout

# SQL accuracy (needs PostgreSQL with the alignments loaded; LLM mode needs SoCLaaS)
python scripts/evaluate_chatbot_sql.py --mode offline --out eval/sql_accuracy/runs/offline.json
python scripts/evaluate_chatbot_sql.py --mode llm --out eval/sql_accuracy/runs/llm_1.json
python scripts/score_chatbot_sql.py eval/sql_accuracy/runs/offline.json
```

On Windows, set `PYTHONPATH=src` first, or install the project with `pip install -e .`. Run files in
`eval/sql_accuracy/runs/` are not committed.

## Files

| Path | What |
|---|---|
| `eval/filter_extraction/` | questions, labels, hold-out set, labelling spec |
| `eval/sql_accuracy/` | questions, reference SQL, data dictionary |
| `scripts/evaluate_chatbot_filters.py` | filter-extraction test |
| `scripts/evaluate_chatbot_sql.py`, `scripts/score_chatbot_sql.py` | SQL accuracy run and scorer |
| `tests/test_chatbot.py` (FilterEvalFixTests, SqlAccuracyFixTests) | unit tests for every fix, plus questions whose reading must not change |
| `tests/test_score_chatbot_sql.py` | scorer tests |
