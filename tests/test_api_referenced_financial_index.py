"""Item 8 -> Item 15 -> a cited, linked financial appendix index."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from sec_disclosure.table_extraction import check_item8_api as check
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from sec_disclosure.table_extraction import run_api_metrics_pipeline as pipeline
from test_extract_10k_tables_xbrl import filing, footer, number, table


REFERENCE = ('The consolidated financial statements and supplementary data are filed '
             'within this Annual Report under Item 15, “Exhibits, Financial Statement Schedules.”')
INDEX_REFERENCE = 'Reference is made to the Index to Consolidated Financial Statements on Page F-1.'
ENTRIES = [
    ('balance', 'Consolidated Balance Sheets', 'F-2'),
    ('income', 'Consolidated Statements of Income', 'F-3'),
    ('cash_flow', 'Consolidated Statements of Cash Flows', 'F-4'),
    ('notes', 'Notes to Consolidated Financial Statements', 'F-5'),
    ('schedule', 'Schedule II — Valuation and Qualifying Accounts', 'F-6'),
]


def appendix_fixture():
    body = '<h1>Item 1. Business</h1><p>Business.</p>'
    body += '<h1>Item 1A. Risk Factors</h1><p>Risk.</p>'
    body += '<h1>Item 7. Management Discussion</h1><p>Discussion.</p>'
    body += '<h1>Item 8. Financial Statements</h1><p>' + REFERENCE + '</p>'
    body += '<h1>Item 9. Changes in Accountants</h1><p>None.</p>'
    body += '<h1>Item 15. Exhibits and Financial Statement Schedules</h1><p>' + INDEX_REFERENCE + '</p>'
    body += '<h1>Item 16. Form 10-K Summary</h1><p>None.</p>'
    body += number('99', 'before_appendix') + footer('39')
    body += '<h2>Index to Consolidated Financial Statements</h2><table id="financial_index">'
    for anchor, title, page in ENTRIES:
        label = title + (' — Years Ended December 31, 2025' if anchor in {'income', 'cash_flow'} else '')
        body += f'<tr><td><a href="#{anchor}">{label}</a></td><td>{page}</td></tr>'
    body += '</table>' + footer('F-1')
    for i, (anchor, title, page) in enumerate(ENTRIES):
        if anchor == 'notes':
            body += f'<h2>{title}</h2><h3 id="{anchor}">Note 1. Basis of Presentation</h3>'
        else:
            body += f'<h2 id="{anchor}">{title}</h2>'
        body += table(number('103.6' if i == 0 else str(i + 1), anchor + '_fact')) + footer(page)
    return filing(body)


class ReferencedFinancialIndexTests(unittest.TestCase):
    def layout(self, data=None):
        doc = ix.Document(appendix_fixture() if data is None else data, 'example.htm')
        return doc, ix.Layout(doc)

    def test_appendix_statements_map_to_item8_but_schedule_and_unrelated_facts_do_not(self):
        doc, layout = self.layout()
        layout.validate(('1', '1A', '7', '8'))
        self.assertEqual(len(layout.ranges), 1)
        scope = layout.ranges[0]
        self.assertEqual(scope['method'], 'referenced_financial_index')
        self.assertEqual(scope['index_reference']['page'], 'F-1')
        self.assertEqual(scope['index_reference']['evidence'].count(INDEX_REFERENCE), 1)
        self.assertEqual([e['page'] for e in scope['index_entries']], ['F-2', 'F-3', 'F-4', 'F-5', 'F-6'])
        for name in ('balance', 'income', 'cash_flow', 'notes'):
            self.assertEqual(layout.membership(doc.ids[name + '_fact'])[1:], ('16', ['8']))
        for name in ('before_appendix', 'schedule_fact'):
            self.assertEqual(layout.membership(doc.ids[name])[1:], ('16', ['16']))

    def test_exact_api_value_gets_item8_membership_without_extracting_table_values(self):
        data = appendix_fixture()
        raw = {'value': '103600000', 'unitRef': 'usd', 'decimals': '-6',
               'period': {'startDate': '2025-01-01', 'endDate': '2025-12-31'}}
        with patch.object(ix, 'collect_tables', side_effect=AssertionError('No table value extraction')):
            index = check.LocationIndex(data, 'example.htm', ('1', '1A', '7', '8'))
            result = index.check('Revenue', raw, '/response/Statements/Revenue/0')
        self.assertTrue(result['value_checked'])
        self.assertEqual(result['matched_items'], ['8'])
        self.assertEqual(result['item_membership']['8'], 'in_item')
        self.assertEqual(result['evidence'][0]['locations'][0]['page'], 'F-2')
        raw['value'] = '1'
        self.assertFalse(index.check('Revenue', raw, '/wrong')['value_checked'])

    def test_no_explicit_item8_reference_does_not_promote_an_appendix(self):
        for reference in (b'No financial statements are incorporated here.',
                          REFERENCE.replace('are filed', 'are not filed').encode()):
            with self.subTest(reference=reference):
                data = appendix_fixture().replace(REFERENCE.encode(), reference)
                doc, layout = self.layout(data)
                self.assertEqual(layout.ranges, [])
                self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['16'])

    def test_boundary_text_block_wrapper_is_also_outside_item8(self):
        data = appendix_fixture().replace(b'<h2 id="schedule">',
            b'<ix:nonNumeric id="schedule_block" name="ex:ScheduleTextBlock" contextRef="annual">'
            b'<h2 id="schedule">').replace(footer('F-6').encode(),
            b'</ix:nonNumeric>' + footer('F-6').encode())
        doc, layout = self.layout(data)
        self.assertEqual(layout.membership(doc.ids['notes_fact'])[2], ['8'])
        self.assertEqual(layout.membership(doc.ids['schedule_block'])[2], ['16'])
        self.assertEqual(layout.membership(doc.ids['schedule_fact'])[2], ['16'])

    def test_other_explicit_wording_and_printed_page_prefixes_work(self):
        for verb in ('included', 'presented', 'set forth'):
            with self.subTest(verb=verb):
                data = appendix_fixture().replace(b'are filed', ('are ' + verb).encode()).replace(b'F-', b'FS-')
                doc, layout = self.layout(data)
                self.assertEqual(layout.ranges[0]['index_reference']['page'], 'FS-1')
                self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['8'])

    def test_recognized_chain_fails_closed_when_evidence_is_missing_or_conflicting(self):
        cases = [
            (b'on Page F-1.', b'on Page F-99.', 'index page is missing'),
            (b'href="#balance"', b'href="other.htm#balance"', 'unique internal links'),
            (b'id="balance"', b'id="missing_balance"', 'link target disagrees'),
            (b'<td>F-2</td>', b'<td>F-3</td>', 'link target disagrees'),
            (b'id="balance">Consolidated Balance Sheets', b'id="balance">Other Information', 'target title'),
            (b'<a href="#schedule">', b'<a>', 'unique internal links'),
            (b'Schedule II', b'Other II', 'no verified end boundary'),
            (footer('F-3').encode(), footer('F-4').encode(), 'link target disagrees'),
            (b'<table id="financial_index">', footer('F-1').encode() + b'<table id="financial_index">', 'index page is missing or ambiguous'),
            (INDEX_REFERENCE.encode(), (INDEX_REFERENCE + ' Index to Consolidated Financial Statements on Page F-2.').encode(), 'multiple financial statement index pages'),
        ]
        for old, new, message in cases:
            with self.subTest(message=message, replacement=new):
                with self.assertRaisesRegex(ValueError, message):
                    self.layout(appendix_fixture().replace(old, new))

    def test_link_to_distant_same_page_title_does_not_establish_a_statement_boundary(self):
        data = appendix_fixture().replace(
            b'<h2 id="balance">', b'<div id="balance"/>' + b'<p>Other content.</p>' * 60 + b'<h2>')
        with self.assertRaisesRegex(ValueError, 'target title'):
            self.layout(data)

    def test_multiple_matching_indexes_are_ambiguous(self):
        data = appendix_fixture()
        toc = data.split(b'<table id="financial_index">', 1)[1].split(b'</table>', 1)[0]
        data = data.replace(b'<table id="financial_index">', b'<table>' + toc + b'</table><table id="financial_index">')
        with self.assertRaisesRegex(ValueError, 'Cannot uniquely verify'):
            self.layout(data)

    def test_missing_interior_page_labels_do_not_broaden_the_range(self):
        data = appendix_fixture().replace(footer('F-5').encode(), footer('F-5').encode() +
            '<p>Continued notes.</p>'.encode() + footer('F-6').encode()).replace(
                b'<td>F-6</td>', b'<td>F-7</td>')
        # Keep the schedule target on F-7 while removing the intervening footer.
        last = data.rfind(footer('F-6').encode())
        data = data[:last] + data[last:].replace(footer('F-6').encode(), footer('F-7').encode(), 1)
        data = data.replace(footer('F-6').encode(), b'<div style="page-break-after:always"/>')
        with self.assertRaisesRegex(ValueError, 'missing, duplicate or discontinuous pages'):
            self.layout(data)

    def test_existing_direct_item15_index_still_works(self):
        data = appendix_fixture().replace(REFERENCE.encode(),
            b'The consolidated financial statements required by this item are included in this Annual Report.')
        data = data.replace(INDEX_REFERENCE.encode(), b'The following index lists the statements.')
        data = data.replace(b'<h1>Item 16. Form 10-K Summary</h1>', b'<h2>Signatures</h2>')
        doc, layout = self.layout(data)
        self.assertEqual(layout.ranges[0]['method'], 'financial_index')
        self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['8'])


@unittest.skipUnless(os.environ.get('SEC_MEI_API_INTEGRATION') == '1', 'Requires cached MEI FY2025 inputs and taxonomy')
class CachedMeiTests(unittest.TestCase):
    def test_full_offline_pipeline_exports_item8_values_and_excludes_schedule(self):
        url = 'https://www.sec.gov/Archives/edgar/data/65270/000095017025094822/mei-20250503.htm'
        cache = Path('data/sec_cache/api_metrics') / pipeline.api.digest({'htm-url': url})
        filing_path, response_path = cache / 'original_filing.htm', cache / 'sec_api_xbrl.json'
        if not filing_path.exists() or not response_path.exists():
            self.skipTest('MEI filing/API cache not available')
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (filing_path, response_path)}
        with TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                    patch.object(pipeline.api, 'fetch_xbrl_json', side_effect=AssertionError('No paid API requests')):
                code = pipeline.main(['--filing', str(filing_path), '--xbrl-json', str(response_path),
                                      '--company', 'MEI', '--year', '2025', '--offline', '--output-dir', tmp])
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            paths = list(Path(tmp).iterdir())
            self.assertEqual([p.name for p in paths], ['mei_2025_api_metrics_with_values.json'])
            result = json.loads(paths[0].read_text())
        self.assertEqual(result['schema_version'], 'api-metrics-1.0')
        self.assertEqual(result['verification']['checked_api_entries'], 2550)
        self.assertEqual(result['verification']['included_api_entries'], 2231)
        self.assertEqual(result['counts']['verified_concepts'], 278)
        self.assertEqual(result['verification']['unique_values'], 1163)
        cash = next(m for m in result['metrics'] if m['concept'] == 'us-gaap:CashAndCashEquivalentsAtCarryingValue')
        amount = next(v for v in cash['value'] if v['period'] == {'instant': '2025-05-03'} and not v['dimensions'])
        self.assertEqual(amount['value'], '103600000')
        self.assertEqual(amount['items'], ['8'])
        self.assertEqual({s['page'] for s in amount['source_labels']}, {'F-5'})
        self.assertNotIn('F-41', {s.get('page') for m in result['metrics'] for v in m['value'] for s in v['source_labels']})
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})


if __name__ == '__main__':
    unittest.main()
