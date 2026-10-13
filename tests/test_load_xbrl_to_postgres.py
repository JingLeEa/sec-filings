"""XBRL loader validation, mapping and opt-in transactional PostgreSQL checks."""
from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import uuid

from sec_disclosure.table_extraction import load_xbrl_to_postgres as loader
from sec_disclosure.table_extraction import run_api_metrics_pipeline as runner
from test_merge_api_metrics import sample
from test_run_api_metrics_pipeline import setup
import test_xbrl_schema as schema_tests

ROOT = Path(__file__).resolve().parents[1]


def export_fixture(root):
    filing, response, output, common, _, _ = setup(root)
    with redirect_stdout(io.StringIO()):
        code = runner.main(['--filing', str(filing), '--xbrl-json', str(response), '--company', 'Intel',
                            '--offline', '--no-db', *common])
    if code != 0:
        raise AssertionError('Could not build the offline export fixture')
    return output / 'intel_2023_api_metrics_with_values.json'


class XbrlLoaderTests(unittest.TestCase):
    def test_json_metadata_retains_decimal_numbers_without_rounding_or_string_coercion(self):
        value = Decimal('9007199254740993.123456789')
        serialized = loader.json_text({'evidence': [value]})
        self.assertEqual(json.loads(serialized, parse_float=Decimal), {'evidence': [value]})
        self.assertNotIn('"9007199254740993.123456789"', serialized)

    def test_exact_values_accuracy_nulls_and_unknown_fields_are_mapped(self):
        document = sample(2023, amount='9007199254740993.123456789')
        metric = document['metrics'][0]
        metric['new_metric_property'] = 'preserve'
        value = metric['value'][0]
        value.update(precision=None, new_value_property={'evidence': 'keep'})
        loader.validate_export(document, 2023)
        row = loader.value_row(value, 5, metric['concept'], 0)
        self.assertEqual(row[3], '9007199254740993.123456789')
        self.assertEqual(row[10], '"-6"')
        self.assertEqual(row[11], 'null')
        self.assertEqual(json.loads(row[-1]), {'new_value_property': {'evidence': 'keep'}})
        self.assertEqual(json.loads(loader.metric_row(metric, 5, 0)[-1]), {'new_metric_property': 'preserve'})
        value.update(value=None, status='nil')
        self.assertIsNone(loader.value_row(value, 5, metric['concept'], 0)[3])

    def test_invalid_inputs_are_rejected_before_database_work(self):
        changes = [
            {'value': 'NaN'}, {'value': True}, {'value': '0', 'status': 'nil'},
            {'period': {'endDate': '2023-12-31'}},
            {'period': {'startDate': '2024-01-01', 'endDate': '2023-12-31'}},
            {'items': ['1A']}, {'dimensions': None},
            {'dimensions': [{'axis': 'Axis', 'member': 'A'}, {'axis': 'Axis', 'member': 'B'}]},
            {'source_labels': [{'status': 'made_up'}]},
        ]
        for change in changes:
            with self.subTest(change=change):
                document = sample(2023)
                document['requested_items'] = ['8']
                document['metrics'][0]['value'][0].update(change)
                with self.assertRaises(ValueError):
                    loader.validate_export(document, 2023)
        document = sample(2023)
        document['schema_version'] = 'api-metrics-merged-1.0'
        with self.assertRaises(ValueError):
            loader.validate_export(document, 2023)

    def test_source_hashes_issuer_and_year_are_verified_offline(self):
        with TemporaryDirectory() as tmp:
            path = export_fixture(Path(tmp))
            with patch.object(runner.api, 'fetch_xbrl_json', side_effect=AssertionError('No API calls')):
                data, digest, identity = loader.read_export(path, 2023, expected_cik='50863')
            self.assertEqual(identity['DocumentFiscalYearFocus'], '2023')
            self.assertEqual(len(digest), 64)
            with self.assertRaises(ValueError):
                loader.read_export(path, 2024)
            with self.assertRaises(ValueError):
                loader.read_export(path, 2023, expected_cik='72971')
            source = Path(data['verification']['source']['api_file'])
            source.write_bytes(source.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'hash differs'):
                loader.read_export(path, 2023)

    def test_connection_failure_message_does_not_expose_credentials(self):
        import psycopg
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://sec:test-secret@localhost/missing'}), \
                patch.object(psycopg, 'connect', side_effect=psycopg.OperationalError('connection refused')):
            with self.assertRaises(loader.DatabaseSaveError) as error:
                loader.connect_database()
        self.assertIn('docker compose up -d', str(error.exception))
        self.assertNotIn('test-secret', str(error.exception))


@unittest.skipUnless(os.environ.get('SEC_XBRL_DB_INTEGRATION') == '1', 'Requires Docker PostgreSQL')
class XbrlLoaderDatabaseTests(unittest.TestCase):
    sql = classmethod(schema_tests.XbrlSchemaTests.sql.__func__)

    @classmethod
    def setUpClass(cls):
        cls.database = 'sec_xbrl_loader_test_' + uuid.uuid4().hex
        cls.sql(f'CREATE DATABASE "{cls.database}";', 'postgres')
        cls.addClassCleanup(cls.sql, f'DROP DATABASE "{cls.database}";', 'postgres')
        for filename in ('01_schema.sql', '02_disclosures_alignments.sql'):
            cls.sql((ROOT / 'db/init' / filename).read_text())

    def setUp(self):
        environment = patch.dict(os.environ, {'POSTGRES_DB': self.database, 'DATABASE_URL': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = export_fixture(Path(self.folder.name))
        loader.prepare_database()  # Installs 04 automatically on the existing v2 schema.
        self.sql('TRUNCATE filings RESTART IDENTITY CASCADE;')

    def test_complete_snapshot_matches_export_and_rerun_creates_no_duplicates(self):
        data, _, _ = loader.read_export(self.path, 2023)
        with redirect_stdout(io.StringIO()):
            first = loader.load_export(self.path, 'Intel', 2023)
            second = loader.load_export(self.path, 'Intel', 2023)
        self.assertEqual(first['extraction_id'], second['extraction_id'])
        self.assertTrue(second['already_present'])
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_extractions;'), '1')
        self.assertEqual(first['metrics'], len(data['metrics']))
        self.assertEqual(first['values'], sum(len(m['value']) for m in data['metrics']))
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_value_context;'), str(first['values']))
        self.assertEqual(self.sql("SELECT value FROM schema_info WHERE key='schema_version';"), '2')
        self.assertEqual(self.sql("SELECT value FROM schema_info WHERE key='xbrl_schema_version';"), '1')

    def test_late_database_error_rolls_back_the_whole_snapshot_and_retry_succeeds(self):
        original = loader.source_row

        def invalid_source(*args):
            row = list(original(*args))
            row[-2] = 'invalid_status'
            return tuple(row)

        with patch.object(loader, 'source_row', side_effect=invalid_source), \
                self.assertRaises(loader.DatabaseSaveError):
            loader.load_export(self.path, 'Intel', 2023)
        for table in ('filings', 'xbrl_extractions', 'xbrl_metrics', 'xbrl_values', 'xbrl_value_sources'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table};'), '0')
        with redirect_stdout(io.StringIO()):
            result = loader.load_export(self.path, 'Intel', 2023)
        self.assertFalse(result['already_present'])

    def test_changed_export_keeps_original_snapshot_and_precise_amount(self):
        with redirect_stdout(io.StringIO()):
            first = loader.load_export(self.path, 'Intel', 2023)
        data = json.loads(self.path.read_text())
        data['metrics'][0]['value'][0]['value'] = '9007199254740993.123456789'
        data['metrics'][0]['value'][0]['precision'] = None
        self.path.write_text(json.dumps(data))
        with redirect_stdout(io.StringIO()):
            second = loader.load_export(self.path, 'Intel', 2023)
        self.assertNotEqual(first['extraction_id'], second['extraction_id'])
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_extractions;'), '2')
        self.assertEqual(self.sql(f'SELECT amount FROM xbrl_values WHERE extraction_id={second["extraction_id"]} '
                                  'AND value_index=0 ORDER BY concept LIMIT 1;'), '9007199254740993.123456789')


if __name__ == '__main__':
    unittest.main()
