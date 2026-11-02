from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from sec_disclosure.storage.starter_data import StarterDataError
from sec_ui import bootstrap


def startup_page():
    import streamlit as st
    from sec_ui.bootstrap import ensure_starter_data
    ensure_starter_data()
    st.write("Dashboard ready")


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
    monkeypatch.setenv("SEC_UI_BOOTSTRAP_TEST_DATA", "1")
    fake = SimpleNamespace(
        StarterDataError=StarterDataError,
        read_manifest=Mock(return_value={"archive_sha256": "dataset-v1"}),
        disclosures=SimpleNamespace(connection_arguments=Mock(return_value={"host": "test-database"})),
        seed_database=Mock(return_value={"imported": {"disclosures": 1}, "companies": ["amd"], "skipped": 0}))
    monkeypatch.setattr(bootstrap, "backend", lambda: fake)
    bootstrap._prepare.clear()
    yield fake
    bootstrap._prepare.clear()


def test_startup_import_is_shared_across_reruns_and_sessions(storage):
    first = AppTest.from_function(startup_page).run()
    assert not first.exception and not first.error
    first.run()
    second = AppTest.from_function(startup_page).run()
    assert not second.exception and not second.error
    storage.seed_database.assert_called_once()
    assert any("Dashboard ready" in text.value for text in second.markdown)


def test_files_mode_and_explicit_opt_out_never_seed(storage, monkeypatch):
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    assert not AppTest.from_function(startup_page).run().exception
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
    monkeypatch.setenv("SEC_UI_BOOTSTRAP_TEST_DATA", "0")
    assert not AppTest.from_function(startup_page).run().exception
    storage.seed_database.assert_not_called()
    storage.read_manifest.assert_not_called()


def test_failed_import_blocks_the_dashboard_and_can_retry(storage):
    result = storage.seed_database.return_value
    storage.seed_database.side_effect = [StarterDataError("Start Docker/PostgreSQL and reload."), result]
    at = AppTest.from_function(startup_page).run()
    assert not at.exception
    assert any("Start Docker/PostgreSQL" in error.value for error in at.error)
    assert all("Dashboard ready" not in text.value for text in at.markdown)
    at.run()
    assert not at.exception and not at.error
    assert any("Dashboard ready" in text.value for text in at.markdown)
    assert storage.seed_database.call_count == 2


def test_switching_database_configuration_rechecks_seed(storage):
    AppTest.from_function(startup_page).run()
    storage.disclosures.connection_arguments.return_value = {"host": "another-test-database"}
    assert not AppTest.from_function(startup_page).run().exception
    assert storage.seed_database.call_count == 2


def test_active_pipeline_cleanup_starts_worker_and_rechecks_on_next_page(storage, monkeypatch):
    from unittest.mock import patch
    monkeypatch.setenv("SEC_UI_REFRESH_AUTOSTART", "1")
    ready = {"imported": {}, "companies": ["amd"], "skipped": 77}
    storage.seed_database.side_effect = [{**ready, "cleanup_pending": ["amd"]}, ready]
    with patch.object(bootstrap.subprocess, "Popen") as worker:
        at = AppTest.from_function(startup_page).run()
        assert not at.exception and not at.error
        assert worker.call_args.args[0][-1] == "--wait"
        at.run()
        at.run()
        assert not at.exception and not at.error
        assert storage.seed_database.call_count == 2
        worker.assert_called_once()
