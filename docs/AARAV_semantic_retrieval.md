# Semantic change detection — handoff

**Owner:** Aarav · **Stage:** semantic change detection (retrieval + classification)
**Branch:** `semantic-retrieval` · **Status:** retrieval smoke-tested on real filings; rule-based classification built and unit-tested

---

## What this is

`indexing/semantic_retrieval.py` finds the previous-year counterpart of a disclosure by
comparing **dense embeddings** rather than words.

Each disclosure is converted to a ~1000-dimension vector by `bge-m3`, where text with
similar meaning lands in a similar place. Finding counterparts is then a nearest-neighbour
search within one SEC Item: embed last year's disclosures, embed this year's, return the
closest.

This matters because a filer that rewrites a disclosure keeps the meaning and changes the
wording — "we expect continued demand for processing power" becomes "compute requirements
keep rising". Those are the same disclosure, and meaning-space puts them together.

---

## Interface — what downstream calls

```python
from sec_disclosure.indexing.semantic_retrieval import SemanticIndex
from sec_disclosure.llm.embeddings import make_embedder

index = SemanticIndex(records, embedder=make_embedder())

index.search(query, year, *, item=None, anchor=None, limit=5, offset=0)
index.candidates(previous_year, current_year, top_k=5)
```

The signatures and the returned hit shape match `DisclosureIndex`, so
`agents/disclosure_alignment.py:623` integrates by swapping the constructor and changing
nothing else.

**Records in:**

```python
{
  "intel_2025_1A_P044": {
      "disclosure_id": "intel_2025_1A_P044",
      "fiscal_year":   "2025",
      "item":          "1A",
      "section":       "Risk Factors",
      "content":       "We are making significant, long-term and inherently risky...",
      "summary":       "",     # from the LLM disclosure stage
      "taxonomy":      "",     # from the LLM disclosure stage
  },
  ...
}
```

**Candidates out** — ranked previous-year counterparts per current-year record:

```python
{
  "intel_2025_1A_P044": [
      {"disclosure_id": "intel_2024_1A_P041", "score": 0.612, "text_score": 0.612, "same_item": True},
      {"disclosure_id": "intel_2024_1A_P038", "score": 0.488, "text_score": 0.488, "same_item": True},
      ...
  ],
}
```

Entries added by the reverse pass carry `"retrieval_direction": "reverse"`.

### This stage outputs candidates, not decisions

It never says "this is a Modified disclosure". It says *"these five previous-year
disclosures are the plausible counterparts, best first."* Choosing the pair is the
alignment agent's job; labelling the change is change detection's.

**Retrieval cannot express "nothing matches."** `top_k` results come back even for a
genuinely new disclosure. Rejecting a bad top-1 is a decision for the layer above — see
*Thresholds*.

---

## Scoring

| Element | Value |
|---|---|
| Field blend | `0.35 × summary + 0.65 × content` |
| Same-taxonomy bonus | `+0.02` (only when a base score exists) |
| Section-similarity bonus | `+0.03 × section cosine` (same condition) |
| Scope | one SEC Item; cross-Item matches are never returned |
| Retrieval | forward, plus a reverse pass for split/merge coverage |

These constants are kept identical to those already used elsewhere in `indexing/`, so that
swapping retrieval implementations changes the similarity function and nothing else.

---

## ⚠️ The score ceiling is 0.65, not 1.0

While `summary` is empty — i.e. until the LLM disclosure stage lands — the 0.35 summary
term contributes nothing. **A perfect match scores 0.65.**

When summaries arrive the ceiling becomes 1.0 and **every hard-coded threshold silently
becomes too permissive**. Express cutoffs as a fraction of ceiling, or normalise before
comparing. `0.45` today is `≈0.69 of ceiling`; after the LLM stage, `0.45` is `0.45 of
ceiling` — a different and much weaker filter.

This is the single most likely integration bug. Please read it twice.

---

## Measured results

Intel **Item 1A**, FY2024 → FY2025. 182 + 165 = 347 records, ~49k tokens, ~$0.0005.
Real `bge-m3` via SoCLaaS.

### Smoke tests — all pass

| Check | Result | Establishes |
|---|---|---|
| Self-retrieval | 40/40 at 0.650 | normalisation and orientation are correct |
| Score separation | true median **0.649** vs random median **0.350**; 0.2% of random pairs above the true median | embeddings discriminate on real filing text |
| Verbatim carry-forward | 20/20 at 0.650 | nothing in the pipeline mutates text |

```
random pairs    0.225 ─── 0.350 ─── 0.385                 (min, median, p75)
true top-1      0.429 ─── 0.595 ─── 0.649 ─ 0.650          (min, p25, median, ceiling)
```

**The floor is 0.35, not 0.** Dense embeddings never score near zero — no two English
sentences are orthogonal. Do not reason as if 0 means unrelated.

### Performance — no vector store needed

Largest realistic block, Citigroup Item 8 (1,265 × 1,302 = 1.6M pairs, 1024-dim):
**build 0.8s, search 10s**. Brute-force cosine per Item is sufficient at this corpus size;
FAISS or similar only earns its keep across many companies.

### How much of the corpus is free

Intel Item 1A, FY2025: **50 of 165 chunks (30.3%) are verbatim identical** to FY2024.
Exact string matching finds those instantly, and the pipeline already reserves exact
matches before any fuzzy matching runs.

**Semantic's contribution lives in the other 69.7%.** Recall reported over all 165 would
be inflated by the free 30%.

---

## Thresholds

**There is no threshold in retrieval today** — it returns a ranking, not a decision. When
one is needed:

- Usable band from the data above: **0.42 – 0.50** (on the 0.65 ceiling)
- Provisional operating point: **0.45**
- **Err low.** This stage should optimise *recall*. A false positive costs the alignment
  agent one extra candidate to read; a false negative is unrecoverable — the true
  counterpart never reaches the agent at all.
- Do **not** carry a threshold over from any other matching method. Similarity scales are
  not comparable between methods, and a number that is strict on one is permissive on
  another.

This band is **provisional**. It comes from top-1 *scores*, not from *verified correct
matches*. Setting it properly needs labelled pairs — see below.

---

## Not yet established

| Established | Not established |
|---|---|
| The component is correct | that top-1 is the **right** counterpart |
| Embeddings discriminate on real filings | recall — how often the true match is found |
| A threshold is placeable | precision at any given threshold |

A confident score is not a correct match. Observed example: `intel_2025_1A_P008` scored
**0.623**, comfortably inside the "good" band, and on inspection was not a true
counterpart — Intel had rewritten that risk factor between years, so no 1:1 counterpart
exists. Retrieval still localised to the correct risk-factor cluster (P007–P009), which is
the intended behaviour.

**Known weakness: negation.** Embeddings score "we no longer expect X" as highly similar
to "we expect X". A 10-K is full of such hedging flips, and they are exactly the material
changes a reader cares about. Anything consuming these candidates should not treat a high
score as evidence that the meaning is unchanged.

### ⚠️ Ground truth must not come from another matcher's output

Any pair set generated by a different matching method encodes that method's recall: its
misses simply are not in the set. Scoring this stage against such a set penalises it for
every correct pair the other method missed, and rewards it for reproducing that method's
behaviour. Use such a set as a **candidate pool** to speed up human labelling, never as
the scorecard.

---

## Dependencies

| | |
|---|---|
| New Python package | `numpy>=1.26,<3` (only addition) |
| Embedding model | `bge-m3` via SoCLaaS — 1024-dim, 131k context, $0.01 / M input tokens |
| Config | `~/.config/soclaas/soclaas.env` — `SOCLAAS_BASE_URL`, `SOCLAAS_API_KEY`, `SOCLAAS_MODEL` |

`llm/embeddings.py` mirrors `llm/client.py`'s structure and error mapping, adding batching
and an on-disk cache keyed by `sha256(model + text)` under `data/embeddings/`. Re-runs are
free, which matters because tuning means re-running.

An empty `summary` yields a zero vector rather than an error, so records score on content
alone until the LLM disclosure stage populates that field.

---

## Classification — populating `change_analysis.semantic`

`indexing/semantic_classification.py` is a **rule-based** classifier that takes the
retrieval candidates produced by `SemanticIndex.candidates()` and writes a label + evidence
into each alignment row's `change_analysis.semantic` field.

### Interface

```python
from sec_disclosure.indexing.semantic_classification import classify_all

# alignment_rows: list of dicts from the alignment JSON (with previous_ids, current_ids)
# candidates: output of SemanticIndex.candidates()
# records: the flat disclosure records dict

classify_all(alignment_rows, candidates, records)
# Mutates each row in place: row["change_analysis"]["semantic"] = {...}
# Also sets row["change_analysis"]["status"] = "semantic_done"
```

### Output shape — `change_analysis.semantic`

```python
{
    "label": "Modified",           # Added | Removed | Unchanged | Modified | Expanded | Reduced | Relocated
    "matched_id": "intel_2024_1A_P041",
    "score": 0.612,                # raw retrieval score
    "normalised_score": 0.941,     # score / ceiling — comparable across ceiling regimes
    "rationale": "Score 94.15% of ceiling with 42% word overlap — same disclosure, wording changed."
}
```

### Classification rules

All thresholds are expressed as fractions of **ceiling** (0.65 without summaries, 1.0
with), so they self-adjust when the LLM disclosure stage lands.

| Normalised score | Label | Additional condition |
|---|---|---|
| ≥ 0.97 | **Unchanged** | word overlap ≥ 90%, or near-identical despite surface differences |
| ≥ 0.70 | **Relocated** | section changed |
| ≥ 0.70 | **Expanded** | current text ≥ 1.4× previous by word count |
| ≥ 0.70 | **Reduced** | current text ≤ 0.7× previous by word count |
| ≥ 0.70 | **Modified** | default for this band |
| 0.55 – 0.70 | **Modified** | weak match — rationale flags low confidence |
| < 0.55 | **Added** | no credible counterpart found |

Single-sided rows:
- `previous_ids` only, no reverse-retrieval hit above threshold → **Removed**
- `current_ids` only, no candidate above threshold → **Added**

Multi-sided rows (splits/merges): always **Modified**, rationale notes the relationship
type and best-pair score.

### Design decisions

- **No LLM calls.** This is a deterministic scoring pass: fast, free, reproducible. The
  `llm` slot in `change_analysis` is reserved for a future LLM-based classifier that can
  handle nuanced cases (negation, hedging flips).
- **Does not overwrite other slots.** `classify_all` writes only to
  `change_analysis.semantic` and updates `status` from `not_started` to `semantic_done`.
  It never touches `lexical`, `llm`, or `final_taxonomy`.
- **Normalised scores make thresholds ceiling-proof.** The score-ceiling warning from the
  retrieval section is handled: all comparisons use `score / ceiling`, not the raw score.

---

## Files

| Path | |
|---|---|
| `src/sec_disclosure/indexing/semantic_retrieval.py` | `SemanticIndex` — retrieval |
| `src/sec_disclosure/indexing/semantic_classification.py` | Rule-based classifier — populates `change_analysis.semantic` |
| `src/sec_disclosure/llm/embeddings.py` | batched embeddings client + cache |
| `tests/test_semantic_retrieval.py` | 12 tests, stub embedder, no key or network needed |
| `tests/test_semantic_classification.py` | 24 tests, stub embedder, no key or network needed |
| `requirements.txt` | `numpy` added |

```bash
python -m pytest tests/test_semantic_retrieval.py tests/test_semantic_classification.py -v   # offline
```

---

## Next steps

1. **Label ~100 real pairs by hand** from one Item, including "no counterpart exists" as a
   valid answer. This is the blocker for every number below.
2. **Sweep the threshold** across 0.30–0.65 on those pairs; report the precision/recall
   curve, not a single figure, so evaluation can pick its own operating point.
3. **Report recall@1 and recall@k per Item, excluding verbatim matches**, so the numbers
   reflect the part of the corpus this stage actually contributes to.
4. **Re-measure once the LLM disclosure stage lands** — summaries will be populated, the
   ceiling moves to 1.0, and every threshold needs rescaling.
5. **Tune classification thresholds** against the labelled pairs from step 1. The current
   bands (0.55 / 0.70 / 0.97) are provisional and derived from score distributions, not
   from verified correct labels.
