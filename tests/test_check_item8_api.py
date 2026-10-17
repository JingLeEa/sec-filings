"""Location checks on fixtures and opt-in Intel/Micron API responses."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec_disclosure.table_extraction import check_item8_api as check
from test_extract_10k_items_7_8 import intel_fixture
from test_extract_item8_xbrl_api import URL

CONCEPT = 'RevenueFromContractWithCustomerExcludingAssessedTax'
FACT = {'value': '54228000000', 'unitRef': 'usd', 'decimals': '-6',
        'period': {'startDate': '2023-01-01', 'endDate': '2023-12-30'}}


def payload():
    return {'CoverPage': {'DocumentFiscalYearFocus': '2023', 'DocumentType': ['10-K'],
                          'EntityCentralIndexKey': '0000050863'},
            'StatementsOfIncome': {CONCEPT: [deepcopy(FACT)]}}


def source_fact(identifier='outside', concept=CONCEPT, value='54,228'):
    return (f'<ix:nonFraction id="{identifier}" name="us-gaap:{concept}" contextRef="annual" '
            f'unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">'
            f'{value}</ix:nonFraction>').encode()


def before_item8(content):
    return intel_fixture().replace(b'<p>There was a change.', content + b'<p>There was a change.')


def run(payload_data=None, data=None):
    return check.check_response(payload_data or payload(), data or intel_fixture(), 'intel.htm',
                                {'htm-url': URL}, 2023)


def group(result, name='StatementsOfIncome'):
    return next(g for g in result['groups'] if g['api_group'] == name)


class MembershipTests(unittest.TestCase):
    def test_exact_value_location_and_no_html_table_extraction(self):
        p = payload()
        original = deepcopy(p)
        with patch.object(check.ix, 'extract_tables', side_effect=AssertionError('must not extract tables')):
            r = run(p)
        g = group(r)
        self.assertEqual(g['status'], 'item_8')
        entry = g['entries'][0]
        self.assertEqual(entry['api_pointer'], '/response/StatementsOfIncome/' + CONCEPT + '/0')
        self.assertEqual(entry['evidence'][0]['source_fact_id'], 'f-44')
        self.assertEqual(entry['evidence'][0]['locations'][0]['page'], '74')
        self.assertTrue(entry['value_checked'])
        self.assertNotIn('value', entry)
        self.assertEqual(p, original)
        self.assertEqual(group(r, 'CoverPage')['status'], 'excluded_metadata')

    def test_wrong_period_unit_value_accuracy_and_dimensions_do_not_match(self):
        cases = [{'value': '1'}, {'unitRef': 'eur'}, {'decimals': '-3'},
                 {'period': {'instant': '2023-12-30'}},
                 {'segment': {'dimension': 'intc:RegionAxis', 'value': 'intc:EuropeMember'}}]
        for update in cases:
            with self.subTest(update=update):
                p = payload()
                p['StatementsOfIncome'][CONCEPT][0].update(update)
                self.assertEqual(group(run(p))['status'], 'unresolved')

    def test_xml_shaped_explicit_dimensions_preserve_all_members_and_input(self):
        members = [{'dimension': 'intc:ProductAxis', '$t': 'intc:MemoryMember'},
                   {'dimension': 'intc:RegionAxis', '$t': 'intc:UnitedStatesMember'}]
        for selected in (members[:1], members):
            segment = '<xbrli:segment>' + ''.join(
                f'<xbrldi:explicitMember dimension="{m["dimension"]}">{m["$t"]}</xbrldi:explicitMember>'
                for m in selected) + '</xbrli:segment>'
            data = intel_fixture().replace(b'</xbrli:entity>', segment.encode() + b'</xbrli:entity>')
            p = payload()
            original_segment = {'explicitMember': selected[0] if len(selected) == 1 else selected}
            p['StatementsOfIncome'][CONCEPT][0]['segment'] = deepcopy(original_segment)
            before = deepcopy(p)
            self.assertEqual(group(run(p, data))['status'], 'item_8')
            self.assertEqual(p, before)
            p['StatementsOfIncome'][CONCEPT][0]['segment'] = {'explicitMember': members[:1]}
            if len(selected) == 2:
                self.assertEqual(group(run(p, data))['status'], 'unresolved')
        for bad in ({'explicitMember': []},
                    {'explicitMember': members, 'typedMember': {'dimension': 'intc:Unknown'}},
                    {'explicitMember': {'dimension': 'intc:ProductAxis', '$t': 'intc:MemoryMember', 'extra': True}}):
            p = payload()
            p['StatementsOfIncome'][CONCEPT][0]['segment'] = bad
            self.assertEqual(group(run(p))['status'], 'unresolved')

    def test_standalone_concepts_are_not_counted_as_table_groups(self):
        block = (b'<ix:nonNumeric id="cyber" name="intc:CybersecurityTextBlock" contextRef="annual">'
                 b'Cybersecurity policies</ix:nonNumeric>'
                 b'<ix:nonNumeric id="flag" name="intc:CybersecurityFlag" contextRef="annual">true</ix:nonNumeric>')
        data = before_item8(block).replace(b'Item 7. MANAGEMENT', b'Item 1C. MANAGEMENT')
        p = payload()
        p['CybersecurityTextBlock'] = 'Cybersecurity policies'
        p['CybersecurityFlag'] = ['true', 'true']
        r = run(p, data)
        self.assertEqual(r['summary']['groups'], 2)
        self.assertEqual(r['summary']['standalone_concepts'], 2)
        self.assertEqual(r['summary']['top_level_entries'], 4)
        self.assertEqual(r['summary']['standalone_concept_status'], {'outside_item_8': 2})
        flags = next(c for c in r['standalone_concepts'] if c['concept'] == 'CybersecurityFlag')
        self.assertEqual([e['api_pointer'] for e in flags['entries']],
                         ['/response/CybersecurityFlag/0', '/response/CybersecurityFlag/1'])
        self.assertTrue(all(not e['value_checked'] for e in flags['entries']))
        self.assertEqual(flags['also_referenced_by_items'], ['1C'])

    def test_shared_fact_is_included_and_mixed_groups_are_not_wholly_included(self):
        data = before_item8(source_fact() + source_fact('other', 'OtherMetric', '9'))
        p = payload()
        r = run(p, data)
        entry = group(r)['entries'][0]
        self.assertEqual(entry['status'], 'item_8')
        self.assertTrue(entry['also_found_outside_item_8'])
        self.assertIn('7', entry['also_referenced_by_items'])
        p['StatementsOfIncome']['OtherMetric'] = [{**FACT, 'value': '9000000'}]
        self.assertEqual(group(run(p, data))['status'], 'mixed')
        p['StatementsOfIncome'].pop(CONCEPT)
        self.assertEqual(group(run(p, data))['status'], 'outside_item_8')

    def test_missing_source_is_unresolved_not_outside_and_pointers_are_escaped(self):
        p = payload()
        p['note/~'] = {'UnknownConcept': {'value': '7'}}
        r = run(p)
        g = group(r, 'note/~')
        self.assertEqual(g['status'], 'unresolved')
        self.assertEqual(g['entries'][0]['api_pointer'], '/response/note~1~0/UnknownConcept')
        p['StatementsOfIncome']['Missing'] = [{'value': '7'}]
        self.assertEqual(group(run(p))['status'], 'partial')

    def test_stripped_namespace_collision_is_not_guessed(self):
        data = before_item8(source_fact().replace(b'name="us-gaap:', b'name="intc:'))
        self.assertEqual(group(run(data=data))['status'], 'unresolved')
        p = payload()
        p['StatementsOfIncome']['us-gaap:' + CONCEPT] = p['StatementsOfIncome'].pop(CONCEPT)
        self.assertEqual(group(run(p, data))['status'], 'item_8')

    def test_hidden_only_facts_need_explicit_visible_references(self):
        data = intel_fixture()
        start = data.index(b'<ix:nonFraction')
        end = data.index(b'</ix:nonFraction>', start) + len(b'</ix:nonFraction>')
        fact = data[start:end]
        data = data[:start] + b'54,228' + data[end:]
        data = data.replace(b'</ix:header>', b'<ix:hidden>' + fact + b'</ix:hidden></ix:header>')
        self.assertEqual(group(run(data=data))['status'], 'unresolved')
        data = data.replace(b'<td>54,228</td>', b'<td><span style="-sec-ix-hidden:f-44">54,228</span></td>')
        self.assertEqual(group(run(data=data))['status'], 'item_8')

    def test_text_block_concept_locations_and_continuations_crossing_items(self):
        block = (b'<ix:nonNumeric id="block" name="intc:ScheduleTableTextBlock" contextRef="annual" '
                 b'escape="true" continuedAt="next"><p>Item 8 table</p></ix:nonNumeric>')
        data = intel_fixture().replace(b'<table><caption>', block + b'<table><caption>')
        data = data.replace(b'</body>', b'<ix:continuation id="next">Item 9 continuation</ix:continuation></body>')
        p = payload()
        p['NoteTables'] = {'ScheduleTableTextBlock': '<table>API content retained</table>'}
        r = run(p, data)
        e = group(r, 'NoteTables')['entries'][0]
        self.assertEqual(e['status'], 'mixed')
        self.assertFalse(e['value_checked'])
        self.assertEqual(e['match_basis'], 'all_source_occurrences_of_text_block_concept')
        missing = data.replace(b'id="next"', b'id="different"')
        self.assertEqual(group(run(p, missing), 'NoteTables')['status'], 'partial')

    def test_missing_item8_and_different_issuer_stop_verification(self):
        with self.assertRaisesRegex(ValueError, 'Cannot resolve'):
            run(data=intel_fixture().replace(b'Item 8.', b'Financial section.'))
        with self.assertRaisesRegex(ValueError, 'entities differ'):
            run(data=intel_fixture().replace(b'>0000050863<', b'>999<'))

    def test_cli_preserves_inputs_and_rejects_wrong_filing_and_output_aliases(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, cache = root / 'intel.htm', root / 'sec_api_xbrl.json'
            data = intel_fixture()
            source.write_bytes(data)
            cache.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                                         'source_sha256': check.sha(data), 'response': payload()}))
            args = [str(cache), '--filing', str(source)]
            before = cache.read_bytes()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(check.main(args), 0)
                self.assertEqual(check.main(args + ['--output', str(cache)]), 1)
                self.assertEqual(check.main(args + ['--output', str(root / 'item_8_xbrl.json')]), 1)
                report = root / 'item_8_membership_check.json'
                report_before = report.read_bytes()
                source.write_bytes(data + b' ')
                self.assertEqual(check.main(args), 1)
                self.assertEqual(report.read_bytes(), report_before)
            self.assertEqual(cache.read_bytes(), before)

    def test_raw_response_sidecar_and_no_provenance_rejection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, raw = root / 'intel.htm', root / 'api.json'
            source.write_bytes(intel_fixture())
            raw.write_text(json.dumps(payload()))
            args = [str(raw), '--filing', str(source), '--filing-url', URL]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(check.main(args), 1)
                sidecar = root / 'intel-source.json'
                sidecar.write_text(json.dumps({'sha256': check.sha(intel_fixture()), 'original_sec_url': URL}))
                self.assertEqual(check.main(args), 0)
            report = json.loads((root / 'item_8_membership_check.json').read_text())
            self.assertTrue(group(report)['entries'][0]['api_pointer'].startswith('/StatementsOfIncome/'))


@unittest.skipUnless(os.environ.get('SEC_INTEL_HYBRID_INTEGRATION') == '1', 'Intel cache integration is opt-in')
class IntelIntegrationTests(unittest.TestCase):
    def test_every_api_entry_accounted_for_and_input_files_unchanged(self):
        root = Path('data/table_output/intel_2023_items_7_8_hybrid_tables')
        paths = [root / n for n in ('sec_api_xbrl.json', 'item_7_html.json', 'item_8_xbrl.json', 'result_hybrid.json')]
        before = [check.sha(p.read_bytes()) for p in paths]
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            output = Path(tmp) / 'report.json'
            self.assertEqual(check.main([str(paths[0]), '--filing', 'data/source_cache/intc-20231230.htm',
                                        '--output', str(output)]), 0)
            r = json.loads(output.read_text())
        self.assertEqual(before, [check.sha(p.read_bytes()) for p in paths])
        self.assertEqual(r['item_8_scope']['page_references'][0]['pages'], list(range(70, 115)))
        self.assertEqual(r['summary']['groups'], 87)
        self.assertEqual(group(r)['status'], 'item_8')
        self.assertEqual(group(r, 'IncomeTaxesTables')['status'], 'item_8')
        self.assertEqual(group(r, 'IncomeTaxesDetails')['status'], 'partial')
        original = json.loads(paths[0].read_text())
        actual = {e['api_pointer'] for g in r['groups'] for e in g['entries']}
        expected = {ptr for g, _, ptr, _ in check.entries(original['response'], '/response')
                    if g not in check.api.EXCLUDED_GROUPS}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), r['summary']['checked_entries'])
        for g in r['groups']:
            for e in g['entries']:
                if e['status'] == 'item_8':
                    self.assertTrue(any('8' in loc['referenced_items'] for evidence in e['evidence']
                                        for loc in evidence['locations']))


@unittest.skipUnless(os.environ.get('SEC_MICRON_API_INTEGRATION') == '1', 'Micron cache integration is opt-in')
class MicronIntegrationTests(unittest.TestCase):
    def test_original_response_coverage_and_outside_item8_disclosures(self):
        root = Path('data/table_output/micron_2025_item_8_api_check')
        cache = root / 'sec_api_xbrl.json'
        before = cache.read_bytes()
        original = json.loads(before)
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            output = Path(tmp) / 'report.json'
            self.assertEqual(check.main([str(cache), '--filing', 'data/source_cache/micron_2025_xbrl/mu-20250828.htm',
                                        '--output', str(output)]), 0)
            r = json.loads(output.read_text())
        self.assertEqual(cache.read_bytes(), before)
        containers = r['groups'] + r['standalone_concepts']
        actual = {e['api_pointer'] for g in containers for e in g['entries']}
        expected = {ptr for g, _, ptr, _ in check.entries(original['response'], '/response')
                    if g not in check.api.EXCLUDED_GROUPS}
        self.assertEqual(actual, expected)
        self.assertEqual(r['summary']['top_level_entries'], 129)
        self.assertEqual(r['summary']['groups'], 114)
        self.assertEqual(r['summary']['entry_status'], {'item_8': 2489, 'outside_item_8': 47, 'unresolved': 75})
        self.assertEqual(group(r)['status'], 'item_8')
        self.assertEqual(group(r, 'ScheduleIIValuationandQualifyingAccountsDetails')['status'], 'outside_item_8')
        outside_groups = [g for g in r['groups'] if g['status'] == 'outside_item_8']
        self.assertEqual(len(outside_groups), 3)
        for g in outside_groups:
            self.assertTrue(g['api_group'].startswith('ScheduleIIValuationandQualifyingAccounts'))
            self.assertEqual(g['also_referenced_by_items'], ['15'])
        self.assertEqual(r['summary']['standalone_concept_status'], {'outside_item_8': 15})
        self.assertTrue(all(g['also_referenced_by_items'] == ['1C'] for g in r['standalone_concepts']))


if __name__ == '__main__':
    unittest.main()
