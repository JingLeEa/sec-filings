from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import io
from contextlib import redirect_stdout, redirect_stderr

from sec_disclosure.analysis.aligned_changes import enrich_alignment
from sec_disclosure.analysis.lexical_analysis import compute_lexical_metrics
from sec_disclosure.agents.change_analysis import compact_change_analysis
from sec_disclosure.storage.alignments import alignment_row


class AlignedChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        records = {}
        for year, number, text in [("2024", 1, "We manufacture chips for computing."),
                                    ("2025", 1, "We manufacture chips for computing and cloud services."),
                                    ("2025", 2, "We opened a new facility."),
                                    ("2024", 2, "We closed an old facility.")]:
            key = f"amd_{year}_7_D{number:03d}"
            records[key] = {"disclosure_id": key, "fiscal_year": year, "item": "7",
                            "section": "Operations", "taxonomy": "Operations", "summary": "",
                            "content": text}
        self.data = SimpleNamespace(ticker="amd", years=("2024", "2025"), records=records,
                                    item_for=lambda keys: "7")
        self.materiality = {"materiality": "No", "materiality_score": 0.1,
                            "confidence_score": 0.9, "reason": "Routine change", "key_change": "Routine update"}
        rows = [self.row(1, ["amd_2024_7_D001"], ["amd_2025_7_D001"], "Modified"),
                self.row(2, [], ["amd_2025_7_D002"], "New"),
                self.row(3, ["amd_2024_7_D002"], [], "Removed")]
        rows[0]["materiality_analysis"] = deepcopy(self.materiality)
        self.document = {"company": "amd", "previous_year": "2024", "current_year": "2025",
                         "run_complete": True, "error": None, "alignments": rows}
        self.path = self.output / "alignments.json"
        self.path.write_text(json.dumps(self.document))
        (self.output / "needs_review.json").write_text('{"sentinel":"leave unchanged"}')

    def row(self, number, before, after, label):
        return {"match_id": f"amd_2024_2025_M{number:04d}", "status": "ai_verified" if before and after else "unmatched",
                "previous_ids": before, "current_ids": after,
                "change_analysis": {"status": "completed", "lexical": None, "semantic": None,
                                    "llm": label, "final_taxonomy": None}}

    def test_matched_text_now_returns_real_lexical_scores(self):
        same = compute_lexical_metrics("Computing demand increased.", "Computing demand increased.")
        self.assertEqual(same["change_taxonomy"], "Unchanged")
        self.assertAlmostEqual(same["cosine_similarity"], 1.0)
        changed = compute_lexical_metrics("We sell chips.", "We sell chips and cloud services.")
        self.assertGreater(changed["word_count_delta"], 0)
        self.assertIn("cloud", changed["added_tokens_sample"])
        self.assertLess(changed["sequence_match_ratio"], 1.0)
        self.assertEqual(compute_lexical_metrics("", "")['change_taxonomy'], "Unchanged")

    def test_postgres_enrichment_reads_alignment_object_without_local_input_json(self):
        from sec_disclosure.analysis import aligned_changes as module
        from sec_disclosure.storage import pipeline_snapshots as snapshots
        self.path.unlink()
        self.data.hashes = {}
        snapshot = {'documents': {'alignments.json': {'payload': self.document},
                                  'needs_review.json': {'payload': {}},
                                  'manifest.json': {'payload': {'input_hashes': {}}}}}
        with patch.object(snapshots, 'alignment_snapshot', return_value=snapshot), \
             patch.object(module, 'AlignmentData', return_value=self.data) as evidence, \
             patch.object(module, 'load_config', return_value=object()), \
             patch.object(module, 'make_embedder', return_value=lambda texts: [[1.0, 0.0] for _ in texts]), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(['--ticker', 'amd', '--previous-year', '2024', '--current-year', '2025',
                '--alignment-dir', str(self.output), '--input-source', 'postgres']), 0)
        self.assertEqual(evidence.call_args.kwargs['input_source'], 'postgres')
        document = json.loads(self.path.read_text())
        self.assertTrue(all(row['change_analysis']['final_taxonomy'] for row in document['alignments']))

    def test_all_fields_survive_json_normalization_and_database_mapping(self):
        review = (self.output / "needs_review.json").read_bytes()
        seen = []
        def embed(texts):
            seen.extend(texts)
            return [[1.0, 0.0] if text.strip() else [0.0, 0.0] for text in texts]
        document = enrich_alignment(self.output, self.data, embedder=embed)
        self.assertTrue(seen)
        for row, label in zip(document["alignments"], ("Modified", "New", "Removed")):
            analysis = row["change_analysis"]
            self.assertIsInstance(analysis["lexical"], dict)
            self.assertIsInstance(analysis["semantic"], dict)
            self.assertEqual(analysis["llm"], label)
            self.assertEqual(analysis["final_taxonomy"], label)
            self.assertEqual(compact_change_analysis(analysis), analysis)
            row.setdefault("relationship", "one_to_one")
            stored = alignment_row(row, 7, "alignments")
            self.assertEqual(json.loads(stored["change_analysis"]), analysis)
        self.assertEqual(document["alignments"][1]["change_analysis"]["semantic"]["label"], "New")
        self.assertEqual(document["alignments"][2]["change_analysis"]["semantic"]["label"], "Removed")
        self.assertEqual(json.loads(alignment_row(document["alignments"][0], 7, "alignments")["extra"])
                         ["materiality_analysis"], self.materiality)
        self.assertEqual((self.output / "needs_review.json").read_bytes(), review)

    def test_missing_evidence_stops_before_embedding_or_writing(self):
        del self.data.records["amd_2024_7_D001"]
        embed = Mock()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "missing"):
            enrich_alignment(self.output, self.data, embedder=embed)
        embed.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_partial_llm_stage_cannot_be_presented_as_completed_analysis(self):
        self.document["alignments"][0]["change_analysis"]["status"] = "not_started"
        self.path.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(ValueError, "LLM change analysis"):
            enrich_alignment(self.output, self.data, embedder=Mock())


if __name__ == "__main__":
    unittest.main()
