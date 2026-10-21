# Change-Analysis Decision Evaluation

Evaluated 24 year pairs across 12 companies on the same 8,777 reference-labeled `ai_verified` alignments.

The majority policy selects an exact taxonomy label when at least two of LLM, lexical and semantic agree. A three-way tie, a missing vote, or a majority label outside the canonical taxonomy falls back to LLM. The production alignments are not changed.

The reference is model-generated, not human ground truth. Results measure agreement with the SoCLaaS benchmark, using the same rows and evidence for both policies. Input files follow the paths recorded in each existing per-pair evaluation report.

Existing per-pair LLM baselines match the current alignment files where checked.

## Overall Comparison

| Policy | Exact agreement | Exact macro F1 | Substantive P | Substantive R | Substantive F1 | Non-substantive F1 | Binary macro F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LLM only | 0.598 | 0.365 | 0.839 | 0.934 | 0.884 | 0.524 | 0.704 |
| Majority, else LLM | 0.588 | 0.413 | 0.838 | 0.943 | 0.888 | 0.525 | 0.706 |

Majority minus LLM: exact macro F1 **+0.047**; binary macro F1 **+0.002**; exact agreement **-0.011**.

### Exact Taxonomy By Label

| Label | Reference n | LLM predicted n | Majority predicted n | LLM F1 | Majority F1 | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Expanded | 1,123 | 209 | 570 | 0.204 | 0.328 | +0.124 |
| Reduced | 902 | 278 | 740 | 0.232 | 0.342 | +0.110 |
| Modified | 4,655 | 6,946 | 6,206 | 0.752 | 0.731 | -0.021 |
| Reworded | 675 | 444 | 384 | 0.207 | 0.213 | +0.006 |
| Unchanged | 1,422 | 900 | 877 | 0.431 | 0.448 | +0.017 |

## Decision Impact

- Majority changed 947 LLM labels (10.8% of compared rows).
- All three labels differed on 3,278 rows; 0 had a missing lexical or semantic vote. Both paths retained LLM.
- Exact taxonomy: majority fixed 368 LLM errors and introduced 461 new errors.
- Binary substantive change: majority fixed 90 LLM errors and introduced 47 new errors.

## Decision

There is no unqualified winner: majority improves class-balanced exact taxonomy F1 but lowers exact-label agreement. Its binary macro F1 change is small. Keep LLM-only as the conservative default unless broader coverage of less common taxonomies is the explicit priority; review override examples and human-label a sample before changing production policy.

## Per Year Pair

| Company | Years | Rows | Overrides | Exact F1 LLM | Exact F1 majority | Binary F1 LLM | Binary F1 majority | Fixed exact | Harmed exact |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AMD | 2023-2024 | 212 | 32 | 0.217 | 0.305 | 0.671 | 0.691 | 11 | 20 |
| AMD | 2024-2025 | 232 | 37 | 0.297 | 0.375 | 0.618 | 0.609 | 17 | 13 |
| AVGO | 2023-2024 | 222 | 31 | 0.338 | 0.385 | 0.721 | 0.730 | 8 | 14 |
| AVGO | 2024-2025 | 195 | 37 | 0.456 | 0.493 | 0.733 | 0.751 | 14 | 20 |
| BAC | 2023-2024 | 524 | 61 | 0.337 | 0.405 | 0.692 | 0.693 | 24 | 33 |
| BAC | 2024-2025 | 485 | 57 | 0.365 | 0.455 | 0.694 | 0.699 | 25 | 30 |
| C | 2023-2024 | 624 | 84 | 0.433 | 0.463 | 0.726 | 0.720 | 27 | 47 |
| C | 2024-2025 | 616 | 46 | 0.337 | 0.384 | 0.659 | 0.648 | 19 | 21 |
| GS | 2023-2024 | 582 | 63 | 0.346 | 0.388 | 0.646 | 0.643 | 24 | 30 |
| GS | 2024-2025 | 576 | 75 | 0.349 | 0.421 | 0.732 | 0.728 | 32 | 32 |
| INTC | 2023-2024 | 259 | 35 | 0.337 | 0.452 | 0.663 | 0.663 | 16 | 18 |
| INTC | 2024-2025 | 305 | 21 | 0.342 | 0.370 | 0.790 | 0.821 | 6 | 14 |
| JPM | 2023-2024 | 600 | 33 | 0.417 | 0.438 | 0.741 | 0.754 | 13 | 16 |
| JPM | 2024-2025 | 576 | 59 | 0.373 | 0.406 | 0.687 | 0.695 | 23 | 29 |
| MS | 2023-2024 | 426 | 44 | 0.347 | 0.398 | 0.708 | 0.691 | 23 | 17 |
| MS | 2024-2025 | 384 | 41 | 0.378 | 0.432 | 0.741 | 0.739 | 20 | 11 |
| MU | 2023-2024 | 186 | 16 | 0.392 | 0.394 | 0.685 | 0.692 | 6 | 9 |
| MU | 2024-2025 | 183 | 19 | 0.361 | 0.401 | 0.786 | 0.799 | 6 | 8 |
| NVDA | 2023-2024 | 196 | 25 | 0.311 | 0.408 | 0.669 | 0.671 | 12 | 9 |
| NVDA | 2024-2025 | 188 | 14 | 0.311 | 0.313 | 0.546 | 0.546 | 3 | 10 |
| QCOM | 2023-2024 | 116 | 17 | 0.489 | 0.441 | 0.767 | 0.769 | 2 | 14 |
| QCOM | 2024-2025 | 107 | 12 | 0.458 | 0.443 | 0.594 | 0.594 | 1 | 11 |
| WFC | 2023-2024 | 498 | 56 | 0.312 | 0.381 | 0.665 | 0.682 | 25 | 21 |
| WFC | 2024-2025 | 485 | 32 | 0.309 | 0.346 | 0.658 | 0.669 | 11 | 14 |

## Interpretation

- Exact macro F1 treats the reference-supported change taxonomies equally. Binary macro F1 groups Modified, Expanded and Reduced as substantive; Reworded and Unchanged as non-substantive.
- New and Removed are not scored in this matched-row (`ai_verified`) policy comparison. Deterministic alignment rows are likewise excluded.
- The paired fixed/harmed counts show what the vote changes on identical rows; they do not establish that the model-generated reference is correct.
