"""Tests for the dashboard's data layer, text comparison and report loaders (no Streamlit needed)."""
import json
from pathlib import Path

import pytest

from sec_ui import config, data, diff, reports

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DATA = Path(__file__).resolve().parents[2] / "sec-filings-test-data" / "test_data"


@pytest.fixture(autouse=True)
def file_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(FIXTURES / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(FIXTURES / "no_disclosures_here"))
    monkeypatch.setenv("SEC_UI_SYNTHESIS_DIRS", str(FIXTURES / "synthesis"))
    monkeypatch.setenv("SEC_UI_PEER_DIR", str(FIXTURES / "peer_comparison"))
    monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "reviews"))


@pytest.fixture()
def amd():
    return data.load_comparison("amd", 2024, 2025, data.discover_source())


def test_discovers_company_and_year_pairs():
    source = data.discover_source()
    assert source.companies == ["amd"]
    assert source.pairs("amd") == [(2023, 2024), (2024, 2025)]


def test_loads_every_alignment_including_needs_review(amd):
    saved = json.loads((FIXTURES / "alignments/amd/2024-2025/alignments.json").read_text())["alignments"]
    review = json.loads((FIXTURES / "alignments/amd/2024-2025/needs_review.json").read_text())
    review = review["alignments"] if isinstance(review, dict) else review
    assert len(amd["records"]) == len(saved) + len(review)
    assert amd["meta"]["source"] == "files"
    assert amd["meta"]["run_complete"] is True


def test_records_have_known_values_and_ids(amd):
    for record in amd["records"]:
        assert record["match_id"].startswith("amd_2024_2025_")
        assert record["status"] in data.STATUS_LABELS
        assert record["relationship"] in data.RELATIONSHIP_LABELS
        assert record["change_label"] in config.CHANGE_LABELS + [data.NOT_LABELLED]
        assert record["materiality"] in {"Material", "Not material", data.NOT_ASSESSED}
        assert record["missing_side"] in {"", "earlier", "later"}
        # one-sided matches must really be one-sided
        if record["relationship"] == "previous_only":
            assert record["missing_side"] == "later" and not record["current_ids"]
        if record["relationship"] == "current_only":
            assert record["missing_side"] == "earlier" and not record["previous_ids"]


def test_evidence_keeps_sentence_ids_and_urls(amd):
    with_evidence = [r for r in amd["records"] if r["evidence"]]
    assert with_evidence
    for record in with_evidence[:50]:
        for row in record["evidence"]:
            assert row["sentence_id"] and row["disclosure_id"]
            assert row["source_url"].startswith("https://www.sec.gov/")


def test_text_falls_back_to_cited_sentences_when_disclosures_missing(amd):
    one_to_one = next(r for r in amd["records"] if r["relationship"] == "one_to_one" and r["evidence"])
    assert one_to_one["text_source"] == "cited sentences"
    assert one_to_one["previous_text"] and one_to_one["current_text"]


def test_filters_combine_and_can_return_nothing(amd):
    records = amd["records"]
    removed = data.filter_records(records, change_labels=["Removed"])
    assert removed and all(r["change_label"] == "Removed" for r in removed)
    item = data.filter_records(records, items=["1A"], change_labels=["Removed"])
    assert 0 < len(item) <= len(removed) and all(r["item"] == "1A" for r in item)
    assert data.filter_records(records, change_labels=["Removed"], statuses=["ai_verified"]) == []
    assert data.filter_records(records, text="zzzz-not-in-any-filing") == []
    assert len(data.filter_records(records)) == len(records)


def test_text_filter_searches_summaries(amd):
    word = amd["records"][0]["previous_summary"].split()[3].lower()
    hits = data.filter_records(amd["records"], text=word)
    assert hits and amd["records"][0] in hits


def test_export_keeps_references(amd):
    rows = data.export_rows(amd["records"][:20])
    assert len(rows) == 20
    for row, record in zip(rows, amd["records"][:20]):
        assert row["match_id"] == record["match_id"]
        assert row["previous_disclosure_ids"] == "; ".join(record["previous_ids"])
        if record["evidence"]:
            assert row["sentence_ids"] and "sec.gov" in row["source_urls"]
    assert "previous_text" not in data.export_rows(amd["records"][:1], include_text=False)[0]
    csv_text = reports.rows_to_csv(rows)
    assert csv_text.splitlines()[0].startswith("match_id,company")


def test_count_by_adds_up(amd):
    counts = data.count_by(amd["records"], "change_label")
    assert sum(counts.values()) == len(amd["records"])


def test_missing_comparison_raises_plain_error():
    with pytest.raises(data.DataError, match="No data for"):
        data.load_comparison("zzz", 2024, 2025, data.discover_source())


def test_no_data_folder_gives_empty_source(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path))
    assert data.discover_source().companies == []


def test_unreadable_alignment_file_is_a_data_error(monkeypatch, tmp_path):
    folder = tmp_path / "amd" / "2024-2025"
    folder.mkdir(parents=True)
    (folder / "alignments.json").write_text("{not json")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path))
    with pytest.raises(data.DataError):
        data.load_comparison("amd", 2024, 2025, data.discover_source())


def test_materiality_and_labels_are_not_invented():
    raw = {"match_id": "x_2024_2025_M0001", "relationship": "one_to_one", "status": "needs_review",
           "previous_ids": ["x_2024_1_D001"], "current_ids": ["x_2025_1_D001"]}
    record = data.normalise(raw, {}, "x", 2024, 2025)
    assert record["change_label"] == data.NOT_LABELLED
    assert record["materiality"] == data.NOT_ASSESSED
    assert record["category"] == data.NOT_CLASSIFIED


def test_default_final_uses_llm_instead_of_an_empty_or_different_pipeline_final():
    for final in (None, "Reduced"):
        record = data.normalise({"change_analysis": {"status": "completed", "llm": "Expanded",
                                 "final_taxonomy": final}}, {}, "amd", 2024, 2025)
        assert record["change_label"] == record["final_change_label"] == "Expanded"
        assert record["pipeline_change_label"] == final
        assert record["final_label_source"] == "llm"


@pytest.mark.skipif(not REAL_DATA.is_dir(), reason="test-data worktree not available")
def test_real_data_counts(monkeypatch):
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(REAL_DATA / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(REAL_DATA / "disclosures"))
    source = data.discover_source()
    assert len(source.companies) >= 11
    result = data.load_comparison("amd", 2024, 2025, source)
    assert len(result["records"]) == 432
    assert result["meta"]["has_text"] is True
    grouped = next(r for r in result["records"] if r["relationship"] == "many_to_one")
    assert len(grouped["previous_ids"]) > 1 and grouped["text_source"] == "disclosure"
    assert len(grouped["previous_members"]) == len(grouped["previous_ids"])


# ------------------------------------------------------------------ text comparison


def test_diff_marks_added_and_removed_words():
    result = diff.diff_texts("The company sells chips.", "The company sells chips and software.")
    assert result["added_words"] == 2 and result["removed_words"] == 0
    assert '<span class="ins">and software</span>.' in result["current_html"]
    assert "span" not in result["previous_html"]


def test_diff_identical_ignores_spacing_and_bullets():
    assert diff.diff_texts("a  b\nc", "a b c")["identical"]
    assert diff.diff_texts("◦Government actions", "• Government actions")["identical"]


def test_diff_escapes_markup_and_dollar_signs():
    result = diff.diff_texts("<script>alert(1)</script> costs $5", "costs $5 and $6")
    combined = result["previous_html"] + result["current_html"]
    assert "<script>" not in combined and "&lt;script&gt;" in combined
    assert "$" not in combined and "&#36;5" in combined


def test_diff_with_empty_side():
    result = diff.diff_texts("", "brand new text")
    assert result["added_words"] == 3 and result["previous_html"] == ""
    assert diff.whole_side_html("gone", "removed") == '<span class="del">gone</span>'


# ------------------------------------------------------------------ synthesis and peer outputs


def test_synthesis_loads_and_every_evidence_id_resolves():
    (path,) = reports.list_synthesis_files()
    synth = reports.load_synthesis(path)
    assert "AMD" in reports.synthesis_label(path, synth)
    for finding in synth["report"]["key_findings"]:
        for evidence_id in finding["evidence_ids"]:
            assert reports.evidence_for(synth, evidence_id) or reports.metric_for(synth, evidence_id), evidence_id


def test_synthesis_evidence_rows_and_markdown_keep_references():
    (path,) = reports.list_synthesis_files()
    synth = reports.load_synthesis(path)
    rows = reports.synthesis_evidence_rows(synth)
    assert rows and not [r for r in rows if "not found" in r.get("note", "")]
    cited = [r for r in rows if r.get("sentence_id")]
    assert cited and all(r["source_url"].startswith("https://www.sec.gov/") for r in cited)
    markdown = reports.synthesis_markdown(synth)
    assert "## Executive summary" in markdown and cited[0]["sentence_id"] in markdown
    assert "model-generated" in markdown


def test_synthesis_unknown_evidence_is_reported_not_hidden():
    synth = {"report": {"key_findings": [{"title": "T", "analysis": "A", "evidence_ids": ["nope"]}]}, "source_catalog": {}}
    rows = reports.synthesis_evidence_rows(synth)
    assert rows[0]["note"] == "Evidence ID not found in the source catalog"


def test_synthesis_rejects_wrong_file(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text(json.dumps({"hello": 1}))
    with pytest.raises(data.DataError):
        reports.load_synthesis(bad)
    bad.write_text("nope")
    with pytest.raises(data.DataError):
        reports.load_synthesis(bad)


def test_trend_rows_format_money_and_growth():
    (path,) = reports.list_synthesis_files()
    rows = reports.trend_rows(reports.load_synthesis(path))
    revenue = next(r for r in rows if r["Measure"] == "Revenue")
    assert revenue["From"] == "$22.68B" and revenue["Change"] == "+13.7%"


def test_peer_output_flattens_with_citations():
    (path,) = reports.list_peer_files()
    peer = reports.load_peer(path)
    rows = reports.peer_rows(peer)
    assert {r["status"] for r in rows} == {"compared", "no_comparable_candidate"}
    compared = [r for r in rows if r["status"] == "compared"]
    assert compared and all(r["target_sentence_ids"] and r["peer_sentence_ids"] and "sec.gov" in r["source_urls"] for r in compared)
    assert "Peer statements are model-generated" in reports.peer_markdown(peer)
    assert peer["_sample_data"]


def test_peer_rejects_wrong_file(tmp_path):
    bad = tmp_path / "peer_comparison.json"
    bad.write_text(json.dumps({"no": "comparisons"}))
    with pytest.raises(data.DataError):
        reports.load_peer(bad)


@pytest.mark.skipif(not (REAL_DATA.is_dir() and __import__("os").environ.get("SEC_UI_TEST_POSTGRES")),
                    reason="needs the test-data worktree and a database holding AMD 2024-2025 (set SEC_UI_TEST_POSTGRES=1)")
def test_postgres_and_files_give_the_same_records(monkeypatch):
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(REAL_DATA / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(REAL_DATA / "disclosures"))
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
    from_db = data.load_comparison("amd", 2024, 2025, data.discover_source())
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    from_files = data.load_comparison("amd", 2024, 2025, data.discover_source())
    assert from_db["meta"]["source"] == "postgres" and from_files["meta"]["source"] == "files"
    assert len(from_db["records"]) == len(from_files["records"]) == 432
    skipped = {"source_file", "previous_members", "current_members"}  # raw member dicts have different extra fields
    for from_db_record, from_file_record in zip(from_db["records"], from_files["records"]):
        assert {k: v for k, v in from_db_record.items() if k not in skipped} == \
               {k: v for k, v in from_file_record.items() if k not in skipped}
        for side in ("previous_members", "current_members"):
            assert [(m["disclosure_id"], m.get("content")) for m in from_db_record[side]] == \
                   [(m["disclosure_id"], m.get("content")) for m in from_file_record[side]]
