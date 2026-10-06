"""One-command retrieval/export, provenance, retry and offline regressions."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import run_api_metrics_pipeline as runner
from test_export_api_metrics import fixture
from test_extract_item8_xbrl_api import URL
from test_query_item8_metrics import package


def setup(root):
    data, payload = fixture()
    filing, response = root / 'intel.htm', root / 'api.json'
    filing.write_bytes(data)
    response.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                                   'source_sha256': runner.export.membership.sha(data), 'response': payload}))
    taxonomy = root / 'taxonomy'
    taxonomy.mkdir()
    (taxonomy / 'us-gaap-2023.zip').write_bytes(package(2023))
    output = root / 'output'
    common = ['--year', '2023', '--output-dir', str(output), '--taxonomy-cache', str(taxonomy),
              '--sec-cache', str(root / 'sec_cache')]
    return filing, response, output, common, data, payload


def run(args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = runner.main(args)
    return code, stdout.getvalue() + stderr.getvalue()


class OneShotPipelineTests(unittest.TestCase):
    def test_empty_export_is_an_error_and_writes_no_deliverable(self):
        with TemporaryDirectory() as tmp:
            filing, response, output, common, _, _ = setup(Path(tmp))
            saved = json.loads(response.read_text())
            def corrupt_values(value):
                if isinstance(value, dict):
                    if 'value' in value and 'unitRef' in value:
                        value['value'] = '98765432101234567890'
                    for child in value.values():
                        corrupt_values(child)
                elif isinstance(value, list):
                    for child in value:
                        corrupt_values(child)
            corrupt_values(saved['response'])
            response.write_text(json.dumps(saved))
            before = {p: p.read_bytes() for p in (filing, response)}
            code, log = run(['--filing', str(filing), '--xbrl-json', str(response), '--offline', *common])
            self.assertEqual(code, 1, log)
            self.assertIn('No verified numeric US-GAAP metrics', log)
            self.assertIn('Exclusion counts:', log)
            self.assertNotIn('Pipeline complete', log)
            self.assertFalse(list(output.glob('*.json')))
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_filing_selection_ignores_comment_nodes_and_preserves_year_checks(self):
        data = b'''<html><body><!-- publisher metadata --><?publisher layout?>
          <div>Fiscal year ended July 31, 2025</div>
          <!-- <ix:nonNumeric name="dei:DocumentFiscalYearFocus">2024</ix:nonNumeric> -->
          <ix:nonNumeric name="dei:DocumentFiscalYearFocus">2025</ix:nonNumeric>
          </body></html>'''
        self.assertEqual(runner.sec.document_fiscal_year(data), 2025)
        with patch.object(runner.sec, 'SecClient') as client:
            client.return_value.json.return_value = {
                'cik': '0000054187', 'name': 'MAYS J W INC', 'tickers': ['MAYS'],
                'filings': {'recent': {
                    'form': ['10-K'], 'accessionNumber': ['0001206774-25-000720'],
                    'reportDate': ['2025-07-31'], 'filingDate': ['2025-10-23'],
                    'primaryDocument': ['mays4503731-10k.htm']}}}
            client.return_value.get.return_value = data
            _, selected = runner.sec.discover_sec_filings(client.return_value, [2025], cik='54187')
        self.assertEqual(selected[2025]['declaredFiscalYear'], 2025)
        self.assertEqual(selected[2025]['fiscalYearSelection'], 'DocumentFiscalYearFocus')
        self.assertIsNone(runner.sec.document_fiscal_year(b'<html><!-- metadata --><div>No DEI year</div></html>'))
        conflicting = data.replace(b'</body>',
            b'<ix:nonNumeric name="dei:DocumentFiscalYearFocus">2024</ix:nonNumeric></body>')
        with self.assertRaisesRegex(ValueError, 'conflicting DocumentFiscalYearFocus'):
            runner.sec.document_fiscal_year(conflicting)

    def test_saved_run_preserves_schema_inputs_and_all_selected_item_evidence(self):
        with TemporaryDirectory() as tmp:
            filing, response, output, common, _, _ = setup(Path(tmp))
            output.mkdir()
            other_year = output / 'intel_2024_api_metrics_with_values.json'
            other_year.write_text('{"preserve": "another year"}\n')
            other_year_bytes = other_year.read_bytes()
            before = {p: p.read_bytes() for p in (filing, response)}
            args = ['--filing', str(filing), '--xbrl-json', str(response), '--offline', *common]
            with patch.object(runner.sec, 'SecClient', side_effect=AssertionError('No SEC access')), \
                 patch.object(runner.api, 'fetch_xbrl_json', side_effect=AssertionError('No API call')):
                code, log = run(args)
            self.assertEqual(code, 0, log)
            result = json.loads((output / 'intel_2023_api_metrics_with_values.json').read_text())
            self.assertEqual(result['schema_version'], 'api-metrics-1.0')
            self.assertEqual(result['requested_items'], ['1', '1A', '7', '8'])
            self.assertEqual(result['counts']['verified_concepts'], 2)
            income = next(m for m in result['metrics'] if m['query_name'] == 'NetIncomeLoss')
            self.assertEqual(income['value'][0]['items'], ['1', '7', '8'])
            self.assertEqual(len(income['value'][0]['source_labels']), 3)
            self.assertEqual(sorted(p.name for p in output.iterdir()),
                             ['intel_2023_api_metrics_with_values.json', other_year.name])
            self.assertEqual(other_year.read_bytes(), other_year_bytes)
            self.assertIsNone(result['membership_report'])
            self.assertEqual(result['verification']['source']['api_file_sha256'], runner.export.membership.sha(before[response]))
            self.assertTrue(Path(result['source']).is_file())
            self.assertNotIn('metric_export_audit.json', ' '.join(result['notes']))
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            saved = {p: p.read_bytes() for p in output.iterdir()}
            code, log = run(args)
            self.assertEqual(code, 1)
            self.assertIn('already exist', log)
            self.assertEqual(saved, {p: p.read_bytes() for p in output.iterdir()})

    def test_raw_response_uses_existing_sidecar_and_item_subset(self):
        with TemporaryDirectory() as tmp:
            filing, response, output, common, data, payload = setup(Path(tmp))
            response.write_text(json.dumps(payload))
            filing.with_name('intel-source.json').write_text(json.dumps({
                'sha256': runner.export.membership.sha(data), 'original_sec_url': URL}))
            code, log = run(['--filing', str(filing), '--xbrl-json', str(response), '--filing-url', URL,
                             '--items', '1a', '--offline', *common])
            self.assertEqual(code, 0, log)
            result = json.loads((output / 'intel_2023_api_metrics_with_values.json').read_text())
            self.assertEqual(result['requested_items'], ['1A'])
            self.assertEqual([m['query_name'] for m in result['metrics']], ['ProfitLoss'])
            self.assertEqual([p.name for p in output.iterdir()], ['intel_2023_api_metrics_with_values.json'])
            self.assertEqual(result['verification']['excluded_api_entries'], 1)

    def test_live_selection_fetches_once_and_keys_are_not_saved(self):
        with TemporaryDirectory() as tmp:
            _, _, output, common, data, payload = setup(Path(tmp))
            selected = {'url': URL, 'accessionNumber': '0000050863-24-000010', 'reportDate': '2023-12-30'}
            with patch.dict(os.environ, {'SEC_API_KEY': 'test-secret-never-persist', 'SEC_USER_AGENT': 'Test test@example.com'}), \
                 patch.object(runner.sec, 'SecClient') as client, \
                 patch.object(runner.sec, 'discover_sec_filings', return_value=({'name': 'Intel'}, {2023: selected})) as discover, \
                 patch.object(runner.api, 'fetch_xbrl_json', return_value=payload) as fetch:
                client.return_value.get.return_value = data
                code, log = run(['--ticker', 'INTC', '--company', 'Intel', *common])
                self.assertEqual(code, 0, log)
                fetch.assert_called_once_with({'htm-url': URL}, 'test-secret-never-persist')
                discover.assert_called_once_with(client.return_value, [2023], ticker='INTC', cik='', accessions=None)
                # An existing final output is rejected before any new download.
                client.reset_mock()
                code, second_log = run(['--ticker', 'INTC', '--company', 'Intel', *common])
                self.assertEqual(code, 1)
                client.assert_not_called()
            folder = Path(tmp) / 'sec_cache/api_metrics' / runner.api.digest({'htm-url': URL})
            self.assertEqual((folder / 'original_filing.htm').read_bytes(), data)
            saved = json.loads((folder / 'sec_api_xbrl.json').read_text())
            self.assertEqual(saved['response'], payload)
            self.assertEqual(saved['source_sha256'], runner.export.membership.sha(data))
            self.assertEqual([p.name for p in output.iterdir()], ['intel_2023_api_metrics_with_values.json'])
            result = json.loads((output / 'intel_2023_api_metrics_with_values.json').read_text())
            self.assertEqual(Path(result['source']), folder / 'sec_api_xbrl.json')
            self.assertNotIn('test-secret-never-persist', log + second_log)
            for path in Path(tmp).rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'test-secret-never-persist', path.read_bytes())

    def test_failed_export_retains_inputs_and_retry_does_not_call_api(self):
        with TemporaryDirectory() as tmp:
            _, _, output, common, data, payload = setup(Path(tmp))
            selected = {'url': URL, 'accessionNumber': '0000050863-24-000010', 'reportDate': '2023-12-30'}
            with patch.dict(os.environ, {'SEC_API_KEY': 'test-key', 'SEC_USER_AGENT': 'Test test@example.com'}), \
                 patch.object(runner.sec, 'SecClient') as client, \
                 patch.object(runner.sec, 'discover_sec_filings', return_value=({}, {2023: selected})), \
                 patch.object(runner.api, 'fetch_xbrl_json', return_value=payload) as fetch:
                client.return_value.get.return_value = data
                with patch.object(runner.export.taxonomy, 'load_dictionary', side_effect=ValueError('Missing taxonomy')):
                    code, log = run(['--ticker', 'INTC', *common])
                self.assertEqual(code, 1, log)
                self.assertFalse(output.exists())
                folder = Path(tmp) / 'sec_cache/api_metrics' / runner.api.digest({'htm-url': URL})
                self.assertTrue((folder / 'sec_api_xbrl.json').exists())
                with patch.dict(os.environ, {'SEC_API_KEY': ''}):
                    code, log = run(['--ticker', 'INTC', *common])
                self.assertEqual(code, 0, log)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual([p.name for p in output.iterdir()], ['intc_2023_api_metrics_with_values.json'])

    def test_wrong_year_hash_and_unbound_raw_json_do_not_produce_metrics(self):
        for failure in ('year', 'hash', 'binding'):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp:
                filing, response, output, common, _, payload = setup(Path(tmp))
                extra = []
                if failure == 'year':
                    common[1] = '2024'
                elif failure == 'hash':
                    filing.write_bytes(filing.read_bytes() + b' ')
                else:
                    response.write_text(json.dumps(payload))
                    extra = ['--filing-url', URL]
                code, log = run(['--filing', str(filing), '--xbrl-json', str(response), '--offline', *common, *extra])
                self.assertEqual(code, 1, log)
                self.assertFalse(output.exists())

    def test_offline_and_input_requirements_fail_before_network(self):
        cases = [(['--ticker', 'INTC', '--offline'], '--offline requires'),
                 (['--filing', 'intel.htm'], 'together'),
                 (['--ticker', 'INTC', '--xbrl-json', 'api.json'], 'together'),
                 (['--ticker', 'INTC', '--filing-url', URL], 'only for saved')]
        with patch.object(runner.sec, 'SecClient', side_effect=AssertionError('No SEC access')):
            for options, message in cases:
                with self.subTest(options=options):
                    code, log = run([*options, '--year', '2023'])
                    self.assertEqual(code, 1)
                    self.assertIn(message, log)

    @unittest.skipUnless(os.environ.get('SEC_API_METRICS_INTEGRATION') == '1', 'Requires local Intel 2023 caches')
    def test_intel_2023_matches_existing_export_exactly_at_metric_level(self):
        filing = Path('data/source_cache/intc-20231230.htm')
        response = Path('data/table_output/intel_2023_item_8_api_check/sec_api_xbrl.json')
        baseline = Path('data/table_output/intel_2023_items_1_1a_7_8_api_metrics/metrics_with_values.json')
        before = {p: runner.export.membership.sha(p.read_bytes()) for p in (filing, response, baseline)}
        with TemporaryDirectory() as tmp:
            code, log = run(['--filing', str(filing), '--xbrl-json', str(response), '--year', '2023',
                             '--company', 'Intel', '--output-dir', tmp, '--offline'])
            self.assertEqual(code, 0, log)
            old = json.loads(baseline.read_text())
            new = json.loads((Path(tmp) / 'intel_2023_api_metrics_with_values.json').read_text())
            self.assertEqual(new['metrics'], old['metrics'])
            self.assertEqual(new['counts'], old['counts'])
            self.assertEqual(len(new['metrics']), 357)
            self.assertEqual(sum(len(m['value']) for m in new['metrics']), 1443)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['intel_2023_api_metrics_with_values.json'])
            self.assertIsNone(new['membership_report'])
            self.assertEqual(new['verification']['unique_values'], 1443)
        self.assertEqual(before, {p: runner.export.membership.sha(p.read_bytes()) for p in before})


if __name__ == '__main__':
    unittest.main()
