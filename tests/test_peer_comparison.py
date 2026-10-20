import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from sec_disclosure.agents.peer_comparison import (
    load_company,
    make_candidates,
    run,
    validate_response,
)


def disclosure(ticker, did, item, content, sid):
    return {
        "disclosure_id": did,
        "item": item,
        "summary": content,
        "taxonomy": "Financial & Capital Resources",
        "content": content,
        "sources": [{
            "paragraph_id": f"{ticker.lower()}_P1",
            "source_url": f"https://www.sec.gov/{ticker.lower()}",
            "sentences": [{"sentence_id": sid, "text": content}],
        }],
    }


class PeerComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = disclosure("AAA", "aaa_2024_1A_P1", "1A", "We depend on a single chip supplier.", "a1")
        self.peer = disclosure("BBB", "bbb_2024_1A_P1", "1A", "A sole semiconductor supplier creates risk.", "b1")
        self.other_item = disclosure("BBB", "bbb_2024_7_P1", "7", "We report operating results.", "b2")
        for ticker, rows in (("aaa", [self.target]), ("bbb", [self.peer, self.other_item])):
            folder = self.root / ticker / "2024"
            folder.mkdir(parents=True)
            (folder / "disclosures.json").write_text(json.dumps({
                "company": ticker, "fiscal_year": "2024", "disclosures": rows
            }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_candidates_and_validate_evidence(self):
        targets = load_company(self.root, "aaa", "2024")
        peers = {"BBB": load_company(self.root, "bbb", "2024")}
        ranked = make_candidates(targets, peers, 3)[self.target["disclosure_id"]]["BBB"]
        self.assertEqual([row["disclosure_id"] for row in ranked], [self.peer["disclosure_id"]])
        result = validate_response({"comparisons": [{
            "peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
            "relationship": "similar", "summary": "Both describe reliance on a sole supplier.",
            "target_sentence_ids": ["a1"], "peer_sentence_ids": ["b1"],
        }], "no_match": []}, targets[0], {"BBB": ranked}, ["BBB"])
        self.assertEqual(result[0]["evidence"][0]["sentences"][0]["sentence_id"], "a1")

    def test_rejects_citation_from_another_disclosure(self):
        targets = load_company(self.root, "aaa", "2024")
        peers = {"BBB": load_company(self.root, "bbb", "2024")}
        ranked = make_candidates(targets, peers, 3)[self.target["disclosure_id"]]["BBB"]
        with self.assertRaisesRegex(ValueError, "sentence IDs"):
            validate_response({"comparisons": [{
                "peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
                "relationship": "similar", "summary": "Comparison.",
                "target_sentence_ids": ["b1"], "peer_sentence_ids": ["b1"],
            }], "no_match": []}, targets[0], {"BBB": ranked}, ["BBB"])

    def test_dry_run_creates_no_files(self):
        args = Namespace(ticker="aaa", peers=["bbb"], year="2024", items=["1A"],
                         disclosures_dir=self.root, data_dir=self.root, output_dir=None,
                         top_k=3, dry_run=True, env_file=None, max_requests=100,
                         max_tokens=100, max_prompt_chars=10000, timeout=10)
        result = run(args)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.root / "peer_comparison").exists())


if __name__ == "__main__":
    unittest.main()
