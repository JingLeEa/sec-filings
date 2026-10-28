# Synthesis fixture (real agent output)

`AMD_2023_2025_synthesis_output.json` is real output of the synthesis agent for AMD fiscal 2023-2025, taken
from the `synthesis-agent` branch history (commit 1fde2a3, file `synthesis_dev/AMD_2023_2025_synthesis_output.json`).
It was produced from the team database with real XBRL financial facts (39 metrics, 33 calculated trends) and
the SoCLaaS model. Every cited alignment (33 match IDs) and every source-catalog entry (50) exists in
`test_data/alignments/amd` with the same materiality assessment.

It predates the 26 Oct rewrite (`src/sec_disclosure/agents/synthesis.py`), which writes the same fields plus
`coverage_summary`/`coverage_gaps`; the loader accepts both. Replace it with a current-version run when one exists.
