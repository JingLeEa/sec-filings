"""Intel-only checks for separate HTML Item 7 and API-only Item 8 outputs."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import extract_10k_items_7_8 as hybrid
from test_extract_item8_xbrl_api import response, URL

def intel_fixture():
    return b'''<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:us-gaap="http://fasb.org/us-gaap/2023"
      xmlns:intc="http://www.intel.com/20231230"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12">
      <body><ix:header><ix:resources>
      <xbrli:context id="annual"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000050863</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      </ix:resources></ix:header>
      <h2 style="font-weight:bold">Item 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</h2>
      <p>There was a change. The following table shows net revenue.</p>
      <table><tr><th>Metric</th><th>2023</th></tr><tr><td>Revenue</td><td>54,228</td></tr></table>
      <div style="position:absolute;bottom:0">30</div><div style="page-break-after:always"/>
      <h2 style="font-weight:bold">Item 8. FINANCIAL STATEMENTS</h2>
      <table><caption>Consolidated Statements of Income</caption><tr><th>Metric</th><th>2023</th></tr>
      <tr><td>Net revenue</td><td><ix:nonFraction id="f-44" name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
      contextRef="annual" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">54,228</ix:nonFraction></td></tr></table>
      <div style="position:absolute;bottom:0">74</div><div style="page-break-after:always"/>
      <h2 style="font-weight:bold">Item 9. CHANGES IN ACCOUNTANTS</h2>
      </body></html>'''


class PipelineTests(unittest.TestCase):
    def test_html_item7_retains_title_grid_and_display_values(self):
        result = hybrid.extract_item7(intel_fixture(), 'Intel', 2023, 'intel.htm')
        self.assertEqual(result['summary']['tables'], 1)
        table = result['tables'][0]
        self.assertEqual(table['title'], 'The following table shows net revenue.')
        cell = next(c for c in table['cells'] if c['display_text'] == '54,228')
        self.assertEqual(cell['html_value']['value'], '54228')
        self.assertNotIn('fact_ids', cell)
        self.assertNotIn('facts', result['documents']['primary'])

    def test_item8_uses_api_values_without_html_scope_or_value_matching(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, cached = root / 'intel.htm', root / 'api.json'
            source.write_bytes(intel_fixture().replace(b'>54,228</ix:nonFraction>', b'>1</ix:nonFraction>'))
            cached.write_text(json.dumps(response()))
            args = [str(source), '--company', 'Intel', '--year', '2023', '--filing-url', URL,
                    '--xbrl-json', str(cached), '--output-dir', str(root)]
            with patch.object(hybrid.ix, 'extract_tables', wraps=hybrid.ix.extract_tables) as extract_html:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(hybrid.main(args), 0)
                self.assertEqual(extract_html.call_count, 1)
                self.assertEqual(extract_html.call_args.kwargs['items'], ('7',))
            item8 = json.loads((root / 'item_8_xbrl.json').read_text())
            self.assertEqual(item8['tables'][0]['rows'][0]['facts'][0]['value'], '54228000000')
            self.assertEqual(item8['schema_version'], 'xbrl-api-groups-1.0')
            self.assertNotIn('documents', item8)
            self.assertFalse((root / 'result_hybrid.json').exists())

    def test_strict_refuses_both_outputs_on_invalid_api_fact(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, saved = root / 'intel.htm', root / 'api.json'
            source.write_bytes(intel_fixture())
            payload = response()
            saved.write_text(json.dumps(payload))
            args = [str(source), '--company', 'Intel', '--year', '2023', '--filing-url', URL,
                    '--xbrl-json', str(saved), '--output-dir', str(root), '--strict']
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(hybrid.main(args), 0)
                paths = [root / n for n in ('item_7_html.json', 'item_8_xbrl.json', 'sec_api_xbrl.json')]
                previous = [p.read_bytes() for p in paths]
                payload['IncomeTaxesDetails']['DeferredTaxAssetsGross']['value'] = 'bad number'
                saved.write_text(json.dumps(payload))
                self.assertEqual(hybrid.main(args), 1)
                self.assertEqual([p.read_bytes() for p in paths], previous)
                with patch.dict(os.environ, {'SEC_API_KEY': ''}):
                    self.assertEqual(hybrid.main([str(source), '--year', '2023']), 1)

    def test_explicit_html_suffix_percent_and_dash(self):
        self.assertEqual(hybrid.html_value('(12.50)')['value'], '-12.50')
        self.assertEqual(hybrid.html_value('6.23%')['unit'], '%')
        self.assertEqual(hybrid.html_value('$(11.9)B')['value'], '-11.9')
        self.assertEqual(hybrid.html_value('$(11.9)B')['display_scale'], 'billions')
        self.assertIsNone(hybrid.html_value('—')['value'])


@unittest.skipUnless(os.environ.get('SEC_INTEL_HYBRID_INTEGRATION') == '1', 'Intel integration is opt-in')
class IntelHtmlIntegrationTests(unittest.TestCase):
    def test_item7_2023_2024_2025_matches_existing_tables(self):
        for year, date, count in [(2023, '20231230', 13), (2024, '20241228', 12), (2025, '20251227', 13)]:
            with self.subTest(year=year):
                source = Path(f'data/source_cache/intc-{date}.htm')
                result = hybrid.extract_item7(source.read_bytes(), 'Intel', year, str(source))
                self.assertEqual(result['summary']['tables'], count)
                old = json.loads(Path(f'data/table_output/intel_{year}_items_1_1a_7_8_xbrl_tables/result_xbrl.json').read_text())
                expected = {t['table_id']: t for t in old['tables'] if '7' in t['referenced_items']}
                self.assertEqual({t['table_id'] for t in result['tables']}, set(expected))
                for table in result['tables']:
                    prior = expected[table['table_id']]
                    for key in ('grid', 'title', 'title_source', 'page', 'source_locator'):
                        self.assertEqual(table[key], prior[key])
                    self.assertEqual([c['display_text'] for c in table['cells']], [c['display_text'] for c in prior['cells']])


if __name__ == '__main__':
    unittest.main()
