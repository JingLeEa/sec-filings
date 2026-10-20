# Peer company comparison agent

## Purpose

Compare the saved disclosures of one target company with one or more peer companies for the same fiscal year. The agent compares topic-level evidence within the same SEC Item, cites the selected source sentences, and reports when none of the retrieved peer candidates supports a comparison.

## Inputs

Run disclosure extraction first for every company and the selected year. The default input root is `data/disclosures/` and each company needs `TICKER/YEAR/disclosures.json`. The loader also includes `review_candidates.json` when present. Company and year metadata, Item labels, selected content, and source sentence IDs are checked before use.

## Run

Validate inputs and see candidate coverage without API credentials or writes:

```bash
PYTHONPATH=src python3 scripts/compare_peer_disclosures.py \
  --ticker NVDA --peers AMD INTC --year 2024 --dry-run
```

Run the agent using the SoCLaaS settings described in [LLM setup](ZIYANG_llm_setup.md):

```bash
PYTHONPATH=src python3 scripts/compare_peer_disclosures.py \
  --ticker NVDA --peers AMD INTC --year 2024 --env-file .env
```

The command defaults to Items `1`, `1A`, `7`, and `8`, retrieves up to three candidates per peer and target disclosure, and makes at most 1,000 target-disclosure calls. Set `--top-k`, `--items`, `--max-requests`, `--disclosures-dir`, or `--output-dir` to adjust scope and paths. Calls not completed because of `--max-requests` produce a partial report. Reruns currently recompute completed target jobs and may make additional paid calls.

## Outputs

Default output is `data/peer_comparison/TARGET/YEAR/`:

- `peer_comparison.json` is the report. It records completion, peer tickers, per-target decisions, cited evidence, and token usage.
- `requests/` stores each raw response and its reported usage before response validation.
- `token_usage.json` tracks cumulative reported usage and requests with unknown usage.

Candidate retrieval uses local TF-IDF scoring within the same SEC Item. Scores rank candidates and are not match probabilities. The model receives only the top candidates. A `no_candidate_match` decision means no supplied candidate was supported; it does not establish that the full peer filing has no counterpart. Model output citations are checked against source sentence IDs and returned with paragraph IDs, source URLs, and text.

## Limitations

This is a first working version. It does not fetch filings, perform full-file exhaustive search, infer market/industry peer sets, or classify materiality. It relies on prior extraction quality; review disclosures are included, and their source status is not yet propagated to each comparison. Candidate retrieval can miss valid comparisons. Agent decisions are evidence-grounded model output, not human verification. Live runs require SoCLaaS credentials and use paid tokens.
