"""API membership across multiple Items, with an optional real Intel regression."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import check_item8_api as check
from test_check_item8_api import CONCEPT, FACT, group, payload, source_fact
from test_extract_10k_items_7_8 import intel_fixture
from test_extract_item8_xbrl_api import URL

ITEMS = ('1', '1A', '7', '8')


def four_items():
    business = (b'<h2 style="font-weight:bold">Item 1. BUSINESS</h2>' + source_fact('business') +
                b'<div style="position:absolute;bottom:0">5</div><div style="page-break-after:always"/>')
    risk = (b'<h2 style="font-weight:bold">Item 1A. RISK FACTORS</h2>' + source_fact('risk', 'RiskMetric', '9') +
            b'<div style="position:absolute;bottom:0">10</div><div style="page-break-after:always"/>')
    return intel_fixture().replace(b'</ix:header>', b'</ix:header>' + business + risk).replace(
        b'<p>There was a change.', source_fact('management') + b'<p>There was a change.')


def run(data=None, api_payload=None, items=ITEMS):
    return check.check_response(payload() if api_payload is None else api_payload,
                                four_items() if data is None else data, 'intel.htm',
                                {'htm-url': URL}, 2023, items=items)


class MultipleItemTests(unittest.TestCase):
    def test_shared_fact_matches_several_items_without_duplicating_entries_or_extracting_tables(self):
        original = payload()
        before = deepcopy(original)
        with patch.object(check.ix, 'Document', wraps=check.ix.Document) as document:
            with patch.object(check.ix, 'extract_tables', side_effect=AssertionError('no table extraction')):
                result = run(api_payload=original)
        self.assertEqual(document.call_count, 1)
        self.assertEqual(original, before)
        self.assertEqual(result['requested_items'], list(ITEMS))
        self.assertEqual(result['summary']['checked_entries'], 1)
        entry = group(result)['entries'][0]
        self.assertEqual(entry['matched_items'], ['1', '7', '8'])
        self.assertEqual(entry['item_membership'], {'1': 'in_item', '1A': 'outside_item',
                                                    '7': 'in_item', '8': 'in_item'})
        self.assertEqual(entry['status'], 'selected_items')
        self.assertTrue(entry['value_checked'])
        self.assertNotIn('value', entry)
        self.assertEqual(entry['api_pointer'], '/response/StatementsOfIncome/' + CONCEPT + '/0')
        self.assertEqual(group(result, 'CoverPage')['item_membership'], dict.fromkeys(ITEMS, 'excluded_metadata'))
        self.assertNotIn('item_8_scope', result)
        for item in ('1', '7', '8'):
            self.assertEqual(result['summary']['by_item'][item]['entries_with_evidence'], 1)

    def test_group_union_does_not_claim_every_entry_belongs_to_every_item(self):
        p = payload()
        p['StatementsOfIncome']['RiskMetric'] = [{**FACT, 'value': '9000000'}]
        result = run(api_payload=p)
        g = group(result)
        self.assertEqual(g['status'], 'selected_items')
        self.assertEqual(g['matched_items'], list(ITEMS))
        self.assertEqual(g['item_membership'], dict.fromkeys(ITEMS, 'mixed'))
        risk = g['entries'][1]
        self.assertEqual(risk['matched_items'], ['1A'])
        self.assertEqual(risk['item_membership']['1A'], 'in_item')
        for summary in result['summary']['by_item'].values():
            self.assertEqual(sum(summary['entry_status'].values()), 2)

    def test_outside_unresolved_and_empty_groups_are_retained(self):
        p = payload()
        p['Outside'] = {'OtherMetric': [{**FACT, 'value': '7000000'}]}
        p['Unknown'] = {'MissingMetric': [deepcopy(FACT)]}
        p['Empty'] = {}
        data = four_items().replace(b'</body>', source_fact('other', 'OtherMetric', '7') + b'</body>')
        result = run(data, p)
        outside = group(result, 'Outside')['entries'][0]
        self.assertEqual(outside['status'], 'outside_selected_items')
        self.assertEqual(outside['item_membership'], dict.fromkeys(ITEMS, 'outside_item'))
        self.assertEqual(outside['also_referenced_by_items'], ['9'])
        unknown = group(result, 'Unknown')['entries'][0]
        self.assertEqual(unknown['item_membership'], dict.fromkeys(ITEMS, 'unresolved'))
        self.assertEqual(unknown['matched_items'], [])
        self.assertEqual(group(result, 'Empty')['status'], 'unresolved')
        self.assertEqual(group(result, 'Empty')['item_membership'], dict.fromkeys(ITEMS, 'unresolved'))

    def test_text_continuations_span_selected_items_and_unresolved_locations(self):
        block = (b'<ix:nonNumeric id="block" name="intc:ScheduleTableTextBlock" contextRef="annual" '
                 b'continuedAt="next">Item 7 disclosure</ix:nonNumeric>')
        data = four_items().replace(b'<p>There was a change.', block + b'<p>There was a change.').replace(
            b'<table><caption>', b'<ix:continuation id="next">Item 8 disclosure</ix:continuation><table><caption>')
        p = payload()
        p['NoteTables'] = {'ScheduleTableTextBlock': '<table>API text</table>'}
        entry = group(run(data, p), 'NoteTables')['entries'][0]
        self.assertEqual(entry['status'], 'selected_items')
        self.assertEqual(entry['matched_items'], ['7', '8'])
        self.assertEqual(entry['item_membership']['7'], 'mixed')
        self.assertEqual(entry['item_membership']['8'], 'mixed')
        self.assertFalse(entry['value_checked'])
        missing = group(run(data.replace(b'id="next"', b'id="missing"'), p), 'NoteTables')['entries'][0]
        self.assertEqual(missing['status'], 'partial')
        self.assertEqual(missing['item_membership']['7'], 'partial')
        self.assertEqual(missing['item_membership']['8'], 'unresolved')

    def test_hidden_numeric_references_and_wrong_values(self):
        data = four_items()
        fact = source_fact('hidden', 'HiddenMetric', '9')
        data = data.replace(b'</ix:header>', b'<ix:hidden>' + fact + b'</ix:hidden></ix:header>')
        p = payload()
        p['Hidden'] = {'HiddenMetric': [{**FACT, 'value': '9000000'}]}
        self.assertEqual(group(run(data, p), 'Hidden')['status'], 'unresolved')
        ref = b'<span style="-sec-ix-hidden:hidden">9</span>'
        data = data.replace(b'<p>There was a change.', ref + b'<p>There was a change.').replace(b'</body>', ref + b'</body>')
        entry = group(run(data, p), 'Hidden')['entries'][0]
        self.assertEqual(entry['status'], 'selected_items')
        self.assertEqual(entry['item_membership']['7'], 'in_item')
        self.assertTrue(entry['also_found_outside_selected_items'])
        p['Hidden']['HiddenMetric'][0]['value'] = '10000000'
        self.assertEqual(group(run(data, p), 'Hidden')['status'], 'unresolved')

    def test_standalone_contextless_concepts_receive_per_item_evidence_without_value_verification(self):
        block = (b'<ix:nonNumeric name="intc:BusinessDescriptionTextBlock" contextRef="annual">'
                 b'Business description</ix:nonNumeric>')
        flag = b'<ix:nonNumeric name="intc:RiskFlag" contextRef="annual">true</ix:nonNumeric>'
        data = four_items().replace(source_fact('business'), block + source_fact('business')).replace(
            source_fact('risk', 'RiskMetric', '9'), flag + source_fact('risk', 'RiskMetric', '9'))
        p = payload()
        p.update(BusinessDescriptionTextBlock='API description', RiskFlag=['true'])
        result = run(data, p)
        standalone = {g['concept']: g for g in result['standalone_concepts']}
        self.assertEqual(standalone['BusinessDescriptionTextBlock']['matched_items'], ['1'])
        self.assertEqual(standalone['RiskFlag']['matched_items'], ['1A'])
        self.assertEqual(result['summary']['standalone_concepts'], 2)
        self.assertEqual(result['summary']['checked_entries'], 3)
        for g in standalone.values():
            self.assertFalse(g['entries'][0]['value_checked'])
            self.assertEqual(g['status'], 'selected_items')

    def test_subsets_require_only_requested_boundaries_and_normalize_input(self):
        data = four_items().replace(b'Item 8.', b'Financial section.')
        result = run(data, items=('1', '1a', '1A', '7'))
        self.assertEqual(result['requested_items'], ['1', '1A', '7'])
        with self.assertRaisesRegex(ValueError, 'Cannot resolve'):
            run(data)
        for invalid in ((), ('1C',)):
            with self.assertRaisesRegex(ValueError, 'Select one or more'):
                run(items=invalid)

    def test_external_incorporation_in_any_selected_item_stops_verification(self):
        original = check.ix.Layout

        def with_external(*args, **kwargs):
            layout = original(*args, **kwargs)
            layout.external.append({'item': '1A'})
            return layout

        with patch.object(check.ix, 'Layout', side_effect=with_external):
            with self.assertRaisesRegex(ValueError, '1A.*external report'):
                run()
            self.assertEqual(run(items=('7',))['requested_items'], ['7'])

    def test_default_item8_records_unchanged_and_match_multi_item_projection(self):
        legacy = check.check_response(payload(), four_items(), 'intel.htm', {'htm-url': URL}, 2023)
        new = group(run())['entries'][0]
        old = group(legacy)['entries'][0]
        self.assertEqual(old['status'], 'item_8')
        self.assertEqual(legacy['schema_version'], 'api-item-membership-1.1')
        self.assertNotIn('item_membership', old)
        self.assertEqual(new['item_membership']['8'], 'in_item')
        for evidence in new['evidence']:
            legacy_evidence = next(e for e in old['evidence'] if e['source_fact_id'] == evidence['source_fact_id'])
            self.assertEqual(evidence['locations'], legacy_evidence['locations'])

    def test_cli_separate_report_raw_pointers_and_original_files_unchanged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, cache = root / 'intel.htm', root / 'sec_api_xbrl.json'
            source.write_bytes(four_items())
            cache.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                                         'source_sha256': check.sha(source.read_bytes()), 'response': payload()}))
            before = {p: p.read_bytes() for p in (source, cache)}
            legacy = root / 'item_8_membership_check.json'
            legacy.write_text('existing Item 8 report')
            args = [str(cache), '--filing', str(source), '--items', '1', '1a', '7', '8']
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(check.main(args), 0)
                self.assertEqual(check.main(args + ['--output', str(legacy)]), 1)
                self.assertEqual(check.main(args + ['--output', str(cache)]), 1)
            self.assertEqual(legacy.read_text(), 'existing Item 8 report')
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            output = root / 'items_1_1a_7_8_membership_check.json'
            report = json.loads(output.read_text())
            self.assertEqual(report['requested_items'], list(ITEMS))
            raw = root / 'raw.json'
            raw.write_text(json.dumps(payload()))
            source.with_name('intel-source.json').write_text(json.dumps({
                'sha256': check.sha(source.read_bytes()), 'original_sec_url': URL}))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main([str(raw), '--filing', str(source), '--filing-url', URL,
                                             '--items', *ITEMS, '--output', str(root / 'raw-report.json')]), 0)
            raw_report = json.loads((root / 'raw-report.json').read_text())
            self.assertEqual(group(raw_report)['entries'][0]['api_pointer'], '/StatementsOfIncome/' + CONCEPT + '/0')


@unittest.skipUnless(os.environ.get('SEC_INTEL_MULTI_ITEM_INTEGRATION') == '1', 'Intel cache integration is opt-in')
class IntelMultipleItemIntegrationTests(unittest.TestCase):
    def test_all_entries_accounted_for_and_item8_evidence_matches_saved_report(self):
        root = Path('data/table_output/intel_2023_item_8_api_check')
        cache, legacy = root / 'sec_api_xbrl.json', root / 'item_8_membership_check.json'
        source = Path('data/source_cache/intc-20231230.htm')
        before = {p: check.sha(p.read_bytes()) for p in (cache, legacy, source)}
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            output = Path(tmp) / 'report.json'
            self.assertEqual(check.main([str(cache), '--filing', str(source), '--items', *ITEMS,
                                         '--output', str(output)]), 0)
            report = json.loads(output.read_text())
        self.assertEqual(before, {p: check.sha(p.read_bytes()) for p in before})
        p = json.loads(cache.read_text())['response']
        entries = [e for g in report['groups'] + report['standalone_concepts'] for e in g['entries']]
        expected = {ptr for g, _, ptr, _ in check.entries(p, '/response') if g not in check.api.EXCLUDED_GROUPS}
        self.assertEqual({e['api_pointer'] for e in entries}, expected)
        self.assertEqual(len(entries), len(expected))
        old = json.loads(legacy.read_text())
        legacy_result = check.check_response(p, source.read_bytes(), str(source), old['source']['request'], 2023)
        for key in ('schema_version', 'item', 'year', 'item_8_scope', 'summary', 'groups', 'standalone_concepts'):
            self.assertEqual(legacy_result[key], old[key], key)
        old_entries = {e['api_pointer']: e for g in old['groups'] + old['standalone_concepts'] for e in g['entries']}
        translate = {'item_8': 'in_item', 'outside_item_8': 'outside_item'}
        for entry in entries:
            previous = old_entries[entry['api_pointer']]
            self.assertEqual(entry['item_membership']['8'], translate.get(previous['status'], previous['status']))
            self.assertEqual(entry['value_checked'], previous['value_checked'])
            self.assertEqual([e['locations'] for e in entry['evidence']], [e['locations'] for e in previous['evidence']])
        for summary in report['summary']['by_item'].values():
            self.assertEqual(sum(summary['entry_status'].values()), len(entries))
        self.assertEqual(report['item_scopes']['8']['page_references'][0]['pages'], list(range(70, 115)))
        self.assertEqual([g['inferred_page'] for g in report['item_scopes']['1']['image_only_pages']], ['3', '4'])


if __name__ == '__main__':
    unittest.main()
