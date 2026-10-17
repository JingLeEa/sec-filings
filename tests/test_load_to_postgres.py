"""Offline tests for scripts/load_to_postgres.py (no database needed)."""
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_to_postgres.py"
spec = importlib.util.spec_from_file_location("load_to_postgres", SCRIPT)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)

CHUNK = {
    "id": "unh_2025_1A_P012", "company": "unh", "year": "2025", "item": "1A",
    "item_default_title": "Risk Factors", "item_title": "Risk Factors",
    "section_path": ["Risk Factors"], "item_chunk_index": 12, "text": "Paragraph text.",
    "html_list_depth": None, "source": "file.html", "brand_new_field": 7,
}
SENTENCE = {
    "id": "unh_2025_1A_P012_S003", "chunk_id": "unh_2025_1A_P012", "company": "unh", "year": "2025",
    "item": "1A", "item_default_title": "Risk Factors", "item_title": "Risk Factors",
    "section_path": ["Risk Factors"], "item_chunk_index": 12, "sentence_index": 3,
    "bullet_level": None, "bullet_indent_pt": None, "html_list_depth": None,
    "source_block_index": "", "text": "A sentence.", "source": "file.html",
}


class LoaderTests(unittest.TestCase):
    def test_chunk_row_maps_fields_and_keeps_unknown_in_extra(self):
        row = loader.chunk_row(CHUNK, 5)
        self.assertEqual(row["chunk_id"], "unh_2025_1A_P012")
        self.assertEqual(row["filing_id"], 5)
        self.assertEqual(row["section_path"], ["Risk Factors"])
        self.assertEqual(row["extra"], '{"brand_new_field": 7}')

    def test_sentence_row_turns_empty_block_index_into_none(self):
        row = loader.sentence_row(SENTENCE, 5)
        self.assertIsNone(row["source_block_index"])
        self.assertEqual(row["sentence_index"], 3)
        self.assertEqual(row["extra"], "{}")

    def test_validate_inputs_accepts_consistent_files(self):
        self.assertEqual(loader.validate_inputs([CHUNK], [SENTENCE]), [])

    def test_validate_inputs_flags_orphan_sentence_and_duplicates(self):
        orphan = dict(SENTENCE, id="x_S001", chunk_id="missing_chunk")
        problems = loader.validate_inputs([CHUNK, CHUNK], [SENTENCE, orphan])
        self.assertTrue(any("duplicate chunk IDs" in p for p in problems))
        self.assertTrue(any("not in the chunks file" in p for p in problems))

    def test_embedding_cache_key_matches_pipeline_formula(self):
        # sha256 of "<model>\0<text>", as in the semantic-retrieval embeddings cache.
        expected = hashlib.sha256("bge-m3\u0000hello".encode("utf-8")).hexdigest()
        self.assertEqual(loader.embedding_cache_key("bge-m3", "hello"), expected)
        self.assertNotEqual(loader.embedding_cache_key("bge-m3", "a"), loader.embedding_cache_key("other", "a"))

    def test_vector_literal(self):
        self.assertEqual(loader.vector_literal([1, 0.5]), "[1.0,0.5]")

    def test_sync_preserves_filing_and_limits_pruning_to_selected_raw_ids(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks, sentences = root / "chunks.json", root / "sentences.json"
            chunks.write_text(json.dumps([CHUNK]))
            sentences.write_text(json.dumps([SENTENCE]))
            conn, cur = MagicMock(), MagicMock()
            conn.__enter__.return_value = conn
            cur.__enter__.return_value = cur
            conn.cursor.return_value = cur
            cur.fetchone.side_effect = [(7,), (1,), (1,)]
            driver = SimpleNamespace(connect=MagicMock(return_value=conn))
            with patch.dict(sys.modules, {"psycopg": driver}), redirect_stdout(io.StringIO()):
                self.assertEqual(loader.main(["--company", "unh", "--year", "2025", "--sync",
                                              "--chunks", str(chunks), "--sentences", str(sentences)]), 0)
            deletes = [call.args for call in cur.execute.call_args_list if call.args[0].startswith("DELETE")]
            self.assertEqual(len(deletes), 2)
            self.assertTrue(all("filing_id = %s" in query for query, _ in deletes))
            self.assertFalse(any("DELETE FROM filings" in query for query, _ in deletes))
            self.assertEqual(deletes[0][1], (7, [SENTENCE["id"]]))
            self.assertEqual(deletes[1][1], (7, [CHUNK["id"]]))
            self.assertEqual(conn.__exit__.call_args.args, (None, None, None))


if __name__ == "__main__":
    unittest.main()
