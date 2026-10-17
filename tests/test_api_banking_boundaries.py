"""Banking layout regressions: table headings, page indexes and report scopes."""
import os
from pathlib import Path
import re
import unittest

from sec_disclosure.table_extraction import api_filing_documents as filing_docs
from sec_disclosure.table_extraction import extract_item8_xbrl_api as api
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from test_api_filing_documents import document, fact, RESOURCES, resolve, page_fixture


def layout(body):
    doc = ix.Document(document(body, RESOURCES), 'filing.htm')
    return doc, ix.Layout(doc)


def report_page(number, body):
    return (f'<div id="p{number}"/><hr style="page-break-after:always"/>' + body
            + f'<div style="position:absolute;bottom:0"><div>{number} EX</div></div>')


def hierarchy_fixture():
    primary = document('<h1>Item 1. Business</h1><p>' + fact(1, 'business') + '</p>'
        '<h1>Item 1A. Risk Factors</h1><p>The information in the Annual Report under '
        '“MD&amp;A – Risk Factors,” is incorporated herein by reference.</p>'
        '<h1>Item 7. Discussion</h1><p>The information required by this Item is set forth in the MD&amp;A '
        'and Notes 1 and 3 of the Notes to Consolidated Financial Statements in the Annual Report, '
        'which portions are incorporated herein by reference.</p>'
        '<h1>Item 8. Financial Statements</h1><p>Reference is made to Item 15 for a detailed listing '
        'of the financial statements, which are incorporated herein by reference.</p>'
        '<h1>Item 9. Accountants</h1><h1>Item 15. Exhibits and Financial Statements</h1>'
        '<p>The statements are incorporated by reference as indicated below. Page numbers refer to pages '
        'of the Annual Report for Financial Statements and Schedules.</p><table>'
        '<tr><td>Consolidated Income Statement</td><td>9</td></tr>'
        '<tr><td>Consolidated Balance Sheet</td><td>10</td></tr>'
        '<tr><td>Consolidated Statement of Cash Flows</td><td>11</td></tr>'
        '<tr><td>Consolidated Statement of Changes in Equity</td><td>12</td></tr>'
        '<tr><td>Notes to Consolidated Financial Statements</td><td>13-15</td></tr>'
        '<tr><td>Report of Independent Registered Public Accounting Firm</td><td>16</td></tr></table>'
        '<table><tr><td>13</td><td><a href="report.htm">Annual Report</a></td></tr></table>', RESOURCES)
    def group(title, bold=True):
        return '<tr><td>' + ('<b>' if bold else '') + title + ':' + ('</b>' if bold else '') + '</td></tr>'
    def entry(title, page, anchor=None, bold=False):
        return ('<tr><td>' + ('<b>' if bold else '') + title + ('</b>' if bold else '')
                + f'</td><td><a href="#{anchor or "p" + str(page)}">{page}</a></td></tr>')
    toc1 = ('<table>' + entry('Financial Summary', 1, bold=True)
        + group('Management’s Discussion and Analysis of Financial Condition and Results of Operations')
        + group('Results of Operations', False) + entry('General', 2) + entry('Risk Factors', 3)
        + entry('Recent Accounting Developments', 5) + group('Supplemental Information', False)
        + entry('Supplement', 6) + entry('Management Report', 7, bold=True)
        + entry('Audit Report', 8, bold=True) + '</table>')
    toc2 = ('<table>' + group('Financial Statements') + entry('Consolidated Income Statement', 9)
        + entry('Consolidated Balance Sheet', 10) + entry('Consolidated Statement of Cash Flows', 11)
        + entry('Consolidated Statement of Changes in Equity', 12)
        + group('Notes to Consolidated Financial Statements')
        + entry('Note 1 – First', 13) + entry('Note 2 – Second', 13, 'second')
        + entry('Note 3 – Third', 15) + entry('Auditor Opinion', 16, bold=True)
        + entry('Glossary', 17, bold=True) + '</table>')
    sections = {1: ('Financial Summary', 'outside'), 2: ('General', 'md'), 3: ('Risk Factors', 'risk'),
        4: ('Risk continues', 'risk_more'), 5: ('Recent Accounting Developments', 'accounting'),
        6: ('Supplement', 'supplement'), 7: ('Management Report', 'control'), 8: ('Audit Report', 'audit'),
        9: ('Consolidated Income Statement', 'income'), 10: ('Consolidated Balance Sheet', 'balance'),
        11: ('Consolidated Statement of Cash Flows', 'cash'), 12: ('Consolidated Statement of Changes in Equity', 'equity'),
        13: ('Note 1–First', 'note1'), 14: ('Note 2 continued', 'note2_more'),
        15: ('Note 3–Third', 'note3'), 16: ('Auditor Opinion', 'opinion'), 17: ('Glossary', 'after')}
    report = toc1 + toc2
    for number, (title, identifier) in sections.items():
        body = f'<div><b>{title}</b></div><p>{fact(number, identifier)}</p>'
        if number == 13:
            body += '<div id="second"/><div><b>Note 2–Second</b></div><p>' + fact(20, 'note2') + '</p>'
        report += report_page(number, body)
    return primary, document(report)


class BankingBoundaryTests(unittest.TestCase):
    def test_multirow_table_titles_and_self_linked_inventory_are_body_headings(self):
        body = ('<h1>Item 1. Business</h1><h1>Item 1A. Risks</h1>'
            '<table><tr><td><b>Item 7. Company and subsidiaries</b></td></tr>'
            '<tr><td>Management discussion</td></tr><tr><td>Table of Contents</td></tr></table>'
            '<p>' + fact(7, 'seven') + '</p><div id="eight"/>'
            '<table><tr><td><b><a href="#eight">Item 8. Financial Statements</a></b></td></tr>'
            + ''.join(f'<tr><td>Statement {n}</td><td>{n}</td></tr>' for n in range(3))
            + '</table><p>' + fact(8, 'eight-value') + '</p><h1>Item 9. Accountants</h1>')
        doc, scope = layout(body)
        scope.validate(('1', '1A', '7', '8'))
        self.assertEqual(scope.membership(doc.ids['seven'])[2], ['7'])
        self.assertEqual(scope.membership(doc.ids['eight-value'])[2], ['8'])
        # A forward link is only a contents entry, not a body heading.
        doc, scope = layout(body.replace('href="#eight"', 'href="#later"') + '<div id="later"/>')
        with self.assertRaisesRegex(ValueError, 'Cannot resolve Items'):
            scope.validate(('8',))

    def test_numbered_cross_reference_index_retains_wrapped_ranges_and_overlap(self):
        toc = ('<div><b>FORM 10-K CROSS-REFERENCE INDEX</b></div><table>'
            '<tr><td>Item Number</td><td>Page</td></tr>'
            '<tr><td>1.</td><td>Business</td><td>4–5,</td></tr>'
            '<tr><td/><td/><td>7,</td></tr><tr><td/><td/><td>8</td></tr>'
            '<tr><td>1A.</td><td>Risks</td><td>6</td></tr>'
            '<tr><td>7.</td><td>Discussion</td><td>7–9</td></tr>'
            '<tr><td>8.</td><td>Statements</td><td>10–11</td></tr></table>')
        body = toc + ''.join(report_page(n, '<p>' + fact(n, f'f{n}') + '</p>') for n in range(4, 12))
        doc, scope = layout(body)
        scope.validate(('1', '1A', '7', '8'))
        self.assertEqual(scope.references[0]['pages'], [4, 5, 7, 8])
        self.assertEqual(scope.membership(doc.ids['f8'])[2], ['1', '7'])
        self.assertEqual(scope.membership(doc.ids['f6'])[2], ['1A'])
        with self.assertRaisesRegex(ValueError, 'Cannot resolve Items'):
            layout(body.replace('FORM 10-K CROSS-REFERENCE INDEX', 'Other numbered list'))[1].validate(('8',))
        with self.assertRaisesRegex(ValueError, 'not uniquely verified'):
            layout(body.replace('8 EX', '7 EX'))[1].validate(('1',))
        with self.assertRaisesRegex(ValueError, 'Invalid printed-page range'):
            layout(body.replace('<td>8</td>', '<td>8,</td>'))
        _, proxy_scope = layout(body.replace('</table>', '<tr><td>10.</td><td>Directors</td>'
                                            '<td>Proxy Statement</td></tr></table>', 1))
        proxy_scope.validate(('1', '1A', '7', '8'))
        with self.assertRaisesRegex(ValueError, 'unverified page reference'):
            proxy_scope.validate(('10',))

    def test_prefixed_cross_reference_index_keeps_existing_page_parser(self):
        toc = ('<div><b>FORM 10-K CROSS-REFERENCE INDEX</b></div><table>'
            '<tr><td>Item Number</td><td>Item</td><td>Page</td></tr>'
            '<tr><td>Item 1.</td><td>Business</td><td>Page 4</td></tr>'
            '<tr><td/><td>Additional business information</td><td>Page 5</td></tr>'
            '<tr><td>Item 1A.</td><td>Risks</td><td>Page 6</td></tr>'
            '<tr><td>Item 7.</td><td>Discussion</td><td>Pages 7-8</td></tr>'
            '<tr><td>Item 8.</td><td>Statements</td><td>Pages 9-10</td></tr></table>')
        doc, scope = layout(toc + ''.join(report_page(n, '<p>' + fact(n, f'f{n}') + '</p>')
                                         for n in range(4, 11)))
        scope.validate(('1', '1A', '7', '8'))
        self.assertEqual(scope.references[0]['pages'], [4, 5])
        self.assertEqual({r['method'] for r in scope.references}, {'item_page_index'})
        for page, item in [(4, '1'), (5, '1'), (6, '1A'), (7, '7'), (8, '7'), (9, '8'), (10, '8')]:
            self.assertEqual(scope.membership(doc.ids[f'f{page}'])[2], [item])

    def test_report_before_page_reference_and_footer_tables(self):
        primary, report, _ = page_fixture()
        old = ('The information under the heading “Management Discussion” on pages 5-6 of the Registrant’s '
               '2023 Annual Report to Shareholders is incorporated herein by reference.').encode()
        primary = primary.replace(old, b'Information in response to this Item can be found in the Annual Report on pages 5 to 6 '
                                  b'under the heading "Management Discussion." That information is incorporated into this report by reference.')
        for number in range(2, 8):
            report = report.replace(f'<p style="text-align:center">{number}</p>'.encode(),
                f'<div style="bottom:0;position:absolute"><table><tr><td>{number} Example 2023 Annual Report</td></tr></table></div>'.encode())
        docs = resolve(primary, report)
        reference = next(r for r in docs['report1']['layout'].references if r['item'] == '7')
        self.assertEqual(reference['pages'], [5, 6])
        self.assertEqual(docs['report1']['layout'].membership(docs['report1']['doc'].ids['outside_after'])[2], [])

    def test_hierarchical_page_links_and_delegated_statement_inventory(self):
        primary, report = hierarchy_fixture()
        docs = resolve(primary, report)
        doc, scope = docs['report1']['doc'], docs['report1']['layout']
        def members(identifier):
            return set(scope.membership(doc.ids[identifier])[2])
        self.assertEqual(members('outside'), set())
        self.assertEqual(members('md'), {'7'})
        self.assertEqual(members('risk'), {'1A', '7'})
        self.assertEqual(members('risk_more'), {'1A', '7'})
        self.assertEqual(members('accounting'), {'7'})
        self.assertEqual(members('control'), set())
        self.assertEqual(members('income'), {'8'})
        self.assertEqual(members('note1'), {'7', '8'})
        self.assertEqual(members('note2'), {'8'})
        self.assertEqual(members('note2_more'), {'8'})
        self.assertEqual(members('note3'), {'7', '8'})
        self.assertEqual(members('after'), set())

    def test_bad_report_hierarchy_links_pages_or_inventory_fail_closed(self):
        primary, report = hierarchy_fixture()
        variants = [
            (primary, report.replace(b'href="#p3"', b'href="#p5"')),
            (primary, report.replace(b'id="p3"', b'id="absent"')),
            (primary, report.replace(b'3 EX', b'2 EX')),
            (primary, report.replace(b'<b>Risk Factors</b>', b'<b>Different Heading</b>')),
            (primary, report.replace(b'<b>Notes to Consolidated Financial Statements:</b>', b'<b>Other Notes:</b>')),
            (primary, report.replace(b'href="#second"', b'href="#p13"')),
            (primary, report.replace(b'13 EX', b'Unnumbered page')),
            (primary.replace(b'<td>13-15</td>', b'<td>11-15</td>'), report),
            (primary.replace(b'<td>13-15</td>', b'<td>unknown</td>'), report),
        ]
        for p, r in variants:
            with self.subTest(variant=variants.index((p, r))), self.assertRaises(ValueError):
                resolve(p, r)

    def test_decorated_page_text_outside_bottom_footer_is_not_a_page(self):
        doc, scope = layout('<p>Revenue 22</p><hr style="page-break-after:always"/><p>' + fact(2) + '</p>')
        self.assertEqual(scope.footers, [])


@unittest.skipUnless(os.environ.get('BANK_API_INTEGRATION') == '1', 'Requires cached FY2025 bank filings')
class CachedBankingBoundaryTests(unittest.TestCase):
    def load(self, path):
        url = 'https://www.sec.gov/Archives/edgar/data/' + path
        cache = Path('data/sec_cache/api_metrics') / api.digest({'htm-url': url})
        filing = cache / 'original_filing.htm'
        if not filing.exists():
            self.skipTest('Original filing is not cached')
        return filing_docs.resolve_documents(filing.read_bytes(), str(filing), {'htm-url': url},
            ('1', '1A', '7', '8'), filing_docs.ReportLoader(Path('data/sec_cache'), offline=True))

    def page_members(self, scope, page):
        position, = [pos for pos, label in scope.footers if label == str(page)]
        return set(scope.membership(scope.doc.nodes[position - 1])[2])

    def test_bac_body_table_headings(self):
        docs = self.load('70858/000007085826000157/bac-20251231.htm')
        scope = docs['primary']['layout']
        self.assertEqual(sum(h['item'] == '7' for h in scope.headings), 1)
        self.assertEqual(sum(h['item'] == '8' for h in scope.headings), 1)
        for item in ('7', '8'):
            heading = next(h for h in scope.headings if h['item'] == item)
            self.assertEqual(scope.membership(scope.doc.nodes[heading['position'] + 1])[2], [item])
        self.assertTrue(any(scope.membership(n)[2] == ['8'] for n in scope.doc.fact_nodes
                            if n.get('name') == 'us-gaap:Assets' and not scope.doc.hidden[n]))

    def test_citigroup_wrapped_item_one_ranges(self):
        docs = self.load('831001/000083100126000011/c-20251231.htm')
        scope = docs['primary']['layout']
        pages = next(r['pages'] for r in scope.references if r['item'] == '1')
        self.assertTrue({129, 160, 164, 299, 300} <= set(pages))
        self.assertEqual(self.page_members(scope, 299), {'1'})
        self.assertEqual(self.page_members(scope, 134), {'8'})

    def test_capital_one_self_linked_item_eight(self):
        docs = self.load('927628/000092762826000024/cof-20251231.htm')
        scope = docs['primary']['layout']
        self.assertEqual(sum(h['item'] == '8' for h in scope.headings), 1)
        assets = [n for n in scope.doc.fact_nodes if n.get('name') == 'us-gaap:Assets' and not scope.doc.hidden[n]]
        self.assertTrue(assets)
        self.assertTrue(all('8' in scope.membership(n)[2] for n in assets))

    def test_us_bancorp_explicit_report_pages(self):
        docs = self.load('36104/000003610426000011/usb-20251231.htm')
        scope = docs['report1']['layout']
        for page, members in [(21, set()), (22, {'7'}), (59, {'7'}), (60, {'8'}),
                              (134, {'8'}), (135, {'1A'}), (150, {'1A'}), (151, set())]:
            self.assertEqual(self.page_members(scope, page), members, page)

    def test_bny_note_boundaries_do_not_assign_the_whole_notes_to_item_seven(self):
        docs = self.load('1390777/000139077726000033/bk-20251231.htm')
        scope = docs['report1']['layout']
        self.assertEqual(self.page_members(scope, 2), set())
        self.assertEqual(self.page_members(scope, 77), {'1A', '7'})
        self.assertEqual(self.page_members(scope, 110), {'7'})
        self.assertEqual(self.page_members(scope, 118), set())
        self.assertEqual(self.page_members(scope, 121), {'8'})
        self.assertEqual(self.page_members(scope, 199), set())
        for note, members in [(5, {'7', '8'}), (6, {'8'}), (13, {'7', '8'}), (14, {'8'})]:
            candidates = [node for _, title, node in scope.blocks
                          if re.match(rf'^Note\s+{note}\s*[–—-]', title) and len(title) < 180]
            self.assertEqual(len(candidates), 1, note)
            self.assertEqual(set(scope.membership(candidates[0])[2]), members, note)


if __name__ == '__main__':
    unittest.main()
