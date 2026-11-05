"""Recovery archives derived state, preserving source data and paid API history."""
import sqlite3
import pytest

from artifact_support import MemoryArtifactStore
from sec_disclosure.pipelines.run_full_pipeline import parse_args, build_stages
from sec_disclosure.pipelines.stage_recovery import archive_progress, restart_stage
from sec_disclosure.storage.artifacts import PostgresWorkspace, checkpoint_saver


def test_archiving_graph_closes_cached_sqlite_and_reopens_fresh_progress():
    store = MemoryArtifactStore()
    with PostgresWorkspace('recovery', store=store) as workspace:
        folder = workspace.root / 'alignments/amd/2023-2024'
        database = folder / 'graph/checkpoints.sqlite'
        with checkpoint_saver(database) as old:
            with old.cursor() as cursor:
                cursor.execute('CREATE TABLE old_state (id INTEGER)')
                cursor.execute('INSERT INTO old_state VALUES (1)')
        request = folder / 'requests/paid.json'
        request.parent.mkdir(parents=True)
        request.write_text('{"status":"completed","usage":125}')
        archive = archive_progress(folder, ('graph',))
        assert (archive / 'graph/checkpoints.sqlite').is_file()
        assert request.read_text() == '{"status":"completed","usage":125}'
        with pytest.raises(sqlite3.ProgrammingError):
            old.conn.execute('SELECT * FROM old_state')
        with checkpoint_saver(database) as fresh:
            assert fresh is not old
            assert fresh.conn.execute("SELECT name FROM sqlite_master WHERE name='old_state'").fetchone() is None


@pytest.mark.parametrize('virtual', [False, True])
def test_materiality_restart_preserves_alignment_input_and_request_budgets(tmp_path, virtual):
    store = MemoryArtifactStore()
    with PostgresWorkspace('materiality_recovery', store=store) as workspace:
        root = workspace.root if virtual else tmp_path
        args = parse_args(['--ticker', 'AMD', '--years', '2023', '2024'])
        args.data_dir = root
        stage = next(s for s in build_stages(args) if s.name == 'materiality_2023_2024')
        folder = root / 'alignments/amd/2023-2024'
        preserved = ('alignments.json', 'materiality/requests/paid.json',
                     'materiality/token_usage.json', 'materiality/run_state.json')
        for name in (*preserved, 'materiality/graph/job.sqlite', 'materiality/jobs/job.json'):
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        archives = restart_stage(stage, args)
        assert len(archives) == 1
        for name in preserved:
            assert (folder / name).read_bytes() == name.encode()
        assert not (folder / 'materiality/graph').exists()
        assert not (folder / 'materiality/jobs').exists()


def test_extraction_restart_keeps_all_requests_and_other_company_years(tmp_path):
    args = parse_args(['--ticker', 'AMD', '--years', '2023', '2024'])
    args.data_dir = tmp_path
    stage = next(s for s in build_stages(args) if s.phase == 'extraction')
    for company, year in (('amd', 2023), ('amd', 2024), ('amd', 2022), ('mu', 2023)):
        folder = tmp_path / 'disclosures' / company / str(year)
        (folder / 'requests').mkdir(parents=True)
        (folder / 'requests/paid.json').write_bytes(b'preserve usage')
        (folder / 'disclosures.json').write_bytes(b'old progress')
    assert len(restart_stage(stage, args)) == 2
    for company, year in (('amd', 2023), ('amd', 2024), ('amd', 2022), ('mu', 2023)):
        folder = tmp_path / 'disclosures' / company / str(year)
        assert (folder / 'requests/paid.json').read_bytes() == b'preserve usage'
        assert (folder / 'disclosures.json').exists() == (company != 'amd' or year == 2022)


def test_change_analysis_restart_preserves_verified_alignment_audit(tmp_path):
    args = parse_args(['--ticker', 'AMD', '--years', '2023', '2024'])
    args.data_dir = tmp_path
    stage = next(s for s in build_stages(args) if s.name == 'llm_changes_2023_2024')
    folder = tmp_path / 'alignments/amd/2023-2024'
    preserved = ('alignments.json', 'jobs/verification_001.json',
                 'validation_errors/verification_001.json', 'traces/verification_001.json',
                 'requests/paid.json', 'token_usage.json')
    for name in (*preserved, 'graph/checkpoints.sqlite', 'change_analysis_cache/label.json'):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    assert len(restart_stage(stage, args)) == 1
    for name in preserved:
        assert (folder / name).read_bytes() == name.encode()
    assert not (folder / 'change_analysis_cache').exists()
