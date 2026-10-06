"""HTML entry point exercises the shared SEC and incorporated-report paths."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import extract_html_metrics as cli
import html_filing_source as sources
import extract_10k_tables_xbrl as ix
from test_html_table_metrics import source, table, values
from test_api_styled_report import styled_fixture
from test_api_filing_documents import fact, URL as REPORT_PRIMARY_URL
from test_api_report_discovery import document_list


URL = 'https://www.sec.gov/Archives/edgar/data/123/000000012326000001/example.htm'
ACCESSION = '0000000123-26-000001'


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(args)
    return code, out.getvalue() + err.getvalue()


def primary():
    data = source(table('<tr><td>Revenue</td><td>52</td></tr>'))
    return data.replace(b'</body>', b'<ix:nonNumeric name="dei:DocumentPeriodEndDate" '
                        b'contextRef="annual">2025-12-31</ix:nonNumeric></body>')


def sidecar(path, data, url=URL):
    path.with_name(path.stem + '-source.json').write_text(json.dumps({'sec_url': url, 'sha256': sources.sha(data)}))


def issuer_response():
    return {'cik': 123, 'name': 'Example', 'tickers': ['EX'], 'filings': {'recent': {
        'form': ['10-K/A', '10-K'], 'accessionNumber': ['0000000123-26-000002', ACCESSION],
        'reportDate': ['2025-12-31', '2025-12-31'], 'filingDate': ['2026-03-01', '2026-02-01'],
        'primaryDocument': ['amended.htm', 'example.htm']}}}


class HtmlSourceTests(unittest.TestCase):
    def test_cik_accession_uses_shared_selection_and_caches_replayable_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache, output = root / 'cache', root / 'out'
            args = ['--cik', '123', '--accession', ACCESSION, '--company', 'Example', '--year', '2025',
                    '--sec-cache', str(cache), '--output-dir', str(output)]
            with patch.object(cli.sec, 'SecClient') as client, \
                 patch.object(sources.api, 'fetch_xbrl_json', side_effect=AssertionError('No paid API')):
                client.return_value.json.return_value = issuer_response()
                client.return_value.get.return_value = primary()
                code, log = run(args)
                self.assertEqual(code, 0, log)
                self.assertTrue(all(call.args[0] == URL for call in client.return_value.get.call_args_list))
                client.reset_mock()
                self.assertEqual(run(args)[0], 1)  # Protect an existing export before any network.
                client.assert_not_called()
            target = output / 'example_2025_html_metrics_with_values.json'
            result = json.loads(target.read_text())
            self.assertEqual(result['verification']['identity']['cik'], '0000000123')
            self.assertEqual(result['verification']['source']['sec_selection']['accessionNumber'], ACCESSION)
            cached = Path(result['source'])
            self.assertEqual(cached.read_bytes(), primary())
            self.assertTrue(cached.with_name(cached.stem + '-source.json').is_file())
            with patch.object(cli.sec, 'SecClient', side_effect=AssertionError('Offline network')):
                code, log = run(['--filing', str(cached), '--company', 'Example', '--year', '2025',
                                 '--offline', '--output-dir', str(root / 'replay')])
                self.assertEqual(code, 0, log)
            replay = json.loads((root / 'replay/example_2025_html_metrics_with_values.json').read_text())
            self.assertEqual(replay['metrics'], result['metrics'])

    def test_ticker_and_ambiguous_accession_error_use_the_correct_cli_options(self):
        with TemporaryDirectory() as tmp, patch.object(cli.sec, 'SecClient') as client:
            client.return_value.json.side_effect = [
                {'0': {'ticker': 'EX', 'cik_str': 123}}, issuer_response()]
            client.return_value.get.return_value = primary()
            code, log = run(['--ticker', 'EX', '--year', '2025', '--sec-cache', tmp,
                             '--output-dir', str(Path(tmp) / 'out')])
            self.assertEqual(code, 0, log)
        with TemporaryDirectory() as tmp, patch.object(cli.sec, 'SecClient'), \
             patch.object(cli.sec, 'discover_sec_filings', side_effect=ValueError(
                 'Multiple original 10-Ks. Choose --previous-accession or --current-accession explicitly.')):
            code, log = run(['--cik', '123', '--year', '2025', '--output-dir', tmp])
            self.assertEqual(code, 1)
            self.assertIn('Choose --accession explicitly', log)

    def test_sidecar_hash_and_url_are_checked_even_with_explicit_url(self):
        for change in ('hash', 'url', 'second_sidecar'):
            with self.subTest(change=change), TemporaryDirectory() as tmp:
                root = Path(tmp); path = root / 'filing.htm'; data = primary()
                path.write_bytes(data); sidecar(path, data)
                if change == 'hash': path.write_bytes(data + b' ')
                elif change == 'url': sidecar(path, data, URL.replace('example.htm', 'different.htm'))
                else:
                    path.with_name(path.name + '.source.json').write_text(json.dumps({
                        'sha256': sources.sha(data), 'sec_url': URL.replace('example.htm', 'different.htm')}))
                code, log = run(['--filing', str(path), '--filing-url', URL, '--year', '2025',
                                 '--offline', '--output-dir', str(root / 'out')])
                self.assertEqual(code, 1, log)
                self.assertFalse((root / 'out').exists())

    def test_api_export_only_supplies_verified_source_not_amounts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'filing.htm'; data = primary(); path.write_bytes(data)
            saved = root / 'api_metrics.json'
            export = {'verification': {'source': {'filing': path.name, 'filing_sha256': sources.sha(data),
                       'request': {'htm-url': URL}}}, 'metrics': [{'value': [{'value': '999999'}]}]}
            saved.write_text(json.dumps(export))
            with patch.object(cli.sec, 'SecClient', side_effect=AssertionError('Offline network')), \
                 patch.object(sources.api, 'fetch_xbrl_json', side_effect=AssertionError('No paid API')):
                code, log = run(['--api-metrics', str(saved), '--company', 'Example', '--year', '2025',
                                 '--offline', '--output-dir', str(root / 'out')])
                self.assertEqual(code, 0, log)
            result = json.loads((root / 'out/example_2025_html_metrics_with_values.json').read_text())
            self.assertEqual([v['value'] for v in values(result)], ['52000000'])
            path.write_bytes(data + b' ')
            code, log = run(['--api-metrics', str(saved), '--year', '2025', '--offline', '--output-dir', str(root / 'bad')])
            self.assertEqual(code, 1)
            self.assertIn('hash differs', log)

    def test_malformed_saved_provenance_reports_an_error_without_a_traceback(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'filing.htm'; data = primary(); path.write_bytes(data)
            malformed = [[], {'sha256': sources.sha(data), 'sec_url': 42},
                         {'sha256': sources.sha(data), 'sec_url': URL, 'sec_selection': ['invalid']}]
            for payload in malformed:
                with self.subTest(payload=payload):
                    path.with_name(path.stem + '-source.json').write_text(json.dumps(payload))
                    code, log = run(['--filing', str(path), '--year', '2025', '--offline', '--output-dir', str(root / 'out')])
                    self.assertEqual(code, 1)
                    self.assertIn('Error:', log)
                    self.assertFalse((root / 'out').exists())

    def test_wrong_issuer_form_or_report_date_cannot_export(self):
        data = primary()
        docs = {'primary': {'doc': ix.Document(data, 'test.htm')}}
        with self.assertRaisesRegex(ValueError, 'CIK'):
            sources.verify_identity(docs, 2025, {'htm-url': URL.replace('/123/', '/999/')})
        for wrong in ('10-Q', '10-K/A'):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, 'original 10-K'):
                sources.verify_identity({'primary': {'doc': ix.Document(data.replace(b'>10-K<', f'>{wrong}<'.encode()), 'test.htm')}}, 2025)
        selection = {'form': '10-K', 'url': URL, 'accessionNumber': ACCESSION, 'cik': '123', 'reportDate': '2025-12-30'}
        with self.assertRaisesRegex(ValueError, 'report date'):
            sources.verify_identity(docs, 2025, {'htm-url': URL}, selection)
        report = ix.Document(data.replace(b'>123<', b'>999<'), 'report.htm')
        with self.assertRaisesRegex(ValueError, 'CIK'):
            sources.verify_identity({**docs, 'report1': {'doc': report}}, 2025, {'htm-url': URL})

    def test_wrong_input_flags_and_invalid_url_stop_before_download(self):
        with TemporaryDirectory() as tmp, patch.object(cli.sec, 'SecClient', side_effect=AssertionError('Network')):
            for args in (['--ticker', 'EX', '--offline'], ['--filing', 'not-needed.htm', '--accession', ACCESSION],
                         ['--ticker', 'EX', '--accession', 'invalid'], ['--cik', '123', '--filing-url', URL]):
                with self.subTest(args=args):
                    self.assertEqual(run([*args, '--year', '2025', '--output-dir', tmp])[0], 1)
            p = Path(tmp) / 'primary.htm'; p.write_bytes(primary())
            code, log = run(['--filing', str(p), '--filing-url', 'https://example.com/fake.htm', '--year', '2025', '--output-dir', tmp])
            self.assertEqual(code, 1)
            self.assertIn('original SEC filing', log)

    def test_inputs_changing_during_extraction_do_not_produce_an_output(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); p = root / 'primary.htm'; p.write_bytes(primary())
            original_build = cli.html_metrics.build
            def mutate(*args, **kwargs):
                result = original_build(*args, **kwargs)
                p.write_bytes(p.read_bytes() + b' ')
                return result
            with patch.object(cli.html_metrics, 'build', side_effect=mutate):
                code, log = run(['--filing', str(p), '--year', '2025', '--offline', '--output-dir', str(root / 'out')])
            self.assertEqual(code, 1)
            self.assertIn('changed during extraction', log)
            self.assertFalse((root / 'out').exists())

    def test_missing_exhibit_link_and_statement_boundaries_keep_only_requested_html_items(self):
        data, report, _ = styled_fixture()
        data = data.replace(b'<a href="report.htm">Annual Report</a>', b'Annual Report')
        data = data.replace(b'</body>', b'<ix:nonNumeric name="dei:DocumentType" contextRef="annual">10-K</ix:nonNumeric>'
                           b'<ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="annual">2023</ix:nonNumeric></body>')
        report = report.replace(('<p>Income: ' + fact(1, 'review') + '</p>').encode(),
                                table('<tr><td>Revenue</td><td>52</td></tr>', date='December 30, 2023').encode())
        report = report.replace(fact(2, 'condition').encode(), b'99')
        with TemporaryDirectory() as tmp:
            root = Path(tmp); p = root / 'primary.htm'; p.write_bytes(data); sidecar(p, data, REPORT_PRIMARY_URL)
            r = root / 'report.htm'; r.write_bytes(report); sidecar(r, report, REPORT_PRIMARY_URL.rsplit('/', 1)[0] + '/report.htm')
            cache = root / 'cache'; cache.mkdir()
            (cache / (sources.sha(sources.documents.filing_index_url(REPORT_PRIMARY_URL).encode()) + '.html')).write_bytes(document_list())
            with patch.object(cli.sec, 'SecClient', side_effect=AssertionError('Offline network')):
                code, log = run(['--filing', str(p), '--company', 'Example', '--year', '2023', '--offline',
                                 '--sec-cache', str(cache), '--output-dir', str(root / 'out')])
                self.assertEqual(code, 0, log)
            result = json.loads((root / 'out/example_2023_html_metrics_with_values.json').read_text())
            self.assertEqual([v['value'] for v in values(result)], ['52000000'])
            self.assertEqual(values(result)[0]['items'], ['7'])
            evidence = result['verification']['documents']['report1']
            self.assertEqual(evidence['report_discovery']['method'], 'sec_filing_document_list')
            self.assertEqual(evidence['boundary_evidence']['items'], ['8'])
            self.assertTrue(evidence['boundary_evidence']['ranges'])
            self.assertTrue(all(r['item'] != '8' for r in evidence['incorporated_sections']))
            self.assertEqual(list(result)[-1], 'classification_summary')

    def test_item_one_subset_does_not_load_unneeded_reports(self):
        data = primary()
        source_input = sources.FilingInput(data, 'test.htm', {'htm-url': URL}, {})
        with patch.object(sources.documents.ReportLoader, '__call__', side_effect=AssertionError('Unneeded report')):
            resolved = sources.resolve_source(source_input, ('1',), sources.documents.ReportLoader('unused', offline=True))
        self.assertEqual(set(resolved), {'primary'})


if __name__ == '__main__':
    unittest.main()
