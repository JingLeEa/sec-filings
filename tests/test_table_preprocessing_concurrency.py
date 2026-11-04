"""Concurrent years share cache publication and SEC request pacing safely."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import unittest
from unittest.mock import patch

from sec_disclosure.comparison import compare_html_tables as sec
from sec_disclosure.table_extraction import xbrl_metric_dictionary as taxonomy
from sec_disclosure.storage.artifacts import PostgresWorkspace
from artifact_support import MemoryArtifactStore
from test_query_item8_metrics import package


class TableCacheConcurrencyTests(unittest.TestCase):
    def test_sec_request_spacing_is_shared_across_clients(self):
        now = [100.0]
        starts = []

        def fetch(*args, **kwargs):
            starts.append(now[0])
            return BytesIO(b'filing')

        with patch.object(sec.SecClient, '_last_request', 0), \
                patch.object(sec.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(sec.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)), \
                patch.object(sec, 'urlopen', side_effect=fetch):
            for name in ('first', 'second', 'third'):
                sec.SecClient('Test test@example.com').get(f'https://www.sec.gov/{name}')
        self.assertEqual(starts, [100.0, 100.25, 100.5])

    def test_concurrent_sec_clients_reuse_one_complete_postgres_cache_entry(self):
        store = MemoryArtifactStore()
        start = threading.Barrier(3, timeout=5)
        url = 'https://www.sec.gov/Archives/shared.htm'
        with PostgresWorkspace('table-cache-test', store=store) as workspace:
            def fetch():
                client = sec.SecClient('Test test@example.com', workspace.root / 'sec_cache')
                start.wait()
                return client.get(url)

            with patch.object(sec, 'urlopen', side_effect=lambda *a, **k: BytesIO(b'complete filing')) as download, \
                    ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(fetch) for _ in range(2)]
                start.wait()
                self.assertEqual([f.result(timeout=5) for f in futures], [b'complete filing'] * 2)
            download.assert_called_once()
        self.assertEqual(len([key for key in store.rows if key.endswith('.html')]), 1)

    def test_concurrent_years_build_the_shared_postgres_taxonomy_only_once(self):
        store = MemoryArtifactStore()
        start = threading.Barrier(3, timeout=5)
        data = package(2025)

        def download(*args, **kwargs):
            response = BytesIO(data)
            response.url = 'https://xbrl.fasb.org/us-gaap/2025/us-gaap-2025.zip'
            return response

        with PostgresWorkspace('taxonomy-cache-test', store=store) as workspace:
            def load():
                start.wait()
                return taxonomy.load_dictionary(2025, workspace.root / 'taxonomy_cache')

            with patch.object(taxonomy, 'urlopen', side_effect=download) as fetch, \
                    patch.object(taxonomy, 'build_dictionary', wraps=taxonomy.build_dictionary) as build, \
                    ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(load) for _ in range(2)]
                start.wait()
                results = [future.result(timeout=5) for future in futures]
            fetch.assert_called_once()
            build.assert_called_once()
            self.assertEqual(results[0], results[1])
