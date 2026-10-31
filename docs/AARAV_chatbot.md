# Disclosure Change Chatbot

Ask natural-language questions about the pipeline's disclosure changes and get answers grounded in
PostgreSQL rows, with every claim cited by `match_id`.

```
User question
    │
    ▼
Query classifier (planner.py)      rule-based: company, years, item, change type, materiality, status …
    │
    ├── structured intents ──► parameterised SQL on alignment_changes (queries.py)
    ├── topic questions ─────► pgvector cosine search + full-text search, fused with RRF (retrieval.py)
    └── anything else ───────► LLM-written SELECT, validated + read-only (sql_guard.py)
    │
    ▼
Answer synthesiser (synthesis.py)  LLM over an evidence pack, or templated answer when offline
    │
    ▼
Grounding check (grounding.py)     cited IDs must exist, quotes must appear in the evidence
```

## Run it

```bash
# 1. Database (pgvector/pg16, schema auto-applied on first start)
docker compose up -d
pip install -r requirements.txt -r app/requirements.txt

# 2. Data: either run the pipeline on main (everything is saved to PostgreSQL) ...
python scripts/run_full_pipeline.py --ticker AMD --years 2023 2024 2025       # alignments, materiality, synthesis
python scripts/run_full_pipeline.py --ticker NVDA --years 2023 2024 2025
python scripts/run_peer_comparison_pipeline.py --ticker AMD --peers NVDA --year 2024

# ... or load the team's saved outputs from the test-data branch (merged into the chatbot branch)
for c in amd avgo bac c gs intc jpm ms mu nvda qcom; do
  for p in 2023-2024 2024-2025; do
    python scripts/load_alignments_to_postgres.py --company $c \
      --previous-year ${p%-*} --current-year ${p#*-} --dir test_data/alignments/$c/$p
  done
done
python scripts/load_peer_comparisons_to_postgres.py test_data/peer_comparison
python scripts/load_synthesis_to_postgres.py test_data/synthesis

# 3. Chat
streamlit run app/streamlit_app.py                       # web UI
PYTHONPATH=src python -m sec_disclosure.chatbot          # interactive CLI
PYTHONPATH=src python -m sec_disclosure.chatbot --show-sql "How many new risk factors did Intel add in 2025?"
```

All data processing lives on `main` (see [Full PostgreSQL Pipeline](ZIYANG_full_pipeline.md) and
[Peer comparison pipeline](ARIEL_ZIYANG_peer_company_comparison_agent.md)). The pipeline saves alignments,
materiality, synthesis reports and peer comparisons straight to PostgreSQL, and the chatbot reads them from
there: nothing needs loading after a pipeline run. The loaders are only for output files made elsewhere, such as
the samples in `test_data`.

On Windows PowerShell, run the loader once per folder, e.g.
`python scripts/load_alignments_to_postgres.py --company bac --previous-year 2023 --current-year 2024 --dir test_data/alignments/bac/2023-2024`,
and set `$env:PYTHONPATH="src"` before `python -m sec_disclosure.chatbot`.

The chatbot creates its view (`db/init/09_chatbot.sql`) on start-up if your Docker volume predates it.
To apply it by hand: `docker compose exec -T db psql -U sec -d sec_disclosure < db/init/09_chatbot.sql`.

### LLM answers (SoCLaaS)

The chatbot uses SoCLaaS as its only LLM service, with the pipeline's settings
(see [LLM setup](ZIYANG_llm_setup.md)). Without them it runs **offline**: the same SQL and retrieval,
with templated Markdown answers.

```dotenv
SOCLAAS_BASE_URL="https://soclaas-api.comp.nus.edu.sg/v1"
SOCLAAS_API_KEY="..."
SOCLAAS_MODEL="default"
SOCLAAS_CHATBOT_MODEL="qwen3.6:35b"   # optional: chatbot model; pipeline stages keep SOCLAAS_MODEL
```

Each value comes from the first place that sets it: an exported environment variable, then the repository
`.env`, then `~/.config/soclaas/soclaas.env`. The sidebar (and CLI banner) shows the model and where the key
came from, never the key itself. Check the connection with one small request:

```bash
python -m sec_disclosure.chatbot --check
```

HTTP 401 means SoCLaaS rejected the key: check that the "key from" source is the file you edited (a leftover
exported `SOCLAAS_API_KEY` wins over `.env`), then regenerate the key in the SoCLaaS portal if needed.

Reasoning models are supported: `<think>…</think>` blocks are stripped before parsing and grounding, and token
budgets (3,000 for SQL, 6,000 for answers) and the 180 s timeout leave room for the model to think first.

### Semantic search (optional)

Topic questions ("what changed about climate risk") always work through PostgreSQL full-text search with a
small finance/tech synonym list. For pgvector search, embed the alignment evidence once with bge-m3:

```bash
python scripts/embed_alignment_evidence.py --dry-run   # count texts
python scripts/embed_alignment_evidence.py             # needs SOCLAAS_* ; cached under data/embeddings
```

This fills `chatbot_evidence_embeddings` (one vector per alignment side). If `disclosure_embeddings` is
populated by the semantic-retrieval stage, it is searched too. Vector and keyword rankings are merged with
reciprocal rank fusion; the reply says which methods ran (`pgvector:evidence + full_text`).

## What it answers

| Question shape | Intent | Source |
|---|---|---|
| most material changes in BAC's 2024 10-K | `material_changes` | `material = 'Yes'` ordered by materiality score |
| how many new risk factors did Intel add in 2025 | `count` | counts + examples; also reports needs-review candidates not yet labelled |
| removed / expanded / reworded … in Item 1A | `list_changes` | filtered rows ordered by materiality |
| what changed about climate risk | `topic_search` | pgvector + full-text retrieval |
| low confidence materiality assessments | `low_confidence` | `confidence_score ≤ 0.8` (or the threshold you give) |
| compare BAC and JPM 2024-2025 | `compare_companies` | change profile per company/period + top material changes |
| most common types of changes in Item 7 | `distribution` | label counts, share, material count |
| semantic vs lexical disagreements | `method_disagreements` | label pairs + agreement rate + examples |
| summarise key changes for BAC 2023→2024 | `summary` | distribution, by-item, top material, new, removed |
| tell me about bac_2023_2024_M0107 | `match_detail` | full row incl. original text |
| compare NVIDIA and AMD on supply chain / how does NVDA compare to its peers on X | `peer_comparison` | peer-agent output first, alignments as fallback (below) |
| summarise AMD 2023→2025 (optionally "about <topic>") | `summary` | synthesis report first, SQL summary as fallback (below) |
| how did AMD's gross margin / revenue / debt change | `financial_trends` | synthesis-report trends computed from XBRL |
| anything else | `custom_sql` | LLM writes a SELECT (offline: keyword search) |

Year convention: years are 10-K fiscal years. "2024 10-K" means `current_year = 2024` (the 2023→2024 run);
"between 2023 and 2024" sets both years; "since 2023" sets `previous_year`. Short follow-ups
("what about JPM?") reuse the previous question's scope.

## Peer comparisons (peer-agent output first)

Questions about how two or more companies disclose the same topic are answered from the peer-comparison
agent's precomputed output (`agents/peer_comparison.py` on main) before touching the alignments. A lookup is one
indexed query (~30 ms on the fixtures, vs ~0.5 s for a full-text search over all alignments), and it answers
something alignments cannot: alignments compare a company with itself across years, the peer agent compares
companies with each other in the same year.

**Where the data comes from.** The peer comparison pipeline (`scripts/run_peer_comparison_pipeline.py` on main)
saves each whole report to `peer_comparison_reports` (`db/init/07_peer_comparison_reports.sql`). That table is
the source of truth. For fast queries the chatbot keeps a normalised copy in its own tables
(`db/init/10_chatbot_peer_comparisons.sql`): on start-up it rebuilds the copy for every report added or re-saved
since the last start, and drops copies whose report is gone. Report files made elsewhere are saved to the same
table, exactly as the pipeline saves them, with:

```bash
python scripts/load_peer_comparisons_to_postgres.py test_data/peer_comparison   # every run under the folder
python scripts/load_peer_comparisons_to_postgres.py data/peer_comparison/nvda/2024 --dry-run   # validate only
```

The chatbot's tables: `peer_comparison_runs` (one per report: target, year, peer set, Items and target
selection; partial runs allowed and flagged), `peer_comparison_targets`, `peer_comparison_results` (compared /
no_comparable_candidate / no_candidate_retrieved per peer), `peer_comparisons` and `peer_comparison_evidence`
(cited sentences), plus the `peer_comparison_pairs` view (both companies side by side with quotes).
`target_selection` records whether the agent compared only the target's disclosures marked material
(`material_changes`, the default) or all of them; answers from a material-only run say so, because a topic missing
there may still be disclosed. When two runs hold the same comparison (the NVDA–AMD pilot and the 2024 industry
run), only the copy from the most recently saved report is shown.

How a peer question is answered:

1. **Coverage** – which loaded runs include the named companies for that fiscal year ("2024 10-K" → 2024).
   One company plus "peers"/"competitors" means every company it was run against.
2. **Lookup** – comparisons for each pair in either direction (NVDA→AMD or AMD→NVDA runs both count). With a
   topic, every word of the topic (or the exact phrase) must appear, so "export controls" does not match
   "change in control"; single words still use the synonym list.
3. **No-match decisions** – disclosures where the agent found no comparable peer disclosure are shown as such
   (not as "the peer never discusses this").
4. **Year-over-year context** – each compared disclosure's own change label from `alignment_changes`, joined by
   disclosure ID, so answers can say "AMD modified this in 2024 while NVIDIA left it unchanged".
5. **Fallback** – when no peer run covers the pair, or nothing matches the topic, the chatbot says so and shows
   each company's own alignment data for that year (topic search, or change profiles when there is no topic).

Answers cite both disclosure IDs (`[nvda_2024_1A_D002 ↔ amd_2024_1A_D063]`) and the cited sentence IDs; the
grounding check removes sentence IDs that are not in the retrieved peer evidence. Partial runs and missing
pairs are stated in the answer.

Loaded peer data on the test-data branch (`test_data/peer_comparison`):

- `2024_industry_top2/<ticker>`: 11 runs for fiscal 2024, each company against its industry. Banks (BAC, C, GS,
  JPM, MS) against each other; chipmakers (AMD, AVGO, INTC, MU, NVDA, QCOM) against each other. Disclosures
  marked material only, all complete: 2,345 target disclosures and 6,720 supported comparisons.
- `nvda/2024_amd_material_pilot`: the first NVDA vs AMD pilot (105 disclosures, 109 comparisons, 8 "no comparable
  candidate" decisions), used by the integration tests.

Peer runs stay within an industry, so a bank-vs-chipmaker question ("NVDA vs JPMorgan") falls back to each
company's own alignments.

## Synthesis reports (summary and financial questions)

The synthesis agent (`agents/synthesis.py` on main, run as the last stage of `scripts/run_full_pipeline.py`) writes
one report per company and span of years: an executive summary, key findings citing alignment match_ids, a
longitudinal and a financial analysis, limitations, and financial trends computed in code from XBRL facts.

The pipeline saves every report to `synthesis_reports(company, years, report jsonb)`
(`db/init/06_synthesis_reports.sql`), so after a pipeline run there is nothing to load. The chatbot only reads
that table, through three views in `db/init/11_chatbot_synthesis.sql` (created automatically):

- `chatbot_synthesis_reports`: one row per report, keyed `amd_2023_2024_2025`.
- `chatbot_synthesis_findings`: one row per key finding, with the match_ids and XBRL fact IDs it cites.
- `chatbot_synthesis_trends`: growth and ratio rows.

A report counts as out of date when the alignments for its years were reloaded after its `generated_at`.

Report files made elsewhere (the samples in `test_data/synthesis`) are saved to the same table with:

```bash
python scripts/load_synthesis_to_postgres.py test_data/synthesis
```

The samples cover 2023-2025 for AMD, AVGO, BAC, INTC, JPM, MS, MU, NVDA and QCOM, and 2023-2024 for GS. Only AMD
and GS include financial trends (the others were generated without XBRL facts loaded), so financial questions
about the other companies say no computed figures are loaded.

Schema version 1 of `11_chatbot_synthesis.sql` created a chatbot-owned `synthesis_reports` with a different
layout; version 2 drops that old layout automatically (reload any report files afterwards).

- **Summary questions** ("Summarise the key changes for AMD from 2023 to 2025") use the report whose span covers
  the asked years (none asked = the latest); a topic ("…about data center") narrows the findings. Each cited
  match_id is joined to `alignment_changes` for its label and original text. Companies without a report get the
  SQL summary over their alignments, and the answer says so.
- **Financial questions** ("How did AMD's gross margin change?", "revenue growth") read `chatbot_synthesis_trends`, so
  every figure is a deterministic calculation with its formula and source XBRL fact IDs. The alignments hold no
  figures, so without a report the chatbot says no financial data is loaded and shows disclosures that discuss
  the metric instead. Questions about what a company *says* ("…describe liquidity risk") stay disclosure
  questions.
- **Staleness**: if a company's alignments were reloaded after its report, the answer warns that the report may be
  out of date; a report that failed the agent's own validation is flagged too.

Tests use the AMD sample in `test_data/synthesis`. AMD 2023-2025: all 180 match_ids it cites exist in
test_data and are marked material. GS 2023-2024: 179 of 180 exist, but only 37 of them are marked material in the
test_data alignments, so GS answers can show a cited finding next to an "Unchanged" or "needs review" label.

The chatbot's schema files (09–11, after main's 01–08) record a version in `schema_info`; the chatbot and the
loaders re-apply any file whose version is missing or older, so an existing Docker volume picks up new columns,
tables and views without a reset. (They were numbered 06–08 before main added its own 06–08.)

## Data model

`alignment_changes` (view) flattens `alignments.change_analysis` and `extra->'materiality_analysis'` into typed
columns and adds item/section/taxonomy and summaries from `alignment_members`, plus the original filing
sentences from the alignment's `evidence` (`previous_text`, `current_text`). It does not need the
`disclosures` table, which the alignment fixtures do not load. Rows in `needs_review` have no change label
(`change_type IS NULL`); answers say so instead of silently dropping them.

## Safety and grounding

- Values from questions are always bound parameters. LLM-written SQL must be a single SELECT/WITH over the
  allowed tables (no system catalogs, `pg_*` functions, `INTO`, writes), is wrapped in `LIMIT 200`, and runs in a
  `READ ONLY` transaction with a 15 s statement timeout. A rejected query is sent back to the model once with
  the reason.
- The answer prompt allows only facts and IDs from the evidence pack. Afterwards every cited match/disclosure
  ID is checked: IDs that exist nowhere are stripped from the answer and reported; IDs that exist but were not
  retrieved are flagged. Quotations of 40+ characters must appear verbatim (ellipses allowed) in the evidence.
- If the LLM call fails, the templated answer is shown with a warning.

## Tests

Accuracy evaluations (filter extraction and SQL accuracy), their results and the changes made from them are
documented in [AARAV_chatbot_evaluation.md](AARAV_chatbot_evaluation.md).

```bash
python -m pytest tests/test_chatbot.py                # no database needed
python -m pytest tests/test_chatbot_integration.py    # needs the six fixture runs loaded; skips otherwise
python -m pytest tests/test_peer_chatbot_integration.py       # real NVDA/AMD pilot + pipeline-save sync; needs NVDA and AMD alignments
python -m pytest tests/test_synthesis_chatbot_integration.py  # AMD 2023-2025 report; needs AMD alignments
```

Integration tests compute expected numbers from the fixture JSON files independently of the chatbot's SQL
(counts per label, Item 7 distribution, disagreement totals, removed JPM risk factors, …), check that every
example answer cites only real `match_id`s, exercise the pgvector path with a deterministic toy embedder, and
exercise the LLM paths with a scripted model (hallucinated ID removal, unsafe SQL rejection, failure fallback).

## Files

```
src/sec_disclosure/chatbot/
  engine.py      Chatbot.ask() orchestration, LLM SQL generation
  planner.py     intent classification, topic extraction, follow-up context
  entities.py    filter extraction (companies, years, items, change types, thresholds)
  queries.py     parameterised SQL per intent
  retrieval.py   pgvector + full-text retrieval, RRF
  sql_guard.py   validation of LLM-written SQL
  synthesis.py   evidence pack, system prompt, offline templates
  grounding.py   citation and quotation checks
  db.py          read-only PostgreSQL access via storage.disclosures.connection_arguments()
  config.py      SOCLAAS_* settings and where each came from
  __main__.py    CLI
app/streamlit_app.py                 web UI
db/init/09_chatbot.sql               alignment_changes view, chatbot_evidence_embeddings table
scripts/embed_alignment_evidence.py  optional bge-m3 vectors for pgvector search
db/init/10_chatbot_peer_comparisons.sql      peer-comparison tables and peer_comparison_pairs view
src/sec_disclosure/storage/peer_comparisons.py   sync from peer_comparison_reports; file loader (scripts/load_peer_comparisons_to_postgres.py)
src/sec_disclosure/chatbot/peers.py  peer lookup, year-over-year join, fallback filters
src/sec_disclosure/chatbot/reports.py  synthesis-report lookup: summaries, findings evidence, financial trends
db/init/11_chatbot_synthesis.sql     chatbot_synthesis_* views over the agent's synthesis_reports
src/sec_disclosure/storage/synthesis_reports.py, scripts/load_synthesis_to_postgres.py   loader
src/sec_disclosure/storage/chatbot_schema.py   versioned schema files 09-11
```
