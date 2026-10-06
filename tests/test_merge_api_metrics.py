"""Latest-filing conflict selection with intact retained values and provenance."""
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import merge_api_metrics as merger


def sample(year, *, cik='50863', company='INTEL CORPORATION', amount='100'):
    value = {'value': amount, 'unit': 'USD', 'period': {'instant': '2023-12-30'},
             'dimensions': [], 'status': 'reported', 'decimals': '-6', 'items': ['8'],
             'source_labels': [{'source_fact_id': 'f-1', 'page': str(year - 2000),
                                'row_label': 'Assets', 'column_label': '2023', 'status': 'resolved'}]}
    metric = {'concept': 'us-gaap:Assets', 'query_name': 'Assets', 'label': 'Assets',
              'definition': 'Definition ' + str(year), 'period_type': 'instant',
              'scope': 'company_wide', 'company_wide_annual_or_year_end_years': [2023],
              'units': ['USD'], 'api_groups': ['BalanceSheets'], 'items': ['8'], 'value': [value]}
    return {'schema_version': 'api-metrics-1.0', 'exporter_version': '1.11.0',
            'company': company, 'taxonomy_year': year, 'source': 'saved_api.json',
            'requested_items': ['1', '1A', '7', '8'],
            'counts': {'verified_concepts': 1, 'company_wide': 1, 'dimension_only': 0},
            'metrics': [metric], 'verification': {'source': {'request': {
                'htm-url': f'https://www.sec.gov/Archives/edgar/data/{cik}/000005086324000010/filing.htm'}}}}


def inputs(*documents):
    return [(2023 + i, Path(f'intel_{2023+i}_api_metrics_with_values.json'), json.dumps(d).encode())
            for i, d in enumerate(documents)]


class MergeMetricsTests(unittest.TestCase):
    def test_nonconflicting_repeats_and_all_original_metadata_remain_intact(self):
        documents = [sample(y) for y in (2023, 2024, 2025)]
        originals = deepcopy(documents)
        merged = merger.merge_documents(inputs(*documents))
        self.assertEqual(merged['counts']['verified_concepts'], 1)
        self.assertEqual(merged['counts']['value_records'], 3)
        self.assertEqual(merged['metrics'][0]['definition'], 'Definition 2025')
        self.assertEqual(merged['metrics'][0]['metadata_source_filing_year'], 2025)
        for year, original in zip((2023, 2024, 2025), originals):
            record = merged['metrics'][0]
            meta = next(m['metadata'] for m in record['metadata_by_filing'] if m['source_filing_year'] == year)
            values = [{k: v for k, v in v.items() if k != 'source_filing_year'}
                      for v in record['value'] if v['source_filing_year'] == year]
            self.assertEqual({**meta, 'value': values}, original['metrics'][0])
            source = next(f for f in merged['filings'] if f['filing_year'] == year)
            self.assertEqual({k: v for k, v in source.items() if k not in
                              {'filing_year', 'metrics_file', 'metrics_file_sha256'}},
                             {k: v for k, v in original.items() if k != 'metrics'})
        self.assertEqual(documents, originals)

    def test_only_older_conflicts_are_removed_not_other_contexts(self):
        documents = [sample(y, amount='100' if y != 2025 else '110') for y in (2023, 2024, 2025)]
        variants = [dict(deepcopy(documents[0]['metrics'][0]['value'][0]), **changes) for changes in
                    [{'dimensions': [{'axis': 'us-gaap:Axis', 'member': 'ex:Segment'}]},
                     {'value': None, 'status': 'nil'}, {'unit': 'EUR'},
                     {'decimals': '-3'}, {'period': {'instant': '2022-12-31'}}]]
        documents[0]['metrics'][0]['value'].extend(variants)
        merged = merger.merge_documents(inputs(*documents))
        # The old 100, nil and different-accuracy 100 lose to 2025's 110.
        # Other dimensions, units and periods have no newer counterpart.
        expected = Counter((y, json.dumps(v, sort_keys=True)) for y, v in
                           [(2023, variants[i]) for i in (0, 2, 4)] +
                           [(2025, documents[2]['metrics'][0]['value'][0])])
        actual = Counter((v['source_filing_year'], json.dumps({k: x for k, x in v.items() if k != 'source_filing_year'}, sort_keys=True))
                         for v in merged['metrics'][0]['value'])
        self.assertEqual(actual, expected)
        self.assertEqual(merged['verification']['input_value_records'], 8)
        self.assertEqual(merged['verification']['output_value_records'], 4)
        self.assertEqual(merged['verification']['values_removed'], 4)
        self.assertEqual(merged['verification']['conflicting_contexts_resolved'], 1)

    def test_truist_sign_conflict_keeps_newest_value_and_its_own_source_labels(self):
        older, newer = sample(2024, amount='-6651000000'), sample(2025, amount='6651000000')
        for document in (older, newer):
            metric = document['metrics'][0]
            metric.update(concept='us-gaap:DebtSecuritiesAvailableForSaleRealizedLoss',
                          query_name='DebtSecuritiesAvailableForSaleRealizedLoss')
            metric['value'][0]['period'] = {'startDate': '2024-01-01', 'endDate': '2024-12-31'}
        older['metrics'][0]['value'][0]['items'] = ['7']
        originals = deepcopy([older, newer])
        entries = [(y, Path(f'truist_{y}.json'), json.dumps(d).encode())
                   for y, d in [(2024, older), (2025, newer)]]
        merged = merger.merge_documents(list(reversed(entries)))
        self.assertEqual(merged['metrics'][0]['value'],
                         [dict(newer['metrics'][0]['value'][0], source_filing_year=2025)])
        self.assertEqual(merged['metrics'][0]['source_filing_years'], [2024, 2025])
        self.assertEqual(len(merged['metrics'][0]['metadata_by_filing']), 2)
        self.assertEqual(merged['verification']['values_removed'], 1)
        self.assertEqual([older, newer], originals)

    def test_latest_filing_containing_each_context_not_latest_metadata_year(self):
        documents = [sample(2023, amount='5'), sample(2024, amount='-5'), sample(2025, amount='12')]
        documents[2]['metrics'][0]['value'][0]['period'] = {'instant': '2025-12-31'}
        metric = merger.merge_documents(inputs(*documents))['metrics'][0]
        self.assertEqual([(v['source_filing_year'], v['value']) for v in metric['value']],
                         [(2024, '-5'), (2025, '12')])
        self.assertEqual(metric['metadata_source_filing_year'], 2025)

    def test_decimal_equivalence_is_not_conflict_and_float_precision_is_not_used(self):
        documents = [sample(2023, amount='100'), sample(2024, amount='100.00'), sample(2025, amount='1E2')]
        documents[0]['metrics'][0]['value'][0]['decimals'] = '-3'
        merged = merger.merge_documents(inputs(*documents))
        self.assertEqual(merged['verification']['values_removed'], 0)
        self.assertEqual([v['value'] for v in merged['metrics'][0]['value']], ['100', '100.00', '1E2'])
        documents = [sample(2023, amount='9007199254740992'), sample(2024, amount='9007199254740993')]
        merged = merger.merge_documents(inputs(*documents))
        self.assertEqual([v['value'] for v in merged['metrics'][0]['value']], ['9007199254740993'])

    def test_dimension_order_is_ignored_but_distinct_members_are_separate(self):
        documents = [sample(2023, amount='10'), sample(2024, amount='20')]
        dims = [{'axis': 'us-gaap:ProductOrServiceAxis', 'member': 'ex:LoanMember'},
                {'axis': 'us-gaap:StatementGeographicalAxis', 'member': 'ex:USMember'}]
        documents[0]['metrics'][0]['value'][0]['dimensions'] = dims
        documents[1]['metrics'][0]['value'][0]['dimensions'] = list(reversed(dims))
        extra = deepcopy(documents[0]['metrics'][0]['value'][0])
        extra['dimensions'][0]['member'] = 'ex:OtherMember'
        documents[0]['metrics'][0]['value'].append(extra)
        merged = merger.merge_documents(inputs(*documents))
        self.assertEqual(merged['metrics'][0]['value'],
                         [dict(extra, source_filing_year=2023),
                          dict(documents[1]['metrics'][0]['value'][0], source_filing_year=2024)])

    def test_nil_and_reported_zero_are_distinct_and_latest_wins_in_both_directions(self):
        for latest_nil in (False, True):
            with self.subTest(latest_nil=latest_nil):
                documents = [sample(2023, amount='0'), sample(2024, amount='0')]
                documents[1 if latest_nil else 0]['metrics'][0]['value'][0].update(value=None, status='nil')
                merged = merger.merge_documents(inputs(*documents))
                self.assertEqual(merged['metrics'][0]['value'],
                                 [dict(documents[1]['metrics'][0]['value'][0], source_filing_year=2024)])
                self.assertEqual(merged['verification']['values_removed'], 1)

    def test_latest_filing_multiple_amounts_are_preserved_and_counted_for_review(self):
        older, newer = sample(2023, amount='-110'), sample(2024, amount='100')
        newer['metrics'][0]['value'].append(dict(deepcopy(newer['metrics'][0]['value'][0]),
                                               value='110', decimals='-1'))
        merged = merger.merge_documents(inputs(older, newer))
        self.assertEqual(merged['metrics'][0]['value'],
                         [dict(v, source_filing_year=2024) for v in newer['metrics'][0]['value']])
        self.assertEqual(merged['verification']['latest_filing_contexts_with_multiple_amounts'], 1)
        self.assertEqual(merged['verification']['values_removed'], 1)
        # A single-year merge must not guess between same-filing variants either.
        single = merger.merge_documents([(2024, Path('intel_2024.json'), json.dumps(newer).encode())])
        self.assertEqual(single['metrics'][0]['value'], merged['metrics'][0]['value'])
        self.assertEqual(single['verification']['values_removed'], 0)

    def test_older_matching_repeats_survive_even_when_other_older_amounts_conflict(self):
        documents = [sample(2023, amount='110'), sample(2024, amount='100'), sample(2025, amount='110')]
        merged = merger.merge_documents(inputs(*documents))
        self.assertEqual([v['source_filing_year'] for v in merged['metrics'][0]['value']], [2023, 2025])
        self.assertEqual(merged['verification']['values_removed'], 1)

    def test_invalid_amounts_fail_instead_of_silently_selecting_them(self):
        for amount, status in [('NaN', 'reported'), ('Infinity', 'reported'),
                               ('abc', 'reported'), (None, 'reported'),
                               (True, 'reported'), ('0', 'nil')]:
            with self.subTest(amount=amount, status=status):
                document = sample(2023, amount=amount)
                document['metrics'][0]['value'][0]['status'] = status
                with self.assertRaises(ValueError):
                    merger.merge_documents(inputs(document))

    def test_union_contains_concepts_missing_from_other_years_and_recomputes_scope(self):
        first, second = sample(2023), sample(2024)
        first['metrics'][0]['value'][0]['dimensions'] = [{'axis': 'ex:Axis', 'member': 'ex:Member'}]
        first['metrics'][0].update(scope='dimension_only', units=[], company_wide_annual_or_year_end_years=[])
        extra = deepcopy(first['metrics'][0])
        extra.update(concept='us-gaap:Liabilities', query_name='Liabilities', label='Liabilities')
        first['metrics'].append(extra)
        first['counts']['verified_concepts'] = 2
        merged = merger.merge_documents(inputs(first, second))
        self.assertEqual(merged['counts'], {'verified_concepts': 2, 'company_wide': 1, 'dimension_only': 1, 'value_records': 3})
        self.assertEqual(merged['metrics'][1]['source_filing_years'], [2023])
        self.assertEqual(merged['metrics'][1]['metadata_source_filing_year'], 2023)

    def test_issuer_and_items_must_match(self):
        for changed in (sample(2024, cik='72971', company='WELLS FARGO'),
                        dict(sample(2024), requested_items=['8'])):
            with self.subTest(changed=changed['company']), self.assertRaises(ValueError):
                merger.merge_documents(inputs(sample(2023), changed))
        renamed = sample(2024, company='Intel Renamed Corporation')
        self.assertEqual(merger.merge_documents(inputs(sample(2023), renamed))['company'], renamed['company'])
        renamed['verification']['source']['request'] = {}
        with self.assertRaisesRegex(ValueError, 'issuers'):
            merger.merge_documents(inputs(sample(2023), renamed))

    def test_taxonomy_year_is_not_used_as_filing_year(self):
        document = sample(2024)
        document['taxonomy_year'] = 2023
        merged = merger.merge_documents([(2024, Path('intel_2024_api_metrics_with_values.json'), json.dumps(document).encode())])
        self.assertEqual(merged['filing_years'], [2024])
        self.assertEqual(merged['taxonomy_years'], [2023])
        self.assertEqual(merged['metrics'][0]['value'][0]['source_filing_year'], 2024)

    def test_invalid_schemas_duplicate_concepts_and_empty_exports_fail(self):
        variants = [dict(sample(2023), schema_version='html-metrics-1.0'),
                    dict(sample(2023), schema_version='api-metrics-merged-1.0'),
                    dict(sample(2023), metrics=[]), dict(sample(2023), fiscal_year=2025)]
        duplicate = sample(2023)
        duplicate['metrics'] *= 2
        variants.append(duplicate)
        for variant in variants:
            with self.subTest(variant=variant.get('schema_version')), self.assertRaises(ValueError):
                merger.merge_documents(inputs(variant))
        entry = inputs(sample(2023))[0]
        with self.assertRaisesRegex(ValueError, 'distinct'):
            merger.merge_documents([entry, entry])

    def test_cli_writes_only_merged_json_and_preserves_inputs_existing_and_missing_years(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for y in (2023, 2024, 2025):
                (root / f'intel_{y}_api_metrics_with_values.json').write_text(json.dumps(sample(y)))
            before = {p: p.read_bytes() for p in root.iterdir()}
            args = ['--company', 'Intel', '--input-dir', str(root)]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(merger.main(args), 0)
                self.assertEqual(merger.main(args), 1)
            self.assertEqual(len(list(root.iterdir())), 4)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            merged = root / 'intel_2023_2025_merged_api_metrics_with_values.json'
            self.assertTrue(merged.exists())
            missing_output = root / 'missing.json'
            with redirect_stderr(io.StringIO()):
                self.assertEqual(merger.main(args + ['--years', '2023', '2026', '--output', str(missing_output)]), 1)
            self.assertFalse(missing_output.exists())

    def test_output_escapes_unicode_and_preserves_exact_source_text(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            document = sample(2023, company='INTEL\u00a0CORPORATION')
            metric = document['metrics'][0]
            metric['label'] = 'Assets \u2014 total'
            metric['definition'] = '\u201cAssets\u201d in Montr\u00e9al and \u6771\u4eac'
            metric['value'][0]['source_labels'][0]['row_label'] = 'Net\u00a0assets \u2212 liabilities'
            path = root / 'intel_2023_api_metrics_with_values.json'
            original = json.dumps(document, ensure_ascii=False).encode('utf-8')
            path.write_bytes(original)
            expected = merger.merge_documents([(2023, path, original)])
            with redirect_stdout(io.StringIO()):
                target = merger.merge_company('Intel', years=[2023], input_dir=root)
            raw = target.read_bytes()
            self.assertTrue(raw.isascii())
            self.assertIn(b'\\u00a0', raw)
            self.assertIn(b'\\u2014', raw)
            self.assertEqual(json.loads(raw), expected)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
