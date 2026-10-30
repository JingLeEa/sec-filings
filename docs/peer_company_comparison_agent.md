# Peer company comparison agent

## Purpose

Compare target disclosures marked material in stage 5 with same-year disclosures from one or more peers. The agent uses same-Item TF-IDF retrieval to select the top three candidates per peer, then compares the source text and cites exact source sentences. It also loads adjacent-year alignment reports as context.

## Data from `test-data`

The latest `test-data` branch contains disclosure files and aligned runs for 11 companies. The datasets are about 227 MiB, so this branch does not duplicate them. Keep the data branch in a separate worktree:

```bash
git fetch origin test-data
git worktree add ../sec-filings-test-data origin/test-data
```

Then point the agent at its data folders:

```bash
PYTHONPATH=src python3 scripts/compare_peer_disclosures.py \
  --ticker NVDA --peers AMD AVGO INTC --year 2024 \
  --disclosures-dir ../sec-filings-test-data/test_data/disclosures \
  --alignments-dir ../sec-filings-test-data/test_data/alignments \
  --workers 6 \
  --dry-run
```

The dry run validates the company/year metadata, required source sentences, alignment report completeness, Item scope, and candidate retrieval. It prints counts by Item, candidate coverage, and a small sample of target jobs. It makes no API calls and writes no files.

## Run the agent

Provide the SoCLaaS settings described in [LLM setup](ZIYANG_llm_setup.md), then run without `--dry-run`:

```bash
PYTHONPATH=src python3 scripts/compare_peer_disclosures.py \
  --ticker NVDA --peers AMD AVGO INTC --year 2024 \
  --disclosures-dir ../sec-filings-test-data/test_data/disclosures \
  --alignments-dir ../sec-filings-test-data/test_data/alignments \
  --env-file .env --workers 6
```

The default run processes every target disclosure whose stage 5 materiality label is `Yes`. Use `--target-selection all` to include all disclosures. The default Items are `1`, `1A`, `7`, and `8`; Item 15 can be added explicitly. TF-IDF retrieves up to three same-Item candidates per peer. The run continues until all selected disclosures finish. `--workers` controls concurrent target comparisons, defaults to 6, and accepts up to 20.

To run 2023, 2024, and 2025 for one target/peer pair with six workers per year, launch one command per year concurrently. That uses at most 18 comparison workers. Use a separate output directory for each year. Make sure all three runs use the same data and peer list.

```bash
for year in 2023 2024 2025; do
  PYTHONPATH=src python3 scripts/compare_peer_disclosures.py \
    --ticker NVDA --peers AMD --year "$year" \
    --disclosures-dir ../sec-filings-test-data/test_data/disclosures \
    --alignments-dir ../sec-filings-test-data/test_data/alignments \
    --output-dir "data/peer_comparison/nvda/${year}_amd_material_pilot" \
    --workers 6 --env-file .env &
done
wait
```

## Save a test run on the `test-data` branch

Keep the comparison code worktree on its feature branch and check out `test-data` in the separate data worktree. The current setup uses a detached data worktree, so create a local tracking branch once:

```bash
git -C ../sec-filings-test-data fetch origin test-data
git -C ../sec-filings-test-data switch --track -c test-data origin/test-data
```

For later runs, update that worktree with `git -C ../sec-filings-test-data pull --ff-only`. Set `--output-dir` to a directory inside the data worktree; for example:

```bash
PYTHONPATH=src python3 scripts/compare_peer_disclosures.py \
  --ticker NVDA --peers AMD --year 2024 \
  --disclosures-dir ../sec-filings-test-data/test_data/disclosures \
  --alignments-dir ../sec-filings-test-data/test_data/alignments \
  --output-dir ../sec-filings-test-data/test_data/peer_comparison/nvda/2024_amd_material_pilot \
  --workers 6 --env-file .env
```

After the run, inspect the report and Git status in the data worktree, then commit the output folder there:

```bash
git -C ../sec-filings-test-data status --short
git -C ../sec-filings-test-data add test_data/peer_comparison/nvda/2024_amd_material_pilot
git -C ../sec-filings-test-data commit -m "Add NVDA AMD 2024 peer comparison pilot"
git -C ../sec-filings-test-data push origin test-data
```

The output folder contains the report, manifest, validated jobs, request records, and token usage. Keep credentials in `.env`; never add that file to Git. If the feature branch has a newer agent version, merge or cherry-pick that code into `test-data` only if the team wants the test-data branch to contain the runnable code too. The comparison command above runs the code from the current feature worktree while saving results to the data worktree.

## How it works

1. Load accepted and review-candidate disclosures for the target and every peer. Reject inconsistent company/year metadata, missing content, invalid Items, or missing/duplicate source sentence IDs.
2. Load completed adjacent-year alignment reports for each company when present. Preserve match status, review reasons, change taxonomy, materiality fields, and prior/current disclosure summaries as context. `needs_review` alignments remain flagged.
3. By default, select target disclosures with stage 5 `materiality_analysis.materiality == "Yes"` in an attached adjacent-year alignment. Use `--target-selection all` to compare every target disclosure.
4. Build a local TF-IDF index and retrieve up to three same-Item candidates from each peer for each selected target. Retrieval scores are rankings, not probabilities.
5. Ask the LLM for supported peer comparisons. A peer with no retrieved candidates is recorded separately from a peer whose retrieved candidates were reviewed and not comparable.
6. Validate every cited disclosure and sentence ID against its source. Store source sentence text, paragraph IDs, and source URLs with the comparison.

The agent's statements are model-generated analysis, not human-verified conclusions. Alignment and materiality labels from the year-over-year reports provide context and are not treated as proof for peer comparisons.

## Outputs and resume behavior

The default output is `data/peer_comparison/TARGET/YEAR/`:

- `peer_comparison.json`: report, completion status, target-by-target peer results, citations, and usage totals.
- `manifest.json`: input fingerprint, scope, retrieval settings, and model identity. A different run configuration needs a new output directory.
- `jobs/`: validated results saved per target disclosure. Completed jobs are skipped on resume.
- `requests/`: exact prompt response records, keyed by request hash. Completed responses are reused; every request's usage is counted once.
- `token_usage.json`: provider-reported token totals and calls with unknown usage.

Runs use multiple worker threads when `--workers` is greater than one. Completed jobs are saved separately and skipped on resume. If any source or alignment input changes, start a new output directory; the manifest prevents mixing results from different inputs. The default material-change selection uses materiality `Yes` from the adjacent-year alignment windows available for the target year. For 2024, either 2023–2024 or 2024–2025 can qualify a disclosure.

## Limitations

Candidate retrieval may miss related disclosures; “no candidate retrieved” is not evidence that a peer does not discuss the topic. The agent compares only the supplied top candidates and is limited by the completeness of saved extraction/alignment files. It does not fetch SEC filings, choose the peer universe, or make investment recommendations. Live runs require SoCLaaS credentials and use paid tokens.
