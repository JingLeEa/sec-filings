"""Metric values retain every verified context without table associations."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import export_item8_metric_values as export
from query_item8_metrics import Filing
from test_query_item8_metrics import saved_fixture, number, COMPONENT


def catalogue(filing, names):
    return {
        'source': str(filing.path), 'company': filing.company,
        'taxonomy_year': filing.taxonomy_year, 'counts': {'verified_concepts': len(names)},
        'notes': ['Preserve existing catalogue metadata.'],
        'metrics': [{'concept': 'us-gaap:' + n, 'query_name': n, 'label': n,
                     'definition': None, 'period_type': 'duration', 'scope': 'company_wide',
                     'company_wide_annual_or_year_end_years': [2024], 'units': ['USD'],
                     'api_groups': ['Income']} for n in names],
    }


class MetricValueTests(unittest.TestCase):
    def test_adds_only_value_and_keeps_dimensions_and_nonannual_periods(self):
        quarterly = number(5)
        quarterly['period'] = {'startDate': '2024-06-01', 'endDate': '2024-08-29'}
        records = {'NetIncomeLoss': [number(10), number(6, segment=COMPONENT), quarterly]}
        with TemporaryDirectory() as tmp:
            filing = Filing(saved_fixture(Path(tmp), records))
            original = catalogue(filing, ['NetIncomeLoss'])
            before = deepcopy(original)
            result = export.add_values(original, filing)
            values = result['metrics'][0].pop('value')
            self.assertEqual(result, before)
            self.assertEqual(original, before)
            self.assertEqual({v['value'] for v in values}, {'10', '6', '5'})
            component = next(v for v in values if v['value'] == '6')
            self.assertEqual(component['dimensions'], [{'axis': 'us-gaap:StatementEquityComponentsAxis',
                                                       'member': 'us-gaap:RetainedEarningsMember'}])
            self.assertEqual(next(v for v in values if v['value'] == '5')['period'], quarterly['period'])
            self.assertIsNone(result['metrics'][0]['definition'])

    def test_identical_repeats_merge_but_conflicts_currencies_and_accuracy_survive(self):
        accurate = number(10)
        accurate['decimals'] = '0'
        records = {'NetIncomeLoss': [number(10), number(10, unit='usd_other'), number(20),
                                    number(10, unit='eur'), accurate]}
        with TemporaryDirectory() as tmp:
            f = Filing(saved_fixture(Path(tmp), records, duplicate=True))
            result = export.add_values(catalogue(f, ['NetIncomeLoss']), f)
            values = result['metrics'][0]['value']
            self.assertEqual(len(values), 4)
            self.assertEqual({v['unit'] for v in values}, {'USD', 'EUR'})
            self.assertEqual({v['value'] for v in values}, {'10', '20'})
            self.assertEqual({v['decimals'] for v in values}, {'-6', '0'})
            self.assertTrue(all('tables' not in v and 'sources' not in v for v in values))

    def test_nil_zero_and_exact_large_decimal_values(self):
        nil = number(1)
        nil.update(value=None, **{'xsi:nil': 'true'})
        exact = '9007199254740993.123456'
        with TemporaryDirectory() as tmp:
            f = Filing(saved_fixture(Path(tmp), {'NetIncomeLoss': [nil, number(0), number(exact)]}))
            values = export.add_values(catalogue(f, ['NetIncomeLoss']), f)['metrics'][0]['value']
            self.assertEqual({v['value'] for v in values}, {None, '0', exact})
            self.assertEqual(next(v for v in values if v['value'] is None)['status'], 'nil')
            self.assertEqual(next(v for v in values if v['value'] == '0')['status'], 'reported')

    def test_outside_or_custom_records_do_not_leak_and_empty_metrics_are_kept(self):
        for options in ({'outside': ('NetIncomeLoss', 0)}, {'custom': 'NetIncomeLoss'}):
            with self.subTest(options=options), TemporaryDirectory() as tmp:
                f = Filing(saved_fixture(Path(tmp), **options))
                original = catalogue(f, ['NetIncomeLoss', 'Assets'])
                result = export.add_values(original, f)
                self.assertEqual(len(result['metrics']), 2)
                self.assertTrue(all(m['value'] == [] for m in result['metrics']))
                self.assertTrue(all(m['definition'] is None for m in result['metrics']))

    def test_source_identity_duplicate_metrics_and_existing_values_fail(self):
        with TemporaryDirectory() as tmp:
            f = Filing(saved_fixture(Path(tmp)))
            original = catalogue(f, ['NetIncomeLoss'])
            for key, value in [('company', 'Another Company'), ('taxonomy_year', 2023)]:
                wrong = deepcopy(original)
                wrong[key] = value
                with self.assertRaisesRegex(ValueError, 'differs'):
                    export.add_values(wrong, f)
            wrong = deepcopy(original)
            wrong['metrics'].append(deepcopy(wrong['metrics'][0]))
            with self.assertRaisesRegex(ValueError, 'Duplicate metric'):
                export.add_values(wrong, f)
            wrong = deepcopy(original)
            wrong['metrics'][0]['value'] = []
            with self.assertRaisesRegex(ValueError, 'already contains'):
                export.add_values(wrong, f)

    def test_cli_creates_separate_output_and_preserves_every_input(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = Filing(saved_fixture(root))
            path = root / 'available_item8_metrics.json'
            original = catalogue(f, ['NetIncomeLoss'])
            path.write_text(json.dumps(original))
            protected = {p: p.read_bytes() for p in (path, f.path, f.report_path, f.filing_path)}
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(export.main([str(path)]), 0)
                self.assertEqual(export.main([str(path)]), 1)
                for p in protected:
                    self.assertEqual(export.main([str(path), '--output', str(p)]), 1)
                alternative = root / 'wrong_named.json'
                alternative.write_text('{}')
                self.assertEqual(export.main([str(path), '--named-json', str(alternative)]), 1)
            result = json.loads((root / 'item_8_metrics_with_values.json').read_text())
            self.assertEqual(result['metrics'][0].pop('value')[0]['value'], '778000000')
            self.assertEqual(result, original)
            self.assertEqual({p: p.read_bytes() for p in protected}, protected)


@unittest.skipUnless(os.environ.get('SEC_METRIC_VALUES_INTEGRATION') == '1', 'Cached Intel/Micron integration is opt-in')
class RealMetricValuesTests(unittest.TestCase):
    def test_every_verified_value_and_original_metadata_are_preserved(self):
        for folder in ['micron_2025_item_8_api_check', 'intel_2023_items_7_8_hybrid_tables']:
            with self.subTest(folder=folder):
                root = Path('data/table_output') / folder
                paths = [root / n for n in ('item_8_named_api.json', 'sec_api_xbrl.json', 'item_8_membership_check.json')]
                before = {p: export.membership.sha(p.read_bytes()) for p in paths}
                f = Filing(paths[0])
                cat_path = root / 'available_item8_metrics.json'
                original = (json.loads(cat_path.read_text()) if cat_path.exists()
                            else catalogue(f, sorted({fact['concept'] for fact in f.facts})))
                result = export.add_values(original, f)
                indexed = {m['query_name']: m['value'] for m in result['metrics']}
                expected, actual = set(), set()
                for fact in f.facts:
                    if fact['concept'] in indexed:
                        expected.add(export.api.digest([fact['concept'], fact['value'], fact['period'],
                                                        fact['dimensions'], fact['unit'], fact['status']]))
                for concept, records in indexed.items():
                    for record in records:
                        dimensions = tuple(sorted((
                            '{' + f.namespaces[d['axis'].split(':')[0]] + '}' + d['axis'].split(':')[1],
                            '{' + f.namespaces[d['member'].split(':')[0]] + '}' + d['member'].split(':')[1],
                        ) for d in record['dimensions']))
                        value = None if record['value'] is None else export.api.decimal_value(record['value'])
                        actual.add(export.api.digest([concept, value, record['period'], dimensions,
                                                      record['unit'], record['status']]))
                self.assertEqual(actual, expected)
                for metric in result['metrics']:
                    self.assertTrue(metric.pop('value'))
                self.assertEqual(result, original)
                if folder.startswith('micron'):
                    self.assertEqual(len(indexed), 302)
                    hedge = indexed['CashFlowHedgeGainLossToBeReclassifiedWithinTwelveMonths']
                    self.assertEqual(len(hedge), 1)
                    self.assertEqual(hedge[0]['value'], '43000000')
                    self.assertEqual(len(hedge[0]['dimensions']), 2)
                    self.assertTrue(indexed['CommonStockDividendsPerShareCashPaid'])
                self.assertEqual({p: export.membership.sha(p.read_bytes()) for p in paths}, before)


if __name__ == '__main__':
    unittest.main()
