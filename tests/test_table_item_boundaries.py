"""Table Item ranges share the unchanged text preprocessor's section selector."""
import unittest
from unittest.mock import patch

from sec_disclosure.extraction import sec_10k_extractor as text
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from sec_disclosure.table_extraction import item_boundaries
from test_extract_10k_tables_xbrl import filing, footer, number, table


def layout(body):
    doc = ix.Document(filing(body), 'original.htm')
    return doc, ix.Layout(doc)


class TableItemBoundaryTests(unittest.TestCase):
    def test_calls_the_text_selector_and_keeps_tables_before_and_after_narrative(self):
        body = '<h1>Item 7. Discussion</h1>' + table(number('1', 'first'))
        body += '<p>Management discusses revenue and its business performance.</p>'
        body += table(number('2', 'last')) + '<h1>Item 7A. Market Risk</h1>'
        body += table(number('3', 'risk')) + '<h1>Item 8. Financial Statements</h1>'
        body += '<p>Financial statements follow.</p>' + table(number('4', 'statement'))
        body += '<h1>Item 9. Accountants</h1>' + table(number('5', 'outside'))
        doc = ix.Document(filing(body), 'original.htm')
        original_paths = dict(doc.paths)
        with patch.object(text, 'extract_section_blocks', wraps=text.extract_section_blocks) as selector:
            scope = ix.Layout(doc)
        selector.assert_called_once()
        for name in ('first', 'last'):
            self.assertEqual(scope.membership(doc.ids[name])[2], ['7'])
        self.assertEqual(scope.membership(doc.ids['risk'])[2], ['7A'])
        self.assertEqual(scope.membership(doc.ids['statement'])[2], ['8'])
        self.assertEqual(scope.membership(doc.ids['outside'])[2], ['9'])
        self.assertEqual({b['item'] for b in scope.item_boundaries}, {'7', '8'})
        self.assertTrue(all(b['method'] == 'text_preprocessing' for b in scope.item_boundaries))
        self.assertEqual(doc.paths, original_paths)
        self.assertEqual(doc.data, filing(body))

    def test_duplicate_item_uses_the_same_longest_section_as_text(self):
        body = '<h1>Item 7. Discussion</h1><p>Brief.</p>' + table(number('1', 'short'))
        body += '<h1>Item 7A. Market Risk</h1><p>Risk.</p>'
        body += '<h1 id="body7">Item 7. Discussion</h1><p>' + 'Detailed management discussion. ' * 8 + '</p>'
        body += table(number('2', 'long')) + '<h1>Item 7A. Market Risk</h1>'
        body += '<h1>Item 8. Statements</h1>'
        selected = text.extract_section_blocks(text.html_to_blocks(filing(body).decode()), items=('7',))
        self.assertIn('Detailed management discussion.', selected['7'][0].text)
        doc, scope = layout(body)
        scope.validate(('7',))
        self.assertEqual(scope.membership(doc.ids['long'])[2], ['7'])
        self.assertNotIn('7', scope.membership(doc.ids['short'])[2])
        self.assertEqual(next(b for b in scope.item_boundaries if b['item'] == '7')['start'],
                         doc.order[doc.ids['body7']])

    def test_unh_heading_and_statement_index_in_one_container(self):
        body = '<h1>Item 7A. Market Risk</h1><p>Risk.</p>'
        body += ('<div id="item8"><span style="font-weight:700">'
                 'ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</span>'
                 '<table><tr><td><div><a href="#balance">Balance sheets</a></div></td>'
                 '<td><div>38</div></td></tr></table></div>')
        body += '<p>These statements present our financial position.</p>'
        body += '<div id="balance"/>' + table(number('4', 'balance_value')) + footer(38)
        body += '<h1>Item 9. Accountants</h1><p>None.</p>'
        doc, scope = layout(body)
        scope.validate(('8',))
        self.assertEqual(scope.membership(doc.ids['balance_value']), ('38', '8', ['8']))
        boundary = next(b for b in scope.item_boundaries if b['item'] == '8')
        self.assertEqual(boundary['start'], doc.order[doc.ids['item8']])

    def test_compact_layout_table_uses_text_heading_recovery(self):
        body = ('<table><tr><td><b>Item 7. Discussion</b></td></tr>'
                '<tr><td>Table of Contents</td></tr></table>'
                '<p>Our discussion explains current year performance.</p>')
        body += table(number('1', 'discussion')) + '<h1>Item 8. Statements</h1>'
        doc, scope = layout(body)
        self.assertEqual(scope.membership(doc.ids['discussion'])[2], ['7'])
        self.assertEqual(scope.item_boundaries[0]['item'], '7')

    def test_table_only_sections_keep_verified_table_heading_fallback(self):
        doc, scope = layout('<h1>Item 8. Statements</h1>' + table(number('1', 'value')))
        self.assertEqual(scope.item_boundaries, [])
        self.assertEqual(scope.membership(doc.ids['value'])[2], ['8'])

    def test_unstyled_complete_sections_use_the_text_boundary(self):
        body = '<p>Item 7. Discussion</p><p>Our narrative explains this year.</p>'
        body += table(number('1', 'discussion')) + '<p>Item 7A. Market Risk</p>'
        body += table(number('2', 'risk')) + '<h1>Item 8. Statements</h1>'
        doc, scope = layout(body)
        self.assertEqual(scope.membership(doc.ids['discussion'])[2], ['7'])
        self.assertEqual(scope.membership(doc.ids['risk'])[2], ['7A'])

    def test_items_10_to_14_are_not_promoted_to_item_1(self):
        body = '<h1>Item 1. Business</h1><p>Business narrative.</p><h1>Item 1A. Risks</h1>'
        for number_ in range(10, 15):
            body += f'<h1>Item {number_}. Governance</h1>' + table(number('1', f'item{number_}'))
        doc, scope = layout(body)
        for number_ in range(10, 15):
            self.assertEqual(scope.membership(doc.ids[f'item{number_}'])[2], [str(number_)])
        self.assertEqual(sum(h['item'] == '1' for h in scope.headings), 1)

    def test_detached_report_uses_explicit_membership_after_headings_are_cleared(self):
        doc, scope = layout('<h1>Item 8. Statements</h1><p>Statements.</p>'
                            + table(number('1', 'value')) + '<h1>Item 9. Accountants</h1>')
        scope.headings = []  # Incorporated reports get only verified reference ranges.
        self.assertEqual(scope.membership(doc.ids['value'])[2], [])

    def test_structure_locations_match_canonical_blocks_with_comments_and_self_closing_tags(self):
        data = filing('<div><!-- comment --><p>Item 7. Discussion</p><br/>'
                      '<p>Performance was strong.</p>' + table() + '</div><h1>Item 8. Statements</h1>')
        doc = ix.Document(data, 'original.htm')
        parser = item_boundaries._read(item_boundaries._Blocks(doc), data.decode())
        self.assertEqual(parser.blocks, text.html_to_blocks(data.decode()))
        self.assertEqual(len(parser.locations), len(parser.blocks))


if __name__ == '__main__':
    unittest.main()
