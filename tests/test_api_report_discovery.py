"""SEC filing document-list fallback when the primary filing has no EX-13 link."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import api_filing_documents as documents
import run_api_metrics_pipeline as runner
from test_api_filing_documents import fixture, ITEMS, URL, REPORT_URL
from test_query_item8_metrics import package


def document_list():
    return (f'<html><body><table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>'
        f'<tr><td>1</td><td>10-K</td><td><a href="/ix?doc={urlparse(URL).path}">primary.htm</a></td><td>10-K</td><td>100</td></tr>'
        f'<tr><td>2</td><td>Annual report</td><td><a href="/ix?doc={urlparse(REPORT_URL).path}">report.htm</a></td><td>EX-13</td><td>200</td></tr>'
        '<tr><td>3</td><td>Not a report</td><td><a href="wrong_ex13.htm">wrong_ex13.htm</a></td><td>EX-4</td><td>300</td></tr>'
        '</table></body></html>').encode()


class ReportDiscoveryTests(unittest.TestCase):
    def test_document_type_and_exact_primary_identify_report_including_viewer_links(self):
        self.assertEqual(documents.filing_index_url(URL), URL.rsplit('/', 1)[0] + '/0000050863-24-000010-index.htm')
        for data in (document_list(), document_list().replace(b'/ix?doc=', b'')):
            with self.subTest(viewer=b'/ix?' in data):
                target, evidence = documents.report_from_document_list(data, URL)
                self.assertEqual(target, REPORT_URL)
                self.assertEqual(evidence['document_type'], 'EX-13')
                self.assertEqual(evidence['index_sha256'], documents.sha(data))
                self.assertTrue(evidence['source_locator'].endswith('/tr[3]'))

    def test_untrusted_ambiguous_and_non_html_targets_are_rejected(self):
        data = document_list()
        report_row = data[data.index(b'<tr><td>2'):data.index(b'<tr><td>3')]
        variants = [
            (data.replace(b'intc-20231230.htm', b'other-10k.htm'), 'selected original 10-K'),
            (data.replace(b'<td>EX-13</td>', b'<td>EX-4</td>'), 'exactly one'),
            (data.replace(b'</table>', report_row.replace(b'report.htm', b'second.htm') + b'</table>'), 'exactly one'),
            (data.replace(urlparse(REPORT_URL).path.encode(), b'https://example.com/report.htm'), 'original SEC filing'),
            (data.replace(urlparse(REPORT_URL).path.encode(), b'/Archives/edgar/data/50863/000005086324999999/report.htm'), 'outside the selected SEC accession'),
            (data.replace(urlparse(REPORT_URL).path.encode(), urlparse(URL).path.encode()), 'distinct HTML EX-13'),
            (data.replace(b'report.htm', b'report.pdf'), 'original SEC filing'),
            (data.replace(b'/ix?doc=', b'/ix?extra=1&amp;doc='), 'Invalid SEC document viewer link'),
            (data.replace(b'<th>Type</th>', b'<th>Other</th>'), 'selected original 10-K'),
            (data.replace(b'<a href="/ix?doc=' + urlparse(REPORT_URL).path.encode() + b'">report.htm</a>', b'report.htm'), 'missing or ambiguous'),
        ]
        for changed, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                documents.report_from_document_list(changed, URL)

    def test_discovery_download_and_offline_cache(self):
        data = document_list()
        index_url = documents.filing_index_url(URL)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            loader = documents.ReportLoader(root, 'Test test@example.com')
            with patch.object(documents.sec, 'SecClient') as client, redirect_stdout(io.StringIO()):
                client.return_value.get.return_value = data
                self.assertEqual(loader.discover_report(URL)[0], REPORT_URL)
                client.return_value.get.assert_called_once_with(index_url)
            # Same path used by SecClient's immutable SEC document cache.
            cached = root / (documents.sha(index_url.encode()) + '.html')
            cached.write_bytes(data)
            with patch.object(documents.sec, 'SecClient', side_effect=AssertionError('Offline network access')):
                offline = documents.ReportLoader(root, offline=True)
                self.assertEqual(offline.discover_report(URL)[0], REPORT_URL)
                cached.write_bytes(data.replace(b'<td>EX-13</td>', b'<td>EX-4</td>'))
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    offline.discover_report(URL)
                cached.unlink()
                with self.assertRaisesRegex(ValueError, 'not cached'):
                    offline.discover_report(URL)

    def test_missing_link_pipeline_uses_index_and_preserves_single_output(self):
        primary, report, payload = fixture()
        primary = primary.replace(b'<a href="report.htm">Annual Report</a>', b'Annual Report')
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, response, report_path = root / 'primary.htm', root / 'api.json', root / 'report.htm'
            source.write_bytes(primary)
            response.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                                           'source_sha256': documents.sha(primary), 'response': payload}))
            report_path.write_bytes(report)
            (root / 'report-source.json').write_text(json.dumps({'sec_url': REPORT_URL, 'sha256': documents.sha(report)}))
            cache, tax = root / 'cache', root / 'tax'
            cache.mkdir()
            tax.mkdir()
            (cache / (documents.sha(documents.filing_index_url(URL).encode()) + '.html')).write_bytes(document_list())
            (tax / 'us-gaap-2023.zip').write_bytes(package(2023))
            output = root / 'out'
            before = {p: p.read_bytes() for p in (source, response, report_path)}
            with (patch.object(documents.sec, 'SecClient', side_effect=AssertionError('Offline network access')),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                code = runner.main(['--filing', str(source), '--xbrl-json', str(response), '--year', '2023',
                                    '--output-dir', str(output), '--taxonomy-cache', str(tax),
                                    '--sec-cache', str(cache), '--offline'])
            self.assertEqual(code, 0)
            filename = 'primary_2023_api_metrics_with_values.json'
            self.assertEqual([p.name for p in output.iterdir()], [filename])
            result = json.loads((output / filename).read_text())
            provenance = result['verification']['source']['documents']['report1']
            self.assertEqual(provenance['sec_url'], REPORT_URL)
            self.assertEqual(provenance['report_discovery']['method'], 'sec_filing_document_list')
            self.assertEqual(provenance['report_discovery']['index_sha256'], documents.sha(document_list()))
            self.assertEqual(result['verification']['unique_values'], 3)
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_linked_ambiguous_or_invalid_reports_do_not_use_fallback(self):
        primary, report, _ = fixture()
        with TemporaryDirectory() as tmp, patch.object(documents.ReportLoader, 'discover_report') as discover:
            loader = documents.ReportLoader(tmp, offline=True)
            for changed in (primary.replace(b'report.htm', b'../other/report.htm'),
                            primary.replace(b'</td></tr></table>', b'<a href="second.htm">Second report</a></td></tr></table>')):
                with self.assertRaisesRegex(ValueError, 'linked HTML Exhibit'):
                    documents.resolve_documents(changed, 'primary.htm', {'htm-url': URL}, ITEMS, loader)
            # A usable direct link is loaded without requesting a document list.
            with patch.object(documents.ReportLoader, '__call__', return_value=(report, 'report.htm')):
                docs = documents.resolve_documents(primary, 'primary.htm', {'htm-url': URL}, ITEMS, loader)
                self.assertEqual(docs['report1']['url'], REPORT_URL)
            discover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
