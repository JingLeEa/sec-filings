import unittest

from include_paragraph_context import context_span, paragraph_context_for_id


def record(chunk_id, text, item_title="Competition"):
    return {
        "id": chunk_id,
        "year": "2024",
        "item": "1",
        "item_title": item_title,
        "text": text,
    }


class IncludeParagraphContextTests(unittest.TestCase):
    def test_non_bullet_includes_following_bullet_list(self):
        records = [
            record("2024_1_P001", "Intro to competitors including:"),
            record("2024_1_P002", "• first competitor group;"),
            record("2024_1_P003", "• second competitor group."),
            record("2024_1_P004", "A new paragraph."),
        ]

        self.assertEqual(context_span(records, 0), (0, 2))

    def test_bullet_includes_lead_in_and_contiguous_bullets(self):
        records = [
            record("2024_1_P001", "Intro to competitors including:"),
            record("2024_1_P002", "• first competitor group;"),
            record("2024_1_P003", "• second competitor group."),
            record("2024_1_P004", "A new paragraph."),
        ]

        self.assertEqual(context_span(records, 2), (0, 2))

    def test_bullet_context_stays_inside_same_section(self):
        records = [
            record("2024_1_P001", "Other section intro.", item_title="Other"),
            record("2024_1_P002", "• other bullet.", item_title="Other"),
            record("2024_1_P003", "• current bullet.", item_title="Competition"),
            record("2024_1_P004", "• another current bullet.", item_title="Competition"),
        ]

        self.assertEqual(context_span(records, 2), (2, 3))


if __name__ == "__main__":
    unittest.main()
