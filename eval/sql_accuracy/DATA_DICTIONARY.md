# Data dictionary for the SQL accuracy evaluation

Used to write the reference ("gold") SQL for each evaluation question. PostgreSQL 16. Write read-only `SELECT`
queries against the two views below only.

## View `alignment_changes` — one row per alignment (a disclosure matched across two consecutive 10-Ks)

| Column | Type | Meaning |
|---|---|---|
| run_id | bigint | comparison run |
| company | text | lowercase ticker: bac, jpm, gs, ms (banks); intc, amd, nvda, avgo, qcom, mu (chipmakers). Citigroup is not loaded. |
| previous_year, current_year | int | the two filings compared. Loaded windows: 2023→2024 and 2024→2025 for every company. |
| match_id | text | unique alignment id, e.g. `bac_2023_2024_M0012` |
| status | text | `ai_verified`, `auto_matched`, `needs_review` (flagged for human review), `unmatched` (new or removed disclosure) |
| relationship | text | `one_to_one`, `one_to_many` (split), `many_to_one` (merged), `many_to_many`, `current_only` (new), `previous_only` (removed) |
| item | text | 10-K Item: `1` Business, `1A` Risk Factors, `7` MD&A, `8` Financial Statements (and notes), `15` Exhibits |
| section | text | section heading path within the Item |
| disclosure_taxonomy | text | `Cybersecurity & Data`, `Technology & AI`, `Supply Chain & Third Parties`, `Regulation, Legal & Compliance`, `Financial & Capital Resources`, `Human Capital & Organization`, `Operations & Capacity`, `Strategy & Business Model`, `Other / Unclassified` |
| analysis_status | text | `completed` or `not_started` (needs_review rows were not analysed) |
| change_type | text | final change label: `Unchanged`, `Modified`, `Reworded`, `Expanded`, `Reduced`, `New`, `Removed`; NULL when not analysed |
| lexical_label | text | change label from the lexical (text-diff) method; NULL when not analysed |
| semantic_label | text | change label from the semantic (embedding) method; NULL when not analysed |
| material | text | `Yes` / `No` from the materiality model; NULL when not analysed |
| materiality_score | float | 0-1; NULL when not analysed |
| confidence_score | float | 0-1, the materiality model's confidence; NULL when not analysed |
| word_count_delta | int | current minus previous word count |
| length_ratio | float | current / previous length |
| key_change, explanation, materiality_reason | text | model explanations |
| previous_text, current_text | text | the disclosure text |

## View `chatbot_synthesis_trends` — financial figures computed from XBRL (only AMD and GS have them)

| Column | Meaning |
|---|---|
| company | lowercase ticker (`amd`, `gs`) |
| kind | `calculated_metric` (a ratio for one fiscal `year`, in `value`) or `year_over_year_change` (`from_year` → `to_year`, `old_value`, `new_value`, `growth_pct`) |
| metric | ratio names: `Gross margin`, `Operating margin`, `Net margin`, `Current ratio`, `Debt / equity`, `Capital expenditure / revenue`, `Operating cash flow / net income`. Growth rows use XBRL labels, e.g. `Revenue from Contract with Customer, Excluding Assessed Tax`, `Net Income (Loss) Attributable to Parent`, `Earnings Per Share, Diluted`, `Gross Profit`, `Operating Income (Loss)`, `Assets` |
| unit | `%` (value already in percent, e.g. 46.1), `x`, `USD` |
| year, from_year, to_year | int |
| value, old_value, new_value, growth_pct | numeric (growth_pct already in percent) |

## Definitions (apply these exactly)

- **Years.** "the 2024 10-K", "in 2024", "for 2024", "FY2024" = `current_year = 2024`.
  "between 2023 and 2024" = `previous_year = 2023 AND current_year = 2024`. "across both years", "from 2023 to 2025",
  no year mentioned = no year filter. For financial figures, a year is the fiscal year of the figure.
- **Material** = `material = 'Yes'`. **Analysed** = `analysis_status = 'completed'`.
- **Changed / changes made** (when asking which disclosures *changed*) = `change_type IN ('Modified','Reworded','Expanded','Reduced','New','Removed')`.
  "alignments" or "changes" used as a plain noun to count rows ("how many changes have materiality above 0.8") = rows, no change_type filter.
- **Added / new** = `change_type = 'New'`. **Removed / dropped** = `'Removed'`. **Shortened** = `'Reduced'`. **Expanded** = `'Expanded'`. **Reworded** = `'Reworded'`.
- **Flagged for review / needs review** = `status = 'needs_review'`. **Merged** = `relationship = 'many_to_one'`. **Split** = `'one_to_many'`.
- **Risk factors** = Item `1A`. **MD&A** = `7`. **Financial statements** = `8`. **Business section** = `1`. **Exhibits** = `15`.
- **Disagreement between the methods** = `lexical_label IS NOT NULL AND semantic_label IS NOT NULL AND lexical_label <> semantic_label`.
- **Banks** = bac, jpm, gs, ms. **Chipmakers** = intc, amd, nvda, avgo, qcom, mu.
- **Percentages / shares** in results: 0-100, not 0-1.

## Output shape by answer type (each question states its type)

| answer_type | Return |
|---|---|
| `count` | one row, one column `n` |
| `ids` | every matching row's `match_id` (no LIMIT) |
| `topn` | every qualifying row's `match_id` and the ranking column as `score`, ordered best first, **no LIMIT** (the scorer applies N and handles ties) |
| `groups` | the grouping column(s) named as in the view, plus `n` (or the stated measure), one row per group |
| `value` | one row, one column `value` |
| `table` | the columns that answer the question, with clear names |
