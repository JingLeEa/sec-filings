"""Direct API -> requested Item membership -> official metric/value export."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import export_api_metrics as export
from test_check_api_items import four_items, ITEMS
from test_check_item8_api import FACT, CONCEPT, source_fact
from test_extract_item8_xbrl_api import URL
from test_query_item8_metrics import package


def fixture():
    data = four_items().replace(CONCEPT.encode(), b'NetIncomeLoss').replace(b'RiskMetric', b'ProfitLoss')
    p = {'CoverPage': {'EntityRegistrantName': 'Intel Corporation', 'EntityCentralIndexKey': '0000050863',
                       'DocumentFiscalYearFocus': '2023', 'DocumentPeriodEndDate': '2023-12-30', 'DocumentType': '10-K'},
         'StatementsOfIncome': {'NetIncomeLoss': [deepcopy(FACT)], 'ProfitLoss': [{**FACT, 'value': '9000000'}]}}
    return data, p


def build(data=None, payload=None, items=ITEMS, dictionary=None):
    default_data, default_payload = fixture()
    data = default_data if data is None else data
    payload = default_payload if payload is None else payload
    report = export.membership.check_response(payload, data, 'intel.htm', {'htm-url': URL}, 2023, items=items)
    dictionary = dictionary or export.taxonomy.build_dictionary(package(2023), 2023)
    return export.build_metrics(payload, data, report, dictionary, 'sec_api_xbrl.json', 'membership.json')


def metric(result, name):
    return next(m for m in result['metrics'] if m['query_name'] == name)


class ApiMetricPipelineTests(unittest.TestCase):
    def test_selects_items_per_record_preserves_values_and_needs_no_named_table_file(self):
        data, p = fixture()
        p['Duplicate'] = deepcopy(p['StatementsOfIncome'])
        before = deepcopy(p)
        with patch.object(export.membership.ix, 'extract_tables', side_effect=AssertionError('no table extraction')):
            catalogue, result, audit = build(data, p)
        self.assertEqual(p, before)
        self.assertEqual(result['counts'], {'verified_concepts': 2, 'company_wide': 2, 'dimension_only': 0})
        income = metric(result, 'NetIncomeLoss')
        self.assertEqual(income['value'][0]['value'], FACT['value'])
        self.assertEqual(income['value'][0]['items'], ['1', '7', '8'])
        self.assertEqual(len(income['value'][0]['source_labels']), 3)
        self.assertEqual({label['source_fact_id'] for label in income['value'][0]['source_labels']},
                         {'business', 'management', 'f-44'})
        self.assertEqual(income['company_wide_annual_or_year_end_years'], [2023])
        self.assertEqual(metric(result, 'ProfitLoss')['value'][0]['items'], ['1A'])
        self.assertEqual(audit['included_api_entries'], 4)
        self.assertEqual(audit['unique_values'], 2)
        self.assertEqual(audit['repeated_api_entries_merged'], 2)
        for m in result['metrics']:
            m.pop('value')
        self.assertEqual(result, catalogue)

    def test_restricting_to_item1a_or8_changes_selection_and_items_not_amounts(self):
        _, only_risk, audit = build(items=('1A',))
        self.assertEqual([m['query_name'] for m in only_risk['metrics']], ['ProfitLoss'])
        self.assertEqual(metric(only_risk, 'ProfitLoss')['value'][0]['items'], ['1A'])
        self.assertEqual(audit['excluded_api_entries'], 1)
        _, only8, _ = build(items=('8',))
        self.assertEqual([m['query_name'] for m in only8['metrics']], ['NetIncomeLoss'])
        self.assertEqual(metric(only8, 'NetIncomeLoss')['value'][0]['items'], ['8'])
        self.assertEqual(metric(only8, 'NetIncomeLoss')['value'][0]['value'], FACT['value'])

    def test_outside_unmatched_custom_and_nonnumeric_records_are_audited(self):
        data, p = fixture()
        data = data.replace(b'</body>', source_fact('outside', 'Assets', '7') + b'</body>')
        custom = source_fact('custom', 'CustomMetric', '6').replace(b'us-gaap:CustomMetric', b'intc:CustomMetric')
        data = data.replace(b'<p>There was a change.', custom + b'<p>There was a change.')
        p['Mixed'] = {'Assets': [{**FACT, 'value': '7000000'}], 'Missing': [FACT],
                      'CustomMetric': [{**FACT, 'value': '6000000'}], 'SomeTextBlock': 'A disclosure'}
        _, result, audit = build(data, p)
        self.assertEqual(len(result['metrics']), 2)
        self.assertEqual(audit['checked_api_entries'], 6)
        self.assertEqual(audit['included_api_entries'] + audit['excluded_api_entries'], 6)
        excluded = {e['concept']: e['reason'] for e in audit['excluded_entries']}
        self.assertEqual(excluded['CustomMetric'], 'custom_or_ambiguous_concept_namespace')
        self.assertEqual(excluded['Assets'], 'not_verified_numeric_fact_in_selected_items')
        self.assertEqual(set(excluded), {'Assets', 'Missing', 'CustomMetric', 'SomeTextBlock'})

    def test_dimensions_and_missing_definitions_are_retained(self):
        data, p = fixture()
        segment = (b'<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementEquityComponentsAxis">'
                   b'us-gaap:RetainedEarningsMember</xbrldi:explicitMember></xbrli:segment>')
        data = data.replace(b'</xbrli:entity>', segment + b'</xbrli:entity>')
        for records in p['StatementsOfIncome'].values():
            records[0]['segment'] = {'explicitMember': {'dimension': 'us-gaap:StatementEquityComponentsAxis',
                                                      '$t': 'us-gaap:RetainedEarningsMember'}}
        dictionary = export.taxonomy.build_dictionary(package(2023), 2023)
        dictionary['concepts']['NetIncomeLoss']['definition'] = None
        _, result, _ = build(data, p, dictionary=dictionary)
        income = metric(result, 'NetIncomeLoss')
        self.assertEqual(income['scope'], 'dimension_only')
        self.assertEqual(income['units'], [])
        self.assertEqual(income['company_wide_annual_or_year_end_years'], [])
        self.assertIsNone(income['definition'])
        self.assertEqual(income['value'][0]['unit'], 'USD')
        self.assertEqual(income['value'][0]['dimensions'], [{'axis': 'us-gaap:StatementEquityComponentsAxis',
                                                           'member': 'us-gaap:RetainedEarningsMember'}])

    def test_nil_zero_conflicts_accuracy_and_nonannual_periods_are_distinct(self):
        data, p = fixture()
        data = data.replace(b'<html ', b'<html xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ')
        nil_fact = (b'<ix:nonFraction id="nil" name="us-gaap:NetIncomeLoss" contextRef="annual" '
                    b'unitRef="usd" decimals="-6" xsi:nil="true"/>')
        zero_fact = source_fact('zero', 'NetIncomeLoss', '0')
        conflict = source_fact('conflict', 'NetIncomeLoss', '5')
        accurate = source_fact('accurate', 'NetIncomeLoss', '54,228').replace(b'decimals="-6"', b'decimals="0"')
        context = (b'<xbrli:context id="quarter"><xbrli:entity>'
                   b'<xbrli:identifier scheme="http://www.sec.gov/CIK">0000050863</xbrli:identifier></xbrli:entity>'
                   b'<xbrli:period><xbrli:startDate>2023-10-01</xbrli:startDate>'
                   b'<xbrli:endDate>2023-12-30</xbrli:endDate></xbrli:period></xbrli:context>')
        quarter = source_fact('quarter_fact', 'NetIncomeLoss', '5').replace(b'contextRef="annual"', b'contextRef="quarter"')
        data = data.replace(b'</ix:resources>', context + b'</ix:resources>').replace(
            b'<table><caption>', nil_fact + zero_fact + conflict + accurate + quarter + b'<table><caption>')
        p['StatementsOfIncome']['NetIncomeLoss'].extend([
            {**FACT, 'value': None, 'xsi:nil': 'true'}, {**FACT, 'value': '0'}, {**FACT, 'value': '5000000'},
            {**FACT, 'decimals': '0'}, {**FACT, 'value': '5000000',
                                      'period': {'startDate': '2023-10-01', 'endDate': '2023-12-30'}}])
        _, result, _ = build(data, p)
        records = metric(result, 'NetIncomeLoss')['value']
        self.assertEqual(len(records), 6)
        self.assertEqual(next(r for r in records if r['value'] is None)['status'], 'nil')
        self.assertEqual(next(r for r in records if r['value'] == '0')['status'], 'reported')
        self.assertEqual(sum(r['value'] == '5000000' for r in records), 2)
        self.assertEqual({r['decimals'] for r in records}, {'-6', '0'})

    def test_wrong_dictionary_or_report_content_is_rejected(self):
        data, p = fixture()
        report = export.membership.check_response(p, data, 'intel.htm', {'htm-url': URL}, 2023, items=ITEMS)
        dictionary = export.taxonomy.build_dictionary(package(2023), 2023)
        changed = deepcopy(p)
        changed['StatementsOfIncome']['NetIncomeLoss'][0]['value'] = '1'
        with self.assertRaisesRegex(ValueError, 'different API/filing content'):
            export.build_metrics(changed, data, report, dictionary, 'api.json', 'report.json')
        with self.assertRaisesRegex(ValueError, 'Dictionary taxonomy differs'):
            export.build_metrics(p, data, report, export.taxonomy.build_dictionary(package(2025), 2025), 'api.json', 'report.json')

    def test_cli_builds_all_stages_preserves_sources_and_refuses_overwrite_or_wrong_filing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, p = fixture()
            source, cache = root / 'intel.htm', root / 'sec_api_xbrl.json'
            source.write_bytes(data)
            cache.write_text(json.dumps({'schema_version': 'sec-api-cache-2.0', 'request': {'htm-url': URL},
                                         'source_sha256': export.membership.sha(data), 'response': p}))
            tax_cache = root / 'taxonomy'
            tax_cache.mkdir()
            (tax_cache / 'us-gaap-2023.zip').write_bytes(package(2023))
            args = [str(cache), '--filing', str(source), '--taxonomy-cache', str(tax_cache), '--offline']
            before = {path: path.read_bytes() for path in (source, cache)}
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(export.main(args), 0)
                output_dir = root / 'items_1_1a_7_8_metrics'
                outputs = {path: path.read_bytes() for path in output_dir.iterdir()}
                self.assertEqual(len(outputs), 4)
                self.assertEqual(export.main(args), 1)
                self.assertEqual(outputs, {path: path.read_bytes() for path in outputs})
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                single = root / 'single'
                self.assertEqual(export.main(args + ['--output-dir', str(single), '--metrics-only']), 0)
                self.assertEqual([path.name for path in single.iterdir()], ['metrics_with_values.json'])
                result = json.loads((single / 'metrics_with_values.json').read_text())
                complete = json.loads(outputs[output_dir / 'metrics_with_values.json'])
                audit = json.loads(outputs[output_dir / 'metric_export_audit.json'])
                self.assertEqual(result['metrics'], complete['metrics'])
                self.assertIsNone(result['membership_report'])
                self.assertTrue(Path(result['source']).is_file())
                self.assertNotIn('metric_export_audit.json', ' '.join(result['notes']))
                for key, value in result['verification'].items():
                    self.assertEqual(value, audit[key])
                source.write_bytes(data + b' ')
                for name, flags in [('bad', []), ('bad_single', ['--metrics-only'])]:
                    self.assertEqual(export.main(args + ['--output-dir', str(root / name), *flags]), 1)
                    self.assertFalse((root / name).exists())


@unittest.skipUnless(os.environ.get('SEC_API_METRICS_INTEGRATION') == '1', 'Intel cached integration is opt-in')
class IntelPipelineIntegrationTests(unittest.TestCase):
    def test_intel2023_direct_values_equal_old_item8_values_and_all_selected_records_are_accounted_for(self):
        root = Path('data/table_output/intel_2023_item_8_api_check')
        source = Path('data/source_cache/intc-20231230.htm')
        cache = root / 'sec_api_xbrl.json'
        old_path = root / 'intel_2023_item_8_metrics_with_values.json'
        before = {path: export.membership.sha(path.read_bytes()) for path in (source, cache, old_path)}
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            output_dir = Path(tmp)
            self.assertEqual(export.main([str(cache), '--filing', str(source), '--items', *ITEMS,
                                           '--output-dir', str(output_dir), '--offline']), 0)
            output = json.loads((output_dir / 'metrics_with_values.json').read_text())
            report = json.loads((output_dir / 'items_1_1a_7_8_membership_check.json').read_text())
            audit = json.loads((output_dir / 'metric_export_audit.json').read_text())
        old = json.loads(old_path.read_text())
        stripped = deepcopy(output['metrics'])
        for m in stripped:
            m.pop('items')
            for record in m['value']:
                record.pop('items')
                record.pop('source_labels')
        self.assertEqual(stripped, old['metrics'])
        entries = {e['api_pointer']: e for g in report['groups'] + report['standalone_concepts'] for e in g['entries']}
        included = {e['api_pointer'] for e in audit['included_entries']}
        excluded = {e['api_pointer'] for e in audit['excluded_entries']}
        self.assertFalse(included & excluded)
        self.assertEqual(included | excluded, set(entries))
        for record in audit['included_entries']:
            entry = entries[record['api_pointer']]
            self.assertTrue(entry['value_checked'])
            self.assertEqual(record['items'], [item for item in ITEMS if entry['item_membership'][item] == 'in_item'])
        self.assertEqual(audit['unique_values'], 1443)
        self.assertEqual(output['counts']['verified_concepts'], 357)
        withholding = metric(output, 'AdjustmentsRelatedToTaxWithholdingForShareBasedCompensation')
        total = next(v for v in withholding['value'] if v['value'] == '420000000' and v['dimensions'] == [])
        self.assertEqual(len(total['source_labels']), 1)
        label = total['source_labels'][0]
        self.assertEqual(label['row_label'], 'Restricted stock unit withholdings')
        self.assertEqual(label['column_label'], 'Total')
        self.assertEqual(label['page'], '78')
        self.assertEqual(label['source_fact_id'], 'f-359')
        self.assertEqual(label['status'], 'resolved')
        operating = metric(output, 'OperatingIncomeLoss')
        dcai = next(v for v in operating['value'] if v['value'] == '7376000000')
        self.assertEqual(dcai['source_labels'][0]['row_label'], 'Data Center and AI')
        self.assertEqual(dcai['source_labels'][0]['column_label'], 'Dec 25, 2021')
        self.assertEqual(before, {path: export.membership.sha(path.read_bytes()) for path in before})


if __name__ == '__main__':
    unittest.main()
