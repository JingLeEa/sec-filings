# Peer company comparison agent

## Purpose

Compare a target company's SEC 10-K disclosures with same-year disclosures from multiple peers. The agent uses same-Item TF-IDF retrieval to select candidate topics, then compares the source text and cites exact source sentences. It also loads adjacent-year alignment reports when available, so the result can show how the target and peer each described or classified the topic over time.

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
  --env-file .env --max-requests 5
```

`--max-requests 5` is a useful paid-call pilot; increase it for broader coverage. The default Items are `1`, `1A`, `7`, and `8`. Item 15 can be added explicitly. The default is three candidate disclosures per peer for each target disclosure. `--top-k`, `--items`, `--max-requests`, `--max-tokens`, `--max-prompt-chars`, and `--output-dir` control scope and runtime. The CLI uses one bounded LLM decision per target disclosure and allows multiple supported matches for each peer.

## How it works

1. Load accepted and review-candidate disclosures for the target and every peer. Reject inconsistent company/year metadata, missing content, invalid Items, or missing/duplicate source sentence IDs.
2. Load completed adjacent-year alignment reports for each company when present. Preserve match status, review reasons, change taxonomy, materiality fields, and prior/current disclosure summaries as context. `needs_review` alignments remain flagged.
3. Build a local TF-IDF index across all companies. For each target disclosure, retrieve candidates from each peer within the exact same SEC Item. Retrieval scores are rankings, not probabilities.
4. Ask the LLM for zero or more supported peer comparisons per company. A peer with no retrieved candidates is recorded separately from a peer whose retrieved candidates were reviewed and not comparable.
5. Validate every cited disclosure and sentence ID against its source. Store source sentence text, paragraph IDs, and source URLs with the comparison.

The agent's statements are model-generated analysis, not human-verified conclusions. Alignment and materiality labels from the year-over-year reports provide context and are not treated as proof for peer comparisons.

## Outputs and resume behavior

The default output is `data/peer_comparison/TARGET/YEAR/`:

- `peer_comparison.json`: report, completion status, target-by-target peer results, citations, and usage totals.
- `manifest.json`: input fingerprint, scope, retrieval settings, and model identity. A different run configuration needs a new output directory.
- `jobs/`: validated results saved per target disclosure. Completed jobs are skipped on resume.
- `requests/`: exact prompt response records, keyed by request hash. Completed responses are reused; every request's usage is counted once.
- `token_usage.json`: provider-reported token totals and calls with unknown usage.

When `--max-requests` is reached, the report is partial. Rerun with the same arguments and output directory to continue. If any source or alignment input changes, start a new output directory; the manifest prevents mixing results from different inputs.

## Limitations

Candidate retrieval may miss related disclosures; “no candidate retrieved” is not evidence that a peer does not discuss the topic. The agent compares only the supplied top candidates and is limited by the completeness of saved extraction/alignment files. It does not fetch SEC filings, choose the peer universe, or make investment recommendations. Live runs require SoCLaaS credentials and use paid tokens.
