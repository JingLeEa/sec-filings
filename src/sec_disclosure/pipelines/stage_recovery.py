"""Rebuild stage progress without discarding upstream results or API ledgers."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sec_disclosure.storage.artifacts import ArtifactPath as Path


def archive_progress(folder, names):
    paths = [folder / name for name in names if (folder / name).exists()]
    if not paths:
        return None
    archive = folder / 'archives' / ('stage_restart_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
                                    + '_' + uuid4().hex[:8])
    archive.mkdir(parents=True, exist_ok=True)
    # PostgreSQL SQLite savers live in memory too. A moved graph must not keep
    # using its old connection on the next attempt. Workers have already drained.
    store = getattr(folder, 'artifact_store', None)
    if store is not None:
        with store.saver_lock:
            for key in list(store.savers):
                if any(key == path.artifact_key or key.startswith(path.artifact_key + '/') for path in paths):
                    store.savers.pop(key).conn.close()
    for path in paths:
        path.rename(archive / path.name)
    return archive


def restart_stage(stage, args):
    """Archive derived progress only; request history and admission budgets stay."""
    def option(name):
        return Path(stage.arguments[stage.arguments.index(name) + 1])

    folders = []
    if stage.script == 'align_disclosures.py':
        if stage.phase == 'alignment':
            names = ('graph', 'traces', 'validation_errors', 'jobs')
        else:
            # Alignment audit records are inputs to this stage. Only rebuild
            # classification progress; keep verified alignment jobs and traces.
            names = ('graph', 'change_analysis_cache')
        folders.append((option('--output-dir'), names))
    elif stage.script == 'classify_materiality.py':
        folders.append((option('--alignment-dir') / 'materiality',
                        ('graph', 'jobs', 'traces', 'verifications.json')))
    elif stage.script == 'extract_disclosures.py':
        for year in args.years:
            folder = option('--output-root') / args.ticker / str(year)
            # Extraction reconstructs its result views from its request ledger.
            names = ('disclosures.json', 'review_candidates.json', 'excluded_sources.json',
                     'unassigned_sources.json', 'invalid_proposals.json', 'boundary_checks.json',
                     'consolidation.json', 'disclosure_relationships.json', 'disclosures.md')
            folders.append((folder, names))
    return [str(archive) for folder, names in folders
            if (archive := archive_progress(folder, names)) is not None]
