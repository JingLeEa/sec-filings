# Semantic retrieval — handoff

**Owner:** Aarav · **Stage:** semantic change detection (retrieval half)
**Branch:** `semantic-retrieval` · **Status:** built, smoke-tested on real filings, not yet validated against labelled pairs

---

## What this is

`indexing/disclosure_retrieval.py` is **lexical**: it ranks two disclosures by the rare
terms they share. Its own docstring is explicit — *"Dependency-free TF-IDF retrieval;
scores are rankings, not match probabilities."* A rewrite that keeps the meaning but
changes the wording scores near zero.

`indexing/semantic_retrieval.py` closes that gap by comparing **dense embeddings**
instead. The existing lexical index is untouched.

| | Compares | Fails on |
|---|---|---|
| TF-IDF (existing) | shared rare terms | "demand for processing power" vs "compute requirements" |
| SemanticIndex (this) | position in meaning-space | negation — "we **no longer** expect X" scores high against "we expect X" |

---

## Interface — what downstream calls

`SemanticIndex` has the **identical public surface** to `DisclosureIndex`. Swapping the
constructor is the whole integration; `agents/disclosure_alignment.py:623` needs no
other change.

```python
from sec_disclosure.indexing.semantic_retrieval import SemanticIndex
from sec_disclosure.llm.embeddings import make_embedder

index = SemanticIndex(records, embedder=make_embedder())

index.search(query, year, *, item=None, anchor=None, limit=5, offset=0)
index.candidates(previous_year, current_year, top_k=5)
```

**Records in** — same shape `DisclosureIndex` takes:

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

Entries added by reverse retrieval carry `"retrieval_direction": "reverse"`.

### This stage outputs candidates, not decisions

It never says "this is a Modified disclosure". It says *"these five previous-year
disclosures are the plausible counterparts, best first."* Choosing the pair is the
alignment agent's job; labelling the change is change detection's.

**Retrieval cannot express "nothing matches."** `top_k` results come back even for a
genuinely new disclosure. Rejecting a bad top-1 is a decision for the layer above —
see *Thresholds*.

---

## Scoring — inherited deliberately

Every scoring decision already in the TF-IDF index is mirrored, so that any later
comparison of the two measures **the similarity function and nothing else**:

| Element | Value |
|---|---|
| Field blend | `0.35 × summary + 0.65 × content` |
| Same-taxonomy bonus | `+0.02` (only when a base score exists) |
| Section-similarity bonus | `+0.03 × section cosine` (same condition) |
| Scope | one SEC Item; cross-Item matches are never returned |
| Retrieval | forward, plus reverse pass for split/merge coverage |

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
**build 0.8s, search 10s** — faster than TF-IDF's 24s on the same work. Brute-force
cosine per Item is sufficient at this corpus size; FAISS or similar only earns its keep
across many companies.

### How much of the corpus is free

Intel Item 1A, FY2025: **50 of 165 chunks (30.3%) are verbatim identical** to FY2024.
Exact string matching finds those instantly, and the repo already reserves exact matches
before any fuzzy matching.

**Semantic's contribution lives in the other 69.7%.** Recall reported over all 165 would
flatter every method equally by counting the free 30%.

---

## Thresholds

**There is no threshold in retrieval today** — by design, matching the existing index.
When one is needed:

- Usable band from the data above: **0.42 – 0.50** (on the 0.65 ceiling)
- Provisional operating point: **0.45**
- **Err low.** This stage should optimise *recall*. A false positive costs the alignment
  agent one extra candidate to read; a false negative is unrecoverable — the true
  counterpart never reaches the agent at all.
- Do **not** inherit the lexical matcher's `0.55` (`DEFAULT_MATCH_THRESHOLD` in
  `export_disclosure_annotations.py`). Different method, different scale.

This band is **provisional**. It comes from top-1 *scores*, not from *verified correct
matches*. Setting it properly needs labelled pairs — see below.

---

## Not yet established

| Established | Not established |
|---|---|
| The component is correct | that top-1 is the **right** counterpart |
| Embeddings discriminate on real filings | recall — how often the true match is found |
| A threshold is placeable | whether semantic beats lexical |

A confident score is not a correct match. Observed example: `intel_2025_1A_P008` scored
**0.623**, comfortably inside the "good" band, and on inspection was not a true
counterpart — Intel had rewritten that risk factor between years, so no 1:1 counterpart
exists. Both methods still localised to the correct risk-factor cluster (P007–P009),
which is retrieval working as intended.

**Known weakness: negation.** Embeddings score "we no longer expect X" as highly similar
to "we expect X". A 10-K is full of such hedging flips, and they are exactly the material
changes a reader cares about. Lexical matching notices the inserted words. A hybrid of
the two would likely beat either alone — out of scope here, but worth someone owning.

### ⚠️ Do not evaluate against the existing 9,766-row benchmark

Every pair in it was produced by the **lexical** matcher at threshold 0.55, and every
`Added`/`Removed` is an inference from that matcher finding no partner. Scoring semantic
against it penalises semantic for every correct pair lexical missed. Use it as a
**candidate pool** to speed up labelling, never as the scorecard.

---

## Dependencies

| | |
|---|---|
| New Python package | `numpy>=1.26,<3` (only addition) |
| Embedding model | `bge-m3` via SoCLaaS — 1024-dim, 131k context, $0.01 / M input tokens |
| Config | `~/.config/soclaas/soclaas.env` — `SOCLAAS_BASE_URL`, `SOCLAAS_API_KEY`, `SOCLAAS_MODEL` |

`llm/embeddings.py` mirrors `llm/client.py`'s structure and error mapping, adding
batching and an on-disk cache keyed by `sha256(model + text)` under `data/embeddings/`.
Re-runs are free, which matters because tuning means re-running.

An empty `summary` yields a zero vector rather than an error, matching the TF-IDF
index's behaviour for an empty document.

---

## Files

| Path | |
|---|---|
| `src/sec_disclosure/indexing/semantic_retrieval.py` | `SemanticIndex` |
| `src/sec_disclosure/llm/embeddings.py` | batched embeddings client + cache |
| `tests/test_semantic_retrieval.py` | 12 tests, stub embedder, no key or network needed |
| `requirements.txt` | `numpy` added |

```bash
python -m unittest tests.test_semantic_retrieval     # offline
```

---

## Next steps

1. **Label ~100 real pairs by hand** from one Item, including "no counterpart exists" as
   a valid answer. This is the blocker for every number below.
2. **Sweep the threshold** across 0.30–0.65 on those pairs; report the precision/recall
   curve, not a single figure, so evaluation can pick its own operating point.
3. **Report recall@1 and recall@k per Item, excluding verbatim matches**, so the numbers
   are comparable across methods and not inflated by the free 30%.
4. **Re-measure once the LLM disclosure stage lands** — summaries will be populated, the
   ceiling moves to 1.0, and every threshold needs rescaling.
