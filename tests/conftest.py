"""Keep ordinary tests away from live model calls and automatic starter imports."""
import pytest


@pytest.fixture(autouse=True)
def disable_automatic_starter_import(monkeypatch):
    monkeypatch.setenv("SEC_UI_BOOTSTRAP_TEST_DATA", "0")
    monkeypatch.setenv("SEC_TABLE_AGENT_FALLBACK", "0")
