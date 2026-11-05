"""Concurrent checkpoint writers must not consume each other's temporary file."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from artifact_support import MemoryArtifactStore
from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.storage.artifacts import PostgresWorkspace


class JsonCheckpointWriteTests(unittest.TestCase):
    def overlapping_writes(self, target):
        ready = Barrier(2, timeout=5)
        path_class = type(target)
        original_write = path_class.write_text

        def write_then_wait(path, *args, **kwargs):
            result = original_write(path, *args, **kwargs)
            if path.suffix == '.tmp':
                ready.wait()  # Both writes finish before either rename starts.
            return result

        with patch.object(path_class, 'write_text', write_then_wait), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write_json, target, {'writer': index, 'payload': 'x' * 1000})
                       for index in range(2)]
            for future in futures:
                future.result(timeout=10)
        self.assertIn(json.loads(target.read_text())['writer'], (0, 1))
        self.assertEqual(list(target.parent.glob('*.tmp')), [])

    def test_overlapping_local_checkpoint_writes_both_succeed(self):
        with TemporaryDirectory() as temporary:
            self.overlapping_writes(Path(temporary) / 'token_usage.json')

    def test_long_request_names_do_not_make_temporary_paths_exceed_windows_limits(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / ('a' * (170 - len(str(root)) - 1))
            target = folder / ('b' * 64 + '_attempt_001.json')
            self.assertLess(len(str(target)), 260)
            write_json(target, {'complete': True})
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'complete': True})
            self.assertEqual(list(folder.glob('*.tmp')), [])

    def test_overlapping_postgres_checkpoint_writes_both_succeed(self):
        store = MemoryArtifactStore()
        with PostgresWorkspace('checkpoint-test', store=store) as workspace:
            self.overlapping_writes(workspace.root / 'token_usage.json')
            self.assertFalse(Path(str(workspace.root)).exists())

    def test_failed_publish_preserves_existing_postgres_checkpoint_and_cleans_temporary_file(self):
        store = MemoryArtifactStore()
        with PostgresWorkspace('checkpoint-test', store=store) as workspace:
            target = workspace.root / 'token_usage.json'
            write_json(target, {'completed': 509})
            with patch.object(type(target), 'replace', side_effect=OSError('Interrupted before publish')):
                with self.assertRaisesRegex(OSError, 'Interrupted before publish'):
                    write_json(target, {'completed': 510})
            self.assertEqual(json.loads(target.read_text()), {'completed': 509})
            self.assertEqual(list(target.parent.glob('*.tmp')), [])
