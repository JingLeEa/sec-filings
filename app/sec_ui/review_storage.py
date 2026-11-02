"""Keep UI review storage registered across Streamlit reruns."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import streamlit as st

from . import config


@st.cache_resource(show_spinner=False)
def _workspace(database_key, legacy_root, env_file):
    from sec_disclosure.storage import human_reviews
    identity = hashlib.sha256((database_key + legacy_root).encode()).hexdigest()[:24]
    workspace = human_reviews.workspace(env_file, prefix="/__sec_postgres__/human-reviews-" + identity)
    workspace.__enter__()
    try:
        human_reviews.migrate_local(legacy_root, workspace.root / "manual_reviews")
    except BaseException:
        workspace.__exit__()
        raise
    return workspace


def root():
    src = str(config.ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from sec_disclosure.storage.artifacts import ArtifactStorageError
    from sec_disclosure.storage.disclosures import connection_arguments
    from .data import DataError
    env_file = os.environ.get("SEC_UI_PIPELINE_ENV_FILE")
    try:
        key = hashlib.sha256(json.dumps(connection_arguments(env_file), sort_keys=True).encode()).hexdigest()
        return _workspace(key, str(config.legacy_manual_reviews_dir().resolve()), env_file).root / "manual_reviews"
    except (ArtifactStorageError, OSError, ValueError):
        raise DataError("Could not load human reviews in PostgreSQL. Check the database and legacy review files; "
                        "local storage is not used as a fallback.") from None
