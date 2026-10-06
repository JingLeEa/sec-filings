#!/usr/bin/env python3
"""Search Item 8 API values using the filing's official US-GAAP dictionary.

python3 query_item8_metrics.py path/to/item_8_named_api.json \
  --metric "net income" --years 2022 2023 2024

Requires the matching item_8_membership_check.json and cached original filing.
The first use downloads the official FASB taxonomy; subsequent queries can use
--offline. Values always come from the API JSON, never HTML table cells.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import json
from pathlib import Path
import re
import sys
from urllib.error import URLError
from zipfile import BadZipFile

from lxml import etree

import check_item8_api as membership
import extract_item8_xbrl_api as api
import xbrl_metric_dictionary as taxonomy

VERSION = '1.0.0'
XI = taxonomy.XI
ISO = 'http://www.xbrl.org/2003/iso4217'

# Interpretation of an alternate reporting representation, NOT an official
# taxonomy alias. Restrict to exactly this one component axis/member, an
# instant, and no other dimensions; never include movements such as dividends.
COMPONENT_RULES = {
    'RetainedEarningsAccumulatedDeficit': {
        'concept': 'StockholdersEquity',
        'axis': 'StatementEquityComponentsAxis', 'member': 'RetainedEarningsMember',
        'basis': 'project_rule: equity balance restricted to the retained earnings component',
    },
}


def expand(value, node):
    if not isinstance(value, str) or value.count(':') != 1:
        raise ValueError('Expected a qualified XBRL name')
    prefix, name = value.split(':')
    namespace = node.nsmap.get(prefix)
    if not namespace or not name:
        raise ValueError(f'Unresolved XBRL prefix: {prefix}')
    return f'{{{namespace}}}{name}'


def display_unit(measures):
    def one(qname):
        return qname[len('{' + ISO + '}'):] if qname.startswith('{' + ISO + '}') else qname
    numerator, denominator = measures
    top = '*'.join(map(one, numerator))
    return top if not denominator else top + '/' + '*'.join(map(one, denominator))


def units(root):
    result = {}
    for node in root.iter(f'{{{XI}}}unit'):
        divide = node.find(f'{{{XI}}}divide')
        if divide is None:
            numerator = node.findall(f'{{{XI}}}measure')
            denominator = []
        else:
            numerator = divide.findall(f'{{{XI}}}unitNumerator/{{{XI}}}measure')
            denominator = divide.findall(f'{{{XI}}}unitDenominator/{{{XI}}}measure')
            if not denominator:
                raise ValueError('Invalid XBRL denominator')
        if not numerator:
            raise ValueError('Invalid XBRL unit')
        result[node.get('id')] = (tuple(sorted(expand(n.text.strip(), n) for n in numerator)),
                                 tuple(sorted(expand(n.text.strip(), n) for n in denominator)))
    return result


def resolve_path(value, parent):
    path = Path(value)
    if path.exists():
        return path
    other = parent / path
    if other.exists():
        return other
    raise ValueError(f'Matching original filing is missing: {path}. Supply --filing for a relocated file.')


def fiscal_period_matches(period, year, fiscal_year, year_end):
    """Match a year-end/annual period to a filing's fiscal anniversary."""
    end = date.fromisoformat(period.get('instant', period.get('endDate')))
    expected_year = year_end.year - (fiscal_year - year)
    try:
        expected = year_end.replace(year=expected_year)
    except ValueError:  # Leap-day anniversary.
        expected = year_end.replace(year=expected_year, day=28)
    if abs((end - expected).days) > 14:
        return False
    return ('instant' in period or
            350 <= (end - date.fromisoformat(period['startDate'])).days + 1 <= 380)


class Filing:
    def __init__(self, path, report_path=None, filing_path=None):
        self.path = Path(path)
        self.named = api.read_json(self.path.read_bytes())
        if self.named.get('schema_version') != 'api-table-titles-1.0' or self.named.get('item') != '8':
            raise ValueError('Input must be an Item 8 item_8_named_api.json file')
        self.report_path = Path(report_path) if report_path else self.path.with_name('item_8_membership_check.json')
        self.report = api.read_json(self.report_path.read_bytes())
        if (self.report.get('schema_version') not in {'api-item-membership-1.0', 'api-item-membership-1.1'}
                or self.report.get('item') != '8'):
            raise ValueError('Unsupported Item 8 membership report')
        self.payload = {key: group['data'] for key, group in self.named['groups'].items()}
        self.source = self.named['source']
        if (api.digest(self.payload) != self.source['response_sha256']
                or self.named['year'] != self.report.get('year')
                or any(self.source.get(k) != self.report['source'].get(k)
                       for k in ('response_sha256', 'filing_sha256', 'request'))):
            raise ValueError('Named JSON and membership evidence do not describe the same unchanged API response')
        self.filing_path = Path(filing_path) if filing_path else resolve_path(self.source['filing'], self.path.parent)
        data = self.filing_path.read_bytes()
        if membership.sha(data) != self.source['filing_sha256']:
            raise ValueError('Original filing hash differs from membership evidence')
        root = taxonomy.xml(data)
        self.namespaces = root.nsmap
        versions = {int(m[1]) for uri in root.nsmap.values()
                    if (m := re.fullmatch(r'http://fasb.org/us-gaap/(\d{4})', uri or ''))}
        if len(versions) != 1:
            raise ValueError('Expected one US-GAAP taxonomy version in the original filing; other taxonomies are not yet supported')
        self.taxonomy_year = next(iter(versions))
        self.namespace = f'http://fasb.org/us-gaap/{self.taxonomy_year}'
        unit_map = units(root)
        cover = self.payload.get('CoverPage', {})
        self.company = str(cover.get('EntityRegistrantName', ''))
        self.cik = str(cover.get('EntityCentralIndexKey', '')).lstrip('0')
        if not self.cik.isdigit():
            raise ValueError('Missing unique company CIK in CoverPage')
        self.fiscal_year = int(cover['DocumentFiscalYearFocus'])
        if self.fiscal_year != self.named['year']:
            raise ValueError('CoverPage fiscal year differs from the named JSON')
        self.year_end = date.fromisoformat(cover['DocumentPeriodEndDate'])
        checked = {e['api_pointer']: e for g in self.report['groups'] + self.report.get('standalone_concepts', [])
                   for e in g['entries']}
        prefix = '/response' if any(p.startswith('/response/') for p in checked) else ''
        tables = defaultdict(list)
        for group in self.named['groups'].values():
            for table in group['tables']:
                for pointer in table['matched_api_pointers']:
                    desc = {k: table[k] for k in ('table_id', 'title', 'title_source', 'page', 'source_locator')}
                    if desc not in tables[pointer]:
                        tables[pointer].append(desc)
        self.facts, self.excluded = [], []
        self.protected = {self.path.resolve(), self.report_path.resolve(), self.filing_path.resolve()}
        if self.source.get('api_file'):
            self.protected.add(Path(self.source['api_file']).resolve())
        for group, concept, pointer, raw in membership.entries(self.payload, prefix):
            if not isinstance(raw, dict) or 'period' not in raw or 'unitRef' not in raw:
                continue
            entry = checked.get(pointer)
            reason = None
            if not entry or entry.get('status') != 'item_8' or not entry.get('value_checked'):
                reason = 'not_verified_in_item_8'
            names = {e['concept'] for e in entry.get('evidence', []) if e.get('status') == 'item_8'} if entry else set()
            expected_name = f'{{{self.namespace}}}{concept.removeprefix("us-gaap:")}'
            if not reason and names != {expected_name}:
                reason = 'custom_or_ambiguous_concept_namespace'
            try:
                fact = api.normalise_fact(membership.matching_record(raw), pointer)
                if fact['status'] not in {'reported', 'nil'} or fact['value_type'] not in {'number', 'nil'}:
                    reason = reason or 'invalid_or_incomplete_numeric_fact'
                dimensions = tuple(sorted((expand(d['axis'], root), expand(d['member'], root))
                                          for d in fact['dimensions'] or []))
                unit = unit_map.get(fact['unit_ref'])
                if unit is None:
                    reason = reason or 'unresolved_unit'
            except ValueError:
                reason, dimensions, unit = reason or 'unsupported_dimensions', (), None
            if reason:
                self.excluded.append({'concept': concept, 'api_pointer': pointer, 'reason': reason})
                continue
            # The checker is the authority on membership; group-level titles
            # alone do not prove that all of a group's facts belong to Item 8.
            suffix = pointer[len(prefix + api.pointer(group)):]
            named_pointer = api.pointer('groups', group, 'data') + suffix
            source = {'file': str(self.path), 'json_pointer': named_pointer,
                      'api_pointer': pointer, 'api_group': group,
                      'tables': tables[pointer],
                      'source_fact_ids': sorted({e['source_fact_id'] for e in entry['evidence']
                                                 if e.get('status') == 'item_8' and e.get('source_fact_id')})}
            self.facts.append({
                'concept': concept.removeprefix('us-gaap:'), 'value': fact['value'],
                'period': {k: v for k, v in fact['period'].items() if k != 'type'},
                'period_type': fact['period']['type'], 'dimensions': dimensions,
                'unit': display_unit(unit), 'unit_signature': unit,
                'unit_ref': fact['unit_ref'], 'status': fact['status'], 'source': source,
            })

    def qname(self, name):
        return f'{{{self.namespace}}}{name}'

    def fiscal_match(self, fact, year):
        """Match fiscal anniversaries, allowing 52/53-week year-end movement.

        Do not use the filing's FY label for every comparative fact, nor map
        quarter/transition periods to an annual result. Large changes in year
        end require review; this function deliberately does not guess them.
        """
        return fiscal_period_matches(fact['period'], year, self.fiscal_year, self.year_end)


def candidates_for(filings, dictionaries, metric):
    grouped = {}
    for filing in filings:
        dictionary = dictionaries[filing.taxonomy_year]
        available = {f['concept'] for f in filing.facts}
        for c in taxonomy.search(dictionary, metric):
            name = c['concept']
            if name not in grouped:
                grouped[name] = {**c, 'taxonomy_years': [], 'available_in_verified_item_8': False}
            item = grouped[name]
            item['taxonomy_years'].append(filing.taxonomy_year)
            item['available_in_verified_item_8'] |= name.split(':')[1] in available
    for c in grouped.values():
        c['taxonomy_years'] = sorted(set(c['taxonomy_years']))
    return sorted(grouped.values(), key=lambda c: (-c['score'], c['concept']))[:12]


def representations(name, filing, dictionary):
    result = [{'concept': name, 'dimensions': (), 'basis': 'official_concept'}]
    rule = COMPONENT_RULES.get(name)
    if rule and all(n in dictionary['concepts'] for n in (rule['concept'], rule['axis'], rule['member'])):
        result.append({'concept': rule['concept'],
                       'dimensions': ((filing.qname(rule['axis']), filing.qname(rule['member'])),),
                       'basis': rule['basis']})
    return result


def query(filings, dictionaries, metric, years, concept=None, search_only=False):
    if not filings or len({f.cik for f in filings}) != 1:
        raise ValueError('Query one company at a time; do not combine different CIKs')
    years = sorted(set(years))
    if not years or any(not 1900 <= y <= 2200 for y in years):
        raise ValueError('Provide four-digit fiscal years between 1900 and 2200')
    candidates = candidates_for(filings, dictionaries, metric)
    result = {
        'schema_version': 'item8-metric-query-1.0', 'query_version': VERSION,
        'metric': metric, 'company': filings[0].company, 'cik': filings[0].cik,
        'years': years, 'status': None, 'selected_concept': None, 'candidates': candidates,
        'taxonomy_sources': [{k: d[k] for k in ('year', 'namespace', 'package_url', 'package_sha256', 'sources')}
                             for _, d in sorted(dictionaries.items())],
        'policy': {
            'scope': 'numeric API records individually verified in Item 8',
            'selection': 'unique strong label/name match, or explicit --concept; definitions also supply search suggestions',
            'dimensions': 'entity-wide facts; retained earnings also permits the explicitly documented equity-component rule',
            'years': 'fiscal year-end anniversaries within 14 days; annual durations of 350–380 days',
            'duplicates': 'equal values for identical periods and units are returned once with all source pointers',
            'conflicts': 'different periods, values or units for one year are returned as ambiguous; no latest-filing override',
            'missing': 'no verified matching fact in these files; no automatic fetch, summation or imputation',
        },
        'results': [],
    }
    if search_only:
        result['status'] = 'search_results'
        return result
    if concept:
        if ':' in concept and not concept.startswith('us-gaap:'):
            raise ValueError('Only official us-gaap concepts are currently supported')
        name = concept.removeprefix('us-gaap:')
        if not any(name in d['concepts'] and taxonomy.metric_concept(d['concepts'][name]) for d in dictionaries.values()):
            raise ValueError('Requested concept is not a numeric metric in the filing taxonomies')
        basis = 'explicit_concept'
    else:
        strong = [c for c in candidates if c['score'] >= 90]
        if not strong:
            result['status'] = 'needs_concept_selection' if candidates else 'metric_not_found'
            return result
        best = max(c['score'] for c in strong)
        winners = [c for c in strong if c['score'] == best]
        if len(winners) != 1:
            result['status'] = 'ambiguous_metric'
            return result
        name = winners[0]['concept'].split(':')[1]
        basis = winners[0]['match_basis']
    definitions = [{k: d['concepts'][name][k] for k in ('concept', 'label', 'definition', 'period_type', 'namespace')}
                   for d in dictionaries.values() if name in d['concepts']]
    if len({d['period_type'] for d in definitions}) != 1:
        raise ValueError('Selected concept has incompatible period types across taxonomy versions')
    period_type = definitions[0]['period_type']
    result['selected_concept'] = {'concept': 'us-gaap:' + name, 'match_basis': basis, 'definitions': definitions}
    relevant_names = {name}
    if name in COMPONENT_RULES:
        relevant_names.add(COMPONENT_RULES[name]['concept'])
    result['excluded_records'] = [dict(e, file=str(f.path)) for f in filings for e in f.excluded
                                  if e['concept'].removeprefix('us-gaap:') in relevant_names]
    for year in years:
        found = {}
        for filing in filings:
            dictionary = dictionaries[filing.taxonomy_year]
            for representation in representations(name, filing, dictionary):
                for fact in filing.facts:
                    if (fact['concept'] != representation['concept']
                            or fact['dimensions'] != representation['dimensions']
                            or fact['period_type'] != period_type or not filing.fiscal_match(fact, year)):
                        continue
                    key = api.digest([fact['period'], fact['unit_signature'], fact['value']])
                    if key not in found:
                        found[key] = {'value': fact['value'], 'unit': fact['unit'], 'period': fact['period'],
                                      'status': 'nil' if fact['status'] == 'nil' else 'found',
                                      'representations': [], 'sources': []}
                    match = found[key]
                    desc = {'concept': 'us-gaap:' + fact['concept'],
                            'dimensions': [{'axis': a, 'member': m} for a, m in fact['dimensions']],
                            'basis': representation['basis']}
                    if desc not in match['representations']:
                        match['representations'].append(desc)
                    if fact['source'] not in match['sources']:
                        match['sources'].append(fact['source'])
        choices = list(found.values())
        if not choices:
            row = {'year': year, 'status': 'missing', 'value': None, 'reason': 'No verified matching annual/year-end fact in supplied files'}
        elif len(choices) == 1:
            row = {'year': year, **choices[0]}
        else:
            row = {'year': year, 'status': 'ambiguous', 'value': None, 'candidates': choices}
        result['results'].append(row)
    result['status'] = ('ambiguous_values' if any(r['status'] == 'ambiguous' for r in result['results']) else
                        'complete' if all(r['status'] == 'found' for r in result['results']) else 'incomplete')
    return result


def query_metric(files, metric, years, *, taxonomy_cache=Path('data/taxonomy_cache'),
                 offline=False, concept=None, search_only=False):
    """Convenience API: one filename or a list of named JSON files for one CIK."""
    paths = [files] if isinstance(files, (str, Path)) else files
    filings = [Filing(p) for p in paths]
    if not filings or len({f.cik for f in filings}) != 1:
        raise ValueError('Query one company at a time; do not combine different CIKs')
    dictionaries = {year: taxonomy.load_dictionary(year, taxonomy_cache, offline)
                    for year in sorted({f.taxonomy_year for f in filings})}
    return query(filings, dictionaries, metric, years, concept, search_only)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('files', type=Path, nargs='+', help='Named Item 8 JSON files for one company')
    parser.add_argument('--metric', required=True, help='Readable metric label or official concept name')
    parser.add_argument('--years', nargs='+', type=int, required=True, help='Fiscal years to retrieve')
    parser.add_argument('--concept', help='Resolve ambiguity using an exact us-gaap concept from --search')
    parser.add_argument('--search', action='store_true', help='Show taxonomy candidates and definitions without selecting values')
    parser.add_argument('--membership', type=Path, help='Override membership report (one input only)')
    parser.add_argument('--filing', type=Path, help='Override original filing location (one input only)')
    parser.add_argument('--taxonomy-cache', type=Path, default=Path('data/taxonomy_cache'))
    parser.add_argument('--offline', action='store_true', help='Require existing official taxonomy packages')
    parser.add_argument('--output', type=Path, help='Write a new query JSON; default is stdout')
    args = parser.parse_args(argv)
    try:
        if len(args.files) > 1 and (args.membership or args.filing):
            raise ValueError('--membership/--filing overrides support one input at a time')
        filings = [Filing(p, args.membership, args.filing) for p in args.files]
        if args.output:
            protected = set().union(*(f.protected for f in filings))
            if (args.output.resolve() in protected or args.output.exists()
                    or args.output.resolve().is_relative_to(args.taxonomy_cache.resolve())):
                raise ValueError('--output must be a new query file, separate from inputs and the taxonomy cache')
        dictionaries = {year: taxonomy.load_dictionary(year, args.taxonomy_cache, args.offline)
                        for year in sorted({f.taxonomy_year for f in filings})}
        result = query(filings, dictionaries, args.metric, args.years, args.concept, args.search)
        if args.output:
            api.write_json(result, args.output)
            print(f'Wrote {args.output} ({result["status"]})')
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, BadZipFile, etree.XMLSyntaxError, URLError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
