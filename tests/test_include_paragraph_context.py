import tempfile
import unittest
from pathlib import Path

from sec_disclosure.annotation.include_paragraph_context import ChunkCache, enrich_rows, paragraph_context_for_id, resolve_columns


class IncludeParagraphContextTests(unittest.TestCase):
    def test_paragraph_context_returns_exact_latest_chunk_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data/raw"
            chunk_dir = root / "nvda/2024"
            chunk_dir.mkdir(parents=True)
            (chunk_dir / "2024_chunks.json").write_text(
                """[
                  {
                    "id": "2024_1_P001",
                    "company": "nvda",
                    "year": "2024",
                    "item": "1",
                    "item_title": "Competition",
                    "text": "Our current competitors include:\\n• accelerated computing suppliers;\\n• cloud service companies."
                  }
                ]""",
                encoding="utf-8",
            )

            text, ids, status = paragraph_context_for_id(
                ChunkCache(root, "nvda"),
                company="nvda",
                year="2024",
                chunk_id_value="2024_1_P001",
            )

        self.assertEqual(ids, "2024_1_P001")
        self.assertEqual(status, "single_chunk")
        self.assertIn("cloud service companies", text)

    def test_enrich_rows_converts_id_then_uses_converted_single_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data/raw"
            chunk_dir = root / "nvda/2024"
            chunk_dir.mkdir(parents=True)
            (chunk_dir / "2024_chunks.json").write_text(
                """[
                  {
                    "id": "2024_1_P001",
                    "company": "nvda",
                    "year": "2024",
                    "item": "1",
                    "item_title": "Competition",
                    "text": "Our current competitors include:\\n• accelerated computing suppliers;\\n• cloud service companies."
                  }
                ]""",
                encoding="utf-8",
            )
            fieldnames = [
                "Company",
                "Previous Fiscal Year",
                "Current Fiscal Year",
                "Item",
                "Previous Section / Subsection",
                "Previous Paragraph / Chunk ID",
                "Previous Disclosure Text",
                "Current Section / Subsection",
                "Current Paragraph / Chunk ID",
                "Current Disclosure Text",
            ]
            rows = [
                {
                    "Company": "NVIDIA",
                    "Previous Fiscal Year": "2024",
                    "Current Fiscal Year": "2024",
                    "Item": "1",
                    "Previous Section / Subsection": "Competition",
                    "Previous Paragraph / Chunk ID": "old_id",
                    "Previous Disclosure Text": "cloud service companies.",
                    "Current Section / Subsection": "Competition",
                    "Current Paragraph / Chunk ID": "2024_1_P001",
                    "Current Disclosure Text": "",
                }
            ]

            enriched, _ = enrich_rows(
                rows,
                fieldnames,
                chunks_root=root,
                default_company="nvda",
                column_map=resolve_columns(fieldnames),
            )

        self.assertEqual(enriched[0]["Previous Paragraph / Chunk ID"], "2024_1_P001")
        self.assertEqual(enriched[0]["Original Previous Paragraph / Chunk ID"], "old_id")
        self.assertEqual(enriched[0]["Previous ID Conversion Status"], "exact_text_match")
        self.assertEqual(enriched[0]["Previous Paragraph Lookup Status"], "single_chunk")
        self.assertIn("Our current competitors include:", enriched[0]["Previous Original Paragraph"])


if __name__ == "__main__":
    unittest.main()
