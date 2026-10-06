"""Opt-in real-filing checks. Use HTML_METRICS_INTEGRATION=1; never download.

The existing API caches bind the test inputs to SEC requests; API amounts are
not passed to the HTML exporter. Outputs are temporary and existing exports
are compared/read only. Table counts do not establish every value's accuracy.
"""
from contextlib import redirect_stderr, redirect_stdout
import gc
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec_disclosure.table_extraction import extract_html_metrics as cli
from sec_disclosure.table_extraction import html_filing_source as sources


# Cases from the earlier filing/section-boundary investigations.
CASES = [
    ('intel', 2023, '50863/000005086324000010/intc-20231230.htm'),
    ('intel', 2024, '50863/000005086325000009/intc-20241228.htm'),
    ('intel', 2025, '50863/000005086326000011/intc-20251227.htm'),
    ('micron', 2023, '723125/000072312523000054/mu-20230831.htm'),
    ('micron', 2025, '723125/000072312525000028/mu-20250828.htm'),
    ('jpmorgan', 2025, '19617/000162828026008131/jpm-20251231.htm'),
    ('wellsfargo', 2025, '72971/000007297126000133/wfc-20251231_d2.htm'),
    ('ffbc', 2025, '708955/000070895526000028/ffbc-20251231.htm'),
    ('j_w_mays', 2025, '54187/000120677425000720/mays4503731-10k.htm'),
    ('pebk', 2025, '1093672/000165495426002154/pebk_10k.htm'),
    ('iboc', 2025, '315709/000110465926020439/iboc-20251231x10k.htm'),
    ('ibcp', 2025, '39311/000003931126000009/ibcp-20251231_d2.htm'),
    ('kffb', 2025, '1297341/000121390025093967/ea0258344-10k_kentucky.htm'),
    ('dauch', 2025, '1062231/000106223126000020/dch-20251231.htm'),
]


@unittest.skipUnless(os.environ.get('HTML_METRICS_INTEGRATION') == '1', 'Requires cached historical edge-case filings')
class CachedFilingTests(unittest.TestCase):
    pass


def cached_test(company, year, document):
    def test(self):
        url = 'https://www.sec.gov/Archives/edgar/data/' + document
        folder = Path('data/sec_cache/api_metrics') / sources.api.digest({'htm-url': url})
        filing, response = folder / 'original_filing.htm', folder / 'sec_api_xbrl.json'
        if not filing.is_file() or not response.is_file():
            self.skipTest(f'Local filing cache missing: {company} {year}')
        saved = json.loads(response.read_text())
        self.assertEqual(saved['request'], {'htm-url': url})
        self.assertEqual(saved['source_sha256'], sources.sha(filing.read_bytes()))
        del saved
        before = {p: sources.sha(p.read_bytes()) for p in (filing, response)}
        with TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                 patch.object(cli.sec, 'SecClient', side_effect=AssertionError('No downloads in integration test')), \
                 patch.object(sources.api, 'fetch_xbrl_json', side_effect=AssertionError('No provider request')):
                code = cli.main(['--filing', str(filing), '--filing-url', url, '--company', company,
                                 '--year', str(year), '--offline', '--output-dir', tmp])
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            filename = f'{company}_{year}_html_metrics_with_values.json'
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [filename])
            result = json.loads((Path(tmp) / filename).read_text())
        self.assertEqual(result['verification']['identity']['cik'], sources.sec.normalize_cik(document.split('/')[0]))
        self.assertEqual(result['verification']['fiscal_year']['value'], year)
        self.assertEqual(result['requested_items'], ['1', '1A', '7'])
        self.assertEqual(list(result)[-1], 'classification_summary')
        self.assertTrue(all(set(v['items']) <= {'1', '1A', '7'} for m in result['metrics'] for v in m['value']))
        summary = result['classification_summary']
        self.assertEqual(len(result['table_classifications']), summary['financial'])
        self.assertTrue(all(d['classification'] == 'financial' for d in result['table_classifications']))
        self.assertEqual(summary['exported_values'], sum(len(m['value']) for m in result['metrics']))
        self.assertEqual(summary['total_tables'], sum(summary[k] for k in ('financial', 'narrative', 'review')))
        self.assertGreater(summary['total_tables'], 0)
        self.assertEqual(before, {p: sources.sha(p.read_bytes()) for p in before})
        if company == 'ibcp':
            report = result['verification']['documents']['report1']
            self.assertEqual(report['boundary_evidence']['items'], ['8'])
            self.assertTrue(all(r['item'] != '8' for r in report['incorporated_sections']))
        baseline = Path('tests/for_table_development') / company / filename
        if baseline.exists():
            old = json.loads(baseline.read_text())
            self.assertEqual(result['metrics'], old['metrics'])
            self.assertEqual(result['classification_summary'], old['classification_summary'])
        print(f'{company} FY{year}: {summary["financial"]} financial, {summary["narrative"]} narrative, '
              f'{summary["review"]} review; {summary["exported_values"]} values '
              f'({summary["values_needing_review"]} need review)', flush=True)
        del result
        gc.collect()
    return test


for company, year, document in CASES:
    setattr(CachedFilingTests, f'test_{company}_{year}', cached_test(company, year, document))


if __name__ == '__main__':
    unittest.main()
