"""Starter dataset integrity, non-destructive startup, and opt-in PostgreSQL acceptance."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from sec_disclosure.storage import starter_data as seed


def test_bundled_reports_include_every_company_and_report_scope():
    manifest = seed.read_manifest()
    assert manifest["companies"] == ["amd", "avgo", "bac", "c", "gs", "intc", "jpm", "ms", "mu", "nvda", "qcom"]
    wanted = {seed.scope_key(entry["scope"]) for entry in manifest["files"]}
    counts, seen = Counter(), set()
    assert all("requests/" not in row["path"] and "/jobs/" not in row["path"] for row in manifest["files"])
    for key, documents in seed.report_groups(seed.BUNDLE, manifest, wanted):
        assert key not in seen
        seen.add(key)
        counts[key[0]] += 1
        if key[0] == "peer_comparison":
            seed.peer_comparisons.validate_report(next(iter(documents.values()))["payload"])
    assert counts == {"disclosures": 33, "alignments": 22, "synthesis": 10, "peer_comparison": 12}
    assert seen == wanted


def test_existing_database_skips_archive_read_and_all_imports():
    manifest = seed.read_manifest()
    wanted = {seed.scope_key(entry["scope"]) for entry in manifest["files"]}
    with patch.object(seed, "existing_scopes", return_value=wanted), \
         patch.object(seed, "report_groups", side_effect=AssertionError("Do not reload existing reports")):
        result = seed.seed_connection(MagicMock())
    assert result["imported"] == {} and result["skipped"] == 77


def test_database_error_rolls_back_without_exposing_credentials():
    connection = MagicMock()
    with patch("psycopg.connect", return_value=connection), \
         patch.object(seed, "seed_connection", side_effect=ValueError("password=private")):
        with pytest.raises(seed.StarterDataError, match="rolled back") as error:
            seed.seed_database()
    assert "private" not in str(error.value)
    assert connection.__exit__.call_args.args[0] is ValueError


def test_archive_integrity_checked_without_extracting_files(tmp_path):
    raw = b'{"company":"amd"}'
    target = tmp_path / "reports.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        member = tarfile.TarInfo("disclosures/amd/2024/disclosures.json")
        member.size = len(raw)
        archive.addfile(member, io.BytesIO(raw))
    scope = {"kind": "disclosures", "company": "amd", "years": [2024]}
    manifest = {"source_commit": "test", "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "files": [{"path": member.name, "scope": scope, "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}]}
    groups = list(seed.report_groups(tmp_path, manifest, {seed.scope_key(scope)}))
    assert groups[0][1]["disclosures.json"]["payload"] == {"company": "amd"}
    assert list(tmp_path.iterdir()) == [target]
    manifest["files"][0]["sha256"] = "incorrect"
    with pytest.raises(ValueError, match="report checksum"):
        list(seed.report_groups(tmp_path, manifest, {seed.scope_key(scope)}))


@pytest.mark.skipif(os.environ.get("SEC_STARTER_DB_INTEGRATION") != "1", reason="Requires local PostgreSQL")
def test_fresh_database_parallel_startup_preserves_new_results_and_repairs_missing_reports(monkeypatch, tmp_path):
    import psycopg
    from psycopg import sql

    arguments = seed.disclosures.connection_arguments()
    connection_info = psycopg.conninfo.conninfo_to_dict(arguments.get("conninfo", ""),
                       **{key: value for key, value in arguments.items() if key != "conninfo"})
    assert connection_info.get("host", "") in {"", "localhost", "127.0.0.1", "::1"}, "Use a local test database"
    database = "sec_starter_test_" + uuid4().hex[:12]
    with psycopg.connect(**arguments, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with psycopg.connect(**{**arguments, "dbname": database}) as conn, conn.cursor() as cur:
                for schema in sorted((seed.ROOT / "db/init").glob("*.sql")):
                    cur.execute(schema.read_text())
            target = {**arguments, "dbname": database}
            with patch.object(seed, "import_alignments", side_effect=RuntimeError("Interrupted import")):
                with pytest.raises(RuntimeError, match="Interrupted import"):
                    with psycopg.connect(**target) as conn:
                        seed.seed_connection(conn)
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT count(*) FROM disclosures").fetchone()[0] == 0

            def launch(_):
                with psycopg.connect(**target) as conn:
                    return seed.seed_connection(conn)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(launch, range(2)))
            assert sorted(sum(row["imported"].values()) for row in results) == [0, 77]
            with psycopg.connect(**target) as conn:
                for table, count in (("filings", 33), ("alignment_runs", 22),
                                     ("synthesis_reports", 10), ("peer_comparison_reports", 12)):
                    assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0] == count
                disclosure = conn.execute("SELECT disclosure_id FROM disclosures ORDER BY disclosure_id LIMIT 1").fetchone()[0]
                conn.execute("UPDATE disclosures SET summary='Newer pipeline result' WHERE disclosure_id=%s", (disclosure,))
                conn.execute("UPDATE synthesis_reports SET report=jsonb_set(report,'{report,executive_summary}',%s::jsonb) "
                             "WHERE company='AMD'", (json.dumps("Newer synthesis result"),))
            assert launch(0)["imported"] == {}
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT summary FROM disclosures WHERE disclosure_id=%s", (disclosure,)).fetchone()[0] == "Newer pipeline result"
                assert conn.execute("SELECT report->'report'->>'executive_summary' FROM synthesis_reports WHERE company='AMD'").fetchone()[0] == "Newer synthesis result"
                conn.execute("DELETE FROM synthesis_reports WHERE company='GS'")
            # Launching the actual app repairs the missing report automatically.
            import streamlit as st
            from streamlit.testing.v1 import AppTest
            from sec_ui import bootstrap, data, disclosure_data
            monkeypatch.setenv("DATABASE_URL", psycopg.conninfo.make_conninfo(**target))
            monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
            monkeypatch.setenv("SEC_UI_BOOTSTRAP_TEST_DATA", "1")
            monkeypatch.setenv("SEC_UI_REFRESH_AUTOSTART", "0")
            monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "reviews"))
            bootstrap._prepare.clear()
            st.cache_data.clear()
            try:
                at = AppTest.from_file(str(seed.ROOT / "app/Home.py"), default_timeout=60).run()
                assert not at.exception and not at.error
                assert len(at.sidebar.selectbox(key="sel_company").options) == 11
                assert any(caption.value == "Data from PostgreSQL" for caption in at.sidebar.caption)
                assert len(disclosure_data.discover_source().scopes) == 33
                source = data.discover_source()
                assert len(source.scopes) == 22
                for previous, current in ((2023, 2024), (2024, 2025)):
                    comparison = data.load_comparison("c", previous, current, source, apply_reviews=False)
                    assert comparison["meta"]["source"] == "postgres" and comparison["records"]
                at.switch_page("pages/5_Manual_Review.py").run()
                at.segmented_control(key="manual_section").set_value("Disclosure").run()
                assert not at.exception and not at.error
                assert len(at.sidebar.selectbox(key="sel_company").options) == 11
                at.switch_page("pages/1_Synthesis_Report.py").run()
                assert not at.exception and not at.error
                assert sum("(PostgreSQL)" in choice for choice in at.sidebar.selectbox(key="sel_synthesis").options) == 10
                with psycopg.connect(**target) as conn:
                    assert conn.execute("SELECT count(*) FROM synthesis_reports WHERE company='GS'").fetchone()[0] == 1
            finally:
                bootstrap._prepare.clear()
                st.cache_data.clear()
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
