#!/usr/bin/env python3
"""Merge yearly API metrics by concept, preferring newer filing values on conflict.

    python3 merge_api_metrics.py --company Intel

Defaults to FY2023–FY2025 under tests/for_table_development/<company>/.
No downloads or API calls. Original yearly files are never overwritten.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from extract_item8_xbrl_api import slug


DEFAULT_YEARS = (2023, 2024, 2025)
VERSION = '1.1.0'


def context_key(value):
    """Concept is grouped separately; accuracy and source locations are not context."""
    return (json.dumps(value['period'], sort_keys=True), value['unit'],
            tuple(sorted(json.dumps(d, sort_keys=True) for d in value['dimensions'])))


def amount_key(value):
    """Compare exact numeric amounts without conflating nil with zero or rounding."""
    if value['status'] == 'nil' and value['value'] is None:
        return ('nil', None)
    if value['status'] != 'reported' or isinstance(value['value'], bool):
        raise ValueError('Expected a reported numeric amount or an explicit nil value')
    try:
        amount = Decimal(str(value['value']))
    except InvalidOperation:
        raise ValueError('Reported amount must be a finite decimal number') from None
    if not amount.is_finite():
        raise ValueError('Reported amount must be a finite decimal number')
    return ('reported', amount)


def select_latest_values(versions):
    """Remove older disagreements, preserving order and each retained record intact."""
    contexts = defaultdict(list)
    for year, _, original in versions:
        for value in original['value']:
            contexts[context_key(value)].append((year, amount_key(value)))
    allowed = {}
    resolved = latest_multiple = 0
    for context, entries in contexts.items():
        latest_year = max(year for year, _ in entries)
        latest_amounts = {amount for year, amount in entries if year == latest_year}
        allowed[context] = latest_amounts
        resolved += any(amount not in latest_amounts for _, amount in entries)
        latest_multiple += len(latest_amounts) > 1
    retained = [dict(deepcopy(value), source_filing_year=year)
                for year, _, original in versions for value in original['value']
                if amount_key(value) in allowed[context_key(value)]]
    return retained, resolved, latest_multiple


def issuer_cik(data):
    """Use the already recorded SEC source identity, not the output label."""
    source = data['verification']['source']
    url = urlparse(source.get('request', {}).get('htm-url', ''))
    match = re.fullmatch(r'/Archives/edgar/data/(\d+)/\d{18}/[^/]+', url.path)
    if url.hostname in {'www.sec.gov', 'sec.gov'} and match:
        return str(int(match[1]))
    field = source.get('identity_metadata', {}).get('EntityCentralIndexKey', {})
    value = field.get('value') if isinstance(field, dict) else None
    return str(int(value)) if isinstance(value, (str, int)) and str(value).isdigit() else None


def validate(data, year):
    if not isinstance(data, dict) or data.get('schema_version') != 'api-metrics-1.0':
        raise ValueError(f'FY{year}: expected a single-year api-metrics-1.0 export, not HTML or a merged file')
    if not isinstance(data.get('company'), str) or not data['company'].strip():
        raise ValueError(f'FY{year}: missing company identity')
    if not isinstance(data.get('taxonomy_year'), int):
        raise ValueError(f'FY{year}: missing taxonomy version')
    if not isinstance(data.get('verification', {}).get('source'), dict):
        raise ValueError(f'FY{year}: missing source verification')
    if data.get('fiscal_year', year) != year:
        raise ValueError(f'FY{year}: input fiscal_year disagrees with its filename')
    if (not isinstance(data.get('requested_items'), list) or not data['requested_items']
            or not set(data['requested_items']) <= {'1', '1A', '7', '8'}):
        raise ValueError(f'FY{year}: invalid requested Items')
    if not isinstance(data.get('metrics'), list) or not data['metrics']:
        raise ValueError(f'FY{year}: input has no metrics')
    concepts = set()
    for metric in data['metrics']:
        concept = metric.get('concept')
        if (not isinstance(concept, str) or not re.fullmatch(r'us-gaap:[A-Za-z_][\w.\-]*', concept)
                or metric.get('query_name') != concept.split(':', 1)[1] or concept in concepts):
            raise ValueError(f'FY{year}: invalid or duplicate concept: {concept}')
        concepts.add(concept)
        if not isinstance(metric.get('value'), list) or not metric['value']:
            raise ValueError(f'FY{year}: {concept} has no value records')
        for value in metric['value']:
            if (not isinstance(value, dict) or not {'value', 'unit', 'period', 'dimensions', 'status', 'items', 'source_labels'} <= value.keys()
                    or not isinstance(value['dimensions'], list) or not isinstance(value['period'], dict)
                    or not isinstance(value['items'], list) or not value['items']
                    or not set(value['items']) <= set(data['requested_items'])
                    or not isinstance(value['source_labels'], list) or 'source_filing_year' in value):
                raise ValueError(f'FY{year}: {concept} has an invalid value record')
            try:
                amount_key(value)
            except ValueError as exc:
                raise ValueError(f'FY{year}: {concept}: {exc}') from None
    if len(concepts) != data.get('counts', {}).get('verified_concepts'):
        raise ValueError(f'FY{year}: metric count does not match the input')


def merge_documents(inputs):
    """Pure merge of (filing_year, path, original_bytes); inputs remain intact."""
    if not inputs or len({year for year, _, _ in inputs}) != len(inputs):
        raise ValueError('Provide one input per distinct fiscal year')
    grouped, filings = defaultdict(list), []
    companies, ciks, item_sets = set(), set(), set()
    for year, path, raw in sorted(inputs):
        data = json.loads(raw)
        validate(data, year)
        companies.add(' '.join(data['company'].casefold().split()))
        cik = issuer_cik(data)
        ciks.add(cik)
        item_sets.add(tuple(sorted(data['requested_items'])))
        filings.append({'filing_year': year, 'metrics_file': str(path),
                        'metrics_file_sha256': hashlib.sha256(raw).hexdigest(),
                        **deepcopy({k: v for k, v in data.items() if k != 'metrics'})})
        for metric in data['metrics']:
            grouped[metric['concept']].append((year, data['taxonomy_year'], metric))
    if len(ciks - {None}) > 1 or (None in ciks and len(companies) != 1):
        raise ValueError('Cannot merge different issuers; company names/SEC CIK evidence disagree')
    if len(item_sets) != 1:
        raise ValueError('Requested Items differ between yearly files; use exports with the same Item selection')

    metrics = []
    input_count = resolved_contexts = latest_multiple_contexts = 0
    for concept, versions in sorted(grouped.items()):
        latest_year, _, latest = versions[-1]
        metric = deepcopy({k: v for k, v in latest.items() if k != 'value'})
        metric['metadata_source_filing_year'] = latest_year
        metric['source_filing_years'] = [year for year, _, _ in versions]
        # Retain the exact per-filing metadata, including definitions and
        # company-wide years, so a newer taxonomy never erases an older one.
        metric['metadata_by_filing'] = [
            {'source_filing_year': year, 'taxonomy_year': tax_year,
             'metadata': deepcopy({k: v for k, v in original.items() if k != 'value'})}
            for year, tax_year, original in versions]
        metric['value'], resolved, latest_multiple = select_latest_values(versions)
        input_count += sum(len(original['value']) for _, _, original in versions)
        resolved_contexts += resolved
        latest_multiple_contexts += latest_multiple
        metric['scope'] = 'company_wide' if any(not v['dimensions'] for v in metric['value']) else 'dimension_only'
        for field in ('company_wide_annual_or_year_end_years', 'units', 'api_groups', 'items'):
            metric[field] = sorted({value for _, _, original in versions for value in original[field]})
        metrics.append(metric)
    count = sum(len(metric['value']) for metric in metrics)
    return {
        'schema_version': 'api-metrics-merged-1.0', 'merger_version': VERSION,
        'company': filings[-1]['company'], 'cik': next(iter(ciks - {None}), None),
        'filing_years': [entry['filing_year'] for entry in filings],
        'taxonomy_years': sorted({entry['taxonomy_year'] for entry in filings}),
        'requested_items': filings[-1]['requested_items'],
        'counts': {'verified_concepts': len(metrics),
                   'company_wide': sum(m['scope'] == 'company_wide' for m in metrics),
                   'dimension_only': sum(m['scope'] == 'dimension_only' for m in metrics),
                   'value_records': count},
        'notes': [
            'Metrics are grouped by exact concept string. Different concept names are not mapped or combined.',
            'For the same concept, exact period, unit and dimensions (ignoring dimension order), the latest source filing year containing that context controls the amount. Older records with different amounts or nil status are removed; identical repeated amounts remain separate.',
            'Amounts are compared as exact decimal numbers, without rounding by decimals/precision. Nil is distinct from zero. If the latest filing contains several amounts for one context, all are retained and the context is counted for review; no amount is invented or summed.',
            'Every retained value is unchanged apart from source_filing_year, including its own Items, accuracy and source labels. The yearly source files remain unchanged; removed values are not stored in this merged file.',
            'Latest-filing selection is a recency policy, not proof that the newer source tagging is correct.',
            'Filing years are selected from the input filenames. They are not taxonomy years or value-period years. Comparative periods before the selected filing years are retained.',
            'Display labels, definitions and period_type use the latest input filing containing the concept. metadata_by_filing preserves every original metric metadata version.',
            'A value source_filing_year identifies its filings[] provenance and metadata_by_filing entry. Fact IDs, document IDs and locators are local to that filing.',
            'Company-wide means the concept has a value without explicit dimensions; it does not imply a Total row.',
            'Source verification is preserved from the yearly exports. This merge does not re-extract or reverify the source filings.',
        ],
        'filings': filings, 'metrics': metrics,
        'verification': {'input_files': len(filings),
                         'input_metric_records': sum(len(versions) for versions in grouped.values()),
                         'input_value_records': input_count, 'output_value_records': count,
                         'values_removed': input_count - count, 'merge_key': 'concept',
                         'conflict_policy': 'latest_source_filing_year_per_context',
                         'conflict_context_key': ['concept', 'period', 'unit', 'dimensions'],
                         'conflicting_contexts_resolved': resolved_contexts,
                         'latest_filing_contexts_with_multiple_amounts': latest_multiple_contexts,
                         'issuer_check': 'sec_cik' if None not in ciks else 'company_name_and_available_sec_cik'},
    }


def merge_company(company, years=DEFAULT_YEARS, input_dir=None, output=None):
    years = sorted(set(years))
    if not years or any(not isinstance(y, int) or not 1900 <= y <= 2200 for y in years):
        raise ValueError('Provide valid four-digit fiscal years')
    label = slug(company)
    folder = Path(input_dir or Path('tests/for_table_development') / label).expanduser()
    year_label = (f'{years[0]}_{years[-1]}' if len(years) > 1 and years == list(range(years[0], years[-1] + 1))
                  else '_'.join(map(str, years)))
    target = Path(output or folder / f'{label}_{year_label}_merged_api_metrics_with_values.json').expanduser()
    if target.exists() or target.is_symlink():
        raise ValueError(f'Merged output already exists: {target}. Use --output for a new file.')
    inputs = []
    for year in years:
        path = folder / f'{label}_{year}_api_metrics_with_values.json'
        inputs.append((year, path, path.read_bytes()))
    result = merge_documents(inputs)
    # Escape source Unicode to avoid editor ambiguity warnings while preserving
    # the exact labels and metadata after JSON decoding.
    serialized = json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + '\n'
    for _, path, raw in inputs:
        if path.read_bytes() != raw:
            raise ValueError(f'Input changed during merge: {path}')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(serialized)
    print(f'Wrote {target}')
    print(f'{len(result["metrics"])} unique concepts; {result["counts"]["value_records"]} value records; '
          f'filing years {", ".join(map(str, years))}.')
    print(f'{result["verification"]["values_removed"]} older conflicting values removed; '
          f'{result["verification"]["latest_filing_contexts_with_multiple_amounts"]} contexts '
          'with multiple amounts in their latest filing retained for review.')
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--company', required=True, help='Existing company folder/filename label, e.g. Intel or INTC')
    parser.add_argument('--years', nargs='+', type=int, default=DEFAULT_YEARS)
    parser.add_argument('--input-dir', type=Path, help='Override the company folder containing the yearly exports')
    parser.add_argument('--output', type=Path, help='Optional new merged JSON path; existing files are preserved')
    args = parser.parse_args(argv)
    try:
        merge_company(args.company, args.years, args.input_dir, args.output)
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
