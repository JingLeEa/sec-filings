"""Incorporated report resolution, document-scoped evidence and shared resources."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from sec_disclosure.table_extraction import api_filing_documents as documents
from sec_disclosure.table_extraction import check_item8_api as check
from sec_disclosure.table_extraction import export_api_metrics as export
from sec_disclosure.table_extraction import run_api_metrics_pipeline as runner
from test_extract_item8_xbrl_api import URL
from test_query_item8_metrics import package


ITEMS = ('1', '1A', '7', '8')
REPORT_URL = URL.rsplit('/', 1)[0] + '/report.htm'
RESOURCES = '''<ix:header><ix:resources>
<xbrli:context id="annual"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000050863</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-30</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
</ix:resources></ix:header>'''


def document(body, resources=''):
    return ('''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2023"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:dei="http://xbrl.sec.gov/dei/2023">
      <body>''' + resources + body + '</body></html>').encode()


def fact(value, identifier='same', concept='NetIncomeLoss'):
    return (f'<ix:nonFraction id="{identifier}" name="us-gaap:{concept}" contextRef="annual" '
            f'unitRef="usd" scale="6" decimals="-6">{value}</ix:nonFraction>')


def fixture():
    primary = document('<h1>Item 1. Business</h1><p>' + fact(1) + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>See this report under Item 1 and the Annual Report under '
        '“Financial Review – Risk Factors.” That information is incorporated by reference.</p>'
        '<h1>Item 7. Management Discussion</h1><p>Information in the Annual Report under “Financial Review” '
        'is incorporated by reference.</p><h1>Item 8. Financial Statements</h1><p>Information in the Annual Report '
        'under “Financial Statements” is incorporated by reference.</p><h1>Item 9. Accountants</h1>'
        '<h1>Item 15. Exhibits</h1><table><tr><td>13</td><td><a href="report.htm">Annual Report</a></td></tr></table>')
    report = document('<h1>Financial Review</h1><p>Review introduction.</p>'
        '<h2>Risk Factors</h2><p>' + fact(3, 'risk', 'ProfitLoss') + '</p>'
        '<h1>Financial Statements</h1><table><tr><th>Metric</th><th>2023</th></tr>'
        '<tr><td>Net income</td><td>' + fact(2) + '</td></tr></table>'
        '<h1>Other Information</h1><p>' + fact(4, 'outside') + '</p>', RESOURCES)
    def record(amount):
        return {'value': str(amount * 1000000), 'unitRef': 'usd', 'decimals': '-6',
                'period': {'startDate': '2023-01-01', 'endDate': '2023-12-30'}}
    payload = {'CoverPage': {'DocumentFiscalYearFocus': '2023', 'DocumentType': '10-K',
                            'EntityCentralIndexKey': '0000050863', 'EntityRegistrantName': 'Example',
                            'DocumentPeriodEndDate': '2023-12-30'},
               'StatementsOfIncome': {'NetIncomeLoss': [record(1), record(2), record(4)], 'ProfitLoss': [record(3)]}}
    return primary, report, payload


def resolve(primary, report, items=ITEMS):
    return documents.resolve_documents(primary, 'primary.htm', {'htm-url': URL}, items,
                                       lambda url, adjacent: (report, 'report.htm'))


def page_fixture():
    """Shareholder-report pages, including out-of-scope facts at both ends."""
    _, _, payload = fixture()
    primary = document('<h1>Item 1. Business</h1><p>' + fact(1) + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>Business risks.</p>'
        '<h1>Item 7. Management Discussion</h1><p>The information under the heading “Management Discussion” '
        'on pages 5-6 of the Registrant’s 2023 Annual Report to Shareholders is incorporated herein by reference.</p>'
        '<h1>Item 8. Financial Statements</h1><p>The Registrant’s Consolidated Financial Statements, '
        'appearing on pages 3 through 4 of the Registrant’s 2023 Annual Report to Shareholders '
        'is incorporated herein by reference. The remaining report is not deemed filed as part of this Form 10-K.</p>'
        '<h1>Item 9. Accountants</h1><h1>Item 15. Exhibits</h1>'
        '<table><tr><td>13*</td><td><a href="report.htm">Annual Report to Shareholders</a></td></tr></table>')
    def page(number, content):
        return (f'<div style="break-before:page"/>{content}'
                f'<p style="text-align:center">{number}</p>')
    report = document(page(2, '<p>Shareholder letter income: ' + fact(4, 'outside_before') + '</p>')
        + page(3, '<table><tr><th>Metric</th><th>2023</th></tr>'
            '<tr><td>Net income</td><td>' + fact(2) + '</td></tr></table>')
        + page(4, '<p>Profit in the notes: ' + fact(3, 'note', 'ProfitLoss') + '</p>')
        + page(5, '<p>Review income: ' + fact(1, 'review') + '</p>')
        + page(6, '<p>Discussion continues.</p>')
        + page(7, '<p>Other income: ' + fact(4, 'outside_after') + '</p>')
        + '<div style="break-before:page"/>', RESOURCES)
    return primary, report, payload


def unquoted_fixture():
    """Unquoted incorporation, no report TOC/h1 tags, primary-owned resources."""
    _, _, payload = fixture()
    titles = ['Reports of Independent Registered Public Accounting Firm', 'Consolidated Balance Sheets',
              'Consolidated Statements of Income', 'Consolidated Statements of Cash Flows',
              'Notes to Consolidated Financial Statements']
    inventory = ''.join('<p>' + title + (' as of December 31, 2023' if 'Balance Sheets' in title else '')
        + ' - Incorporated by reference from Example’s 2023 Annual Report</p>' for title in titles)
    primary = document('<h1>Item 1. Business</h1><p>Income: ' + fact(1) + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>Business risks.</p>'
        '<h1>Item 7. Review</h1><p>The information contained in the Overview of Annual Results section '
        'of Example’s 2023 Annual Report to Shareholders (included as Exhibit 13 of this report) '
        'is incorporated herein by reference in response to this item.</p>'
        '<h1>Item 8. Financial Statements</h1><p>The consolidated financial statements and the reports '
        'of our independent registered public accounting firm included in the Consolidated Financial Statements '
        'and the Notes to Consolidated Financial Statements in Example’s 2023 Annual Report to Shareholders '
        '(included as Exhibit 13 of this report), are incorporated herein by reference.</p>'
        '<h1>Item 9. Accountants</h1><h1>Item 15. Exhibits</h1>' + inventory
        + '<table><tr><td>13</td><td><a href="report.htm">Annual Report</a></td></tr></table>', RESOURCES)
    def heading(title):
        return '<div><b>' + title + '</b></div>'
    def page(number, content):
        label = f'Example 2023 Annual Report {number}' if number % 2 else f'{number} Example 2023 Annual Report'
        return '<div style="break-before:page"/>' + content + '<div>' + label + '</div>'
    def income(identifier):
        return '<table><tr><th>Metric</th><th>2023</th></tr><tr><td>Net income</td><td>' + fact(2, identifier) + '</td></tr></table>'
    report = document(page(1, heading('Glossary') + '<p>Outside income: ' + fact(4, 'outside_before') + '</p>')
        + page(2, heading('Overview of Annual Results and Operations') + '<p>Review income: ' + fact(1, 'review') + '</p>')
        + page(3, heading('Overview of Annual Results and Operations') + '<p>Discussion continues.</p>')
        + page(4, '<table><tr><td style="text-align:center">Report of Independent Registered Public Accounting Firm</td></tr></table>')
        + page(5, heading('Consolidated Balance Sheets') + '<p>Balance sheet discussion.</p>')
        + page(6, heading('Consolidated Statements of Income') + income('income'))
        + page(7, heading('Consolidated Statements of Cash Flows') + income('cash_flow'))
        + page(8, heading('Notes to Consolidated Financial Statements') + '<p>Note profit: ' + fact(3, 'profit', 'ProfitLoss') + '</p>')
        + page(9, heading('Notes to Consolidated Financial Statements')
            + '<ix:nonNumeric name="us-gaap:IncomeTaxDisclosureTextBlock" contextRef="annual">'
            + heading('Appendix Heading') + '<p>Nested disclosure.</p></ix:nonNumeric>')
        + page(10, heading('Shareholder Returns') + '<p>Outside income: ' + fact(4, 'outside_after') + '</p>'))
    return primary, report, payload


def prefixed_page_fixture():
    """Exhibit (13), appendix page labels and in-table page-break footers."""
    _, _, payload = fixture()
    primary = document('<h1>Item 1. Business</h1><p>' + fact(1) + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>Business risks.</p>'
        '<h1>Item 7. Discussion</h1><p>Information in “Management Discussion” on pages A-5 through A-6 '
        'of the Annual Report is filed as Exhibit (13). That section is incorporated herein by reference.</p>'
        '<h1>Item 8. Financial Statements</h1><p>The consolidated financial statements are set forth '
        'on pages A-3 through A-5 of the Annual Report filed with this Form 10-K as Exhibit (13). '
        'The financial statements on pages A-3 through A-4 of the Annual Report are incorporated herein by reference.</p>'
        '<h1>Item 9. Accountants</h1><h1>Item 15. Exhibits</h1>'
        '<table><tr><td><a href="report.htm">Exhibit (13)</a></td>'
        '<td><a href="report.htm">Annual Report</a></td></tr></table>', RESOURCES)
    def page(number, content):
        return (content + '<table><tr><td> </td></tr><tr><td style="text-align:center">'
                f'A-{number}</td></tr><tr><td><p style="page-break-after:always"/></td></tr></table>')
    report = document(page(1, '<p>Cover</p>')
        + page(2, '<p>Outside income: ' + fact(4, 'outside_before') + '</p>')
        + page(3, '<table><tr><th>Metric</th><th>2023</th></tr><tr><td>Net income</td><td>' + fact(2) + '</td></tr></table>')
        + page(4, '<p>Notes profit: ' + fact(3, 'profit', 'ProfitLoss') + '</p>')
        + page(5, '<p>Review income: ' + fact(1, 'review') + '</p>')
        + page(6, '<p>Discussion continues.</p>')
        + page(7, '<p>Outside income: ' + fact(4, 'outside_after') + '</p>'))
    return primary, report, payload


class IncorporatedReportTests(unittest.TestCase):
    def test_multiple_incorporation_clauses_keep_disjoint_pages_and_the_gap(self):
        primary, report, payload = page_fixture()
        primary = primary.replace(b'Annual Report to Shareholders', b'Annual Report')
        primary = primary.replace(b'The remaining report is not deemed filed',
            b'The quarterly income statements on pages 7 and 8 of our 2023 Annual Report are incorporated herein by reference. The remaining report is not deemed filed')
        # The first exhibit listing has no link; a later index provides it.
        primary = primary.replace(b'<td>13*</td>', b'<td>(13)**</td>')
        primary = primary.replace(b'<a href="report.htm">Annual Report</a>', b'Annual Report')
        primary = primary.replace(b'</body>', '<table><tr><td>Exhibit 13—</td><td><a href="report.htm">Annual Report</a></td></tr></table></body>'.encode())
        extra = ('<div style="break-before:page"/><p>Quarterly income: ' + fact(5, 'quarter1') + '</p><p style="text-align:center">7</p>'
                 '<div style="break-before:page"/><p>Quarterly income: ' + fact(6, 'quarter2') + '</p><p style="text-align:center">8</p>')
        report = report.replace(b'<div style="break-before:page"/><p>Other income:', extra.encode() + b'<div style="break-before:page"/><p>Other income:')
        report = report.replace(b'>7</p><div style="break-before:page"/></body>', b'>9</p><div style="break-before:page"/></body>')
        template = payload['StatementsOfIncome']['NetIncomeLoss'][0]
        payload['StatementsOfIncome']['NetIncomeLoss'].extend([{**template, 'value': str(n * 1000000)} for n in (5, 6)])
        docs = resolve(primary, report)
        self.assertEqual([r['pages'] for r in docs['report1']['layout'].references], [[5, 6], [3, 4, 7, 8]])
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023, items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        income = next(m for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss')
        values = {v['value']: v for v in income['value']}
        self.assertEqual(set(values), {'1000000', '2000000', '5000000', '6000000'})
        self.assertEqual(values['1000000']['items'], ['1', '7'])
        for amount, page in [('5000000', '7'), ('6000000', '8')]:
            self.assertEqual(values[amount]['items'], ['8'])
            self.assertEqual(values[amount]['source_labels'][0]['page'], page)
        self.assertEqual(audit['excluded_api_entries'], 1)
        for page in (b'7', b'8'):
            with self.subTest(missing=page), self.assertRaisesRegex(ValueError, 'not uniquely verified'):
                resolve(primary, report.replace(b'>' + page + b'</p>', b'>Missing</p>'))

    def test_exhibit_labels_and_later_indexes_find_same_report(self):
        primary, report, _ = fixture()
        for label in ('13', 'EX-13', 'Exhibit 13—', 'Exhibit 13 -', '(13)**', 'Exhibit (13)*', '13.1†'):
            with self.subTest(label=label):
                docs = resolve(primary.replace(b'<td>13</td>', f'<td>{label}</td>'.encode()), report)
                self.assertEqual(docs['report1']['url'], REPORT_URL)

    def test_prefixed_pages_follow_explicit_incorporation_not_descriptive_range(self):
        primary, report, payload = prefixed_page_fixture()
        docs = resolve(primary, report)
        layout = docs['report1']['layout']
        self.assertEqual([p for _, p in layout.footers], [f'A-{n}' for n in range(1, 8)])
        self.assertEqual([r['pages'] for r in layout.references], [['A-5', 'A-6'], ['A-3', 'A-4']])
        self.assertEqual(len(docs['report1']['doc'].contexts), 1)
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        income = next(m for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss')
        values = {v['value']: v for v in income['value']}
        self.assertEqual(set(values), {'1000000', '2000000'})
        self.assertEqual(values['1000000']['items'], ['1', '7'])
        self.assertEqual(values['2000000']['items'], ['8'])
        label = values['2000000']['source_labels'][0]
        self.assertEqual((label['document_id'], label['page'], label['row_label']), ('report1', 'A-3', 'Net income'))
        self.assertEqual(audit['excluded_api_entries'], 1)

    def test_prefixed_pages_reject_wrong_prefix_missing_duplicates_and_gaps(self):
        primary, report, _ = prefixed_page_fixture()
        for changed, message in [
            (report.replace(b'>A-4</td>', b'>4</td>'), 'not uniquely verified'),
            (report.replace(b'>A-4</td>', b'>B-4</td>'), 'not uniquely verified'),
            (report.replace(b'>A-7</td>', b'>A-4</td>'), 'not uniquely verified'),
            (report.replace(b'>A-3</td>', b'>swap</td>').replace(b'>A-4</td>', b'>A-3</td>').replace(b'>swap</td>', b'>A-4</td>'), 'out of order'),
            (report.replace(b'>A-3</td>', b'>A-3</td></tr><tr><td><p style="break-after:page"/></td>'), 'unverified gaps'),
            (report.replace(b'>A-4</td>', b'>A-4</td><td>Revenue</td>'), 'not uniquely verified'),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                resolve(primary, changed)

    def test_report_zerodash_matches_api_zero_without_rewriting_values(self):
        primary, report, payload = prefixed_page_fixture()
        payload['StatementsOfIncome']['NetIncomeLoss'][1]['value'] = '0'
        report = report.replace(b'xmlns:ix=', b'xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2015-02-26" xmlns:ix=')
        report = report.replace(fact(2).encode(), fact('-', concept='NetIncomeLoss').replace('scale="6"', 'scale="6" format="ixt:zerodash"').encode())
        original = deepcopy(payload)
        docs = resolve(primary, report)
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=ITEMS, documents=docs)
        _, output, _ = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        income = next(m for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss')
        zero = next(v for v in income['value'] if v['value'] == '0')
        self.assertEqual((zero['status'], zero['items'], zero['source_labels'][0]['page']), ('reported', ['8'], 'A-3'))
        self.assertEqual(payload, original)

    def test_prefixed_reference_syntax_and_conflicting_incorporation(self):
        primary, report, _ = prefixed_page_fixture()
        for span in ('A-3-A-4', 'A-3–A-4', 'A-3 to A-4', 'A-3, A-4', 'A-3 and A-4'):
            with self.subTest(span=span):
                docs = resolve(primary.replace(b'A-3 through A-4', span.encode()), report)
                self.assertEqual(docs['report1']['layout'].references[1]['pages'], ['A-3', 'A-4'])
        for span in ('A-3 through B-4', 'A-3 through 4', 'A-4 through A-3'):
            with self.subTest(span=span), self.assertRaisesRegex(ValueError, 'Invalid printed-page range'):
                resolve(primary.replace(b'A-3 through A-4', span.encode()), report)
        conflict = primary.replace(b'as Exhibit (13).', b'as Exhibit (13) and are incorporated by reference.')
        with self.assertRaisesRegex(ValueError, 'Multiple external report page references'):
            resolve(conflict, report)
        self.assertIsNone(documents.ix.external_report_pages(
            'Pages A-3 through A-4 of the Annual Report are incorporated by reference.'))

    def test_unquoted_sections_use_statement_inventory_and_reverse_shared_resources(self):
        primary, report, payload = unquoted_fixture()
        docs = resolve(primary, report)
        self.assertEqual(len(docs['report1']['doc'].contexts), 1)
        self.assertEqual([p for _, p in docs['report1']['layout'].footers], [str(i) for i in range(1, 11)])
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        values = {v['value']: v for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss' for v in m['value']}
        self.assertEqual(set(values), {'1000000', '2000000'})
        self.assertEqual(values['1000000']['items'], ['1', '7'])
        self.assertEqual(values['2000000']['items'], ['8'])
        self.assertEqual({s['page'] for s in values['2000000']['source_labels']}, {'6', '7'})
        self.assertEqual(audit['excluded_api_entries'], 1)
        sections = docs['report1']['layout'].ranges
        self.assertEqual(len(sections), 6)
        self.assertTrue(all(r['method'] == 'external_report_unquoted_section' for r in sections))
        self.assertTrue(all(r['index_evidence'] for r in sections if r['item'] == '8'))
        self.assertEqual(sections[-1]['index_evidence'][0]['document_id'], 'primary')
        self.assertEqual(sections[-1]['section'], 'Notes to Consolidated Financial Statements')

    def test_unquoted_sections_fail_instead_of_silently_exporting_zero(self):
        primary, report, _ = unquoted_fixture()
        variants = [
            (primary.replace(b'Overview of Annual Results section', b'Unknown section'), report, 'Cannot uniquely locate'),
            (primary.replace(b'Consolidated Balance Sheets as of', b'Unknown as of'), report, 'unquoted sections cannot be resolved'),
            (primary, report.replace(b'<b>Consolidated Statements of Income</b>', b'Consolidated Statements of Income'), 'Cannot uniquely locate'),
            (primary, report.replace(b'<b>Shareholder Returns</b>', b'Shareholder Returns'), 'Cannot verify the end'),
            (primary, report.replace(b'8 Example 2023 Annual Report', b'7 Example 2023 Annual Report'), 'missing or repeated printed pages'),
            (primary, report.replace(b'8 Example 2023 Annual Report', b'swap').replace(b'Example 2023 Annual Report 9', b'8 Example 2023 Annual Report').replace(b'swap', b'Example 2023 Annual Report 9'), 'unordered pages'),
            (primary, report.replace(b'<p>Discussion continues.</p>', b'<div><b>Overview of Annual Results with Different Scope</b></div>'), 'Cannot uniquely locate'),
        ]
        for source, changed, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                resolve(source, changed)

    def test_external_page_ranges_and_footnoted_exhibit_preserve_scope(self):
        primary, report, payload = page_fixture()
        docs = resolve(primary, report)
        self.assertEqual([r['pages'] for r in docs['report1']['layout'].references], [[5, 6], [3, 4]])
        self.assertEqual(docs['primary']['layout'].references, [])
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        income = next(m for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss')
        values = {v['value']: v for v in income['value']}
        self.assertEqual(set(values), {'1000000', '2000000'})
        self.assertEqual(values['1000000']['items'], ['1', '7'])
        self.assertEqual(values['2000000']['items'], ['8'])
        source = values['2000000']['source_labels'][0]
        self.assertEqual((source['document_id'], source['page'], source['row_label']), ('report1', '3', 'Net income'))
        self.assertEqual(audit['excluded_api_entries'], 1)
        self.assertEqual(checked['item_scopes']['8']['page_references'][0]['document_id'], 'report1')
        self.assertEqual(documents.describe(docs)['report1']['incorporated_pages'][1]['pages'], [3, 4])

    def test_external_page_ranges_reject_missing_repeated_and_unordered_pages(self):
        primary, report, _ = page_fixture()
        variants = [
            (report.replace(b'>4</p>', b'>Missing</p>'), 'not uniquely verified'),
            (report.replace(b'>7</p>', b'>4</p>'), 'not uniquely verified'),
            (report.replace(b'>3</p>', b'>swap</p>').replace(b'>4</p>', b'>3</p>').replace(b'>swap</p>', b'>4</p>'), 'out of order'),
            (report.replace(b'>3</p><div style="break-before:page"/>', b'>3</p>'), 'unique page boundary'),
            (report.replace(b'>3</p>', b'>3</p><div style="break-before:page"/>'), 'unverified gaps'),
        ]
        for changed, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                resolve(primary, changed)

    def test_page_reference_syntax_does_not_treat_internal_pages_as_external(self):
        primary, report, _ = page_fixture()
        for span in ('3-4', '3–4', '3 to 4', '3, 4', '3 and 4', '3, and 4'):
            with self.subTest(span=span):
                docs = resolve(primary.replace(b'3 through 4', span.encode()), report)
                self.assertEqual(docs['report1']['layout'].references[1]['pages'], [3, 4])
        reference = 'The financial statements on pages 3-4 of this Form 10-K are incorporated by reference.'
        self.assertIsNone(documents.ix.external_report_pages(reference))
        reference = 'Pages 3-4 of the Annual Report to Shareholders are not incorporated by reference.'
        self.assertIsNone(documents.ix.external_report_pages(reference))
        with self.assertRaisesRegex(ValueError, 'Invalid printed-page range'):
            resolve(primary.replace(b'3 through 4', b'4 through 3'), report)
        for label in (b'13x', b'113*'):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, 'linked HTML Exhibit'):
                resolve(primary.replace(b'13*', label), report)

    def test_shared_contexts_and_duplicate_fact_ids_preserve_document_scope(self):
        primary, report, payload = fixture()
        original = deepcopy(payload)
        docs = resolve(primary, report)
        self.assertEqual(len(docs['primary']['doc'].contexts), 1)
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=ITEMS, documents=docs)
        dictionary = export.taxonomy.build_dictionary(package(2023), 2023)
        _, output, audit = export.build_metrics(payload, primary, checked, dictionary, 'api.json', None, documents=docs)
        income = next(m for m in output['metrics'] if m['query_name'] == 'NetIncomeLoss')
        values = {v['value']: v for v in income['value']}
        self.assertEqual(set(values), {'1000000', '2000000'})
        self.assertEqual(values['1000000']['items'], ['1', '1A'])
        self.assertEqual(values['2000000']['items'], ['8'])
        primary_label = values['1000000']['source_labels'][0]
        report_label = values['2000000']['source_labels'][0]
        self.assertEqual(primary_label['source_fact_id'], report_label['source_fact_id'])
        self.assertEqual(primary_label['document_id'], 'primary')
        self.assertEqual(report_label['document_id'], 'report1')
        self.assertEqual(report_label['source'], 'report.htm')
        self.assertEqual(report_label['row_label'], 'Net income')
        self.assertEqual(report_label['column_label'], '2023')
        risk = next(m for m in output['metrics'] if m['query_name'] == 'ProfitLoss')
        self.assertEqual(risk['value'][0]['items'], ['1A', '7'])
        self.assertEqual(audit['excluded_api_entries'], 1)
        self.assertEqual(payload, original)
        docs['report1']['doc'].data += b' '
        with self.assertRaisesRegex(ValueError, 'different incorporated report'):
            export.build_metrics(payload, primary, checked, dictionary, 'api.json', None, documents=docs)

    def test_wrong_issuer_year_or_missing_named_section_is_rejected(self):
        primary, report, payload = fixture()
        variants = [
            (report.replace(b'0000050863', b'0000000001'), 'entities differ'),
            (report.replace(b'<h1>Financial Statements</h1>', b'<h1>Missing</h1>'), 'referenced report headings'),
            (report.replace(b'</body>', b'<ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="annual">2024</ix:nonNumeric></body>'), 'fiscal year differs'),
        ]
        for data, message in variants:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                docs = resolve(primary, data)
                check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023, items=ITEMS, documents=docs)

    def test_only_linked_same_accession_report_is_loaded_and_subset_needs_no_report(self):
        primary, report, _ = fixture()
        loader = Mock(return_value=(report, 'report.htm'))
        complete_primary = primary.replace(b'<body>', b'<body>' + RESOURCES.encode())
        documents.resolve_documents(complete_primary, 'primary.htm', {'htm-url': URL}, ('1',), loader)
        loader.assert_not_called()
        for href in ('https://example.com/report.htm', '../other/report.htm', 'report.pdf'):
            with self.subTest(href=href), self.assertRaisesRegex(ValueError, 'linked HTML Exhibit'):
                documents.resolve_documents(primary.replace(b'href="report.htm"', f'href="{href}"'.encode()),
                    'primary.htm', {'htm-url': URL}, ITEMS, loader)
        loader.assert_not_called()
        documents.resolve_documents(primary, 'renamed_cache.htm', {'htm-url': URL}, ITEMS, loader)
        self.assertEqual(loader.call_args.args[0], REPORT_URL)

    def test_subset_borrows_needed_resources_without_claiming_report_facts(self):
        primary, report, payload = fixture()
        docs = resolve(primary, report, ('1',))
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=('1',), documents=docs)
        _, output, _ = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        self.assertEqual(len(output['metrics']), 1)
        self.assertEqual([v['value'] for v in output['metrics'][0]['value']], ['1000000'])
        self.assertEqual(docs['report1']['layout'].ranges, [])

    def test_same_unit_id_with_different_currencies_is_not_merged(self):
        primary, report, payload = fixture()
        primary = primary.replace(b'<body>', b'<body>' + RESOURCES.replace('iso4217:USD', 'iso4217:EUR').encode())
        primary = primary.replace(b'>1</ix:nonFraction>', b'>2</ix:nonFraction>')
        docs = resolve(primary, report)
        checked = check.check_response(payload, primary, 'primary.htm', {'htm-url': URL}, 2023,
                                       items=ITEMS, documents=docs)
        _, output, audit = export.build_metrics(payload, primary, checked,
            export.taxonomy.build_dictionary(package(2023), 2023), 'api.json', None, documents=docs)
        self.assertEqual([m['query_name'] for m in output['metrics']], ['ProfitLoss'])
        self.assertTrue(any(e['reason'] == 'unsupported_numeric_context' and 'Ambiguous unit' in e['detail']
                            for e in audit['excluded_entries']))

    def test_loader_downloads_once_and_offline_rejects_tampered_cache(self):
        _, report, _ = fixture()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with patch.object(documents.sec, 'SecClient') as client, redirect_stdout(io.StringIO()):
                client.return_value.get.return_value = report
                loaded, path = documents.ReportLoader(cache, 'Test test@example.com')(REPORT_URL, cache / 'missing.htm')
                client.return_value.get.assert_called_once_with(REPORT_URL)
            self.assertEqual(loaded, report)
            offline = documents.ReportLoader(cache, offline=True)
            with patch.object(documents.sec, 'SecClient', side_effect=AssertionError('No network')):
                self.assertEqual(offline(REPORT_URL, cache / 'missing.htm'), (report, path))
                Path(path).write_bytes(report + b' ')
                with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
                    offline(REPORT_URL, cache / 'missing.htm')
                with self.assertRaisesRegex(ValueError, 'not cached'):
                    offline(REPORT_URL.replace('report.htm', 'other.htm'), cache / 'missing.htm')

    def test_complete_runner_exports_only_metrics_with_external_provenance(self):
        primary, report, payload = fixture()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, response, report_path = root / 'primary.htm', root / 'api.json', root / 'report.htm'
            source.write_bytes(primary)
            report_path.write_bytes(report)
            (root / 'report-source.json').write_text(json.dumps({'sec_url': REPORT_URL, 'sha256': documents.sha(report)}))
            response.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                                           'source_sha256': documents.sha(primary), 'response': payload}))
            tax = root / 'tax'
            tax.mkdir()
            (tax / 'us-gaap-2023.zip').write_bytes(package(2023))
            output = root / 'out'
            before = {p: p.read_bytes() for p in (source, response, report_path)}
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = runner.main(['--filing', str(source), '--xbrl-json', str(response), '--year', '2023',
                                    '--output-dir', str(output), '--taxonomy-cache', str(tax),
                                    '--sec-cache', str(root / 'cache'), '--offline'])
            self.assertEqual(code, 0)
            self.assertEqual([p.name for p in output.iterdir()], ['primary_2023_api_metrics_with_values.json'])
            result = json.loads((output / 'primary_2023_api_metrics_with_values.json').read_text())
            provenance = result['verification']['source']['documents']
            self.assertEqual(provenance['report1']['sec_url'], REPORT_URL)
            self.assertEqual(provenance['report1']['sha256'], documents.sha(report))
            self.assertEqual(result['verification']['unique_values'], 3)
            self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == '__main__':
    unittest.main()
