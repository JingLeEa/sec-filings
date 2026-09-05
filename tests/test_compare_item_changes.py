import unittest

from compare_item_changes import compare_records


class CompareItemChangesTests(unittest.TestCase):
    def test_removes_same_sentences_but_keeps_paragraph_rows(self):
        old_records = [
            {
                "id": "2023_P001",
                "company": "nvda",
                "year": "2023",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Our Company",
                "item_chunk_index": 1,
                "source_block_index": 10,
                "text": "This sentence is unchanged. This sentence only appears in 2023. Another old-only sentence.",
            }
        ]
        new_records = [
            {
                "id": "2024_P001",
                "company": "nvda",
                "year": "2024",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Our Company",
                "item_chunk_index": 1,
                "source_block_index": 11,
                "text": "This sentence is unchanged. This sentence only appears in 2024.",
            }
        ]

        comparison = compare_records(old_records, new_records, old_year="2023", new_year="2024", company="nvda")
        title_group = comparison["items"][0]["item_titles"][0]

        self.assertEqual(title_group["unchanged_sentences_removed"], 1)
        self.assertEqual(title_group["2023_only_sentence_count"], 2)
        self.assertEqual(title_group["2024_only_sentence_count"], 1)
        self.assertEqual(len(title_group["2023_only"]), 1)
        self.assertEqual(title_group["2023_only"][0]["source_id"], "2023_P001")
        self.assertEqual(title_group["2023_only"][0]["changed_sentence_count"], 2)
        self.assertEqual(
            title_group["2023_only"][0]["text"],
            "This sentence only appears in 2023. Another old-only sentence.",
        )

    def test_normalizes_typography_in_diff_text(self):
        old_records = [
            {
                "id": "2023_P001",
                "company": "nvda",
                "year": "2023",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Our Company",
                "item_chunk_index": 1,
                "source_block_index": 10,
                "text": "NVIDIA’s “platform” changed — materially.",
            }
        ]
        new_records = [
            {
                "id": "2024_P001",
                "company": "nvda",
                "year": "2024",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Our Company",
                "item_chunk_index": 1,
                "source_block_index": 11,
                "text": "NVIDIA's \"platform\" changed - materially.",
            }
        ]

        comparison = compare_records(old_records, new_records, old_year="2023", new_year="2024", company="nvda")

        self.assertEqual(comparison["totals"]["unchanged_sentences_removed"], 1)
        self.assertEqual(comparison["totals"]["2023_only_sentences"], 0)
        self.assertEqual(comparison["totals"]["2024_only_sentences"], 0)


if __name__ == "__main__":
    unittest.main()
