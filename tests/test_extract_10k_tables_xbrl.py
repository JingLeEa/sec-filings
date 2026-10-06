"""Offline behavioral tests for XBRL values, provenance, layout and scope."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec_disclosure.table_extraction import extract_10k_tables_xbrl as x


def filing(body, resources='', hidden=''):
    return f'''<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:ex="urn:example"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2022-02-16"
      xmlns:sec="http://www.sec.gov/inlineXBRL/transformation/2015-08-31">
      <head><title>Test filing</title></head><body>
      <ix:header><ix:resources>
       <xbrli:context id="annual"><xbrli:entity><xbrli:identifier scheme="test">123</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
       <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
       <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
       {resources}</ix:resources><ix:hidden>{hidden}</ix:hidden></ix:header>
      {body}</body></html>'''.encode()


def number(value='1,234', identifier='f1', **kwargs):
    attrs = {'id': identifier, 'name': 'us-gaap:Revenue', 'contextRef': 'annual',
             'unitRef': 'usd', 'decimals': '-6', 'scale': '6', 'format': 'ixt:num-dot-decimal'}
    attrs.update(kwargs)
    attributes = ' '.join(f'{k}="{v}"' for k, v in attrs.items() if v is not None)
    return f'<ix:nonFraction {attributes}>{value}</ix:nonFraction>'


def text_fact(value, identifier='text1', **kwargs):
    attrs = {'id': identifier, 'name': 'ex:Description', 'contextRef': 'annual', **kwargs}
    attributes = ' '.join(f'{k}="{v}"' for k, v in attrs.items() if v is not None)
    return f'<ix:nonNumeric {attributes}>{value}</ix:nonNumeric>'


def table(content=None, title=''):
    return f'<table>{"<caption>" + title + "</caption>" if title else ""}<tr><td>Revenue</td><td>{content or number()}</td></tr></table>'


def footer(page):
    return f'<div style="position:absolute;bottom:0">{page}</div><div style="page-break-after:always"/>'


def extract(body, **kwargs):
    return x.extract_tables(filing(body), 'Example', 2025, 'example.htm', items=None, **kwargs)


class XbrlValuesTests(unittest.TestCase):
    def fact(self, content, resources=''):
        doc = x.Document(filing(table(content), resources), 'test.htm')
        return doc.fact(doc.fact_nodes[0])

    def test_sign_scale_and_accuracy_have_different_meanings(self):
        fact = self.fact(number('1,234.50', sign='-', decimals='-3'))
        self.assertEqual(fact['value'], '-1234500000')
        self.assertEqual(fact['decimals'], '-3')
        self.assertEqual(fact['period']['endDate'], '2025-12-31')
        self.assertEqual(fact['unit'], 'iso4217:USD')

    def test_large_decimal_not_rounded_to_default_precision(self):
        value = '123456789012345678901234567890123456789.123456789'
        fact = self.fact(number(value, scale='3', format=None, decimals='INF'))
        self.assertEqual(fact['value'], '123456789012345678901234567890123456789123.456789')

    def test_ratio_comma_decimal_and_fixed_zero(self):
        self.assertEqual(self.fact(number('6.23', unitRef='pure', scale='-2'))['value'], '0.0623')
        self.assertEqual(self.fact(number('1.234,56', scale='0', format='ixt:num-comma-decimal'))['value'], '1234.56')
        self.assertEqual(self.fact(number('—', format='ixt:fixed-zero'))['value'], '0')

    def test_nil_is_not_zero(self):
        fact = self.fact(number('', **{'xsi:nil': 'true'}))
        self.assertEqual(fact['status'], 'nil')
        self.assertIsNone(fact['value'])

    def test_registry_three_zerodash_accepts_only_its_defined_inputs(self):
        for raw in ('-', '—', '－', '  –  ', '0', '', '--', 'n/a', '−'):
            with self.subTest(raw=raw):
                data = filing(table(number(raw, format='ixt:zerodash'))).replace(b'2022-02-16', b'2015-02-26')
                doc = x.Document(data, 'test.htm')
                fact = doc.fact(doc.fact_nodes[0])
                expected = raw.strip() in {'-', '—', '－', '–'}
                self.assertEqual(fact['status'], 'ok' if expected else 'error')
                self.assertEqual(fact['value'], '0' if expected else None)
        for attrs in ({'format': 'ixt:zerodash'}, {'format': None}):
            self.assertEqual(self.fact(number('-', **attrs))['status'], 'error')

    def test_invalid_values_are_not_guessed(self):
        cases = [('1,23', {}), ('(123)', {}), ('-123', {}), ('1', {'sign': '+'}),
                 ('1', {'format': 'ex:num-dot-decimal'}), ('1', {'format': 'ixt:unknown'}),
                 ('1', {'contextRef': 'missing'}), ('1', {'unitRef': 'missing'}),
                 ('1', {'name': 'missing:Concept'}),
                 ('1', {'decimals': '2', 'precision': '3'}), ('1', {'scale': '10001'}),
                 ('1', {'xsi:nil': 'yes'}), ('1', {'target': 'different-instance'})]
        for raw, attrs in cases:
            with self.subTest(raw=raw, attrs=attrs):
                fact = self.fact(number(raw, **attrs))
                self.assertEqual(fact['status'], 'error')
                self.assertIsNone(fact['value'])
                self.assertEqual(fact['raw_text'], raw)

    def test_namespace_aliases_do_not_change_values(self):
        data = filing(table()).replace(b'xmlns:ix=', b'xmlns:tag=').replace(b'ix:', b'tag:')
        doc = x.Document(data, 'test.htm')
        self.assertEqual(doc.fact(doc.fact_nodes[0])['value'], '1234000000')

    def test_dimensions_and_divided_unit(self):
        resources = '''<xbrli:context id="dim"><xbrli:entity><xbrli:identifier scheme="test">123</xbrli:identifier>
        <xbrli:segment><xbrldi:explicitMember dimension="ex:SegmentAxis">ex:BankingMember</xbrldi:explicitMember>
        <xbrldi:typedMember dimension="ex:ProductAxis"><ex:Product>Loans</ex:Product></xbrldi:typedMember></xbrli:segment>
        </xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
        <xbrli:unit id="eps"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
        <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'''
        doc = x.Document(filing(table(number('2.94', contextRef='dim', unitRef='eps', scale='0')), resources), 'test.htm')
        fact = doc.fact(doc.fact_nodes[0])
        self.assertEqual(fact['value'], '2.94')
        self.assertEqual(fact['unit'], 'iso4217:USD/xbrli:shares')
        self.assertEqual(fact['period']['type'], 'instant')
        dims = doc.contexts['dim']['dimensions']
        self.assertEqual(dims[0]['member']['expanded_name'], '{urn:example}BankingMember')
        self.assertIn('Loans', dims[1]['typed_value_xml'])

    def test_continuation_excludes_footnotes_and_detects_cycles(self):
        body = table(text_fact('First<ix:exclude>[1]</ix:exclude>', continuedAt='part2'))
        body += '<ix:continuation id="part2"> second</ix:continuation>'
        r = extract(body, financial_only=False)
        self.assertEqual(r['documents']['primary']['facts']['text1']['value'], 'First second')
        doc = x.Document(filing(body.replace('id="part2"', 'id="part2" continuedAt="part2"')), 'test.htm')
        self.assertIn('Cyclic', doc.fact(doc.fact_nodes[0])['error'])

    def test_dates_and_fixed_flags(self):
        self.assertEqual(self.fact(text_fact('9/21/2018', format='ixt:date-month-day-year'))['value'], '2018-09-21')
        self.assertEqual(self.fact(text_fact('anything', format='ixt:fixed-false'))['value'], 'false')
        self.assertEqual(self.fact(text_fact('2/30/2025', format='ixt:date-month-day-year'))['status'], 'error')

    def test_unsupported_duration_and_escaped_xml_are_explicit(self):
        fact = self.fact(text_fact('9.1', format='sec:duryear'))
        self.assertEqual(fact['raw_text'], '9.1')
        self.assertEqual(fact['status'], 'error')
        self.assertIn('Unsupported nonNumeric', fact['error'])
        self.assertEqual(self.fact(text_fact('<b>Text</b>', escape='true'))['status'], 'error')

    def test_no_xbrl_and_duplicate_ids_fail(self):
        with self.assertRaisesRegex(ValueError, 'No Inline XBRL'):
            x.Document(filing('<p>Untagged filing</p>'), 'test.htm')
        with self.assertRaisesRegex(ValueError, 'Duplicate XML ID'):
            x.Document(filing(table(number() + number())), 'test.htm')
        with self.assertRaisesRegex(ValueError, 'well-formed Inline XBRL'):
            x.Document(b'<html><p>broken</html>', 'test.htm')

    def test_local_source_fiscal_year_is_checked(self):
        data = filing(table() + text_fact('2024', name='dei:DocumentFiscalYearFocus'))
        data = data.replace(b'xmlns:ex="urn:example"', b'xmlns:ex="urn:example" xmlns:dei="http://xbrl.sec.gov/dei/2025"')
        with self.assertRaisesRegex(ValueError, 'Requested FY2025'):
            x.extract_tables(data, 'Example', 2025, 'wrong-year.htm', items=None)


class XbrlLayoutTests(unittest.TestCase):
    def test_grid_references_cells_without_duplicating_facts(self):
        body = '<table><tr><th rowspan="2">Revenue</th><td colspan="2">'
        body += number() + number('2', identifier='f2') + '</td></tr><tr><td>Untyped</td><td>99</td></tr></table>'
        r = extract(body)
        t = r['tables'][0]
        self.assertEqual(t['grid'], [['r1c1', 'r1c2', 'r1c2'], ['r1c1', 'r2c1', 'r2c2']])
        self.assertEqual(t['cells'][1]['fact_ids'], ['f1', 'f2'])
        self.assertEqual(len(t['cells']), 4)
        self.assertEqual(t['cells'][-1]['tagging'], 'untagged')
        self.assertEqual(r['summary']['facts'], 2)

    def test_nested_facts_are_both_preserved(self):
        r = extract(table(text_fact(number())))
        self.assertEqual(set(r['tables'][0]['cells'][1]['fact_ids']), {'text1', 'f1'})

    def test_nested_tables_have_one_fact_owner(self):
        r = extract('<table><tr><td>' + number() + table(number('2', identifier='f2')) + '</td></tr></table>', financial_only=False)
        self.assertEqual(len(r['tables']), 2)
        self.assertEqual([t['fact_ids'] for t in r['tables']], [['f1'], ['f2']])

    def test_text_block_wrapper_is_metadata_not_duplicate_numeric_data(self):
        r = extract(text_fact(table(), escape='true'))
        self.assertEqual(r['tables'][0]['fact_ids'], ['f1'])
        self.assertEqual(r['tables'][0]['enclosing_xbrl_concepts'][0]['concept'], 'ex:Description')

    def test_hidden_fact_only_maps_with_explicit_sec_reference(self):
        body = table('<span style="-sec-ix-hidden:f1">1,234</span><span style="display:none">999</span>')
        r = x.extract_tables(filing(body, hidden=number()), 'Example', 2025, 'test.htm', items=None)
        self.assertEqual(r['tables'][0]['cells'][1]['display_text'], '1,234')
        self.assertEqual(r['tables'][0]['fact_ids'], ['f1'])
        r = x.extract_tables(filing(table('1,234'), hidden=number()), 'Example', 2025, 'test.htm', items=None)
        self.assertEqual(r['tables'][0]['fact_ids'], [])

    def test_bad_spans_keep_source_cells_and_facts(self):
        r = extract(table().replace('<td>Revenue', '<td rowspan="bad">Revenue'))
        self.assertEqual(r['summary']['layout_errors'], 1)
        self.assertEqual(r['tables'][0]['grid'], [])
        self.assertEqual(r['tables'][0]['fact_ids'], ['f1'])
        self.assertEqual(len(r['tables'][0]['cells']), 2)

    def test_title_caption_and_last_sentence(self):
        r = extract('<p>Example Inc. grew this year. Revenue is shown below:</p>' + table())
        self.assertEqual(r['tables'][0]['title'], 'Revenue is shown below:')
        self.assertEqual(r['tables'][0]['title_source'], 'preceding_sentence')
        r = extract('<h2>Earlier heading</h2>' + table(title='Actual caption'))
        self.assertEqual(r['tables'][0]['title'], 'Actual caption')

    def test_positions_remain_stable_when_filtering_items(self):
        body = '<h1>Item 1. Business</h1>' + table() + '<h1>Item 1A. Risk Factors</h1>'
        body += '<h1>Item 7. Discussion</h1>' + table(number('2', 'f2'))
        body += '<h1>Item 8. Statements</h1>' + table(number('3', 'f3')) + footer(5)
        data = filing(body)
        full = x.extract_tables(data, 'Example', 2025, 'test.htm')
        item8 = x.extract_tables(data, 'Example', 2025, 'test.htm', items=('8',))
        self.assertEqual(item8['tables'][0], full['tables'][2])
        self.assertEqual(item8['tables'][0]['position_id'], 'example*20258*5*3')

    def test_item_index_overlaps_are_retained(self):
        index = '<table><tr><td>Item 1</td><td>Business</td><td>Page 5</td></tr>'
        index += '<tr><td>Item 7</td><td>Discussion</td><td>Page 5</td></tr>'
        index += '<tr><td>Item 8</td><td>Statements</td><td>Page 6</td></tr></table>'
        data = filing(index + footer(1) + table() + footer(5) + table(number('2', 'f2')) + footer(6))
        r = x.extract_tables(data, 'Example', 2025, 'test.htm', items=('7',))
        self.assertEqual(r['tables'][0]['referenced_items'], ['1', '7'])
        self.assertEqual(r['tables'][0]['item'], '1')
        self.assertEqual(r['tables'][0]['page'], '5')

    def test_missing_referenced_page_fails_closed(self):
        body = '<h1>Item 7. Discussion</h1><p>Management’s discussion and analysis appears on pages 3–4.</p>'
        body += '<h1>Item 8. Statements</h1>' + table() + footer(3)
        with self.assertRaisesRegex(ValueError, r'missing=\[4\]'):
            x.extract_tables(filing(body), 'Example', 2025, 'test.htm', items=('7',))

    def test_jpm_style_reference_excludes_secondary_page_range(self):
        body = '<h1>Item 7. Discussion</h1><p>Management’s discussion and analysis appears on pages 3–4. Read in conjunction with pages 5–6.</p>'
        body += '<h1>Item 8. Statements</h1>' + footer(2) + table() + footer(3)
        body += footer(4) + table(number('2', 'f2')) + footer(5)
        r = x.extract_tables(filing(body), 'Example', 2025, 'test.htm', items=('7',))
        self.assertEqual([t['page'] for t in r['tables']], ['3'])
        self.assertEqual(r['tables'][0]['physical_item'], '8')

    def test_external_report_has_separate_fact_namespace(self):
        primary = '<h1>Item 7. Discussion</h1><p>Information in “Financial Review” of the Annual Report is incorporated by reference.</p>'
        primary += '<h1>Item 8. Statements</h1>' + table() + footer(1)
        primary += '<h1>Item 15. Exhibits</h1><table><tr><td>13</td><td><a href="report.htm">Annual Report</a></td></tr></table>'
        report = filing('<h1>Financial Review</h1>' + table(number('2')) + footer(3) + '<h1>Other</h1>')
        r = x.extract_tables(filing(primary), 'Example', 2025, '/tmp/main.htm', items=('7', '8'), report_loader=lambda _: report)
        self.assertEqual(r['documents']['primary']['facts']['f1']['value'], '1234000000')
        self.assertEqual(r['documents']['report1']['facts']['f1']['value'], '2000000')
        self.assertEqual(r['tables'][1]['item'], '7')
        self.assertEqual(r['tables'][1]['source'], str(Path('/tmp/report.htm').resolve()))

    def test_unverified_page_is_null(self):
        r = extract(table())
        self.assertIsNone(r['tables'][0]['page'])
        self.assertEqual(r['summary']['tables_without_verified_page'], 1)

    def test_strict_mode_does_not_replace_existing_output(self):
        r = extract(table(number('invalid')))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.json'
            path.write_text('existing')
            with self.assertRaisesRegex(ValueError, 'Strict extraction failed'):
                x.write_result(r, path, strict=True)
            self.assertEqual(path.read_text(), 'existing')
            x.write_result(r, path)
            self.assertEqual(json.loads(path.read_text())['summary']['fact_status'], {'error': 1})

    def test_cli_local_and_mocked_sec_api(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(tmp) / 'sample.htm'
            path.write_bytes(filing('<h1>Item 8. Statements</h1>' + table()))
            args = ['--company', 'Example', '--year', '2025', '--items', '8', '--output-dir', tmp]
            self.assertEqual(x.main([str(path), *args]), 0)
            result = json.loads((Path(tmp) / 'result_xbrl.json').read_text())
            self.assertEqual(result['summary']['facts'], 1)
            with patch.object(x.html_tables, 'SecClient') as client, patch.object(x.html_tables, 'discover_sec_filings') as discover:
                client.return_value.get.return_value = path.read_bytes()
                discover.return_value = ({'name': 'Example'}, {2025: {'url': 'https://www.sec.gov/filing.htm', 'accessionNumber': 'test', 'reportDate': '2025-12-31'}})
                self.assertEqual(x.main(['--ticker', 'EX', '--user-agent', 'Name name@example.com', *args]), 0)
                self.assertEqual(discover.call_args.kwargs['ticker'], 'EX')


class FinancialTableFilterTests(unittest.TestCase):
    def financial(self, body, **kwargs):
        # A hidden fact makes this genuine Inline XBRL even when a visible
        # table is untagged; it must not qualify that table by itself.
        return x.extract_tables(filing(body, hidden=number(identifier='hidden')), 'Example', 2025,
                                'test.htm', items=None, **kwargs)

    def test_prose_containing_tagged_numbers_is_filtered(self):
        body = '<table><tr><td>Revenue increased to ' + number() + ' million in 2025.</td></tr></table>'
        r = self.financial(body)
        self.assertEqual(r['tables'], [])
        self.assertEqual(r['summary']['filtered_tables'], 1)
        self.assertEqual(r['documents']['primary']['facts'], {})
        self.assertEqual(r['documents']['primary']['diagnostics']['filtered_tables'][0]['excluded_fact_ids'], ['f1'])

    def test_financial_paragraph_and_legends_are_filtered_but_kpis_remain(self):
        body = '<h2>A Year in Review</h2><table><tr><td>Revenue was $54.2 billion, down 14%.</td></tr></table>'
        body += '<table><tr><td>Revenue</td><td>Gross margin</td></tr></table>'
        body += '<table><tr><td>$54.2B</td><td>40.0%</td></tr><tr><td>GAAP revenue</td><td>GAAP gross margin</td></tr></table>'
        r = self.financial(body)
        self.assertEqual(len(r['tables']), 1)
        self.assertEqual(r['tables'][0]['title'], 'A Year in Review')
        self.assertEqual(r['tables'][0]['page_table_index'], 3)
        self.assertEqual(r['tables'][0]['fact_ids'], [])
        self.assertEqual(r['tables'][0]['cells'][0]['display_text'], '$54.2B')

    def test_bullets_footnotes_and_year_headers_are_not_amounts(self):
        bodies = [
            '<table><tr><td>-</td><td>Revenue fell $5 billion.</td></tr><tr><td>+</td><td>Cost savings of $1 million.</td></tr></table>',
            '<table><tr><td>(1)</td><td>Cash includes deposits at banks.</td></tr><tr><td>(2)</td><td>See Note 3.</td></tr></table>',
            '<table><tr><td>Year</td><td>2025</td><td>2024</td></tr></table>',
            '<table><tr><td>Tax years</td><td>Status</td></tr><tr><td>2015–2018</td><td>Examination</td></tr></table>',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.financial(body)['tables'], [])

    def test_page_reference_column_does_not_make_a_financial_table(self):
        body = '<table><tr><td>Topic</td><td>Page reference</td></tr><tr><td>Revenue</td><td>100</td></tr>'
        body += '<tr><td>Derivatives</td><td>211–212</td></tr></table>'
        self.assertEqual(self.financial(body)['tables'], [])

    def test_year_shaped_data_and_split_currency_are_kept(self):
        body = '<table><tr><td>Year</td><td>2025</td><td>2024</td></tr>'
        body += '<tr><td>Cash</td><td>2024</td><td>2023</td></tr></table>'
        self.assertEqual(len(self.financial(body)['tables']), 1)
        body = '<table><tr><td>Cash</td><td>$</td><td>10</td></tr></table>'
        self.assertEqual(len(self.financial(body)['tables']), 1)

    def test_footnoted_amounts_short_suffixes_and_ranges(self):
        for raw in ('$54.2B', '$(11.9)B', '1,234 (a)', '9.1%(1)', '0.5%–1.5%', '$76 billion'):
            with self.subTest(raw=raw):
                r = self.financial(table(raw))
                self.assertEqual(len(r['tables']), 1)
                self.assertEqual(r['tables'][0]['cells'][1]['display_text'], raw)
                self.assertEqual(r['tables'][0]['cells'][1]['fact_ids'], [])

    def test_all_dash_nil_and_unsupported_numeric_tables_remain(self):
        for value in ('—', number('', **{'xsi:nil': 'true'}), number('invalid')):
            with self.subTest(value=value):
                self.assertEqual(len(self.financial(table(value))['tables']), 1)

    def test_single_data_row_under_headers_is_kept(self):
        body = '<table><tr><td>Year</td><td>Revenue</td></tr><tr><td>2025</td><td>100</td></tr></table>'
        self.assertEqual(len(self.financial(body)['tables']), 1)

    def test_credit_ratings_are_financial_categorical_data(self):
        body = '<table><tr><td>Agency</td><td>Long-term</td><td>Short-term</td></tr>'
        body += '<tr><td>Agency one</td><td>Aa2</td><td>P-1</td></tr><tr><td>Agency two</td><td>A+</td><td>F1+</td></tr></table>'
        self.assertEqual(len(self.financial(body)['tables']), 1)

    def test_executive_roster_is_not_financial(self):
        body = '<table><tr><td>Name</td><td>Age</td><td>Position</td></tr>'
        body += '<tr><td>Jane Example</td><td>55</td><td>CEO</td></tr></table>'
        self.assertEqual(self.financial(body)['tables'], [])

    def test_opt_out_retains_original_inventory_and_ids(self):
        body = '<table><tr><td>Financial commentary with $50 inside a sentence.</td></tr></table>' + table()
        financial = self.financial(body)
        all_tables = self.financial(body, financial_only=False)
        self.assertEqual(financial['table_filter'], 'financial')
        self.assertEqual(all_tables['table_filter'], 'all')
        self.assertEqual(len(all_tables['tables']), 2)
        self.assertEqual(financial['tables'][0], all_tables['tables'][1])
        self.assertEqual(financial['documents']['primary']['facts'], all_tables['documents']['primary']['facts'])

    def test_cli_default_and_include_layout_flag(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(tmp) / 'sample.htm'
            path.write_bytes(filing('<h1>Item 8. Statements</h1><table><tr><td>Some prose.</td></tr></table>' + table()))
            args = [str(path), '--year', '2025', '--items', '8', '--output-dir', tmp]
            self.assertEqual(x.main(args), 0)
            self.assertEqual(json.loads((Path(tmp) / 'result_xbrl.json').read_text())['summary']['tables'], 1)
            self.assertEqual(x.main([*args, '--include-layout-tables']), 0)
            self.assertEqual(json.loads((Path(tmp) / 'result_xbrl.json').read_text())['summary']['tables'], 2)


@unittest.skipUnless(os.environ.get('SEC_XBRL_INTEGRATION') == '1', 'Set SEC_XBRL_INTEGRATION=1 to audit cached real filings')
class CachedFilingTests(unittest.TestCase):
    def test_all_numeric_table_facts_match_source_and_key_financials(self):
        from decimal import Decimal, localcontext
        from lxml import etree
        root_dir = Path(__file__).resolve().parents[1]
        jobs = [('Intel', 2023, 'intc-20231230.htm', '1689000000'),
                ('Intel', 2024, 'intc-20241228.htm', '-18756000000'),
                ('Intel', 2025, 'intc-20251227.htm', '-267000000'),
                ('NVIDIA', 2025, 'nvda-20250126.htm', '72880000000'),
                ('JPMorgan', 2025, 'jpm-20251231.htm', '57048000000'),
                ('Wells Fargo', 2025, 'wells_fargo_2025/wfc-20251231_d2.htm', '21338000000')]
        for company, year, filename, net_income in jobs:
            with self.subTest(company=company, year=year):
                path = root_dir / 'data/source_cache' / filename
                self.assertTrue(path.is_file(), f'Missing cached original filing: {path}')
                r = x.extract_tables(path.read_bytes(), company, year, str(path), report_loader=lambda s: Path(s).read_bytes())
                self.assertEqual(r['summary']['layout_errors'], 0)
                self.assertEqual(r['summary']['tables_without_verified_page'], 0)
                annual_net_income = set()
                for document_id, document in r['documents'].items():
                    original = etree.fromstring(Path(document['source']).read_bytes(), etree.XMLParser(resolve_entities=False, no_network=True))
                    namespaces = {k: v for k, v in original.nsmap.items() if k}
                    selected_tables = {t['source_locator'] for t in r['tables'] if t['document_id'] == document_id}
                    expected_ids = set()
                    for node in original.iter('{http://www.xbrl.org/2013/inlineXBRL}nonFraction'):
                        owner = next(node.iterancestors('{http://www.w3.org/1999/xhtml}table'), None)
                        if owner is not None and original.getroottree().getpath(owner) in selected_tables:
                            expected_ids.add(node.get('id'))
                    actual_ids = {f['source_id'] for f in document['facts'].values() if f['kind'] == 'nonFraction'}
                    self.assertEqual(expected_ids, actual_ids)
                    for f in document['facts'].values():
                        matches = original.xpath(f['source_locator'], namespaces=namespaces)
                        self.assertEqual(len(matches), 1)
                        node = matches[0]
                        self.assertEqual(node.get('name'), f['concept']['name'])
                        self.assertEqual(node.get('contextRef'), f['context_ref'])
                        if f['kind'] != 'nonFraction' or f['nil']:
                            continue
                        self.assertEqual(f['status'], 'ok')
                        raw = ''.join(node.itertext()).strip()
                        fmt = node.get('format', '').split(':')[-1]
                        self.assertIn(fmt, {'', 'fixed-zero', 'num-dot-decimal', 'numdotdecimal'})
                        raw = '0' if fmt == 'fixed-zero' else raw.replace(',', '')
                        with localcontext() as ctx:
                            ctx.prec = 128
                            expected = Decimal(raw) * Decimal(10) ** int(node.get('scale', '0'))
                            if node.get('sign') == '-':
                                expected = -expected
                            self.assertEqual(Decimal(f['value']), expected)
                        context = document['contexts'][f['context_ref']]
                        if (f['concept']['local_name'] == 'NetIncomeLoss' and not context['dimensions']
                                and context['period'].get('endDate', '').startswith(str(year))):
                            annual_net_income.add(f['value'])
                self.assertIn(net_income, annual_net_income)
                if company == 'Intel' and year == 2023:
                    self.assertEqual({g['inferred_page'] for g in r['documents']['primary']['diagnostics']['image_only_pages']}, {'3', '4'})
                    page5 = [t for t in r['tables'] if t['page'] == '5']
                    self.assertEqual([t['table_id'] for t in page5], ['intel_2023_primary_5_t3'])
                    self.assertEqual(page5[0]['title'], 'A Year in Review')
                    self.assertIn('$54.2B', {c['display_text'] for c in page5[0]['cells']})
                    self.assertEqual(page5[0]['fact_ids'], [])
                if company == 'NVIDIA':
                    self.assertTrue(any(t['item'] == '8' and t['physical_item'] == '15' for t in r['tables']))
                if company == 'JPMorgan':
                    refs = r['documents']['primary']['scope']['page_references']
                    self.assertEqual(next(e['pages'] for e in refs if e['item'] == '7'), list(range(46, 161)))
                    self.assertEqual(next(e['pages'] for e in refs if e['item'] == '8'), list(range(162, 315)))
                if company == 'Wells Fargo':
                    self.assertIn('report1', r['documents'])
                    self.assertTrue(any(t['document_id'] == 'report1' and t['item'] == '8' for t in r['tables']))
                    self.assertTrue(any(t['table_id'] == 'wells_fargo_2025_report1_47_t2' for t in r['tables']))


if __name__ == '__main__':
    unittest.main()
