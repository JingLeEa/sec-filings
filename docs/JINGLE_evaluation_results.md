# Consolidated Alignment Evaluation

Source: `data/evaluation_soclaas`; 24 year-pair comparisons across 12 companies. Candidate: Qwen.

These are pooled agreement scores against model-generated references, not accuracy against human ground truth. Reference names are kept as recorded in each comparison: x-test-2 (24 pairs). The model labels should not be assumed interchangeable without checking run provenance.

Missing company-year reports among observed year pairs: none.

## Key Findings

- Pooled substantive-change macro F1 is 0.704 for LLM, 0.640 for lexical and 0.510 for semantic. Exact-taxonomy macro F1 is 0.365 for LLM and 0.442 for lexical; the ranking depends on the classification task.
- Materiality Yes precision is 0.557 and recall is 0.757. Qwen predicted Yes on 3,591 rows versus 2,641 reference Yes rows; 1,578 reference-No rows were predicted Yes.
- Semantic produced unmapped candidate taxonomies on 1,337 of 8,777 compared change rows; its binary scores apply only to the mapped subset.

## Change Analysis

Binary substantive change: Modified, Expanded and Reduced are substantive; Reworded and Unchanged are non-substantive. New and Removed are outside this matched-row metric.

| Method | Compared / eligible | Binary coverage | Substantive P | R | F1 | Non-substantive F1 | Binary macro F1 | Exact taxonomy macro F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LLM | 8,777 / 8,777 | 1.000 | 0.839 | 0.934 | 0.884 | 0.524 | 0.704 | 0.365 |
| LEXICAL | 8,777 / 8,777 | 1.000 | 0.914 | 0.621 | 0.740 | 0.539 | 0.640 | 0.442 |
| SEMANTIC | 8,777 / 8,777 | 0.848 | 0.754 | 0.624 | 0.683 | 0.338 | 0.510 | 0.224 |

Binary coverage is the fraction of compared rows with a mappable candidate taxonomy. Unmapped labels such as Relocated are excluded from binary P/R/F1, but count against coverage.

### Detailed Binary Metrics

Per-label precision, recall and F1 for the substantive/non-substantive task. Macro F1 is the unweighted mean of the two reference-supported class F1 scores.

| Method | Label | Reference n | Candidate n | Precision | Recall | F1 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| LLM | Substantive | 6,680 | 7,433 | 0.839 | 0.934 | 0.884 |
| LLM | Non-substantive | 2,097 | 1,344 | 0.670 | 0.430 | 0.524 |
| LLM | **Macro F1** | - | - | - | - | **0.704** |
| LEXICAL | Substantive | 6,680 | 4,538 | 0.914 | 0.621 | 0.740 |
| LEXICAL | Non-substantive | 2,097 | 4,239 | 0.403 | 0.815 | 0.539 |
| LEXICAL | **Macro F1** | - | - | - | - | **0.640** |
| SEMANTIC | Substantive | 5,506 | 4,553 | 0.754 | 0.624 | 0.683 |
| SEMANTIC | Non-substantive | 1,934 | 2,887 | 0.282 | 0.421 | 0.338 |
| SEMANTIC | **Macro F1** | - | - | - | - | **0.510** |

### Detailed Taxonomy Metrics

Exact-label scores for the matched-row change taxonomy. New and Removed have no reference support in these model rows and are omitted from the table and macro average. Unlike the binary semantic score, exact-taxonomy recall counts unmapped candidate labels such as Relocated as mismatches.

| Method | Label | Reference n | Candidate n | Precision | Recall | F1 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| LLM | Expanded | 1,123 | 209 | 0.651 | 0.121 | 0.204 |
| LLM | Reduced | 902 | 278 | 0.493 | 0.152 | 0.232 |
| LLM | Modified | 4,655 | 6,946 | 0.628 | 0.937 | 0.752 |
| LLM | Reworded | 675 | 444 | 0.261 | 0.172 | 0.207 |
| LLM | Unchanged | 1,422 | 900 | 0.557 | 0.352 | 0.431 |
| LLM | **Macro F1** | - | - | - | - | **0.365** |
| LEXICAL | Expanded | 1,123 | 1,538 | 0.382 | 0.524 | 0.442 |
| LEXICAL | Reduced | 902 | 1,561 | 0.317 | 0.548 | 0.401 |
| LEXICAL | Modified | 4,655 | 1,439 | 0.889 | 0.275 | 0.420 |
| LEXICAL | Reworded | 675 | 3,508 | 0.164 | 0.853 | 0.275 |
| LEXICAL | Unchanged | 1,422 | 731 | 0.989 | 0.508 | 0.672 |
| LEXICAL | **Macro F1** | - | - | - | - | **0.442** |
| SEMANTIC | Expanded | 1,123 | 401 | 0.419 | 0.150 | 0.221 |
| SEMANTIC | Reduced | 902 | 535 | 0.355 | 0.211 | 0.264 |
| SEMANTIC | Modified | 4,655 | 3,617 | 0.518 | 0.402 | 0.453 |
| SEMANTIC | Reworded | 675 | 0 | - | 0.000 | 0.000 |
| SEMANTIC | Unchanged | 1,422 | 2,887 | 0.135 | 0.275 | 0.181 |
| SEMANTIC | **Macro F1** | - | - | - | - | **0.224** |

### Error Rates

False substantive rate divides false substantive calls by all candidate substantive calls. Missed substantive rate divides missed substantive cases by all reference substantive changes. These are pooled rates, not averages of the company-year rates.

| Method | False substantive | Of candidate substantive | Missed substantive | Of reference substantive |
| --- | ---: | ---: | ---: | ---: |
| LLM | 1,196 | 16.1% (1,196/7,433) | 443 | 6.6% (443/6,680) |
| LEXICAL | 388 | 8.6% (388/4,538) | 2,530 | 37.9% (2,530/6,680) |
| SEMANTIC | 1,120 | 24.6% (1,120/4,553) | 2,073 | 37.6% (2,073/5,506) |

For semantic, the denominators include only rows with a mappable candidate label.

## Materiality

Model rows compared: **12,229 / 12,229** (coverage 1.000).

| Label | Reference n | Candidate n | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Yes | 2,641 | 3,591 | 0.557 | 0.757 | 0.641 |
| No | 9,562 | 8,638 | 0.924 | 0.835 | 0.877 |
| Uncertain | 26 | 0 | - | 0.000 | 0.000 |

Primary Yes/No macro F1: **0.759**. Three-label macro F1: 0.506. The primary score gives Uncertain no separate class weight; its predictions can still affect Yes/No precision.
False Yes against reference No: **1,578/3,591 (43.9%)** of candidate Yes calls. Missed reference Yes: **642/2,641 (24.3%)** of reference Yes cases. Another 14 candidate Yes calls had an Uncertain reference, so the false-Yes percentage alone is not exactly 1 minus precision.

## Coverage

Eligible means reference alignment rows in the task's evaluation scope: `ai_verified` for change analysis; `ai_verified` plus `unmatched` for materiality. Compared means eligible rows with both a valid reference label and a candidate label for the same `match_id`. Deterministic `auto_matched` rows are excluded here.

| Task | Eligible | Reference label pending | Candidate row missing | Candidate label missing | Compared |
| --- | ---: | ---: | ---: | ---: | ---: |
| LLM change | 8,777 | 0 | 0 | 0 | 8,777 |
| Materiality | 12,229 | 0 | 0 | 0 | 12,229 |

Reference-label gaps by company-year:
- None.

## Reference Breakdown

| Recorded reference | Pairs | LLM binary macro F1 | Lexical binary macro F1 | Semantic binary macro F1 | Materiality Yes F1 | Materiality Yes/No macro F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| x-test-2 | 24 | 0.704 | 0.640 | 0.510 | 0.641 | 0.759 |

## Company-Year Results

Each score below is calculated within its own year pair; the pooled scores above use summed confusion counts rather than averaging pair scores.

| Company | Years | Reference | Change n | LLM binary macro F1 | Lexical | Semantic | Materiality n | Yes P | Yes R | Yes F1 | Yes/No macro F1 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AMD | 2023-2024 | x-test-2 | 212 | 0.671 | 0.621 | 0.431 | 292 | 0.598 | 0.891 | 0.715 | 0.814 |
| AMD | 2024-2025 | x-test-2 | 232 | 0.618 | 0.581 | 0.477 | 309 | 0.650 | 0.753 | 0.698 | 0.779 |
| AVGO | 2023-2024 | x-test-2 | 222 | 0.721 | 0.742 | 0.619 | 329 | 0.582 | 0.788 | 0.669 | 0.744 |
| AVGO | 2024-2025 | x-test-2 | 195 | 0.733 | 0.682 | 0.545 | 267 | 0.552 | 0.828 | 0.662 | 0.757 |
| BAC | 2023-2024 | x-test-2 | 524 | 0.692 | 0.611 | 0.474 | 639 | 0.555 | 0.600 | 0.577 | 0.729 |
| BAC | 2024-2025 | x-test-2 | 485 | 0.694 | 0.629 | 0.399 | 616 | 0.603 | 0.671 | 0.635 | 0.760 |
| C | 2023-2024 | x-test-2 | 624 | 0.726 | 0.641 | 0.514 | 834 | 0.556 | 0.738 | 0.634 | 0.753 |
| C | 2024-2025 | x-test-2 | 616 | 0.659 | 0.655 | 0.540 | 930 | 0.531 | 0.756 | 0.624 | 0.740 |
| GS | 2023-2024 | x-test-2 | 582 | 0.646 | 0.562 | 0.476 | 777 | 0.495 | 0.679 | 0.573 | 0.722 |
| GS | 2024-2025 | x-test-2 | 576 | 0.732 | 0.598 | 0.494 | 846 | 0.521 | 0.764 | 0.620 | 0.737 |
| INTC | 2023-2024 | x-test-2 | 259 | 0.663 | 0.634 | 0.577 | 404 | 0.588 | 0.813 | 0.683 | 0.750 |
| INTC | 2024-2025 | x-test-2 | 305 | 0.790 | 0.732 | 0.595 | 534 | 0.634 | 0.849 | 0.726 | 0.757 |
| JPM | 2023-2024 | x-test-2 | 600 | 0.741 | 0.697 | 0.594 | 808 | 0.450 | 0.682 | 0.542 | 0.712 |
| JPM | 2024-2025 | x-test-2 | 576 | 0.687 | 0.632 | 0.394 | 768 | 0.554 | 0.649 | 0.598 | 0.752 |
| MS | 2023-2024 | x-test-2 | 426 | 0.708 | 0.653 | 0.483 | 566 | 0.443 | 0.783 | 0.565 | 0.739 |
| MS | 2024-2025 | x-test-2 | 384 | 0.741 | 0.588 | 0.401 | 586 | 0.284 | 0.838 | 0.425 | 0.671 |
| MU | 2023-2024 | x-test-2 | 186 | 0.685 | 0.553 | 0.523 | 264 | 0.575 | 0.806 | 0.671 | 0.771 |
| MU | 2024-2025 | x-test-2 | 183 | 0.786 | 0.693 | 0.683 | 271 | 0.594 | 0.851 | 0.700 | 0.775 |
| NVDA | 2023-2024 | x-test-2 | 196 | 0.669 | 0.664 | 0.596 | 273 | 0.622 | 0.875 | 0.727 | 0.810 |
| NVDA | 2024-2025 | x-test-2 | 188 | 0.546 | 0.653 | 0.568 | 244 | 0.573 | 0.671 | 0.618 | 0.723 |
| QCOM | 2023-2024 | x-test-2 | 116 | 0.767 | 0.484 | 0.593 | 165 | 0.804 | 0.882 | 0.841 | 0.882 |
| QCOM | 2024-2025 | x-test-2 | 107 | 0.594 | 0.480 | 0.497 | 173 | 0.803 | 0.791 | 0.797 | 0.835 |
| WFC | 2023-2024 | x-test-2 | 498 | 0.665 | 0.662 | 0.531 | 689 | 0.592 | 0.804 | 0.682 | 0.803 |
| WFC | 2024-2025 | x-test-2 | 485 | 0.658 | 0.645 | 0.597 | 645 | 0.552 | 0.772 | 0.644 | 0.769 |

## Reading The Results

- Scores cover only eligible model-classified rows with predictions on both sides; deterministic exact-text cases are outside these model-row tables.
- Semantic binary scores can exclude unmapped taxonomies. Check binary coverage before comparing methods.
- Class prevalence varies by company and year. Inspect per-label precision and recall, not only macro F1.
- Alignment quality and reference-label correctness are not measured here. Review disagreement examples before treating these values as performance claims.
