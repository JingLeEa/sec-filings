"""Stored XBRL reconstruction, value-grained CSV and safe snapshot selection."""
from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec_disclosure.table_extraction import export_xbrl_from_postgres as exporter
from test_merge_api_metrics import sample


def stored_fixture():
    metadata = {key: value for key, value in sample(2025).items() if key != 'metrics'}
    metadata['custom_evidence'] = Decimal('9007199254740993.123456789')
    metric = {
        'concept': 'us-gaap:Assets', 'query_name': 'Assets', 'label': 'Assets',
        'definition': 'Resources controlled by the company', 'period_type': 'instant',
        'scope': 'company_wide', 'company_wide_annual_or_year_end_years': [2023],
        'units': ['USD'], 'api_groups': ['BalanceSheets'], 'items': ['8'],
        'extra': {'custom_metric': 'keep'},
    }
    value = {
        'concept': 'us-gaap:Assets', 'value_index': 0,
        'value_text': '9007199254740993.123456789', 'unit': 'USD', 'status': 'reported',
        'instant_date': date(2023, 12, 30), 'period_start_date': None, 'period_end_date': None,
        'dimensions': [], 'decimals': '-6', 'precision': None, 'items': ['8'],
        'has_decimals': True, 'has_precision': True,
        'extra': {'custom_value': {'number': Decimal('0.123456789123456789')}},
    }
    nil = dict(value, value_index=1, value_text=None, status='nil',
               has_decimals=False, has_precision=False, extra={})
    zero = dict(value, value_index=2, value_text='0', decimals=0, extra={})
    source = {
        'concept': 'us-gaap:Assets', 'value_index': 0, 'document_id': 'primary',
        'source': 'https://www.sec.gov/example.htm', 'page': 'F-1',
        'source_fact_id': 'f1', 'source_locator': '/html/body/table[1]/tr[1]/td[2]',
        'row_label': 'Assets, total', 'column_label': '2023', 'label_method': 'table',
        'label_status': 'resolved', 'extra': {'custom_source': 'keep'},
    }
    sources = [source, dict(source, document_id='report1', label_status='partial')]
    snapshot = {'company': 'intel', 'source_filing_year': 2025, 'form_type': '10-K',
                'extraction_id': 3, 'taxonomy_year': 2025,
                'requested_items': metadata['requested_items'], 'loaded_at': '2026-10-13',
                'file_path': 'original.json'}
    return snapshot, metadata, [metric], [value, nil, zero], sources


class ExportXbrlTests(unittest.TestCase):
    def test_restore_preserves_exact_amounts_nil_zero_accuracy_and_source_occurrences(self):
        snapshot, metadata, metrics, values, sources = stored_fixture()
        document = exporter.restore_document(metadata, metrics, values, sources)
        exporter.database.validate_export(document, snapshot['source_filing_year'])
        metric = document['metrics'][0]
        precise, nil, zero = metric['value']
        self.assertEqual([v['value'] for v in metric['value']],
                         ['9007199254740993.123456789', None, '0'])
        self.assertEqual(precise['period'], {'instant': '2023-12-30'})
        self.assertIsNone(precise['precision'])
        self.assertIn('precision', precise)
        self.assertNotIn('precision', nil)
        self.assertNotIn('decimals', nil)
        self.assertEqual(zero['decimals'], 0)
        self.assertEqual(metric['custom_metric'], 'keep')
        self.assertEqual(len(precise['source_labels']), 2)
        self.assertEqual([s['document_id'] for s in precise['source_labels']], ['primary', 'report1'])
        self.assertEqual(precise['source_labels'][0]['custom_source'], 'keep')

    def test_duration_and_dimensional_contexts_are_preserved(self):
        _, metadata, metrics, values, _ = stored_fixture()
        values = [dict(values[0], instant_date=None, period_start_date=date(2022, 12, 31),
                       period_end_date=date(2023, 12, 30), dimensions=[{'axis': 'Axis', 'member': 'Member'}])]
        document = exporter.restore_document(metadata, metrics, values, [])
        value = document['metrics'][0]['value'][0]
        self.assertEqual(value['period'], {'startDate': '2022-12-31', 'endDate': '2023-12-30'})
        self.assertEqual(value['dimensions'], [{'axis': 'Axis', 'member': 'Member'}])
        self.assertEqual(value['source_labels'], [])

    def test_json_and_csv_outputs_keep_precision_and_do_not_multiply_values(self):
        snapshot, metadata, metrics, values, sources = stored_fixture()
        document = exporter.restore_document(metadata, metrics, values, sources)
        with TemporaryDirectory() as tmp:
            json_path, csv_path = Path(tmp) / 'nested' / 'export.json', Path(tmp) / 'export.csv'
            exporter.write_export(json_path, 'json', snapshot, document)
            restored = json.loads(json_path.read_text(), parse_float=Decimal)
            self.assertEqual(restored, document)
            exporter.write_export(csv_path, 'csv', snapshot, document)
            with csv_path.open(newline='') as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]['value'], '9007199254740993.123456789')
            self.assertEqual(rows[1]['status'], 'nil')
            self.assertEqual(rows[2]['value'], '0')
            self.assertEqual(rows[0]['source_filing_year'], '2025')
            self.assertEqual(rows[0]['instant_date'], '2023-12-30')
            self.assertEqual(rows[0]['decimals'], '"-6"')
            self.assertEqual(rows[0]['precision'], 'null')
            self.assertEqual(rows[1]['precision'], '')
            self.assertEqual(len(json.loads(rows[0]['source_labels'])), 2)
            self.assertEqual(json.loads(rows[0]['source_labels'])[0]['row_label'], 'Assets, total')
            original = json_path.read_bytes()
            with self.assertRaises(FileExistsError):
                exporter.write_export(json_path, 'json', snapshot, document)
            self.assertEqual(json_path.read_bytes(), original)

    def test_failed_serialization_removes_incomplete_output(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / 'export.json'
            with self.assertRaises(TypeError):
                exporter.write_export(target, 'json', {}, {'invalid': object()})
            self.assertFalse(target.exists())

    def test_ambiguous_and_missing_snapshots_require_explicit_selection(self):
        snapshot, *_ = stored_fixture()
        self.assertEqual(exporter.select_snapshot([snapshot]), snapshot)
        with self.assertRaisesRegex(ValueError, 'No matching'):
            exporter.select_snapshot([])
        with self.assertRaisesRegex(ValueError, 'IDs: 3, 4.*--extraction-id'):
            exporter.select_snapshot([snapshot, dict(snapshot, extraction_id=4)])

    def test_cli_selects_requested_snapshot_and_lists_without_writing(self):
        snapshot, metadata, metrics, values, sources = stored_fixture()
        document = exporter.restore_document(metadata, metrics, values, sources)
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), \
                patch.object(exporter, 'read_database', return_value=([snapshot], document)) as read:
            target = Path(tmp) / 'export.csv'
            self.assertEqual(exporter.main(['--extraction-id', '3', '--format', 'csv', '--output', str(target)]), 0)
            read.assert_called_once_with(company=None, year=None, extraction_id=3, list_only=False)
            original = target.read_bytes()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(exporter.main(['--extraction-id', '3', '--output', str(target)]), 1)
            self.assertEqual(target.read_bytes(), original)
            with patch.object(exporter, 'write_export', side_effect=AssertionError('Listing must not write')):
                self.assertEqual(exporter.main(['--list', '--company', 'Intel']), 0)


if __name__ == '__main__':
    unittest.main()
