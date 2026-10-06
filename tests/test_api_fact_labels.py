"""Source labels follow verified cell positions, never empty dimensions."""
import unittest

from api_fact_labels import FactLabelIndex
from test_extract_10k_items_7_8 import intel_fixture
from test_check_item8_api import source_fact


def fixture(body):
    original = intel_fixture()
    start = original.index(b'<h2 ')
    return original[:start] + body + b'</body></html>'


def labels(body, identifier='value'):
    index = FactLabelIndex(fixture(body), 'intel.htm')
    node = index.doc.ids[identifier]
    entry = {'evidence': [{'source_fact_id': identifier, 'locations': [
        {'source_locator': index.doc.paths[node], 'page': '78', 'referenced_items': ['8']}]}]}
    return index.for_entry(entry, ['8'])


class FactLabelTests(unittest.TestCase):
    def test_merged_column_headers_and_total_column(self):
        body = (b'<table><tr><th rowspan="2">Description</th><th colspan="2">Equity components</th>'
                b'<th rowspan="2">Total</th></tr><tr><th>Capital</th><th>Retained earnings</th></tr>'
                b'<tr><td>Restricted stock unit withholdings</td><td>359</td><td>61</td><td>' +
                source_fact('value') + b'</td></tr></table>')
        result = labels(body)[0]
        self.assertEqual(result['row_label'], 'Restricted stock unit withholdings')
        self.assertEqual(result['column_label'], 'Total')
        self.assertEqual(result['status'], 'resolved')
        component = body.replace(b'<td>359</td>', b'<td>' + source_fact('component') + b'</td>')
        self.assertEqual(labels(component, 'component')[0]['column_label'], 'Equity components / Capital')

    def test_rowspan_and_row_header_without_scope(self):
        body = (b'<table><tr><th>Description</th><th>2022</th><th>2023</th></tr>'
                b'<tr><th rowspan="2">Withholdings</th><td>61</td><td>420</td></tr>'
                b'<tr><td>10</td><td>' + source_fact('value') + b'</td></tr></table>')
        result = labels(body)[0]
        self.assertEqual(result['row_label'], 'Withholdings')
        self.assertEqual(result['column_label'], '2023')

    def test_explicit_header_ids_and_bad_references(self):
        body = (b'<table><tr><th></th><th id="total" scope="col">Total</th></tr>'
                b'<tr><th id="row" scope="row">Withholdings</th><td headers="row total">' +
                source_fact('value') + b'</td></tr></table>')
        result = labels(body)[0]
        self.assertEqual(result['column_label'], 'Total')
        self.assertEqual(result['row_label'], 'Withholdings')
        self.assertEqual(result['label_method'], 'html_headers')
        bad = labels(body.replace(b'headers="row total"', b'headers="missing total"'))[0]
        self.assertEqual(bad['status'], 'unresolved')
        self.assertIsNone(bad['column_label'])

    def test_empty_dimensions_do_not_create_total_and_invalid_layout_is_retained(self):
        body = b'<table><tr><td>Withholdings</td><td>' + source_fact('value') + b'</td></tr></table>'
        result = labels(body)[0]
        self.assertEqual(result['row_label'], 'Withholdings')
        self.assertIsNone(result['column_label'])
        self.assertEqual(result['status'], 'partial')
        invalid = labels(body.replace(b'<td>Withholdings', b'<td colspan="bad">Withholdings'))[0]
        self.assertEqual(invalid['status'], 'invalid_layout')
        self.assertIsNone(invalid['column_label'])

    def test_prose_and_missing_source_are_explicit_and_outside_locations_are_ignored(self):
        result = labels(b'<p>Some disclosure ' + source_fact('value') + b'.</p>')[0]
        self.assertEqual(result['status'], 'not_in_table')
        self.assertIsNone(result['row_label'])
        index = FactLabelIndex(fixture(b'<p>' + source_fact('value') + b'</p>'), 'intel.htm')
        entry = {'evidence': [{'source_fact_id': 'value', 'locations': [
            {'source_locator': 'missing', 'page': '78', 'referenced_items': ['8']},
            {'source_locator': 'outside', 'page': '9', 'referenced_items': ['1']}]}]}
        self.assertEqual(len(index.for_entry(entry, ['8'])), 1)
        self.assertEqual(index.for_entry(entry, ['8'])[0]['status'], 'source_not_found')

    def test_repeated_visible_occurrences_keep_different_labels_and_deduplicate_points(self):
        first = b'<table><tr><td>First label</td><td>' + source_fact('value') + b'</td></tr></table>'
        second = b'<table><tr><td>Another label</td><td><span id="visible">420</span></td></tr></table>'
        index = FactLabelIndex(fixture(first + second), 'intel.htm')
        locations = [{'source_locator': index.doc.paths[index.doc.ids[i]], 'page': '78', 'referenced_items': ['8']}
                     for i in ('value', 'visible', 'value')]
        result = index.for_entry({'evidence': [{'source_fact_id': 'value', 'locations': locations}]}, ['8'])
        self.assertEqual([r['row_label'] for r in result], ['First label', 'Another label'])
        self.assertTrue(all(r['source_fact_id'] == 'value' for r in result))


if __name__ == '__main__':
    unittest.main()
