"""Keep ordinary UI tests from importing starter reports into a developer database."""
import pytest


@pytest.fixture(autouse=True)
def disable_automatic_starter_import(monkeypatch):
    monkeypatch.setenv("SEC_UI_BOOTSTRAP_TEST_DATA", "0")
