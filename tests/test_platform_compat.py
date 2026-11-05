"""Exercise macOS/POSIX and Windows boundaries using real files and processes."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from artifact_support import MemoryArtifactStore
from sec_disclosure.compat import atomic_replace, exclusive_file_lock, source_digest
from sec_disclosure.pipelines.pipeline_progress import invocation_lock
from sec_disclosure.storage.artifacts import ArtifactPath, ArtifactStorageError, PostgresWorkspace


def test_source_fingerprints_ignore_only_checkout_newlines(tmp_path):
    path = tmp_path / 'source.py'
    source = b'def example():\n    return 1\n'
    path.write_bytes(source)
    expected = source_digest(path)
    path.write_bytes(source.replace(b'\n', b'\r\n'))
    assert source_digest(path) == expected == hashlib.sha256(source).hexdigest()
    path.write_bytes(source.replace(b'return 1', b'return 2'))
    assert source_digest(path) != expected


def test_empty_read_only_legacy_lock_excludes_another_handle_and_releases(tmp_path):
    path = tmp_path / '.run.lock'
    path.write_bytes(b'')
    with path.open('rb') as first, path.open('rb') as second:
        with exclusive_file_lock(first):
            with pytest.raises(BlockingIOError):
                with exclusive_file_lock(second):
                    pytest.fail('An overlapping writer acquired the legacy lock')
        with exclusive_file_lock(second):
            pass
    assert path.read_bytes() == b''


def test_local_invocations_exclude_other_writers_and_preserve_stage_errors(tmp_path):
    with invocation_lock(tmp_path):
        with pytest.raises(ValueError, match='already running'):
            with invocation_lock(tmp_path):
                pytest.fail('A second runner acquired the checkpoint lock')
    with pytest.raises(BlockingIOError, match='stage failure'):
        with invocation_lock(tmp_path):
            raise BlockingIOError('stage failure')
    with invocation_lock(tmp_path):
        pass


def test_virtual_paths_have_identical_keys_and_serialization_on_every_platform():
    store = MemoryArtifactStore()
    with PostgresWorkspace('portable', store=store) as workspace:
        path = workspace.root / 'disclosures/amd/2024/report.json'
        assert str(path) == '/__sec_postgres__/portable/disclosures/amd/2024/report.json'
        assert path.artifact_key == 'disclosures/amd/2024/report.json'
        assert ArtifactPath(str(path)).artifact_key == path.artifact_key
        if os.name == 'nt':
            assert ArtifactPath(str(path).replace('/', '\\')).artifact_key == path.artifact_key
        path.parent.mkdir(parents=True)
        with path.open('w', encoding='utf-8') as stream:
            stream.write('report\n')
        assert path.read_bytes() == b'report\n'
        assert not Path(str(path)).exists()


def test_migration_refuses_an_active_legacy_writer(tmp_path):
    path = tmp_path / '.run.lock'
    path.write_bytes(b'')
    (tmp_path / 'report.json').write_text('{}', encoding='utf-8')
    store = MemoryArtifactStore()
    with path.open('rb') as handle, exclusive_file_lock(handle):
        with PostgresWorkspace('portable', store=store) as workspace:
            with pytest.raises(ArtifactStorageError, match='older local pipeline'):
                workspace.import_local(tmp_path)
    with PostgresWorkspace('portable', store=store) as workspace:
        assert workspace.import_local(tmp_path)
        assert (workspace.root / 'report.json').read_text() == '{}'


@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing violations')
def test_atomic_publish_retries_a_transient_sharing_violation(tmp_path):
    source, target = tmp_path / 'report.tmp', tmp_path / 'report.json'
    source.write_text('completed', encoding='utf-8')
    original = type(source).replace
    blocked = PermissionError('file is temporarily open')
    blocked.winerror = 32
    attempts = []

    def replace(path, destination):
        attempts.append(path)
        if len(attempts) == 1:
            raise blocked
        return original(path, destination)

    with patch.object(type(source), 'replace', replace):
        atomic_replace(source, target)
    assert target.read_text() == 'completed'
    assert len(attempts) == 2


@pytest.mark.parametrize('script', ['run_full_pipeline.py', 'run_peer_comparison_pipeline.py', 'run_synthesis.py'])
def test_pipeline_entry_points_import_on_native_python(script):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'scripts' / script), '--help'],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
