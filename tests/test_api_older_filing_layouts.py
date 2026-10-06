"""Older statement indexes must retain exact Item 8 location evidence."""
import unittest

import extract_10k_tables_xbrl as ix
from test_api_primary_appendix import ENTRIES, appendix
from test_extract_10k_tables_xbrl import filing, footer, number, table


def page_linked_index():
    entries = ENTRIES + [('schedule', 'Schedule II Valuation and Qualifying Accounts', 'F-5')]
    body = ('<h1>Item 1. Business</h1><h1>Item 1A. Risk Factors</h1>'
            '<h1>Item 7. Management Discussion</h1><h1>Item 8. Financial Statements</h1>'
            '<p>The information required by this Item is set forth in our Consolidated Financial '
            'Statements and Notes thereto included in this Annual Report on Form 10-K.</p>'
            '<h1>Item 9. Changes in Accountants</h1><h1>Item 15. Exhibits</h1><table id="index">')
    for anchor, title, page in entries:
        dates = (' for the years ended <a href="#income">December 31, 2024</a>'
                 '<a href="#income"> an</a><a href="#income">d</a>'
                 '<a href="#income"> December 31, 2023</a>') if anchor != 'notes' else ''
        body += (f'<tr><td><a href="#{anchor}">{title}</a>{dates}</td>'
                 f'<td><a href="#{anchor}">{page}</a></td></tr>')
    body += '</table>' + footer('40')
    for anchor, title, page in entries:
        # A title span immediately above a nested table (NVIDIA equity/schedule).
        body += f'<a id="{anchor}"/><div><span style="font-weight:700">{title}</span>'
        body += table(number('1', anchor + '_fact')) + '</div>' + footer(page)
    body += '<h1>Item 16. Form 10-K Summary</h1>' + table(number('99', 'outside')) + footer('F-6')
    return filing(body)


def custom_final_note():
    note = ('<div style="font-weight:700">Note 16. '
            '<ix:nonNumeric name="ex:DividendsPaidTextBlock" contextRef="annual" '
            'id="custom_note" continuedAt="custom_end">Dividends</ix:nonNumeric></div>'
            '<ix:continuation id="custom_end">' + table(number('5', 'dividends')) + '</ix:continuation>')
    return appendix(ending=None).replace(footer('F-4').encode(), note.encode() + footer('F-4').encode())


class OlderFilingLayoutTests(unittest.TestCase):
    def layout(self, data):
        doc = ix.Document(data, 'example.htm')
        layout = ix.Layout(doc)
        layout.validate(('1', '1A', '7', '8'))
        return doc, layout

    def test_date_links_and_fragmented_and_use_verified_page_destination(self):
        doc, layout = self.layout(page_linked_index())
        self.assertEqual(layout.ranges[0]['method'], 'financial_index_page_links')
        for identifier in ('balance_fact', 'income_fact', 'cash_fact', 'notes_fact'):
            self.assertEqual(layout.membership(doc.ids[identifier])[2], ['8'])
        self.assertEqual(layout.membership(doc.ids['schedule_fact'])[2], ['15'])
        self.assertEqual(layout.membership(doc.ids['outside'])[2], ['16'])

    def test_non_date_links_and_conflicting_title_links_are_rejected(self):
        data = page_linked_index()
        mutations = [data.replace(b'>December 31, 2024</a>', b'>Revenue for December 31, 2024</a>', 1),
                     data.replace(b'<a href="#balance">Consolidated Balance Sheets',
                                  b'<a href="#cash">Consolidated Balance Sheets', 1),
                     data.replace(b'<a href="#income"> an</a>', b'<a href="#cash"> an</a>', 1)]
        for malformed in mutations:
            with self.subTest(malformed=mutations.index(malformed)):
                with self.assertRaisesRegex(ValueError, 'Page-linked financial index'):
                    self.layout(malformed)

    def test_page_link_cannot_override_missing_hidden_or_wrong_title_target(self):
        data = page_linked_index()
        mutations = [data.replace(b'<a id="balance"/>', b'<a id="missing"/>'),
                     data.replace(b'<a id="balance"/>', b'<a id="balance" hidden="hidden"/>'),
                     data.replace(b'700">Consolidated Balance Sheets', b'700">Unrelated Section'),
                     data.replace(b'>F-1</a>', b'>F-2</a>', 1),
                     data.replace(b'<a id="balance"/>', b'<a id="balance"/><a name="balance"/>')]
        for malformed in mutations:
            with self.subTest(malformed=mutations.index(malformed)):
                with self.assertRaisesRegex(ValueError, 'Primary appendix'):
                    self.layout(malformed)

    def test_page_index_requires_ordered_targets_and_closing_boundary(self):
        data = page_linked_index()
        mutations = [data.replace(b'<td><a href="#schedule">F-5</a></td>', b'<td>F-5</td>'),
                     data.replace(b'>Schedule II Valuation and Qualifying Accounts</a>', b'>Other Material</a>'),
                     data.replace(footer('F-2').encode(), footer('F-1').encode())]
        for malformed in mutations:
            with self.subTest(malformed=mutations.index(malformed)):
                with self.assertRaises(ValueError):
                    self.layout(malformed)

    def test_anchor_before_page_break_can_cross_only_empty_markup(self):
        data = page_linked_index().replace(
            (footer('40') + '<a id="balance"/>').encode(),
            ('<div style="position:absolute;bottom:0">40</div><a id="balance"/>'
             '<div style="page-break-after:always"/>').encode())
        doc, layout = self.layout(data)
        self.assertEqual(layout.membership(doc.ids['balance_fact'])[2], ['8'])
        with self.assertRaisesRegex(ValueError, 'printed page'):
            self.layout(data.replace(b'<a id="balance"/>', b'<a id="balance"/>Unrelated prose'))

    def test_units_suffix_does_not_change_statement_identity(self):
        data = appendix().replace(b'<h2 id="income">Consolidated Statements of Income</h2>',
                                 b'<h2 id="income">Consolidated Statements of Income (in millions, except share data)</h2>')
        doc, layout = self.layout(data)
        self.assertEqual(layout.membership(doc.ids['income_fact'])[2], ['8'])
        with self.assertRaisesRegex(ValueError, 'target title cannot be verified'):
            self.layout(data.replace(b'(in millions, except share data)', b'(Parent Company Only)'))

    def test_truncated_comprehensive_title_needs_unique_page_and_statement_family(self):
        data = appendix().replace(b'>Consolidated Statements of Income</a>',
            b'>Consolidated Statements of Comprehensive Twelve Months Ended December 31, 2024, 2023, and 2022</a>')
        data = data.replace(b'<h2 id="income">Consolidated Statements of Income</h2>',
                            b'<h2 id="income">Consolidated Statements of Comprehensive Income</h2>')
        doc, layout = self.layout(data)
        self.assertEqual(layout.membership(doc.ids['income_fact'])[2], ['8'])
        for malformed in (data.replace(b'>Consolidated Statements of Comprehensive Income</h2>', b'>Consolidated Statements of Income</h2>'),
                          data.replace(b'>F-2</td>', b'>F-3</td>'),
                          data.replace(b'<h2 id="income">', b'<h2>Consolidated Statements of Comprehensive Loss</h2><h2 id="income">')):
            with self.assertRaisesRegex(ValueError, 'Primary appendix'):
                self.layout(malformed)

    def test_custom_final_numbered_note_follows_continuations_across_pages(self):
        data = custom_final_note().replace(b'<ix:continuation id="custom_end">',
            (footer('F-4') + '<ix:continuation id="custom_end">').encode())
        data = data.replace(b'</ix:continuation>' + footer('F-4').encode() + b'</body>',
                            b'</ix:continuation>' + footer('F-5').encode() + b'</body>')
        doc, layout = self.layout(data)
        self.assertEqual(layout.membership(doc.ids['dividends'])[2], ['8'])
        self.assertEqual(layout.ranges[0]['end_method'], 'final_note_continuation')

    def test_custom_note_requires_styled_numbered_heading_and_matching_title(self):
        data = custom_final_note()
        for malformed in (data.replace(b'Note 16. ', b'Other material. '),
                          data.replace(b'<div style="font-weight:700">Note 16.', b'<div>Note 16.'),
                          data.replace(b'>Dividends</ix:nonNumeric></div>', b'>Dividends</ix:nonNumeric> Unrelated information</div>')):
            with self.assertRaisesRegex(ValueError, 'Unverified content'):
                self.layout(malformed)

    def test_custom_note_does_not_hide_broken_chains_or_trailing_content(self):
        data = custom_final_note()
        for malformed in (data.replace(b'continuedAt="custom_end"', b'continuedAt="missing"'),
                          data.replace(b'id="custom_end"', b'id="custom_end" continuedAt="custom_end"'),
                          data.replace(b'</body>', b'<p>Unrelated appendix.</p></body>'),
                          data.replace(b'</body>', number('9', 'trailing_fact').encode() + b'</body>')):
            with self.assertRaisesRegex(ValueError, '(?:appendix|continuation)'):
                self.layout(malformed)


if __name__ == '__main__':
    unittest.main()
