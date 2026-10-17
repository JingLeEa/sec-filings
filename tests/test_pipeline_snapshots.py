import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from sec_disclosure.storage import pipeline_snapshots as snapshots


class PipelineSnapshotTests(unittest.TestCase):
    def test_reader_uses_read_only_consistent_transaction_and_returns_database_payload(self):
        documents = {f'2024_{suffix}.json': {'payload': [], 'sha256': 'a' * 64, 'source_path': '/old/' + suffix}
                     for suffix in ('chunks', 'chunk_sentences')}
        revision = hashlib.sha256(json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        conn, cur = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = {'documents': documents, 'revision': revision}
        with patch('psycopg.connect', return_value=conn), \
             patch('sec_disclosure.storage.disclosures.connection_arguments', return_value={}):
            result = snapshots.read_snapshots('amd', [('raw', '2024', '2024')])
        self.assertTrue(conn.read_only)
        self.assertEqual(result[('raw', '2024', '2024')]['documents'], documents)
        self.assertTrue(all('SELECT' in call.args[0] for call in cur.execute.call_args_list))

    def test_missing_scope_fails_without_reading_local_files(self):
        conn, cur = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = None
        with patch('psycopg.connect', return_value=conn), \
             patch('sec_disclosure.storage.disclosures.connection_arguments', return_value={}), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('No fallback allowed')):
            with self.assertRaisesRegex(snapshots.PipelineInputError, 'Missing PostgreSQL'):
                snapshots.read_snapshots('amd', [('raw', '2024', '2024')])

    def test_connection_error_does_not_expose_credentials(self):
        with patch('psycopg.connect', side_effect=RuntimeError('password=secret')), \
             patch('sec_disclosure.storage.disclosures.connection_arguments', return_value={}):
            with self.assertRaises(snapshots.PipelineInputError) as error:
                snapshots.read_snapshots('amd', [('raw', '2024', '2024')])
        self.assertNotIn('password=secret', str(error.exception))
        self.assertIn('not used as a fallback', str(error.exception))

    def test_database_artifact_paths_cannot_escape_the_output_folder(self):
        with TemporaryDirectory() as directory:
            for name in ('../escape.json', '/tmp/escape.json'):
                with self.subTest(name=name), self.assertRaises(snapshots.PipelineInputError):
                    snapshots.alignment_output({'documents': {name: {'payload': {}}}}, Path(directory))


if __name__ == '__main__':
    unittest.main()
