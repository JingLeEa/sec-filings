"""Prepare the bundled example reports once per app process and database configuration."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import streamlit as st

from . import config


def backend():
    project_src = str(config.ROOT / "src")
    if project_src not in sys.path:
        sys.path.insert(0, project_src)
    from sec_disclosure.storage import starter_data
    return starter_data


@st.cache_resource(show_spinner="Preparing starter data in PostgreSQL...")
def _prepare(database_key, dataset_key):
    result = backend().seed_database()
    if sum(result["imported"].values()):
        st.cache_data.clear()
    return result


def ensure_starter_data():
    if config.data_source_mode() == "files" or os.environ.get("SEC_UI_BOOTSTRAP_TEST_DATA", "1") == "0":
        return
    storage = backend()
    try:
        manifest = storage.read_manifest()
        # Cache by configuration without storing credentials in visible labels.
        settings = storage.disclosures.connection_arguments()
        database_key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        result = _prepare(database_key, manifest["archive_sha256"])
    except (storage.StarterDataError, OSError, ValueError) as error:
        st.error(str(error) if isinstance(error, storage.StarterDataError) else
                 "The bundled starter dataset is missing or invalid. Restore starter_data from this branch and reload.")
        st.stop()
    if sum(result["imported"].values()) and not st.session_state.get("_starter_data_ready"):
        st.session_state["_starter_data_ready"] = True
        st.toast(f"Starter data ready in PostgreSQL: {len(result['companies'])} companies.")
