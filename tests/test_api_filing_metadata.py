"""Metadata layouts, verified DEI fallbacks and rejected-response retries."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import api_filing_metadata as metadata
import extract_item8_xbrl_api as api
import extract_10k_tables_xbrl as ix
import run_api_metrics_pipeline as runner
from test_run_api_metrics_pipeline import setup, run
from test_export_api_metrics import fixture
from test_extract_item8_xbrl_api import URL


def with_dei(data):
    _, payload = fixture()
    fields = dict(payload['CoverPage'])
    fields['DocumentPeriodEndDate'] = 'December 30, 2023'
    facts = ''.join(f'<ix:nonNumeric xmlns:dei="http://xbrl.sec.gov/dei/2023" '
        f'xmlns:metaformat="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" '
        f'name="dei:{key}" contextRef="annual" id="metadata-{key}" '
        + ('format="metaformat:date-monthname-day-year-en" ' if key == 'DocumentPeriodEndDate' else '')
        + f'>{value}</ix:nonNumeric>' for key, value in fields.items())
    return data.replace(b'</body>', ('<ix:hidden>' + facts + '</ix:hidden></body>').encode())


class IdentityMetadataTests(unittest.TestCase):
    def test_api_cover_and_top_level_are_equivalent_without_rewriting(self):
        data, payload = fixture()
        data = with_dei(data)
        standard = metadata.resolve(payload, data, {'htm-url': URL}, 2023)
        self.assertIsNone(standard['evidence'])
        top = deepcopy(payload)
        top.update(top.pop('CoverPage'))
        original = deepcopy(top)
        result = metadata.resolve(top, data, {'htm-url': URL}, 2023)
        self.assertEqual(result['values'], standard['values'])
        self.assertEqual(result['evidence']['DocumentFiscalYearFocus']['api_paths'], ['/DocumentFiscalYearFocus'])
        self.assertEqual(top, original)
        both = deepcopy(payload)
        both['DocumentFiscalYearFocus'] = [{'value': '2023'}, {'value': '2023'}]
        self.assertEqual(metadata.resolve(both, data, {'htm-url': URL})['values'], standard['values'])

    def test_missing_api_metadata_uses_official_dei_with_provenance(self):
        data, payload = fixture()
        data = with_dei(data)
        original = deepcopy(payload)
        payload.pop('CoverPage')
        result = metadata.resolve(payload, data, {'htm-url': URL}, 2023)
        self.assertEqual(result['values'], original['CoverPage'])
        self.assertTrue(all(e['method'] == 'filing_dei' for e in result['evidence'].values()))
        self.assertEqual(result['evidence']['DocumentFiscalYearFocus']['facts'][0]['source_fact_id'],
                         'metadata-DocumentFiscalYearFocus')
        self.assertNotIn('CoverPage', payload)

    def test_missing_primary_contexts_can_use_bound_sec_url_for_issuer(self):
        data, _ = fixture()
        data = with_dei(data)
        from lxml import etree
        root = etree.fromstring(data)
        for n in list(root.iter()):
            if (n.tag == f'{{{ix.XBRLI}}}context' or n.get('name') == 'dei:EntityCentralIndexKey'):
                n.getparent().remove(n)
        data = etree.tostring(root)
        result = metadata.resolve({'Income': {}}, data, {'htm-url': URL}, 2023)
        self.assertEqual(result['values']['EntityCentralIndexKey'], '50863')
        self.assertEqual(result['evidence']['EntityCentralIndexKey']['method'], 'filing_request_url')
        self.assertFalse(result['evidence']['DocumentFiscalYearFocus']['facts'][0]['context_available'])
        with self.assertRaisesRegex(ValueError, 'verified issuer CIK'):
            metadata.resolve({'Income': {}}, data, {'accession-no': '0000050863-24-000010'}, 2023)

    def test_conflicting_invalid_or_unverifiable_identities_fail(self):
        data, payload = fixture()
        data = with_dei(data)
        cases = []
        for key, wrong in [('DocumentFiscalYearFocus', '2022'), ('EntityCentralIndexKey', '123'),
                           ('DocumentType', '10-Q'), ('DocumentPeriodEndDate', '2023-12-31')]:
            changed = deepcopy(payload)
            changed[key] = wrong
            cases.append((changed, data, {'htm-url': URL}))
            changed = deepcopy(payload)
            changed['CoverPage'][key] = wrong
            cases.append((changed, data, {'htm-url': URL}))
        for bad in [None, [], {}, '', ['2023', '2022'], {'value': '2023', 'xsi:nil': 'true'},
                    {'value': '2023', 'segment': {'dimension': 'x:Axis', 'value': 'x:Member'}}]:
            changed = deepcopy(payload)
            changed['CoverPage']['DocumentFiscalYearFocus'] = bad
            cases.append((changed, data, {'htm-url': URL}))
        no_year = data.replace(b'name="dei:DocumentFiscalYearFocus"', b'name="dei:Other"')
        cases.append(({'Income': {}}, no_year, {'htm-url': URL}))
        custom_year = data.replace(b'name="dei:DocumentFiscalYearFocus"',
                                  b'xmlns:custom="urn:custom" name="custom:DocumentFiscalYearFocus"')
        cases.append(({'Income': {}}, custom_year, {'htm-url': URL}))
        conflict = data.replace(b'</body>', b'<ix:nonNumeric xmlns:dei="http://xbrl.sec.gov/dei/2023" '
            b'name="dei:DocumentFiscalYearFocus" contextRef="annual">2022</ix:nonNumeric></body>')
        cases.append((payload, conflict, {'htm-url': URL}))
        cases.append(({'Income': {}}, data.replace(b'contextRef="annual" id="metadata-', b'id="metadata-'), {'htm-url': URL}))
        cases.append((payload, data, {'htm-url': URL.replace('/50863/', '/123/')}))
        for p, source, params in cases:
            with self.subTest(payload=p.get('CoverPage')), self.assertRaises(ValueError):
                metadata.resolve(p, source, params, 2023)

    def test_english_month_dates_are_bounded_and_registry_checked(self):
        from lxml import etree
        for registry, name in [('2020-02-12', 'date-monthname-day-year-en'),
                               ('2015-02-26', 'datemonthdayyearen')]:
            node = etree.fromstring(f'<n xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/{registry}" format="ixt:{name}"/>')
            self.assertEqual(ix.transformed_text(node, ' June 30 , 2025 '), '2025-06-30')
            for value in ['February 30, 2025', 'June 30, 25', 'June 30, 2025 or July 1, 2025']:
                with self.assertRaises(ValueError):
                    ix.transformed_text(node, value)
        node = etree.fromstring('<n xmlns:ixt="urn:unknown" format="ixt:date-monthname-day-year-en"/>')
        with self.assertRaises(ValueError):
            ix.transformed_text(node, 'June 30, 2025')

    def test_pipeline_top_level_and_filing_fallback_keep_the_same_metrics(self):
        results = []
        for mode in ['cover', 'top', 'filing']:
            with self.subTest(mode=mode), TemporaryDirectory() as tmp:
                filing, response, output, common, data, payload = setup(Path(tmp))
                data = with_dei(data)
                if mode == 'top':
                    payload.update(payload.pop('CoverPage'))
                elif mode == 'filing':
                    payload.pop('CoverPage')
                filing.write_bytes(data)
                response.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                    'source_sha256': runner.export.membership.sha(data), 'response': payload}))
                before = response.read_bytes()
                code, log = run(['--filing', str(filing), '--xbrl-json', str(response), '--offline', *common])
                self.assertEqual(code, 0, log)
                result = json.loads((output / 'intel_2023_api_metrics_with_values.json').read_text())
                results.append(result['metrics'])
                self.assertEqual(before, response.read_bytes())
                self.assertEqual([p.name for p in output.iterdir()], ['intel_2023_api_metrics_with_values.json'])
                if mode != 'cover':
                    self.assertIn('identity_metadata', result['verification']['source'])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def test_rejected_identity_response_is_cached_and_revalidated_without_another_call(self):
        with TemporaryDirectory() as tmp:
            _, _, output, common, data, payload = setup(Path(tmp))
            payload['DocumentFiscalYearFocus'] = '2022'
            selected = {'url': URL, 'accessionNumber': '0000050863-24-000010', 'reportDate': '2023-12-30'}
            with patch.dict(os.environ, {'SEC_API_KEY': 'test-key-not-saved', 'SEC_USER_AGENT': 'Test test@example.com'}), \
                 patch.object(runner.sec, 'SecClient') as client, \
                 patch.object(runner.sec, 'discover_sec_filings', return_value=({}, {2023: selected})), \
                 patch.object(runner.api, 'fetch_xbrl_json', return_value=payload) as fetch:
                client.return_value.get.return_value = data
                for _ in range(2):
                    code, log = run(['--ticker', 'INTC', *common])
                    self.assertEqual(code, 1)
                    self.assertIn('Conflicting API metadata', log)
                    self.assertFalse(output.exists())
                self.assertEqual(fetch.call_count, 1)
            folder = Path(tmp) / 'sec_cache/api_metrics' / api.digest({'htm-url': URL})
            self.assertEqual(json.loads((folder / 'sec_api_xbrl.json').read_text())['response'], payload)
            self.assertEqual(json.loads((folder / 'identity_validation.json').read_text())['status'], 'rejected')
            for p in folder.iterdir():
                self.assertNotIn(b'test-key-not-saved', p.read_bytes())


if __name__ == '__main__':
    unittest.main()
