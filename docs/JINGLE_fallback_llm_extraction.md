# Fallback LLM Item extraction

**Person in charge: JING LE**

This guide documents the LLM fallback used to locate SEC 10-K Items **1, 1A,
7, and 8**, its integration with the extractor, and its use by the full pipeline.
For the detailed printed-page algorithm, cache behavior, and comparison tools,
see [printed-page recovery](JINGLE_filing_page_recovery.md).

## Current implementation status

| Capability | `extract_filings.py` | `run_full_pipeline.py` |
| --- | --- | --- |
| Deterministic Item extraction first | Yes | Yes, through each year's text-preprocessing command |
| Baseline LLM fallback | Automatic when needed, unless disabled | Inherited automatically from the extractor |
| Additional printed-page recovery | Automatic fallback after the baseline locator | Inherited automatically from the extractor |
| Disable printed-page recovery only | `--no-agent-page-recovery` | Not exposed by the runner |
| Force an agent check | `--agent-check-items 1 1A 7 8` | Not exposed by the runner |
| Disable Item-agent recovery | `--no-agent-fallback` | Not exposed by the runner |
| Set agent request timeout/token limit | `--agent-timeout`, `--agent-max-tokens` | Not exposed; extractor defaults apply |

**The fallback is implemented, enabled by default, and connected to both entry
points.** Automatic page recovery does not mean an LLM runs on every filing. If deterministic
extraction succeeds without a qualifying suspect pointer, the agents are skipped.

This is part of **text preprocessing**. The later pipeline stage named
`extraction` runs disclosure extraction on the resulting chunks. That downstream
LLM task is separate from the Item-boundary fallback described here.

## What the LLM returns

The LLM locates existing content; it does not generate or rewrite the extracted
Item body. Depending on the stage, it returns JSON containing table IDs, index
row references, titles and printed pages, narrative-block ranges, or footer
source-event IDs. Code validates the response and copies the original text.
Normal merging, footnote resolution, chunk generation, and sentence generation
then apply to the selected blocks.

Each API request contains a system prompt and the evidence for that request.
Calls do not retain a conversation history. A later stage sees earlier findings
only when code includes them in its prompt. Filing text is treated as untrusted
evidence, never instructions.

The provider/model is selected by `SOCLAAS_BASE_URL`, `SOCLAAS_API_KEY`, and
`SOCLAAS_MODEL`; a particular Qwen model is not hardcoded into the locator.
The shared client uses JSON-mode Chat Completions and exposes reported usage and
the actual response model to callers. A configured alias such as `default` can
refer to a different backend over time.

## Implementation files

| File | Responsibility |
| --- | --- |
| [scripts/extract_filings.py](../scripts/extract_filings.py) | CLI entry point for filing text extraction |
| [sec_10k_extractor.py](../src/sec_disclosure/extraction/sec_10k_extractor.py) | Deterministic extraction, fallback triggers, accepted replacements, report references, and final outputs |
| [filing_item_fallback.py](../src/sec_disclosure/agents/filing_item_fallback.py) | Shared Item system prompt, JSON validation/retries, and independent-agent output writing |
| [filing_item_locator.py](../src/sec_disclosure/agents/filing_item_locator.py) | Baseline evidence stages and automatic page-recovery orchestration |
| [filing_page_locator.py](../src/sec_disclosure/agents/filing_page_locator.py) | Table-preserving HTML chunks, printed-page mapping, pointer resolution, cache, and source-ID translation |
| [llm/config.py](../src/sec_disclosure/llm/config.py) / [llm/client.py](../src/sec_disclosure/llm/client.py) | Model configuration and bounded API requests |
| [run_full_pipeline.py](../src/sec_disclosure/pipelines/run_full_pipeline.py) | Builds and executes yearly preprocessing and downstream stages |

## Integration with the extractor

`scripts/extract_filings.py` calls `sec_10k_extractor.main()`. Its relevant order is:

1. Download/read the filing and build source blocks and structural evidence.
2. Try deterministic Item headings and index-guided extraction. Resolve
   supplementary Item 15 content where applicable. If no block sections exist,
   also try heading extraction from cleaned filing text.
3. Build `fallback_targets` from requested Items still missing and Items explicitly
   listed in `--agent-check-items`. Call `apply_item_fallback()`.
4. That wrapper checks the supported Items, disable flag, and LLM settings, then
   calls `locate_item_blocks()`. Valid selections replace the corresponding Item
   only when source-overlap checks permit them. Blocks can move out of
   supplementary Item 15; overlap with another core Item rejects the replacement.
5. Process embedded reports, merge blocks, and follow incorporated-report
   references unless `--no-follow-references` was supplied.
6. Make an additional fallback call for recognized short Item 7/8 pointers that
   were not already recovered or resolved through a referenced report. Default
   pointer detection includes wording such as “included in this Annual Report”
   with F-numbered page references; opting out of page recovery retains the
   narrower baseline pointer detection.
7. Deduplicate shared content, resolve footnotes, and write the usual Item,
   chunk, sentence, and source-audit files under `data/raw/<company>/<year>/`.

The wrapper does not read the comparison tool's `agent/` output directory.
Production invokes the locator on its own source evidence.

## Baseline locator stages

These stages are a fixed sequence of requests controlled by code, rather than
an autonomous tool-using agent. Later recovery stages run for unresolved Items.

| Stage | Evidence supplied | Model response |
| --- | --- | --- |
| Navigation-table inventory | Table previews in windows of up to 45 tables | Relevant table IDs |
| Index reading | Selected table rows, up to 45 per window | Item/title/page references tied to row IDs |
| Indexed selection | Parsed page map; excerpts and index hints for overlapping page ownership | Ownership of shared page spans, when needed |
| Heading-based selection | Filtered candidate headings and extracted index hints | Start/end-exclusive narrative-block ranges |
| Outline search | Up to 120 shortened candidate blocks per window, with index hints | Candidate start/end locations |
| Outline range selection | Model-located boundaries | Validated source ranges |

The complete original cross-reference index is **not** sent in every baseline
prompt. Index-reading requests receive selected rows; later stages generally
receive extracted references. The final outline range-selection request uses
boundary candidates and a generic context message without the original index.

The baseline narrative-block stream excludes tables. Its separate structural
stream retains tables for index reading. This distinction explains the original
INTC failure: the model could read its index, but table-based section headings
and footers were unavailable or unrecognized in the body-mapping route.

## When printed-page recovery runs

Within an agent invocation, printed-page recovery runs automatically after the
baseline attempts unless `--no-agent-page-recovery` was supplied, when either:

- A requested Item has no validated selection; or
- An explicit short Item 7/8 section points to printed pages, even if a candidate
  body has already been returned.

The stage preserves tables in bounded HTML chunks, retains original index
evidence, and asks for printed-page footer source IDs. It can reread missing
boundaries, recognize repeated footer layouts from validated examples, and
follow explicit body-page pointers such as `F-1 through F-27`. Source-quoted
references are checked for complete scope, including disjoint supplementary
paragraphs. Another Item's pointer, a date, or an audit-firm identifier is not
valid evidence for the requested body's page range.

Footer IDs belong to the **structural source-event stream**. They are translated
to the original narrative-block IDs before extraction; they are not used as
output chunk IDs. Existing narrative table-filtering rules still apply.

Usable page references are required. If those cannot be established, the stage
leaves the Item unresolved. There is no CLI flag to force page recovery
unconditionally. A nonempty incorrect selection can still bypass the stage if
neither trigger applies.

## Configuration and extractor commands

For standalone extraction, `--env-file .env` supplies the SoCLaaS settings.
Without that flag, the configuration loader uses the default
`~/.config/soclaas/soclaas.env` when present. Exported environment variables take
priority. Pass your actual SEC contact through `--user-agent`, or set
`SEC_USER_AGENT` in the environment. SEC identification is unrelated to LLM
selection or triggering.

Run extraction with the automatic fallback chain; no enable flag is needed:

```bash
.venv/bin/python scripts/extract_filings.py \
  --ticker intc --year 2025 --env-file .env \
  --user-agent "Your Name you@example.com"
```

Force a baseline agent check, retaining automatic subsequent page recovery:

```bash
.venv/bin/python scripts/extract_filings.py \
  --ticker txn --year 2025 --env-file .env \
  --user-agent "Your Name you@example.com" \
  --agent-check-items 1 1A 7 8
```

Use `--agent-check-items 8` for just Item 8. Add `--no-agent-page-recovery` to check
with the baseline alone. `--agent-page-recovery` remains a compatible explicit
enable flag, but is no longer required. `--no-agent-fallback` takes precedence and disables
both agent paths even if check/recovery flags are present. Item 15 is not a
supported output of this LLM locator.

Default baseline request settings are 180 seconds and 1,200 completion tokens,
controlled by `--agent-timeout` and `--agent-max-tokens`. JSON/validation and
retryable failures allow three retries after the first attempt. The footer
mapper uses at least 2,400 completion tokens, two concurrent chunk workers, and
at most one retry per chunk; pointer-resolution requests also allow one retry.
Timeouts bound individual attempts, not the whole filing workflow.

## Integration with `run_full_pipeline.py`

The full runner schedules the following preprocessing commands for each year:

```text
extract_filings.py --ticker <ticker> --year <year> ...
load_to_postgres.py --company <ticker> --year <year> ... --sync
```

Both fallback stages are available automatically through the first command.
The runner keeps the extractor's recovery defaults and internally passes a
PostgreSQL-backed `--agent-page-cache-dir`. Page maps, pointer resolutions, and
cached requests are stored in `pipeline_artifacts`, under the selected cache
namespace's `filing_page_cache/` prefix. Local exports do not include this cache;
changing `--data-dir` does not change its active storage location.

SEC timeout retries also live in the extractor. A timed-out download restarts
that filing's extraction with increasing per-download timeouts of 60, 180, then
300 seconds. This can repeat work from the earlier attempt; the runner waits
for the extractor's final result before saving the text. Other errors are not
retried. Customize the standalone extractor with `--sec-timeouts 90 240 360`,
or use `--sec-timeout 180` for one attempt. The runner exposes neither flag.

The runner loads its explicit env file and repository `.env` into the child
process environment without overriding existing variables. This supplies the
extractor's LLM settings; the runner does not currently forward `--env-file`
directly to its text-extraction commands. SEC identification is passed explicitly.
Preprocessing-only preflight does not require LLM credentials for filings that
deterministic code can extract. If an agent is needed, the extractor checks its
configuration and warns when unavailable. Downstream LLM stages still require
their configuration and dependencies in full-pipeline preflight.

Inspect the generated commands without downloads, model requests, or database
writes:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker intc --years 2024 2025 --env-file .env \
  --user-agent "Your Name you@example.com" \
  --dry-run
```

Remove `--dry-run` to execute the normal pipeline, including its PostgreSQL saves
and downstream processing. The runner's resume behavior may skip completed
stages; `--from-stage extraction` or a later start skips text preprocessing
entirely, so this Item fallback does not run then.
The runner currently does **not** expose `--agent-check-items`,
`--no-agent-fallback`, `--agent-timeout`, `--agent-max-tokens`,
`--agent-page-recovery`, `--no-agent-page-recovery`, or `--agent-page-cache-dir`.
Use the standalone extractor when those controls are needed. The extractor's
default page fallback does not force an invocation on a filing already handled
by deterministic code.

## Logs, outputs, and failure behavior

| Evidence | Meaning |
| --- | --- |
| `Item fallback agent: checking ...` | The extractor invoked the Item locator |
| `Printed-page recovery: ...` | The additional stage was entered; footer/request/cache counts describe its mapping result |
| `Item <n>: recovered by fallback agent ...` | A source-selected replacement passed the extractor's acceptance checks |
| Missing-Item warning | At least one requested section remains absent |

Full-pipeline runs cache page and pointer requests in PostgreSQL. The standalone
extractor defaults to `data/filing_page_cache/` and accepts an explicit
`--agent-page-cache-dir`. Cached mappings can produce zero new footer requests. Baseline inventory and
range-selection requests are not covered by that page cache.

Normal `*_sources.json` files record primary/incorporated document sources, not
complete per-Item LLM stage provenance. Keep the console log or use comparison
runs and their response traces to establish which agent stages actually ran.
An enabled flag, a nonempty file, or a high text-overlap score is not proof of
page-agent use or boundary accuracy.

If LLM settings are missing or recovery fails, existing deterministic sections
are retained and warnings are printed. If no sections remain, extraction returns
failure and the full pipeline stops at that stage. If some sections remain,
extraction can return success despite missing Items, and the pipeline can load
partial results. There is no mandatory four-Item completeness gate yet.

Cross-document reference following is a separate extractor feature. For example,
WFC incorporates several Item bodies from an EX-13 shareholder report. Normal
extraction follows such reports by default; the independent comparison tool
intentionally tests only the primary filing and does not fetch those bodies.

## Verification and related guides

- [Printed-page recovery commands and algorithm](JINGLE_filing_page_recovery.md)
- [Current text-preprocessing flow](current_text_preprocessing_flow.md)
- [Full-pipeline guide](ZIYANG_full_pipeline.md)
- [Baseline retry/integration tests](../tests/test_filing_item_fallback.py)
- [Baseline locator tests](../tests/test_filing_item_locator.py)
- [Printed-page recovery tests](../tests/test_filing_page_locator.py)
- [Full-pipeline integration and timeout tests](../tests/test_full_pipeline.py)

The comparison commands run deterministic extraction and independent agent
outputs separately; their agent folders are diagnostics, not inputs automatically
loaded by production. See the printed-page guide for reproducible single-filing
and company-list comparisons.
