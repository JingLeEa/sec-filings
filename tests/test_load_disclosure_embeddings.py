"""Offline tests for scripts/load_disclosure_embeddings.py (no database needed)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("load_disclosure_embeddings", SCRIPTS / "load_disclosure_embeddings.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


class EmbeddingLoaderTests(unittest.TestCase):
    def test_field_texts_skips_empty_and_blank(self):
        row = {"content": "text", "summary": "", "section": "   "}
        self.assertEqual(loader.field_texts(row), {"content": "text"})

    def test_field_texts_respects_requested_fields(self):
        row = {"content": "a", "summary": "b", "section": "c"}
        self.assertEqual(loader.field_texts(row, ("summary",)), {"summary": "b"})

    def test_find_vector_uses_pipeline_cache_key_and_tolerates_bad_files(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            key = loader.embedding_cache_key("bge-m3", "hello")
            (cache / f"{key}.json").write_text(json.dumps([0.1, 0.2]))
            self.assertEqual(loader.find_vector(cache, "bge-m3", "hello"), [0.1, 0.2])
            self.assertIsNone(loader.find_vector(cache, "bge-m3", "other text"))
            (cache / f"{loader.embedding_cache_key('bge-m3', 'broken')}.json").write_text("{not json")
            self.assertIsNone(loader.find_vector(cache, "bge-m3", "broken"))


if __name__ == "__main__":
    unittest.main()
