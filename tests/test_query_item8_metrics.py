"""Official-taxonomy resolution and evidence-bound financial query regressions."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from lxml import etree

from sec_disclosure.table_extraction import extract_item8_xbrl_api as api
from sec_disclosure.table_extraction import query_item8_metrics as q
from sec_disclosure.table_extraction import xbrl_metric_dictionary as t


def package(year=2025):
    """Small XLink package with resource IDs unrelated to concept names."""
    ns = {'xs': t.XS, 'xbrli': t.XI, 'link': t.LINK, 'xlink': t.XL}
    schema = etree.Element(f'{{{t.XS}}}schema', nsmap=ns, targetNamespace=f'http://fasb.org/us-gaap/{year}')
    specs = [
        ('NetIncomeLoss', 'Net Income (Loss) Attributable to Parent', 'Parent profit after tax.', 'duration', False),
        ('ProfitLoss', 'Profit (Loss)', 'Profit including noncontrolling interests.', 'duration', False),
        ('RetainedEarningsAccumulatedDeficit', 'Retained Earnings (Accumulated Deficit)', 'Accumulated undistributed earnings.', 'instant', False),
        ('StockholdersEquity', "Stockholders' Equity", 'Equity attributable to parent.', 'instant', False),
        ('StatementEquityComponentsAxis', 'Equity Components [Axis]', 'Components.', 'duration', True),
        ('RetainedEarningsMember', 'Retained Earnings [Member]', 'Accumulated undistributed earnings.', 'duration', True),
        ('Assets', 'Assets', 'Resources controlled by the entity.', 'instant', False),
        ('SomethingTextBlock', 'Net income', 'Narrative only.', 'duration', False),
    ]
    for name, _, _, period, abstract in specs:
        el = etree.SubElement(schema, f'{{{t.XS}}}element', name=name, id='ID_' + name,
                              type='xbrli:monetaryItemType' if not abstract else 'domainItemType')
        if 'TextBlock' in name:
            el.set('type', 'textBlockItemType')
        el.set(f'{{{t.XI}}}periodType', period)
        if abstract:
            el.set('abstract', 'true')
    docs = {'xsd': schema}
    for kind, role in [('lab', 'label'), ('doc', 'documentation')]:
        root = etree.Element(f'{{{t.LINK}}}linkbase', nsmap=ns)
        link = etree.SubElement(root, f'{{{t.LINK}}}labelLink')
        for i, (name, label, definition, _, _) in enumerate(specs):
            loc = etree.SubElement(link, f'{{{t.LINK}}}loc')
            loc.set(f'{{{t.XL}}}label', f'locator{i}')
            loc.set(f'{{{t.XL}}}href', f'us-gaap-{year}.xsd#ID_{name}')
            resource = etree.SubElement(link, f'{{{t.LINK}}}label')
            resource.set(f'{{{t.XL}}}label', f'resource{i}')
            resource.set(f'{{{t.XL}}}role', t.ROLE + role)
            resource.set(t.LANG, 'en-US')
            resource.text = label if kind == 'lab' else definition
            arc = etree.SubElement(link, f'{{{t.LINK}}}labelArc')
            arc.set(f'{{{t.XL}}}from', f'locator{i}')
            arc.set(f'{{{t.XL}}}to', f'resource{i}')
        docs[kind] = root
    out = io.BytesIO()
    with ZipFile(out, 'w') as z:
        for kind, root in docs.items():
            filename = f'us-gaap-{year}.xsd' if kind == 'xsd' else f'us-gaap-{kind}-{year}.xml'
            z.writestr(f'us-gaap-{year}/elts/{filename}', etree.tostring(root))
    return out.getvalue()


def number(value, year=2024, instant=False, segment=None, unit='usd'):
    period = {'instant': f'{year}-08-29'} if instant else {'startDate': f'{year-1}-09-01', 'endDate': f'{year}-08-29'}
    result = {'value': str(value), 'unitRef': unit, 'period': period, 'decimals': '-6'}
    if segment is not None:
        result['segment'] = segment
    return result


COMPONENT = {'explicitMember': {'dimension': 'us-gaap:StatementEquityComponentsAxis', '$t': 'us-gaap:RetainedEarningsMember'}}


def saved_fixture(root, records=None, duplicate=False, outside=None, custom=None):
    source = (b'<html xmlns:us-gaap="http://fasb.org/us-gaap/2025" '
              b'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
              b'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">'
              b'<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
              b'<xbrli:unit id="usd_other"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
              b'<xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit></html>')
    filing = root / 'source.htm'
    filing.write_bytes(source)
    payload = {'CoverPage': {'EntityRegistrantName': 'Example', 'EntityCentralIndexKey': '000123',
                            'DocumentFiscalYearFocus': '2025', 'DocumentPeriodEndDate': '2025-08-28'},
               'Income': records or {'NetIncomeLoss': [number(778000000)]}}
    if duplicate:
        payload['CashFlows'] = deepcopy(payload['Income'])
    source_meta = {'request': {'accession-no': '0000000123-25-000001'}, 'response_sha256': api.digest(payload),
                   'filing_sha256': q.membership.sha(source), 'filing': str(filing)}
    groups = []
    for group, data in payload.items():
        entries = []
        if group != 'CoverPage':
            for concept, records in data.items():
                for i, record in enumerate(records):
                    pointer = '/response' + api.pointer(group, concept, i)
                    status = 'outside_item_8' if outside == (concept, i) else 'item_8'
                    namespace = 'http://example.com/custom' if custom == concept else 'http://fasb.org/us-gaap/2025'
                    entries.append({'api_pointer': pointer, 'concept': concept, 'status': status,
                                    'value_checked': True, 'evidence': [
                                        {'concept': '{' + namespace + '}' + concept, 'status': status, 'source_fact_id': 'f1'}]})
        groups.append({'api_group': group, 'entries': entries})
    report = {'schema_version': 'api-item-membership-1.1', 'item': '8', 'year': 2025,
              'source': source_meta, 'groups': groups}
    named = {'schema_version': 'api-table-titles-1.0', 'item': '8', 'year': 2025, 'source': source_meta,
             'groups': {key: {'data': data, 'tables': []} for key, data in payload.items()}}
    output = root / 'item_8_named_api.json'
    output.write_text(json.dumps(named))
    (root / 'item_8_membership_check.json').write_text(json.dumps(report))
    return output


class DictionaryTests(unittest.TestCase):
    def test_official_labels_resolve_through_xlink_and_types(self):
        d = t.build_dictionary(package(), 2025)
        c = d['concepts']['NetIncomeLoss']
        self.assertEqual(c['label'], 'Net Income (Loss) Attributable to Parent')
        self.assertEqual(c['definition'], 'Parent profit after tax.')
        self.assertEqual(c['period_type'], 'duration')
        self.assertEqual(len(d['sources']), 3)
        self.assertEqual(t.search(d, 'net income')[0]['concept'], 'us-gaap:NetIncomeLoss')
        self.assertEqual(t.search(d, 'retained earnings')[0]['concept'], 'us-gaap:RetainedEarningsAccumulatedDeficit')
        self.assertNotIn('us-gaap:SomethingTextBlock', {x['concept'] for x in t.search(d, 'net income')})
        self.assertEqual(t.search(d, 'imaginary metric'), [])
        self.assertEqual(t.search(d, 'resources controlled')[0]['match_basis'], 'definition_tokens')

    def test_version_namespace_and_offline_cache_integrity(self):
        with self.assertRaisesRegex(ValueError, 'Expected one official'):
            t.build_dictionary(package(2023), 2025)
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'cache missing'):
                t.load_dictionary(2025, cache, offline=True)
            (cache / 'us-gaap-2025.zip').write_bytes(package())
            first = t.load_dictionary(2025, cache, offline=True)
            path = cache / 'us-gaap-2025-dictionary.json'
            changed = deepcopy(first)
            changed['concepts']['Assets']['definition'] = 'wrong'
            path.write_text(json.dumps(changed))
            with patch.object(t, 'urlopen', side_effect=AssertionError('offline')):
                self.assertEqual(t.load_dictionary(2025, cache, offline=True), first)


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = t.build_dictionary(package(), 2025)

    def run_query(self, root, records=None, metric='net income', years=(2022, 2023, 2024), **kwargs):
        f = q.Filing(saved_fixture(root, records, **kwargs))
        return q.query([f], {2025: self.dictionary}, metric, years)

    def test_annual_entity_wide_values_duplicate_sources_and_missing(self):
        records = {'NetIncomeLoss': [number(778000000), number(-5833000000, 2023),
                                    number(999, segment=COMPONENT),
                                    {'value': '111', 'unitRef': 'usd', 'period': {'startDate': '2024-06-01', 'endDate': '2024-08-29'}}]}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = self.run_query(root, records, duplicate=True)
            self.assertEqual([x['value'] for x in r['results']], [None, '-5833000000', '778000000'])
            self.assertEqual(r['results'][0]['status'], 'missing')
            self.assertEqual(len(r['results'][2]['sources']), 2)
            self.assertEqual(r['results'][2]['unit'], 'USD')
            named = json.loads((root / 'item_8_named_api.json').read_text())
            for source in r['results'][2]['sources']:
                value = named
                for part in source['json_pointer'].split('/')[1:]:
                    value = value[int(part)] if isinstance(value, list) else value[part.replace('~1', '/').replace('~0', '~')]
                self.assertEqual(value['value'], '778000000')

    def test_retained_earnings_component_rule_deduplicates_and_excludes_movements(self):
        intel_style = {'dimension': 'us-gaap:StatementEquityComponentsAxis', 'value': 'us-gaap:RetainedEarningsMember'}
        extra = [intel_style, {'dimension': 'us-gaap:OtherAxis', 'value': 'us-gaap:OtherMember'}]
        records = {'RetainedEarningsAccumulatedDeficit': [number(40877000000, instant=True)],
                   'StockholdersEquity': [number(40877000000, instant=True, segment=COMPONENT),
                                          number(40824000000, 2023, instant=True, segment=intel_style),
                                          number(1, 2023, instant=True, segment=extra), number(2, segment=COMPONENT)],
                   'NetIncomeLoss': [number(3, segment=COMPONENT)]}
        with TemporaryDirectory() as tmp:
            r = self.run_query(Path(tmp), records, metric='retained earnings')
            self.assertEqual([x['value'] for x in r['results']], [None, '40824000000', '40877000000'])
            self.assertEqual(len(r['results'][2]['representations']), 2)
            self.assertTrue(r['results'][2]['representations'][1]['basis'].startswith('project_rule:'))

    def test_conflicting_values_or_currencies_are_not_summed_or_overwritten(self):
        for second in [number(2), number(1, unit='eur')]:
            with self.subTest(second=second), TemporaryDirectory() as tmp:
                r = self.run_query(Path(tmp), {'NetIncomeLoss': [number(1), second]}, years=[2024])
                self.assertEqual(r['results'][0]['status'], 'ambiguous')
                self.assertEqual(len(r['results'][0]['candidates']), 2)
        with TemporaryDirectory() as tmp:
            r = self.run_query(Path(tmp), {'NetIncomeLoss': [number(1), number(1, unit='usd_other')]}, years=[2024])
            self.assertEqual(r['results'][0]['value'], '1')

    def test_nil_outside_item8_and_custom_namespace_are_not_false_values(self):
        nil = number(1)
        nil.update(value=None, **{'xsi:nil': 'true'})
        with TemporaryDirectory() as tmp:
            r = self.run_query(Path(tmp), {'NetIncomeLoss': [nil]}, years=[2024])
            self.assertEqual(r['results'][0]['status'], 'nil')
        for options in ({'outside': ('NetIncomeLoss', 0)}, {'custom': 'NetIncomeLoss'}):
            with TemporaryDirectory() as tmp:
                r = self.run_query(Path(tmp), years=[2024], **options)
                self.assertEqual(r['results'][0]['status'], 'missing')
                self.assertEqual(len(r['excluded_records']), 1)

    def test_unknown_ambiguous_and_definition_only_queries_require_selection(self):
        with TemporaryDirectory() as tmp:
            f = q.Filing(saved_fixture(Path(tmp)))
            self.assertEqual(q.query([f], {2025: self.dictionary}, 'unknown something', [2024])['status'], 'metric_not_found')
            self.assertEqual(q.query([f], {2025: self.dictionary}, 'resources controlled', [2024])['status'], 'needs_concept_selection')
            changed = deepcopy(self.dictionary)
            changed['concepts']['ProfitLoss']['labels'][0]['text'] = 'Net Income (Loss)'
            # Both exact readable labels have equal scores.
            changed['concepts']['NetIncomeLoss']['labels'][0]['text'] = 'Net Income (Loss)'
            r = q.query([f], {2025: changed}, 'net income', [2024])
            self.assertEqual(r['status'], 'ambiguous_metric')
            r = q.query([f], {2025: changed}, 'net income', [2024], concept='us-gaap:NetIncomeLoss')
            self.assertEqual(r['results'][0]['value'], '778000000')

    def test_fiscal_year_with_january_year_end_and_transition_period(self):
        with TemporaryDirectory() as tmp:
            f = q.Filing(saved_fixture(Path(tmp)))
            f.fiscal_year, f.year_end = 2023, date(2024, 1, 5)
            fact = {'period': {'startDate': '2022-01-08', 'endDate': '2023-01-06'}}
            self.assertTrue(f.fiscal_match(fact, 2022))
            self.assertFalse(f.fiscal_match(fact, 2023))
            fact['period']['startDate'] = '2022-07-01'
            self.assertFalse(f.fiscal_match(fact, 2022))

    def test_wrong_evidence_modified_data_and_mixed_companies_fail(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = saved_fixture(root)
            saved = path.read_bytes()
            changed = json.loads(saved)
            changed['groups']['Income']['data']['NetIncomeLoss'][0]['value'] = '2'
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'unchanged API'):
                q.Filing(path)
            path.write_bytes(saved)
            f = q.Filing(path)
            other = deepcopy(f)
            other.cik = '999'
            with self.assertRaisesRegex(ValueError, 'one company'):
                q.query([f, other], {2025: self.dictionary}, 'net income', [2024])
            f.filing_path.write_bytes(b'wrong filing')
            with self.assertRaisesRegex(ValueError, 'hash differs'):
                q.Filing(path)

    def test_python_interface_combines_comparatives_from_same_company(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for i, facts in enumerate(([number(10, 2024), number(9, 2023)], [number(9, 2023), number(8, 2022)])):
                folder = root / str(i)
                folder.mkdir()
                paths.append(saved_fixture(folder, {'NetIncomeLoss': facts}))
            (root / 'us-gaap-2025.zip').write_bytes(package())
            r = q.query_metric(paths, 'net income', range(2022, 2025), taxonomy_cache=root, offline=True)
            self.assertEqual(r['status'], 'complete')
            self.assertEqual([row['value'] for row in r['results']], ['8', '9', '10'])
            self.assertEqual(len(r['results'][1]['sources']), 2)

    def test_cli_uses_cached_taxonomy_and_protects_inputs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = saved_fixture(root)
            before = path.read_bytes()
            (root / 'us-gaap-2025.zip').write_bytes(package())
            output = root / 'query.json'
            args = [str(path), '--metric', 'net income', '--years', '2024', '--offline', '--taxonomy-cache', str(root)]
            # Output is deliberately outside the taxonomy cache directory.
            with TemporaryDirectory() as outdir, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                output = Path(outdir) / 'query.json'
                self.assertEqual(q.main(args + ['--output', str(output)]), 0)
                self.assertEqual(json.loads(output.read_text())['results'][0]['value'], '778000000')
                self.assertEqual(q.main(args + ['--output', str(path)]), 1)
                self.assertEqual(q.main(args + ['--output', str(output)]), 1)
            self.assertEqual(path.read_bytes(), before)


@unittest.skipUnless(os.environ.get('SEC_METRIC_QUERY_INTEGRATION') == '1', 'Cached Intel/Micron query integration is opt-in')
class RealFilingTests(unittest.TestCase):
    def test_official_packages_and_real_values_with_unchanged_inputs(self):
        cases = [
            ('micron_2025_item_8_api_check', 2025, [None, '-5833000000', '778000000'],
             ['47274000000', '40824000000', '40877000000']),
            ('intel_2023_items_7_8_hybrid_tables', 2023, ['8014000000', '1689000000', None],
             ['70405000000', '69156000000', None]),
        ]
        for folder, year, income, earnings in cases:
            with self.subTest(folder=folder):
                root = Path('data/table_output') / folder
                paths = [root / name for name in ['item_8_named_api.json', 'item_8_membership_check.json', 'sec_api_xbrl.json']]
                before = {p: q.membership.sha(p.read_bytes()) for p in paths}
                f = q.Filing(paths[0])
                self.assertEqual(f.taxonomy_year, year)
                d = t.load_dictionary(year, offline=True)
                self.assertGreater(len(d['concepts']), 17000)
                for metric, expected in [('net income', income), ('retained earnings', earnings)]:
                    r = q.query([f], {year: d}, metric, [2022, 2023, 2024])
                    self.assertEqual([row['value'] for row in r['results']], expected)
                    for row in r['results']:
                        for source in row.get('sources', []):
                            self.assertTrue(source['source_fact_ids'])
                            obj = f.named
                            for part in source['json_pointer'].split('/')[1:]:
                                obj = obj[int(part)] if isinstance(obj, list) else obj[part.replace('~1', '/').replace('~0', '~')]
                            self.assertEqual(api.decimal_value(obj['value']), row['value'])
                self.assertEqual({p: q.membership.sha(p.read_bytes()) for p in paths}, before)


if __name__ == '__main__':
    unittest.main()
