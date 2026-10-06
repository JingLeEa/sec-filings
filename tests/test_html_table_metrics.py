"""Behavioral tests: prose versus tables, source units, scope and safe output."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import extract_html_metrics as cli
import html_table_metrics as h
import extract_10k_tables_xbrl as ix
from test_extract_10k_tables_xbrl import filing, number
from test_api_filing_documents import fixture as incorporated_fixture, URL as FILING_URL


def source(body):
    data = filing('<h1>ITEM 1. Business</h1><p>Business narrative.</p>'
                  '<h1>ITEM 1A. Risk Factors</h1><p>Risk narrative.</p>'
                  '<h1>ITEM 7. Management Discussion</h1>' + body +
                  '<h1>ITEM 8. Financial Statements</h1><table><tr><td>Excluded</td><td>' +
                  number('9', 'item8') + '</td></tr></table>',
                  hidden='<ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="annual">2025</ix:nonNumeric>'
                         '<ix:nonNumeric name="dei:DocumentType" contextRef="annual">10-K</ix:nonNumeric>')
    return data.replace(b'xmlns:ex="urn:example"', b'xmlns:ex="urn:example" xmlns:dei="http://xbrl.sec.gov/dei/2025"').replace(
        b'scheme="test">123', b'scheme="http://www.sec.gov/CIK">123')


def extract(body):
    doc = ix.Document(source(body), 'test.htm')
    layout = ix.Layout(doc)
    layout.validate(h.ITEMS)
    return h.build({'primary': {'doc': doc, 'layout': layout, 'url': None}}, 'Example', 2025)


def table(rows, heading='Year Ended ($ In Millions)', date='December 31, 2025'):
    return '<table><tr><th>' + heading + '</th><th>' + date + '</th></tr>' + rows + '</table>'


def values(result):
    return [v for m in result['metrics'] for v in m['value']]


class HtmlMetricsTests(unittest.TestCase):
    def test_numbers_and_tagged_numbers_inside_paragraphs_are_not_data_tables(self):
        prose = '<table><tr><td>Revenue was $52 million in 2025, up 5%.</td></tr></table>'
        prose += '<table><tr><td>Revenue was ' + number('52', 'prose') + ' million, up 5%.</td></tr></table>'
        prose += '<table><tr><td>(1)</td><td>Revenue decreased $20 million in 2025.</td></tr></table>'
        result = extract(prose)
        self.assertEqual(result['metrics'], [])
        self.assertEqual(result['table_classifications'], [])
        self.assertEqual(result['classification_summary']['narrative'], 3)
        self.assertEqual(result['classification_summary']['financial'], 0)

    def test_single_row_table_normalizes_amount_and_keeps_null_concept_and_dimensions(self):
        result = extract(table('<tr><td>Revenue</td><td>52,853</td></tr>'))
        metric, value = result['metrics'][0], values(result)[0]
        self.assertEqual(value['value'], '52853000000')
        self.assertEqual(value['unit'], 'USD')
        self.assertEqual(value['period'], {'startDate': '2025-01-01', 'endDate': '2025-12-31'})
        self.assertEqual(value['items'], ['7'])
        self.assertIsNone(metric['concept'])
        self.assertIsNone(value['dimensions'])
        self.assertEqual(metric['scope'], 'unknown')
        self.assertEqual(value['extraction_status'], 'resolved')
        self.assertEqual(result['classification_summary']['financial'], 1)
        self.assertEqual(list(result)[-1], 'classification_summary')

    def test_percent_split_symbol_and_per_share_exception(self):
        body = '<table><tr><th>Years Ended (In Millions, Except Per Share Amounts)</th>'
        body += '<th colspan="3">December 31, 2025</th></tr>'
        body += '<tr><td>Net revenue</td><td>$</td><td>52,853</td><td/></tr>'
        body += '<tr><td>Operating margin %</td><td/><td>(4.2)</td><td>%</td></tr>'
        body += '<tr><td>Earnings per share</td><td>$</td><td>(0.06)</td><td/></tr></table>'
        result = extract(body)
        records = {m['label']: m['value'][0] for m in result['metrics']}
        self.assertEqual(records['Net revenue']['value'], '52853000000')
        self.assertEqual(records['Operating margin %']['value'], '-0.042')
        self.assertEqual(records['Operating margin %']['unit'], 'pure')
        self.assertEqual(records['Earnings per share']['value'], '-0.06')
        self.assertEqual(records['Earnings per share']['unit'], 'USD/shares')

    def test_dashes_unknown_units_and_conflicting_scales_remain_reviewable(self):
        body = table('<tr><td>Revenue</td><td>—</td></tr>')
        body += table('<tr><td>Volume</td><td>500</td></tr>', heading='Year Ended')
        body += table('<tr><td>Revenue</td><td>10</td></tr>', heading='Year Ended ($ in millions; in thousands)')
        result = extract(body)
        self.assertEqual(len(values(result)), 3)
        self.assertTrue(all(v['value'] is None for v in values(result)))
        self.assertTrue(all(v['status'] == 'unresolved' for v in values(result)))
        self.assertEqual(result['classification_summary']['values_needing_review'], 3)

    def test_measurements_are_not_currency_and_dimensions_are_not_invented(self):
        body = '<p>As of December 31, 2025, our major facilities consisted of:</p>'
        body += '<table><tr><th>Square Feet (In Millions)</th><th>United States</th><th>Total</th></tr>'
        body += '<tr><td>Owned facilities</td><td>35</td><td>60</td></tr></table>'
        result = extract(body)
        self.assertEqual([v['value'] for v in values(result)], ['35000000', '60000000'])
        self.assertTrue(all(v['unit'] == 'square_feet' for v in values(result)))
        self.assertEqual(values(result)[0]['context_labels'], ['United States'])
        self.assertEqual(values(result)[0]['period'], {'instant': '2025-12-31'})

    def test_inline_currency_and_explicit_preceding_unit_legend(self):
        body = '<p>USD millions</p>' + table('<tr><td>Revenue</td><td>$52</td></tr>', heading='Year Ended')
        result = extract(body)
        self.assertEqual(values(result)[0]['value'], '52000000')
        self.assertEqual(values(result)[0]['unit'], 'USD')
        self.assertEqual(values(result)[0]['extraction_status'], 'resolved')

    def test_tags_are_skipped_but_untagged_cell_in_same_table_survives(self):
        body = table('<tr><td>Revenue</td><td>' + number('52', 'tagged') + '</td></tr>'
                     '<tr><td>Operating margin %</td><td>29%</td></tr>')
        result = extract(body)
        self.assertEqual(len(values(result)), 1)
        self.assertEqual(values(result)[0]['value'], '0.29')
        self.assertEqual(result['classification_summary']['tagged_value_cells_skipped'], 1)

    def test_hidden_fact_reference_is_not_exported_as_untagged(self):
        body = table('<tr><td>Revenue</td><td><span style="-sec-ix-hidden:hiddenvalue">52</span></td></tr>'
                     '<tr><td>Operating margin %</td><td>29%</td></tr>')
        data = source(body).replace(b'</ix:hidden>', number('52', 'hiddenvalue').encode() + b'</ix:hidden>')
        doc = ix.Document(data, 'test.htm')
        result = h.build({'primary': {'doc': doc, 'layout': ix.Layout(doc)}}, 'Example', 2025)
        self.assertEqual([v['value'] for v in values(result)], ['0.29'])

    def test_heading_in_large_type_overrides_date_panel_title(self):
        body = '<div><span style="font-size:12pt">Product Financial Performance</span></div>'
        body += '<table><tr><th colspan="2">December 31, 2025</th></tr>'
        body += '<tr><td>Year Ended ($ In Millions)</td><td>CCG</td></tr>'
        body += '<tr><td>Revenue</td><td>52</td></tr></table>'
        result = extract(body)
        self.assertEqual(result['table_classifications'][0]['title'], 'Product Financial Performance')

    def test_missing_annual_start_and_ambiguous_currency_are_not_inferred(self):
        body = table('<tr><td>Revenue</td><td>52</td></tr>', date='December 31, 2024')
        result = extract(body)
        self.assertIn('unverified_period_start', values(result)[0]['issues'])
        self.assertEqual(values(result)[0]['period'], {'endDate': '2024-12-31'})
        parsed = h.amount('52')
        value, unit, _, issues = h.normalize(parsed, '52', 'Revenue', [], ['In millions'], ['$'], {'USD', 'CAD'})
        self.assertIsNone(value)
        self.assertIsNone(unit)
        self.assertIn('missing_or_ambiguous_unit', issues)

    def test_text_block_enclosing_table_does_not_make_its_cells_numeric_tags(self):
        body = '<ix:nonNumeric name="ex:DisclosureTextBlock" contextRef="annual">'
        body += table('<tr><td>Revenue</td><td>52</td></tr>') + '</ix:nonNumeric>'
        result = extract(body)
        self.assertEqual(values(result)[0]['value'], '52000000')

    def test_repeated_item_membership_is_counted_once_and_requested_items_only(self):
        doc = ix.Document(source(table('<tr><td>Revenue</td><td>52</td></tr>')), 'test.htm')
        layout = ix.Layout(doc)
        node = next(t for t in doc.tables if 'Revenue' in doc.display(t))
        layout.ranges.extend({'item': item, 'start': doc.order[node], 'end': doc.order[node] + 1}
                             for item in ('1', '7', '8'))
        result = h.build({'primary': {'doc': doc, 'layout': layout}}, 'Example', 2025)
        self.assertEqual(values(result)[0]['items'], ['1', '7'])
        self.assertEqual(result['classification_summary']['financial'], 1)
        self.assertEqual(result['classification_summary']['by_item']['1']['financial'], 1)
        self.assertEqual(result['classification_summary']['by_item']['7']['financial'], 1)

    def test_date_header_is_not_a_metric_but_year_shaped_amount_is(self):
        result = extract(table('<tr><td>Revenue</td><td>2025</td></tr>'))
        self.assertEqual(len(values(result)), 1)
        self.assertEqual(values(result)[0]['value'], '2025000000')

    def test_units_and_year_header_row_is_not_exported_as_data(self):
        body = '<table><tr><td/><td colspan="3">Year ended December 31,</td></tr>'
        body += '<tr><td>(in millions, except per share amounts)</td><td colspan="3">2025</td></tr>'
        body += '<tr><td>Revenue</td><td>$</td><td>52</td><td/></tr></table>'
        result = extract(body)
        self.assertEqual([m['label'] for m in result['metrics']], ['Revenue'])
        self.assertEqual([v['value'] for v in values(result)], ['52000000'])
        self.assertEqual(values(result)[0]['period'], {'startDate': '2025-01-01', 'endDate': '2025-12-31'})

    def test_percent_mark_on_first_year_applies_only_to_comparable_year_columns(self):
        body = '<table><tr><td/><td colspan="3">Year ended December 31,</td></tr>'
        body += '<tr><td/><td>2025</td><td>2024</td><td>2023</td></tr>'
        body += '<tr><td>Return on average assets</td><td>1.07%</td><td>1.03</td><td>0.99</td></tr>'
        body += '<tr><td>Per share</td><td>$6.26</td><td>5.37</td><td>4.83</td></tr></table>'
        result = extract(body)
        ratios = [v for m in result['metrics'] if m['label'].startswith('Return on average assets') for v in m['value']]
        self.assertEqual([v['value'] for v in ratios], ['0.0107', '0.0103', '0.0099'])
        self.assertTrue(all(v['unit'] == 'pure' for v in ratios))
        self.assertTrue(ratios[1]['normalization']['row_percent_source_locators'])
        self.assertEqual(ratios[1]['display_text'], '1.03')
        mixed = '<table><tr><td>($ in millions)</td><td>2025 Amount</td><td>2024 Change</td></tr>'
        mixed += '<tr><td>Revenue</td><td>50</td><td>10%</td></tr></table>'
        result = extract(mixed)
        self.assertEqual({v['value'] for v in values(result)}, {'50000000', '0.1'})

    def test_image_only_and_review_tables_are_omitted_but_financial_measurements_survive(self):
        body = '<table><tr><td><img src="chart.png" alt="Revenue chart"/></td></tr></table>'
        body += '<table><tr><td>Products</td></tr><tr><td><img src="products.png"/></td></tr></table>'
        body += '<table><tr><td rowspan="3">Revenue</td><td>10</td></tr></table>'
        body += table('<tr><td>Revenue<img src="icon.png"/></td><td>52</td></tr>')
        result = extract(body)
        summary = result['classification_summary']
        self.assertEqual(summary['ignored_image_only_tables'], 2)
        self.assertEqual(summary['ignored_image_only_by_item'], {'1': 0, '1A': 0, '7': 2})
        self.assertEqual(summary['review'], 1)
        self.assertEqual(summary['financial'], 1)
        self.assertEqual(summary['total_tables'], 2)
        self.assertEqual([d['classification'] for d in result['table_classifications']], ['financial'])
        self.assertTrue(all('cells_for_review' not in d and 'text_preview' not in d
                            for d in result['table_classifications']))
        self.assertEqual([v['value'] for v in values(result)], ['52000000'])

    def test_nested_wrapper_is_not_a_duplicate_and_summary_reconciles(self):
        result = extract('<table><tr><td>' + table('<tr><td>Revenue</td><td>52</td></tr>') + '</td></tr></table>')
        summary = result['classification_summary']
        self.assertEqual(summary['total_tables'], 1)
        self.assertEqual(sum(summary[k] for k in h.CLASSES), summary['total_tables'])
        self.assertEqual(len(values(result)), 1)
        self.assertEqual(summary['by_item']['1A']['total_tables'], 0)

    def test_cli_writes_separate_file_and_protects_existing_output(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            filing_path = root / 'filing.htm'
            filing_path.write_bytes(source(table('<tr><td>Revenue</td><td>52</td></tr>')))
            before = filing_path.read_bytes()
            args = ['--filing', str(filing_path), '--company', 'Example', '--year', '2025', '--offline', '--output-dir', tmp]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                 patch.object(cli.sec, 'SecClient', side_effect=AssertionError('No network')):
                self.assertEqual(cli.main(args), 0)
                target = root / 'example_2025_html_metrics_with_values.json'
                result = target.read_bytes()
                self.assertEqual(cli.main(args), 1)
            self.assertEqual(result, target.read_bytes())
            self.assertEqual(before, filing_path.read_bytes())
            self.assertEqual(list(json.loads(result))[-1], 'classification_summary')

    def test_cli_verifies_year_in_incorporated_report_and_extracts_its_selected_items(self):
        primary, report, _ = incorporated_fixture()
        primary = primary.replace(b'</body>', b'<ix:nonNumeric name="dei:DocumentType" contextRef="annual">10-K</ix:nonNumeric></body>')
        year = b'<ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="annual">2023</ix:nonNumeric>'
        untagged = table('<tr><td>Revenue</td><td>52</td></tr>', date='December 30, 2023').encode()
        report = report.replace(b'<h1>Financial Review</h1>', year + b'<h1>Financial Review</h1>' + untagged)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'primary.htm').write_bytes(primary)
            (root / 'report.htm').write_bytes(report)
            (root / 'report.htm.source.json').write_text(json.dumps({
                'sec_url': FILING_URL.rsplit('/', 1)[0] + '/report.htm',
                'sha256': hashlib.sha256(report).hexdigest()}))
            args = ['--filing', str(root / 'primary.htm'), '--filing-url', FILING_URL,
                    '--company', 'Example', '--year', '2023', '--offline',
                    '--sec-cache', str(root / 'cache'), '--output-dir', tmp]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                 patch.object(cli.sec, 'SecClient', side_effect=AssertionError('No network')):
                self.assertEqual(cli.main(args), 0)
            result = json.loads((root / 'example_2023_html_metrics_with_values.json').read_text())
            evidence = result['verification']['fiscal_year']
            self.assertEqual(evidence['value'], 2023)
            self.assertEqual(evidence['facts'][0]['document_id'], 'report1')
            self.assertEqual(set(result['verification']['documents']), {'primary', 'report1'})
            self.assertEqual([v['value'] for v in values(result)], ['52000000'])
            self.assertEqual(values(result)[0]['items'], ['7'])
            self.assertEqual(list(result)[-1], 'classification_summary')

    def test_fiscal_year_rejects_mismatch_and_conflicting_report(self):
        primary = ix.Document(source(''), 'primary.htm')
        report = ix.Document(source('').replace(b'>2025</ix:nonNumeric>', b'>2024</ix:nonNumeric>'), 'report.htm')
        with self.assertRaisesRegex(ValueError, 'fiscal year is 2025, expected 2024'):
            cli.verify_fiscal_year({'primary': {'doc': primary}}, 2024)
        with self.assertRaisesRegex(ValueError, 'Conflicting DocumentFiscalYearFocus'):
            cli.verify_fiscal_year({'primary': {'doc': primary}, 'report1': {'doc': report}}, 2025)

    def test_fiscal_year_does_not_guess_from_requested_year_or_custom_tag(self):
        raw = source('')
        year = b'<ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="annual">2025</ix:nonNumeric>'
        for data in (raw.replace(year, b''), raw.replace(b'name="dei:DocumentFiscalYearFocus"',
                                                       b'name="ex:DocumentFiscalYearFocus"')):
            with self.subTest(data=data[-100:]):
                doc = ix.Document(data, 'company_2025.htm')
                with self.assertRaisesRegex(ValueError, 'No verified DocumentFiscalYearFocus'):
                    cli.verify_fiscal_year({'primary': {'doc': doc}}, 2025)

    def test_fiscal_year_uses_shared_context_but_rejects_missing_or_dimensioned_context(self):
        doc = ix.Document(source(''), 'primary.htm')
        context = doc.contexts.pop('annual')
        documents = {'primary': {'doc': doc}}
        with self.assertRaisesRegex(ValueError, 'no verified undimensioned context'):
            cli.verify_fiscal_year(documents, 2025)
        report = ix.Document(source(''), 'report.htm')
        documents['report1'] = {'doc': report}
        cli.filing_docs.share_resources(documents)
        self.assertEqual(cli.verify_fiscal_year(documents, 2025)['value'], 2025)
        doc.contexts['annual'] = {**context, 'dimensions': [{'unverified': True}]}
        with self.assertRaisesRegex(ValueError, 'no verified undimensioned context'):
            cli.verify_fiscal_year(documents, 2025)

    def test_cross_document_hidden_reference_is_tagged_and_does_not_supply_html_amounts(self):
        primary_data = source('').replace(b'</ix:hidden>', number('52', 'remote').encode() + b'</ix:hidden>')
        report_data = source(table('<tr><td>Tagged</td><td><span style="-sec-ix-hidden:remote">52</span></td></tr>'
                                   '<tr><td>Untagged</td><td>78</td></tr>'))
        def entry(data, name):
            doc = ix.Document(data, name)
            return {'doc': doc, 'layout': ix.Layout(doc)}
        docs = {'primary': entry(primary_data, 'primary.htm'), 'report1': entry(report_data, 'report.htm')}
        result = h.build(docs, 'Example', 2025)
        self.assertEqual([v['value'] for v in values(result)], ['78000000'])
        self.assertEqual(result['classification_summary']['tagged_value_cells_skipped'], 1)
        self.assertEqual(result['verification']['cross_document_hidden_references'][0]['fact_document_id'], 'primary')
        docs['report2'] = entry(primary_data, 'another.htm')
        with self.assertRaisesRegex(ValueError, 'ambiguous cross-document hidden fact'):
            h.build(docs, 'Example', 2025)
        # An ID defined locally is document-scoped even if another report uses it.
        docs['report1'] = entry(report_data.replace(b'</ix:hidden>', number('52', 'remote').encode() + b'</ix:hidden>'), 'report.htm')
        result = h.build(docs, 'Example', 2025)
        self.assertEqual([v['value'] for v in values(result)], ['78000000'])
        self.assertEqual(result['verification']['cross_document_hidden_references'], [])


if __name__ == '__main__':
    unittest.main()
