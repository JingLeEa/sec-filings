"""Source-text exclusions before and after disclosure grouping."""


TABLE_INTRODUCTION_POLICY = "exclude_single_sentence_colon_paragraphs_v2"
TABLE_INTRODUCTION_REASON = "single_sentence_paragraph_ending_with_colon"
POST_EXTRACTION_FILTER_POLICY = "exclude_single_sentence_colon_disclosures_v1"
POST_EXTRACTION_REASON = "single_sentence_disclosure_ending_with_colon"


def is_single_sentence_colon(text: str, sentence_count: int) -> bool:
    """Check paragraph or disclosure source text using its sentence record count.

    Ignore trailing whitespace, but do not infer table content or resplit text.
    """
    return sentence_count == 1 and text.rstrip().endswith(":")
