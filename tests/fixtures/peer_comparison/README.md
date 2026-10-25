# Peer-comparison fixture (synthetic comparisons)

`nvda/2024/peer_comparison.json` was produced by running `agents/peer_comparison.py` (peer-comparison branch)
on the real `test-data` disclosures for NVDA vs AMD and INTC, Item 1A, fiscal 2024, with `--max-requests 14`,
but with the LLM replaced by a scripted responder. So the file structure, candidate retrieval, cited sentence
IDs, sentence text and year-over-year context are real; every `comparison` text starts with
"SYNTHETIC TEST FIXTURE" and is not analysis. It is a partial run (14 of 53 target disclosures).

Use it only for loader and chatbot tests. Replace it with a real pilot run before demonstrating answers.
