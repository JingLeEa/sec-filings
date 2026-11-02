"""Source ownership remains exact when comparisons split, merge or repeat text."""
from html import unescape
import re

from sec_ui import data, diff, provenance


def member(disclosure_id, paragraphs):
    sources = []
    for index, text in enumerate(paragraphs, 1):
        paragraph_id = f"{disclosure_id}_P{index:03d}"
        sources.append({"paragraph_id": paragraph_id, "selected_text": text,
                        "source_url": "https://www.sec.gov/example.htm",
                        "sentences": [{"sentence_id": f"{paragraph_id}_S001", "text": text}]})
    return {"disclosure_id": disclosure_id, "content": "\n\n".join(paragraphs), "sources": sources}


def comparison(earlier, later, evidence=None):
    raw = {"match_id": "x_2023_2024_M001", "previous_ids": [m["disclosure_id"] for m in earlier],
           "current_ids": [m["disclosure_id"] for m in later], "evidence": evidence or []}
    return data.normalise(raw, {m["disclosure_id"]: m for m in earlier + later}, "x", 2023, 2024)


def test_identical_paragraph_text_keeps_distinct_disclosure_owners():
    first = member("x_2023_D001", ["Repeated sentence.", "Second paragraph."])
    second = member("x_2023_D002", ["Repeated sentence."])
    record = comparison([first, second], [])
    blocks = provenance.source_blocks(record, "previous")
    assert [(b["disclosure_id"], b["paragraph_id"]) for b in blocks] == [
        (first["disclosure_id"], first["sources"][0]["paragraph_id"]),
        (first["disclosure_id"], first["sources"][1]["paragraph_id"]),
        (second["disclosure_id"], second["sources"][0]["paragraph_id"])]
    assert [record["previous_text"][b["start"]:b["end"]] for b in blocks] == [
        "Repeated sentence.", "Second paragraph.", "Repeated sentence."]


def test_merge_labels_do_not_create_artificial_text_changes():
    record = comparison([member("x_2023_D001", ["Alpha earnings."]), member("x_2023_D002", ["Beta sales."])],
                        [member("x_2024_D001", ["Alpha earnings.", "Beta sales."])])
    previous = provenance.source_blocks(record, "previous")
    current = provenance.source_blocks(record, "current")
    result = diff.diff_texts(record["previous_text"], record["current_text"],
                             previous_ranges=[(b["start"], b["end"]) for b in previous],
                             current_ranges=[(b["start"], b["end"]) for b in current])
    assert result["identical"]
    assert result["added_words"] == result["removed_words"] == 0
    assert result["previous_blocks_html"] == result["current_blocks_html"] == ["Alpha earnings.", "Beta sales."]
    assert previous[0]["disclosure_id"] != current[0]["disclosure_id"]


def test_global_highlights_stay_with_their_source_paragraph_and_escape_text():
    earlier = ["Revenue is $5.", "<script>old</script> sales."]
    later = ["Revenue is $7.", "<script>new</script> sales."]
    record = comparison([member("x_2023_D001", earlier)], [member("x_2024_D001", later)])
    blocks = {side: provenance.source_blocks(record, side) for side in ("previous", "current")}
    result = diff.diff_texts(record["previous_text"], record["current_text"],
        previous_ranges=[(b["start"], b["end"]) for b in blocks["previous"]],
        current_ranges=[(b["start"], b["end"]) for b in blocks["current"]])
    for side, paragraphs in (("previous", earlier), ("current", later)):
        for rendered, text in zip(result[f"{side}_blocks_html"], paragraphs):
            assert unescape(re.sub(r"<[^>]*>", "", rendered)) == text
            assert "<script>" not in rendered and "$" not in rendered
    assert 'class="del"' in result["previous_blocks_html"][0]
    assert 'class="ins"' in result["current_blocks_html"][1]


def test_cited_fallback_keeps_saved_order_and_never_guesses_a_paragraph_id():
    first, second = {"disclosure_id": "x_2023_D001"}, {"disclosure_id": "x_2023_D002"}
    citations = [
        {"disclosure_id": second["disclosure_id"], "sentences": [{"sentence_id": "P200_S001", "paragraph_id": "P200", "text": "Same text."}]},
        {"disclosure_id": first["disclosure_id"], "sentences": [{"sentence_id": "P100_S001", "text": "Same text."}]},
        {"disclosure_id": second["disclosure_id"], "sentences": [{"sentence_id": "P201_S001", "paragraph_id": "P201", "text": "Later text."}]}]
    record = comparison([first, second], [], citations)
    blocks = provenance.source_blocks(record, "previous")
    assert [(b["disclosure_id"], b["paragraph_id"]) for b in blocks] == [
        (second["disclosure_id"], "P200"), (first["disclosure_id"], ""), (second["disclosure_id"], "P201")]
    assert [record["previous_text"][b["start"]:b["end"]] for b in blocks] == ["Same text.", "Same text.", "Later text."]


def test_missing_member_text_is_still_identified():
    first = member("x_2023_D001", ["Available text."])
    second = {"disclosure_id": "x_2023_D002"}
    record = comparison([first, second], [])
    text = record["previous_text"]
    blocks = provenance.source_blocks(record, "previous")
    assert blocks[1]["disclosure_id"] == second["disclosure_id"]
    assert blocks[1]["start"] == blocks[1]["end"] == len(text)
    assert record["previous_text"] == text


def test_inconsistent_source_text_does_not_get_an_invented_paragraph_owner():
    first = member("x_2023_D001", ["Wrong saved source text."])
    first["content"] = "Actual displayed disclosure content."
    record = comparison([first], [])
    (block,) = provenance.source_blocks(record, "previous")
    assert block["disclosure_id"] == first["disclosure_id"] and block["paragraph_id"] == ""
    assert provenance.sentence_rows(record) == []


def test_sentence_table_keeps_all_selected_sentences_and_citation_flags():
    first = member("x_2023_D001", ["Repeated wording.", "Additional detail."])
    second = member("x_2023_D002", ["Repeated wording."])
    cited = {"disclosure_id": first["disclosure_id"], "sentences": [
        {**first["sources"][0]["sentences"][0], "paragraph_id": first["sources"][0]["paragraph_id"]}]}
    record = comparison([first, second], [], [cited])
    rows = provenance.sentence_rows(record)
    assert len(rows) == 3
    assert [r["cited"] for r in rows] == [True, False, False]
    assert rows[0]["text"] == rows[2]["text"] and rows[0]["disclosure_id"] != rows[2]["disclosure_id"]
    assert all(r["disclosure_id"] and r["paragraph_id"] and r["sentence_id"] and r["source_url"] for r in rows)
