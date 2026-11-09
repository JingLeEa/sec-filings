"""Table Item ranges share the unchanged text preprocessor's section selector."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sec_disclosure.extraction import sec_10k_extractor as text
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from sec_disclosure.table_extraction import item_boundaries
from test_extract_10k_tables_xbrl import filing, footer, number, table


def layout(body):
    doc = ix.Document(filing(body), 'original.htm')
    return doc, ix.Layout(doc)


class TableItemBoundaryTests(unittest.TestCase):
    def test_referenced_report_uses_shared_fallback_only_after_deterministic_recovery_fails(self):
        reference = {'item': '8', 'section_paths': [['Financial Statements']],
                     'reference_text': 'Financial statements incorporated by reference.'}
        options = text.item_recovery_options('cache', enabled=True)
        for tag, expected_calls in (('h1', 0), ('p', 1)):
            with self.subTest(tag=tag):
                body = f'<{tag}>Financial Statements</{tag}>' + table(number('10', 'inside'))
                body += f'<{tag}>Outside</{tag}>' + table(number('20', 'outside'))
                doc = ix.Document(filing(body), 'report.htm')
                scope = ix.Layout(doc, resolve_references=False)

                def recover(source, raw, events, paths, args):
                    start = next(i for i, event in enumerate(events) if event.text == 'Financial Statements')
                    end = next(i for i, event in enumerate(events) if event.text == 'Outside')
                    return SimpleNamespace(nodes=[{'title': 'Financial Statements',
                        'key': text.report_title_key('Financial Statements'), 'start': start, 'end': end}])

                with patch.object(text, 'apply_report_fallback', side_effect=recover) as fallback:
                    ix.attach_report_ranges(scope, [reference], agent_args=options)
                self.assertEqual(fallback.call_count, expected_calls)
                self.assertEqual(scope.membership(doc.ids['inside'])[2], ['8'])
                self.assertNotIn('8', scope.membership(doc.ids['outside'])[2])

    def test_ms_style_index_keeps_numeric_tables_without_item_number_headings(self):
        body = ('<table><tr><th>Table of Contents</th><th>Part</th><th>Item Number</th><th>Page</th></tr>'
                '<tr><td>Business</td><td>I</td><td>1</td><td>1</td></tr>'
                '<tr><td>Risk Factors</td><td>I</td><td>1A</td><td>2</td></tr>'
                '<tr><td>Management\'s Discussion and Analysis of Financial Condition and Results of Operations</td><td>II</td><td>7</td><td>3</td></tr>'
                '<tr><td>Financial Statements and Supplementary Data</td><td>II</td><td>8</td><td>4</td></tr>'
                '</table><h1>Business</h1><p>Our business serves clients.</p>')
        body += '<h1>Risk Factors</h1><p>Market risks affect results.</p>'
        body += '<h1>Management\'s Discussion and Analysis of Financial Condition and Results of Operations</h1>'
        body += table(number('10', 'mda_first')) + '<p>Revenue increased.</p>' + table(number('20', 'mda_last'))
        body += '<h1>Financial Statements and Supplementary Data</h1>'
        body += table(number('30', 'statement_first')) + '<p>These are our financial statements.</p>'
        body += table(number('40', 'statement_last'))
        doc, scope = layout(body)
        scope.validate(('1', '1A', '7', '8'))
        for name in ('mda_first', 'mda_last'):
            self.assertEqual(scope.membership(doc.ids[name])[2], ['7'])
        for name in ('statement_first', 'statement_last'):
            self.assertEqual(scope.membership(doc.ids[name])[2], ['8'])
        self.assertTrue(all(boundary['method'] == 'text_index' for boundary in scope.item_boundaries))

    def test_shared_agent_ranges_map_to_original_tables_only_when_recovery_is_enabled(self):
        from sec_disclosure.agents.filing_item_fallback import FallbackResult
        body = '<h1>Discussion</h1><p>We discuss performance.</p>'
        body += table(number('10', 'inside')) + '<p>Further business details.</p>'
        body += '<h1>Appendix</h1>' + table(number('20', 'outside'))
        doc = ix.Document(filing(body), 'original.htm')
        blocks = text.html_to_blocks(doc.data.decode())
        end = next(block.index for block in blocks if block.text == 'Appendix')
        chosen = [block for block in blocks if block.index < end]
        recovered = FallbackResult({'7': chosen}, 1, {'7': [
            {'coordinate': 'blocks', 'start': chosen[0].index, 'end': end, 'method': 'text_agent'}]})
        options = text.item_recovery_options('cache', enabled=True)
        with patch('sec_disclosure.llm.config.load_config', return_value=object()), \
             patch('sec_disclosure.agents.filing_item_locator.locate_item_blocks', return_value=recovered) as agent:
            scope = ix.Layout(doc, items=('7',), agent_args=options)
        agent.assert_called_once()
        scope.validate(('7',))
        self.assertEqual(scope.membership(doc.ids['inside'])[2], ['7'])
        self.assertNotIn('7', scope.membership(doc.ids['outside'])[2])

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
