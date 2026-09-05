import unittest

from compare_item_changes import compare_records, parse_title_map_args


class CompareItemChangesTests(unittest.TestCase):
    def test_removes_same_sentences_as_sentence_rows(self):
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
        self.assertEqual(len(title_group["2023_only"]), 2)
        self.assertEqual(len(title_group["2024_only"]), 1)
        self.assertEqual(title_group["2023_only"][0]["source_id"], "2023_P001")
        self.assertEqual(title_group["2023_only"][0]["sentence"], "This sentence only appears in 2023.")
        self.assertEqual(title_group["2023_only"][1]["sentence"], "Another old-only sentence.")
        self.assertNotIn("normalized", title_group["2023_only"][0])

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

    def test_item_titles_follow_extracted_sequence_not_alphabetical_order(self):
        old_records = [
            {
                "id": "2023_P001",
                "company": "nvda",
                "year": "2023",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Zeta Section",
                "item_chunk_index": 1,
                "source_block_index": 10,
                "text": "Old zeta sentence.",
            },
            {
                "id": "2023_P002",
                "company": "nvda",
                "year": "2023",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Alpha Section",
                "item_chunk_index": 2,
                "source_block_index": 11,
                "text": "Old alpha sentence.",
            },
        ]
        new_records = [
            {
                "id": "2024_P001",
                "company": "nvda",
                "year": "2024",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Zeta Section",
                "item_chunk_index": 1,
                "source_block_index": 12,
                "text": "New zeta sentence.",
            },
            {
                "id": "2024_P002",
                "company": "nvda",
                "year": "2024",
                "item": "1",
                "item_default_title": "Business",
                "item_title": "Alpha Section",
                "item_chunk_index": 2,
                "source_block_index": 13,
                "text": "New alpha sentence.",
            },
        ]

        comparison = compare_records(old_records, new_records, old_year="2023", new_year="2024", company="nvda")
        titles = [group["item_title"] for group in comparison["items"][0]["item_titles"]]

        self.assertEqual(titles, ["Zeta Section", "Alpha Section"])

    def test_manual_title_mapping_compares_verified_header_rename(self):
        old_records = [
            {
                "id": "2024_P001",
                "company": "nvda",
                "year": "2024",
                "item": "1A",
                "item_default_title": "Risk Factors",
                "item_title": "Risks Related to Demand, Supply and Manufacturing",
                "item_chunk_index": 1,
                "source_block_index": 10,
                "text": "Demand can change quickly. This sentence only appears in 2024.",
            }
        ]
        new_records = [
            {
                "id": "2025_P001",
                "company": "nvda",
                "year": "2025",
                "item": "1A",
                "item_default_title": "Risk Factors",
                "item_title": "Risks Related to Demand, Supply, and Manufacturing",
                "item_chunk_index": 1,
                "source_block_index": 11,
                "text": "Demand can change quickly. This sentence only appears in 2025.",
            }
        ]

        comparison = compare_records(
            old_records,
            new_records,
            old_year="2024",
            new_year="2025",
            company="nvda",
            title_mappings={
                ("1A", "Risks Related to Demand, Supply and Manufacturing"):
                    "Risks Related to Demand, Supply, and Manufacturing"
            },
        )
        title_group = comparison["items"][0]["item_titles"][0]

        self.assertEqual(title_group["title_match"], "manual")
        self.assertEqual(title_group["unchanged_sentences_removed"], 1)
        self.assertEqual(len(title_group["2024_only"]), 1)
        self.assertEqual(len(title_group["2025_only"]), 1)
        self.assertEqual(
            title_group["item_title"],
            "2024: Risks Related to Demand, Supply and Manufacturing -> "
            "2025: Risks Related to Demand, Supply, and Manufacturing",
        )

    def test_parse_title_map_args(self):
        mappings = parse_title_map_args(
            [
                "item_1a::Risks Related to Demand, Supply and Manufacturing::"
                "Risks Related to Demand, Supply, and Manufacturing"
            ]
        )

        self.assertEqual(
            mappings,
            {
                ("1A", "Risks Related to Demand, Supply and Manufacturing"):
                    "Risks Related to Demand, Supply, and Manufacturing"
            },
        )

    def test_manual_title_mapping_requires_exact_headers(self):
        old_records = [
            {
                "id": "2024_P001",
                "company": "nvda",
                "year": "2024",
                "item": "1A",
                "item_default_title": "Risk Factors",
                "item_title": "Old Header",
                "text": "Old sentence.",
            }
        ]
        new_records = [
            {
                "id": "2025_P001",
                "company": "nvda",
                "year": "2025",
                "item": "1A",
                "item_default_title": "Risk Factors",
                "item_title": "New Header",
                "text": "New sentence.",
            }
        ]

        with self.assertRaisesRegex(ValueError, "new header was not found"):
            compare_records(
                old_records,
                new_records,
                old_year="2024",
                new_year="2025",
                company="nvda",
                title_mappings={("1A", "Old Header"): "Typo Header"},
            )


if __name__ == "__main__":
    unittest.main()
