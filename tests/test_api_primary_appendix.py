"""Same-filing Item 8 appendices, nested headings and inert navigation anchors."""
import unittest

from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from test_extract_10k_tables_xbrl import filing, footer, number, table


REFERENCE = ('The consolidated financial statements listed under Item 15(a)(1) '
             'are filed as part of this Form 10-K.')
ENTRIES = [('balance', 'Consolidated Balance Sheets', 'F-1'),
           ('income', 'Consolidated Statements of Income', 'F-2'),
           ('cash', 'Consolidated Statements of Cash Flows', 'F-3'),
           ('notes', 'Notes to Consolidated Financial Statements', 'F-4')]


def appendix(*, reference=REFERENCE, ending='schedule', nested=False):
    index = '<table id="index">'
    # Auditor identity tags are not financial values and may occur in indexes.
    index += '<tr><td>Auditor <ix:nonNumeric name="ex:AuditorFirmId" contextRef="annual">42</ix:nonNumeric></td></tr>'
    for anchor, title, page in ENTRIES:
        index += f'<tr><td><a href="#{anchor}">{title}</a></td><td>{page}</td></tr>'
    index += '</table>'
    body = '<h1>Item 1. Business</h1><p>Business.</p><h1>Item 1A. Risk Factors</h1>'
    body += '<h1>Item 7. Management Discussion</h1><h1>Item 8. Financial Statements</h1>'
    body += '<p>' + reference + '</p><h1>Item 9. Changes in Accountants</h1>'
    body += '<h1>Item 15. Exhibits and Financial Statement Schedules</h1>' + index
    body += '<h1>Item 16. Form 10-K Summary</h1>' + table(number('99', 'outside')) + footer('40')
    for anchor, title, page in ENTRIES:
        if anchor == 'balance' and nested:
            body += '<h2>Item 1. Financial Statements</h2>'
        body += f'<h2 id="{anchor}">{title}</h2>'
        if anchor == 'notes':
            body += '<ix:nonNumeric name="us-gaap:SubsequentEventsTextBlock" contextRef="annual" continuedAt="endnote">Subsequent Events</ix:nonNumeric>'
            body += '<ix:continuation id="endnote">' + table(number('4', 'notes_fact')) + '</ix:continuation>'
        else:
            body += table(number('1', anchor + '_fact'))
        body += footer(page)
    if ending:
        title = 'Schedule II - Valuation and Qualifying Accounts' if ending == 'schedule' else 'INDEX TO EXHIBITS'
        body += '<h2>' + title + '</h2>' + table(number('98', 'after_notes')) + footer('F-5')
    return filing(body)


class PrimaryAppendixTests(unittest.TestCase):
    def layout(self, data):
        doc = ix.Document(data, 'example.htm')
        return doc, ix.Layout(doc)

    def test_statement_index_after_item16_includes_notes_and_excludes_other_sections(self):
        for end in ('schedule', 'exhibits', None):
            with self.subTest(end=end):
                doc, layout = self.layout(appendix(ending=end))
                layout.validate(('1', '1A', '7', '8'))
                self.assertEqual(layout.ranges[0]['method'], 'primary_appendix_index')
                self.assertEqual(layout.membership(doc.ids['outside'])[2], ['16'])
                for identifier in ('balance_fact', 'income_fact', 'cash_fact', 'notes_fact'):
                    self.assertEqual(layout.membership(doc.ids[identifier])[2], ['8'])
                if end:
                    self.assertEqual(layout.membership(doc.ids['after_notes'])[2], ['16'])

    def test_nested_financial_heading_is_not_a_duplicate_business_item(self):
        doc, layout = self.layout(appendix(nested=True))
        layout.validate(('1', '1A', '7', '8'))
        self.assertEqual(sum(h['item'] == '1' for h in layout.headings), 1)
        self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['8'])
        data = appendix(nested=True).replace(b'Item 1. Financial Statements', b'Item 1. Business')
        _, layout = self.layout(data)
        with self.assertRaisesRegex(ValueError, 'Multiple body headings'):
            layout.validate(('1', '8'))

    def test_explicit_item8_schedule_incorporation_requires_its_own_index(self):
        reference = ('The consolidated financial statements and the Financial Statement Schedule listed '
                     'in the index appearing under Part IV, Item 15(a)(2) of this Form 10-K '
                     'are filed as part of this Form 10-K and incorporated herein by reference in Item 8.')
        data = appendix(reference=reference)
        index = ('<table><tr><td><a href="#schedule">Schedule II - Valuation and Qualifying Accounts '
                 'for the years ended December 31, 2025</a></td><td>F-5</td></tr></table>')
        data = data.replace(b'<h1>Item 16.', index.encode() + b'<h1>Item 16.')
        data = data.replace(b'<h2>Schedule II', b'<h2 id="schedule">Schedule II')
        data = data.replace(table(number('98', 'after_notes')).encode(),
            ('<ix:nonNumeric name="ex:ScheduleOfValuationAndQualifyingAccountsDisclosureTextBlock" '
             'contextRef="annual">' + table(number('98', 'after_notes')) + '</ix:nonNumeric>').encode())
        doc, layout = self.layout(data)
        self.assertEqual(layout.membership(doc.ids['after_notes'])[2], ['8'])
        self.assertEqual(layout.ranges[0]['end_method'], 'explicit_schedule_continuation')
        with self.assertRaisesRegex(ValueError, 'schedule whose index destination'):
            self.layout(data.replace(b'href="#schedule"', b'href="#unknown"'))

    def test_explicit_internal_page_range_and_prefixes(self):
        for sentence in ('The information required by this item is included in this Annual Report on pages F-1 through F-4.',
                         'Our consolidated financial statements appear at pages F-1 through F-4 of this Annual Report.'):
            for prefix in ('F-', 'FS-'):
                data = appendix(reference=sentence).replace(b'F-', prefix.encode())
                doc, layout = self.layout(data)
                self.assertEqual(layout.references[0]['method'], 'primary_appendix_pages')
                self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['8'])
                self.assertEqual(layout.membership(doc.ids['after_notes'])[2], ['16'])

    def test_page_reference_rejects_missing_duplicate_or_discontinuous_pages(self):
        data = appendix(reference='The information required by this item is included in this Annual Report on pages F-1 through F-4.')
        mutations = [data.replace(footer('F-2').encode(), b''),
                     data.replace(footer('F-3').encode(), footer('F-2').encode()),
                     data.replace(footer('F-2').encode(), footer('F-2').encode() + '<div style="page-break-after:always"/>'.encode())]
        for malformed in mutations:
            with self.subTest(malformed=mutations.index(malformed)):
                with self.assertRaisesRegex(ValueError, 'pages are missing, duplicate or discontinuous'):
                    self.layout(malformed)

    def test_no_reference_or_negation_does_not_promote_appendix(self):
        for reference in ('No financial statements are incorporated here.', REFERENCE.replace('are filed', 'are not filed')):
            doc, layout = self.layout(appendix(reference=reference))
            self.assertFalse(layout.ranges)
            self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['16'])

    def test_index_requires_correct_unique_links_pages_and_titles(self):
        data = appendix()
        mutations = [data.replace(b'href="#balance"', b'href="#missing"'),
                     data.replace(b'<h2 id="balance">', b'<a id="balance"/><h2 id="balance">'),
                     data.replace(b'<td>F-1</td>', b'<td>F-2</td>'),
                     data.replace(b'<h2 id="balance">Consolidated Balance Sheets', b'<h2 id="balance">An Unrelated Table')]
        for malformed in mutations:
            with self.subTest(malformed=mutations.index(malformed)):
                with self.assertRaisesRegex(ValueError, 'Primary appendix|Duplicate XML ID'):
                    self.layout(malformed)

    def test_final_note_cannot_hide_trailing_content_or_broken_continuations(self):
        data = appendix(ending=None)
        for malformed in (data.replace(b'continuedAt="endnote"', b'continuedAt="missing"'),
                          data.replace(b'id="endnote"', b'id="endnote" continuedAt="endnote"'),
                          data.replace(b'</body>', b'<p>Unrelated appendix.</p></body>'),
                          data.replace(b'</body>', b'<div style="-sec-ix-hidden:other"/></body>'),
                          data.replace(footer('F-4').encode(),
                              b'<ix:nonNumeric name="ex:UnrelatedTextBlock" contextRef="annual">Other section</ix:nonNumeric>'
                              + footer('F-4').encode()),
                          data.replace(b'</body>', number('9', 'late').encode() + b'</body>')):
            with self.subTest(malformed=malformed[-130:]):
                with self.assertRaisesRegex(ValueError, '(?:appendix|continuation)'):
                    self.layout(malformed)

    def test_empty_named_hash_anchors_are_headings_but_navigation_is_not(self):
        body = '<table><tr><td><b>Item 8.</b></td><td><b><a id="item8" href="#"/>Financial Statements</b></td></tr></table>'
        _, layout = self.layout(filing(body + number()))
        self.assertEqual([h['item'] for h in layout.headings], ['8'])
        for replacement in ('<a id="item8" href="#body8"/>', '<a href="#"/>', '<a id="item8" href="#">Go</a>'):
            _, layout = self.layout(filing(body.replace('<a id="item8" href="#"/>', replacement) + number()))
            self.assertFalse(layout.headings)


if __name__ == '__main__':
    unittest.main()
