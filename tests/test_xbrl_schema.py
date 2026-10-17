"""Opt-in SQL integration checks in a disposable Docker PostgreSQL database."""
import os
from pathlib import Path
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'db/init/04_xbrl_data.sql'


@unittest.skipUnless(os.environ.get('SEC_XBRL_DB_INTEGRATION') == '1',
                     'set SEC_XBRL_DB_INTEGRATION=1 to test with Docker')
class XbrlSchemaTests(unittest.TestCase):
    @classmethod
    def sql(cls, statement, database=None):
        command = ['docker', 'compose', 'exec', '-T', 'db', 'sh', '-c',
                   'exec psql -X -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 -A -t',
                   'sh', database or cls.database]
        result = subprocess.run(command, cwd=ROOT, input=statement, text=True,
                                capture_output=True, check=True, timeout=30)
        return result.stdout.strip()

    @classmethod
    def setUpClass(cls):
        cls.database = 'sec_xbrl_test_' + uuid.uuid4().hex
        cls.sql(f'CREATE DATABASE "{cls.database}";', 'postgres')
        cls.addClassCleanup(cls.sql, f'DROP DATABASE "{cls.database}";', 'postgres')
        for filename in ('01_schema.sql', '02_disclosures_alignments.sql', '04_xbrl_data.sql'):
            cls.sql((ROOT / 'db/init' / filename).read_text())
        cls.sql("INSERT INTO filings (filing_id, company, fiscal_year) VALUES (1, 'test', 2025);")

    def setUp(self):
        self.sql("""
            TRUNCATE xbrl_extractions RESTART IDENTITY CASCADE;
            INSERT INTO xbrl_extractions
                (filing_id, file_path, file_sha256, export_schema_version,
                 exporter_version, taxonomy_year, requested_items, metadata)
            VALUES (1, 'test_2025_api_metrics_with_values.json', repeat('a', 64),
                    'api-metrics-1.0', '1.11.0', 2025, ARRAY['7', '8'],
                    '{"verification":{"source":{"request":{"htm-url":"verified-filing"}}}}');
            INSERT INTO xbrl_metrics
                (extraction_id, concept, metric_index, query_name, label, period_type, scope, items)
            VALUES (1, 'us-gaap:Assets', 0, 'Assets', 'Assets', 'instant', 'company_wide', ARRAY['8']);
            INSERT INTO xbrl_values
                (extraction_id, concept, value_index, value_text, unit, status, instant_date,
                 decimals, precision, items)
            VALUES
                (1, 'us-gaap:Assets', 0, '9007199254740993.123456789', 'USD', 'reported',
                 '2023-12-30', '"-6"', NULL, ARRAY['8']),
                (1, 'us-gaap:Assets', 1, NULL, 'USD', 'nil',
                 '2023-12-30', NULL, '"INF"', ARRAY['8']),
                (1, 'us-gaap:Assets', 2, '0', 'USD', 'reported',
                 '2023-12-30', '-3', 'null', ARRAY['8']),
                (1, 'us-gaap:Assets', 3, '1E2', 'USD', 'reported',
                 '2023-12-30', NULL, NULL, ARRAY['8']);
            INSERT INTO xbrl_value_sources
                (extraction_id, concept, value_index, source_index, document_id,
                 source_fact_id, page, row_label, column_label, label_status)
            VALUES
                (1, 'us-gaap:Assets', 0, 0, 'primary', 'f-1', '74', 'Assets', '2023', 'resolved'),
                (1, 'us-gaap:Assets', 0, 1, 'report1', 'f-1', 'F-1', NULL, '2023', 'partial');
        """)

    def test_migration_is_repeatable_and_does_not_claim_embedding_migration(self):
        self.sql(MIGRATION.read_text())
        self.assertEqual(self.sql("SELECT value FROM schema_info WHERE key='schema_version';"), '2')
        self.assertEqual(self.sql("SELECT value FROM schema_info WHERE key='xbrl_schema_version';"), '1')
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_values;'), '4')

    def test_exact_amount_nil_zero_and_accuracy_types(self):
        self.assertEqual(self.sql('SELECT amount FROM xbrl_values WHERE value_index=0;'),
                         '9007199254740993.123456789')
        self.assertEqual(self.sql('SELECT amount IS NULL FROM xbrl_values WHERE value_index=1;'), 't')
        self.assertEqual(self.sql('SELECT amount FROM xbrl_values WHERE value_index=2;'), '0')
        self.assertEqual(self.sql('SELECT value_text, amount FROM xbrl_values WHERE value_index=3;'), '1E2|100')
        self.assertEqual(self.sql('SELECT jsonb_typeof(decimals), jsonb_typeof(precision) '
                                  'FROM xbrl_values WHERE value_index=2;'), 'number|null')
        self.assertEqual(self.sql('SELECT precision FROM xbrl_values WHERE value_index=1;'), '"INF"')

    def test_comparative_period_and_multiple_occurrences_do_not_multiply_values(self):
        self.assertEqual(self.sql('SELECT source_filing_year, instant_date FROM xbrl_value_context '
                                  'WHERE value_index=0;'), '2025|2023-12-30')
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_value_context WHERE value_index=0;'), '1')
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_value_sources WHERE value_index=0;'), '2')

    def test_duration_and_dimensions_remain_separate_records(self):
        self.sql("""
            INSERT INTO xbrl_metrics
                (extraction_id, concept, metric_index, query_name, label, period_type, scope, items)
            VALUES (1, 'us-gaap:NetIncomeLoss', 1, 'NetIncomeLoss', 'Net income',
                    'duration', 'dimension_only', ARRAY['7', '8']);
            INSERT INTO xbrl_values
                (extraction_id, concept, value_index, value_text, unit, status,
                 period_start_date, period_end_date, dimensions, items)
            VALUES (1, 'us-gaap:NetIncomeLoss', 0, '43.00', 'USD', 'reported',
                    '2023-01-01', '2023-12-30',
                    '[{"axis":"us-gaap:ProductOrServiceAxis","member":"ex:Segment"}]', ARRAY['7', '8']);
        """)
        self.assertEqual(self.sql("SELECT count(*) FROM xbrl_values WHERE dimensions @> "
                                  "'[{\"member\":\"ex:Segment\"}]'::jsonb;"), '1')
        self.assertEqual(self.sql("SELECT period_start_date, period_end_date FROM xbrl_value_context "
                                  "WHERE concept='us-gaap:NetIncomeLoss';"), '2023-01-01|2023-12-30')

    def test_invalid_facts_are_rejected(self):
        overrides = [
            "value_text=NULL", "status='nil'", "value_text='NaN'", "value_text='Infinity'",
            "value_text='-Infinity'", "instant_date=NULL",
            "period_start_date='2023-01-01'", "unit=''",
            "dimensions='null'", "dimensions='{}'", "dimensions='[{}]'",
            "dimensions='[{\"axis\":\"Axis\",\"member\":\"A\"},{\"axis\":\"Axis\",\"member\":\"B\"}]'",
            "instant_date=NULL, period_start_date='2024-01-01', period_end_date='2023-12-31'",
        ]
        for override in overrides:
            with self.subTest(override=override), self.assertRaises(subprocess.CalledProcessError) as error:
                self.sql(f'UPDATE xbrl_values SET {override} WHERE value_index=0;')
            self.assertIn('check constraint', error.exception.stderr)

    def test_snapshot_identity_and_foreign_keys(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.sql("UPDATE xbrl_values SET concept='us-gaap:Missing' WHERE value_index=0;")
        self.assertIn('foreign key constraint', error.exception.stderr)
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.sql("INSERT INTO xbrl_extractions "
                     "(filing_id, file_path, file_sha256, export_schema_version, exporter_version, taxonomy_year, requested_items) "
                     "SELECT filing_id, 'other_path.json', file_sha256, export_schema_version, exporter_version, taxonomy_year, requested_items "
                     "FROM xbrl_extractions;")
        self.assertIn('unique constraint', error.exception.stderr)
        self.sql("INSERT INTO xbrl_extractions "
                 "(filing_id, file_path, file_sha256, export_schema_version, exporter_version, taxonomy_year, requested_items) "
                 "SELECT filing_id, file_path, repeat('b',64), export_schema_version, exporter_version, taxonomy_year, requested_items "
                 "FROM xbrl_extractions;")
        self.assertEqual(self.sql('SELECT count(*) FROM xbrl_extractions;'), '2')

    def test_delete_extraction_cascades_without_deleting_filing(self):
        self.sql('DELETE FROM xbrl_extractions;')
        for table in ('xbrl_metrics', 'xbrl_values', 'xbrl_value_sources'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table};'), '0')
        self.assertEqual(self.sql('SELECT count(*) FROM filings;'), '1')


if __name__ == '__main__':
    unittest.main()
