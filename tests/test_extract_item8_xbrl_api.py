"""API-only Item 8 tests. All filing fixtures and integration inputs are Intel."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import extract_item8_xbrl_api as api

URL = 'https://www.sec.gov/Archives/edgar/data/50863/000005086324000010/intc-20231230.htm'
PARAMETERS = {'htm-url': URL}


def response():
    return {
        'CoverPage': {'EntityCentralIndexKey': '0000050863', 'DocumentFiscalYearFocus': '2023', 'DocumentType': ['10-K']},
        'AuditInformation': {'AuditorName': 'Example'},
        'StatementsOfIncome': {'RevenueFromContractWithCustomerExcludingAssessedTax': [
            {'value': '54228000000', 'unitRef': 'usd', 'decimals': '-6',
             'period': {'startDate': '2023-01-01', 'endDate': '2023-12-30'}}]},
        'AccountingPolicies': {'PolicyTextBlock': '<div><table><tr><td>Never parse this HTML</td></tr></table></div>'},
        'IncomeTaxesDetails': {'DeferredTaxAssetsGross': {'value': '11881000000', 'unitRef': 'usd',
            'decimals': '-6', 'period': {'instant': '2022-12-31'}}},
    }


def extract(payload=None, **kwargs):
    return api.extract_item8(payload or response(), 'Intel', 2023, PARAMETERS, **kwargs)


class ApiGroupTests(unittest.TestCase):
    def test_groups_rows_direct_values_without_html_fields(self):
        result = extract()
        self.assertEqual([t['api_group'] for t in result['tables']], ['StatementsOfIncome', 'IncomeTaxesDetails'])
        fact = result['tables'][0]['rows'][0]['facts'][0]
        self.assertEqual(fact['value'], '54228000000')
        self.assertEqual(fact['period']['endDate'], '2023-12-30')
        self.assertEqual(fact['dimensions'], [])
        self.assertEqual(fact['unit_ref'], 'usd')
        self.assertEqual(fact['status'], 'reported')
        self.assertFalse(result['scope']['item_boundaries_verified'])
        self.assertEqual(result['summary']['numeric_facts'], 2)
        text = json.dumps(result)
        for name in ('source_locator', 'position_id', 'inline_check', 'fact_ids', '<table>'):
            self.assertNotIn(name, text)
        self.assertEqual(fact['api_pointer'], '/StatementsOfIncome/RevenueFromContractWithCustomerExcludingAssessedTax/0')

    def test_dimensions_periods_and_rounding_variants_are_preserved(self):
        payload = response()
        records = payload['StatementsOfIncome']['RevenueFromContractWithCustomerExcludingAssessedTax']
        raw = deepcopy(records[0])
        records.extend([deepcopy(raw), {**raw, 'value': '54200000000', 'decimals': '-8'},
                        {**raw, 'value': '123', 'segment': [
                            {'dimension': 'intc:SegmentAxis', 'value': 'intc:ClientMember'},
                            {'dimension': 'srt:RegionAxis', 'value': 'country:US'}]},
                        {**raw, 'value': '99', 'period': {'instant': '2023-12-30'}}])
        row = extract(payload)['tables'][0]['rows'][0]
        self.assertEqual(len(row['facts']), 5)
        self.assertEqual(len({f['fact_id'] for f in row['facts']}), 5)
        self.assertEqual(len(row['facts'][3]['dimensions']), 2)
        self.assertEqual(len(row['context_variants']), 1)
        self.assertEqual(row['context_variants'][0]['kind'], 'multiple_reported_values')
        self.assertEqual(len(row['context_variants'][0]['fact_ids']), 3)

    def test_nil_duration_and_missing_context_are_distinct(self):
        payload = response()
        payload['IncomeTaxesDetails'].update({
            'CommitmentsAndContingencies': {'xsi:nil': 'true', 'unitRef': 'usd', 'period': {'instant': '2023-12-30'}},
            'UsefulLife': {'value': 'P9Y1M6D', 'period': {'instant': '2023-12-30'}},
            'Enumeration': ['http://fasb.org/us-gaap/2023#OtherAssetsCurrent']})
        rows = {r['concept']: r for r in extract(payload)['tables'][1]['rows']}
        self.assertEqual(rows['CommitmentsAndContingencies']['facts'][0]['status'], 'nil')
        duration = rows['UsefulLife']['facts'][0]
        self.assertEqual((duration['value'], duration['value_type'], duration['status']), ('P9Y1M6D', 'duration', 'reported'))
        unscoped = rows['Enumeration']['facts'][0]
        self.assertEqual(unscoped['status'], 'incomplete')
        self.assertIsNone(unscoped['period'])
        self.assertIsNone(unscoped['dimensions'])
        with self.assertRaisesRegex(ValueError, 'missing metadata'):
            api.check_strict(extract(payload))

    def test_custom_concepts_units_and_raw_fields_never_infer_namespaces(self):
        payload = response()
        payload['IncomeTaxesDetails'] = {'intc_CustomConcept': {'value': '7.25', 'unitRef': 'customUnit',
            'scale': 6, 'period': {'instant': '2023-12-30'}, 'customMetadata': 'keep'}}
        row = extract(payload)['tables'][1]['rows'][0]
        self.assertEqual(row['concept'], 'intc_CustomConcept')
        self.assertEqual(row['facts'][0]['value'], '7.25')
        self.assertEqual(row['facts'][0]['raw_record']['customMetadata'], 'keep')
        self.assertNotIn('namespace', row)

    def test_invalid_numeric_period_and_dimensions_preserve_raw(self):
        baseline = response()['IncomeTaxesDetails']['DeferredTaxAssetsGross']
        for modification in ({'value': 'NaN'}, {'value': True}, {'value': '1,234'},
                             {'value': '1e10001'}, {'period': {'instant': '2023-02-30'}},
                             {'period': {'startDate': '2023-12-31', 'endDate': '2023-01-01'}},
                             {'segment': [{'dimension': 'a', 'value': 'b'}, {'dimension': 'a', 'value': 'c'}]},
                             {'xsi:nil': 'true'}, {'xsi:nil': 'invalid'}, {'value': None}):
            raw = {**baseline, **modification}
            fact = api.normalise_fact(raw, '/test')
            self.assertEqual(fact['status'], 'invalid', modification)
            self.assertEqual(fact['raw_record'], raw)

    def test_identity_and_group_selection(self):
        self.assertEqual(extract(groups=['IncomeTaxesDetails'])['summary']['tables'], 1)
        with self.assertRaises(ValueError):
            extract(groups=['MissingGroup'])
        for key, value in [('EntityCentralIndexKey', '999'), ('DocumentFiscalYearFocus', '2024'), ('DocumentType', '10-Q')]:
            payload = response()
            payload['CoverPage'][key] = value
            with self.assertRaises(ValueError):
                extract(payload)
        with self.assertRaises(ValueError):
            api.fetch_xbrl_json({'token': 'must-not-be-a-query-parameter'}, 'secret-key')

    def test_transport_gzip_auth_and_exact_numbers(self):
        with patch.object(api, 'urlopen') as open_url:
            http = open_url.return_value.__enter__.return_value
            http.headers = {'Content-Encoding': 'gzip'}
            http.read.return_value = gzip.compress(json.dumps(response()).encode())
            self.assertEqual(api.fetch_xbrl_json(PARAMETERS, 'secret-key'), response())
            request = open_url.call_args.args[0]
            self.assertNotIn('secret-key', request.full_url)
            self.assertEqual(request.get_header('Authorization'), 'secret-key')
            http.read.return_value = b'bad gzip'
            with self.assertRaisesRegex(ValueError, 'valid JSON'):
                api.fetch_xbrl_json(PARAMETERS, 'secret-key')
        with patch.object(api, 'urlopen', side_effect=HTTPError(URL, 401, 'secret-key', None, None)):
            with self.assertRaisesRegex(ValueError, 'HTTP 401') as error:
                api.fetch_xbrl_json(PARAMETERS, 'secret-key')
            self.assertNotIn('secret-key', str(error.exception))
        self.assertEqual(api.read_json('{"n": 12345678901234567890.123456789}')['n'], '12345678901234567890.123456789')

    def test_standalone_cli_without_html_imports_or_filing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / 'api.json'
            saved.write_text(json.dumps({'schema_version': 'sec-api-cache-1.0', 'filing_url': URL,
                                         'source_sha256': 'no-html-file-exists', 'response': response()}))
            command = [sys.executable, '-c',
                       'import sys; import extract_item8_xbrl_api as a; '
                       'assert "lxml" not in sys.modules; '
                       'assert "extract_10k_tables_xbrl" not in sys.modules; '
                       'raise SystemExit(a.main(sys.argv[1:]))',
                       '--company', 'Intel', '--year', '2023', '--xbrl-json', str(saved), '--output-dir', str(root)]
            run = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads((root / 'item_8_xbrl.json').read_text())
            self.assertEqual(result['summary']['tables'], 2)
            with self.assertRaisesRegex(ValueError, 'different filing'):
                api.load_response(saved, {'accession-no': '0000050863-25-000009'})

    def test_strict_preserves_existing_output_and_api_failure_writes_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'api.json'
            payload = response()
            raw.write_text(json.dumps(payload))
            args = ['--company', 'Intel', '--year', '2023', '--filing-url', URL, '--xbrl-json', str(raw), '--output-dir', str(root)]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(api.main(args + ['--strict']), 0)
                before = (root / 'item_8_xbrl.json').read_bytes()
                payload['IncomeTaxesDetails']['Unscoped'] = ['Interest cost']
                raw.write_text(json.dumps(payload))
                self.assertEqual(api.main(args + ['--strict']), 1)
                self.assertEqual((root / 'item_8_xbrl.json').read_bytes(), before)
                raw.write_text('{"error":"invalid token"}')
                self.assertEqual(api.main(args), 1)
                self.assertEqual((root / 'item_8_xbrl.json').read_bytes(), before)


@unittest.skipUnless(os.environ.get('SEC_INTEL_HYBRID_INTEGRATION') == '1', 'Intel integration is opt-in')
class IntelApiIntegrationTests(unittest.TestCase):
    def test_all_selected_source_facts_preserved_from_live_response(self):
        cached = Path('data/table_output/intel_2023_items_7_8_hybrid_tables/sec_api_xbrl.json')
        payload, parameters = api.load_response(cached)
        result = api.extract_item8(payload, 'Intel', 2023, parameters)
        self.assertEqual(result['summary']['tables'], 49)
        self.assertEqual(result['summary']['numeric_facts'], 2192)
        self.assertEqual(result['summary']['fact_status'], {'reported': 2234, 'nil': 2, 'incomplete': 13})
        pointers = set()
        for table in result['tables']:
            for row in table['rows']:
                for fact in row['facts']:
                    value = payload
                    for part in fact['api_pointer'].split('/')[1:]:
                        part = part.replace('~1', '/').replace('~0', '~')
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    self.assertEqual(value, fact['raw_record'])
                    self.assertNotIn(fact['api_pointer'], pointers)
                    pointers.add(fact['api_pointer'])
                    if fact['value_type'] == 'number':
                        from decimal import Decimal
                        self.assertEqual(Decimal(fact['value']), Decimal(str(value['value'])))
        groups = {t['api_group'] for t in result['tables']}
        expected = set()
        for group in groups:
            for concept, content in payload[group].items():
                for i, raw in enumerate(content if isinstance(content, list) else [content]):
                    if api.is_text_block(concept, raw):
                        continue
                    expected.add(api.pointer(group, concept, i) if isinstance(content, list) else api.pointer(group, concept))
        self.assertEqual(pointers, expected)


if __name__ == '__main__':
    unittest.main()
