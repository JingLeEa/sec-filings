#!/usr/bin/env python3
"""Build metric JSON directly from an API response after checking selected Items.

python3 export_api_metrics.py \
  data/table_output/intel_2023_item_8_api_check/sec_api_xbrl.json \
  --filing data/source_cache/intc-20231230.htm --items 1 1A 7 8 --offline

Default Items: 1, 1A, 7, 8. No table-name mapping or named JSON is required.
The original filing supplies membership evidence, row/column labels and unit/namespace metadata;
all exported amounts come from the original API records.
"""
from __future__ import annotations

import argparse
import os
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from pathlib import Path
import re
import sys
from urllib.error import URLError
from zipfile import BadZipFile

from lxml import etree

from sec_disclosure.table_extraction.api_fact_labels import FactLabelIndex
from sec_disclosure.table_extraction import check_item8_api as membership
from sec_disclosure.table_extraction import export_item8_metric_values as values
from sec_disclosure.table_extraction import extract_item8_xbrl_api as api
from sec_disclosure.table_extraction.query_item8_metrics import display_unit, expand, fiscal_period_matches, units
from sec_disclosure.table_extraction import xbrl_metric_dictionary as taxonomy
from sec_disclosure.table_extraction import api_filing_documents as filing_docs
from sec_disclosure.table_extraction import api_filing_metadata as metadata

VERSION = '1.11.0'


def cover_value(payload, key):
    value, _ = api.identity_field(payload, key)
    if value is None:
        raise ValueError(f'API metadata has no unique {key}')
    return value


def taxonomy_year(root):
    versions = {int(match[1]) for uri in root.nsmap.values()
                if (match := re.fullmatch(r'http://fasb.org/us-gaap/(\d{4})', uri or ''))}
    if len(versions) != 1:
        raise ValueError('Expected one US-GAAP taxonomy version in the original filing')
    return next(iter(versions))


def verified_items(entry, items):
    if not entry['value_checked']:
        return []
    if items == ('8',):
        return ['8'] if entry['status'] == 'item_8' else []
    return [item for item in items if entry['item_membership'].get(item) == 'in_item']


def build_metrics(payload, data, report, dictionary, api_file, report_file, prefix='/response', *, documents=None):
    """Keep only individually verified numeric facts in the selected Items.

    Every checked API pointer is accounted for in the export audit. Table/group
    names never determine scope, and no unverified raw value is exported.
    """
    if report['schema_version'] == 'api-items-membership-1.0':
        items = membership.normalize_items(report['requested_items'])
    elif report['schema_version'] == 'api-item-membership-1.1' and report.get('item') == '8':
        items = ('8',)
    else:
        raise ValueError('Unsupported membership report')
    if (report['source']['response_sha256'] != api.digest(payload)
            or report['source']['filing_sha256'] != membership.sha(data)):
        raise ValueError('Membership report belongs to different API/filing content')
    root = taxonomy.xml(data)
    tax_year = taxonomy_year(root)
    namespace = f'http://fasb.org/us-gaap/{tax_year}'
    if dictionary['year'] != tax_year or dictionary['namespace'] != namespace:
        raise ValueError('Dictionary taxonomy differs from the original filing')
    identity = metadata.resolve(payload, data, report['source']['request'], report['year'])
    company = identity['values']['EntityRegistrantName']
    year = int(identity['values']['DocumentFiscalYearFocus'])
    year_end = date.fromisoformat(identity['values']['DocumentPeriodEndDate'])
    if year != report['year']:
        raise ValueError('Membership report fiscal year differs from the API response')
    roots, unit_maps = {'primary': root}, {'primary': units(root)}
    if documents is not None:
        if report['source'].get('documents') is not None and report['source']['documents'] != filing_docs.describe(documents):
            raise ValueError('Membership report belongs to different incorporated report content')
        for identifier, entry in documents.items():
            roots[identifier] = entry['doc'].root
            if taxonomy_year(roots[identifier]) != tax_year:
                raise ValueError('Incorporated report uses a different US-GAAP taxonomy')
            unit_maps[identifier] = {key: filing_docs.unit_key(unit) for key, unit in entry['doc'].units.items()}
    elif report['source'].get('documents'):
        raise ValueError('Membership report requires its original incorporated documents')
    checked = {}
    for container in report['groups'] + report.get('standalone_concepts', []):
        for entry in container['entries']:
            if entry['api_pointer'] in checked:
                raise ValueError('Duplicate membership API pointer')
            checked[entry['api_pointer']] = entry
    records = [(group, concept, pointer, raw) for group, concept, pointer, raw in membership.entries(payload, prefix)
               if group not in api.EXCLUDED_GROUPS]
    if set(checked) != {pointer for _, _, pointer, _ in records}:
        raise ValueError('Membership report does not cover the same API records')
    label_indexes = ({identifier: FactLabelIndex(entry['doc'].data, entry['doc'].source)
                      for identifier, entry in documents.items()} if documents is not None else
                     {'primary': FactLabelIndex(data, report['source']['filing'])})

    facts = defaultdict(list)
    merged_values = defaultdict(dict)
    included, excluded = [], []
    for group, concept, pointer, raw in records:
        entry = checked[pointer]
        selected = verified_items(entry, items)
        reason = None
        if not selected:
            reason = 'not_verified_numeric_fact_in_selected_items'
        elif not isinstance(raw, dict) or 'unitRef' not in raw or 'period' not in raw:
            reason = 'not_a_structured_numeric_fact'
        else:
            if items == ('8',):
                names = {e['concept'] for e in entry['evidence'] if e['status'] == 'item_8'}
            else:
                names = {e['concept'] for e in entry['evidence']
                         if any(e['item_membership'].get(item) == 'in_item' for item in selected)}
            # Use the verified expanded QName, not a guessed bare API name.
            if len(names) != 1 or not next(iter(names)).startswith('{' + namespace + '}'):
                reason = 'custom_or_ambiguous_concept_namespace'
            else:
                name = next(iter(names)).split('}', 1)[1]
                definition = dictionary['concepts'].get(name)
                if not definition or not taxonomy.metric_concept(definition):
                    reason = 'not_an_official_numeric_metric'
        if reason:
            excluded.append({'api_pointer': pointer, 'concept': concept, 'reason': reason})
            continue
        try:
            normalized = api.normalise_fact(membership.matching_record(raw), pointer)
            if normalized['status'] not in {'reported', 'nil'} or normalized['value_type'] not in {'number', 'nil'}:
                raise ValueError('Not a supported numeric fact')
            relevant = [e for e in entry['evidence'] if
                        (e['status'] == 'item_8' if items == ('8',) else
                         any(e['item_membership'].get(item) == 'in_item' for item in selected))]
            contexts = set()
            for evidence in relevant:
                identifier = evidence.get('document_id', 'primary')
                dimensions = tuple(sorted((expand(d['axis'], roots[identifier]), expand(d['member'], roots[identifier]))
                                          for d in normalized['dimensions'] or []))
                unit = unit_maps[identifier].get(normalized['unit_ref'])
                if unit is None:
                    raise ValueError('Unresolved unit reference')
                contexts.add((dimensions, unit))
            if len(contexts) != 1:
                raise ValueError('Ambiguous unit or dimensions across source documents')
            dimensions, unit = contexts.pop()
        except ValueError as exc:
            excluded.append({'api_pointer': pointer, 'concept': concept,
                             'reason': 'unsupported_numeric_context', 'detail': str(exc)})
            continue
        fact = {
            'concept': name, 'value': normalized['value'],
            'period': {k: v for k, v in normalized['period'].items() if k != 'type'},
            'period_type': normalized['period']['type'], 'dimensions': dimensions,
            'unit': display_unit(unit), 'unit_signature': unit, 'status': normalized['status'],
            'source': {'api_pointer': pointer, 'api_group': group},
        }
        key = values.value_key(fact, raw)
        if key not in merged_values[name]:
            merged_values[name][key] = {**values.value_record(fact, raw), 'items': [], 'source_labels': []}
        merged = merged_values[name][key]
        merged['items'] = [item for item in items if item in set(merged['items']) | set(selected)]
        for identifier, label_index in label_indexes.items():
            scoped = {**entry, 'evidence': [e for e in entry['evidence'] if e.get('document_id', 'primary') == identifier]}
            for source_label in label_index.for_entry(scoped, selected):
                if len(label_indexes) > 1:
                    source_label.update(document_id=identifier, source=documents[identifier]['doc'].source)
                if source_label not in merged['source_labels']:
                    merged['source_labels'].append(source_label)
        facts[name].append(fact)
        included.append({'api_pointer': pointer, 'concept': 'us-gaap:' + name,
                         'items': selected, 'value_key': key})

    metrics = []
    for name, concept_facts in sorted(facts.items()):
        definition = dictionary['concepts'][name]
        company_facts = [f for f in concept_facts if not f['dimensions']]
        eligible = [f for f in company_facts if f['period_type'] == definition['period_type']]
        years = sorted({y for f in eligible for y in range(year - 10, year + 1)
                        if fiscal_period_matches(f['period'], y, year, year_end)})
        metrics.append({
            'concept': definition['concept'], 'query_name': name, 'label': definition['label'],
            'definition': definition['definition'], 'period_type': definition['period_type'],
            'scope': 'company_wide' if company_facts else 'dimension_only',
            'company_wide_annual_or_year_end_years': years,
            'units': sorted({f['unit'] for f in company_facts}),
            'api_groups': sorted({f['source']['api_group'] for f in concept_facts}),
            'items': [item for item in items if any(item in r['items'] for r in merged_values[name].values())],
        })
    catalogue = {
        'schema_version': 'api-metrics-1.0', 'exporter_version': VERSION,
        'source': str(api_file), 'membership_report': str(report_file) if report_file is not None else None,
        'requested_items': list(items),
        'company': company, 'taxonomy_year': tax_year,
        'counts': {'verified_concepts': len(metrics),
                   'company_wide': sum(m['scope'] == 'company_wide' for m in metrics),
                   'dimension_only': sum(m['scope'] == 'dimension_only' for m in metrics)},
        'notes': [
            'Only numeric API records with exact location evidence in the requested Items and an official US-GAAP metric are included.',
            'Original API amounts are already in their XBRL unit; decimals describes accuracy, not scaling.',
            'All verified periods and explicit dimensions are retained in value[]. Company-wide years/units describe only facts without explicit dimensions.',
            'A value may belong to several requested Items. Identical values/contexts/accuracy are stored once with their Item memberships merged.',
            ('Missing definitions are retained as null. Custom concepts, unresolved facts and other exclusions are listed in metric_export_audit.json.'
             if report_file is not None else
             'Missing definitions are retained as null. Custom concepts, unresolved facts and other exclusions are counted in verification.exclusion_counts. Membership and export checks run in memory; separate reports are not saved.'),
            'No table-name mapping, component reinterpretation, summation or missing-value inference is performed.',
            'source_labels preserves row/column wording at verified filing occurrences. Layout-based associations can be partial/unresolved; empty dimensions never imply a Total label.',
        ],
        'metrics': metrics,
    }
    output = deepcopy(catalogue)
    for metric in output['metrics']:
        # Item annotations must not reorder otherwise unchanged value records.
        metric['value'] = sorted(merged_values[metric['query_name']].values(), key=lambda record:
                                 values.value_sort_key({k: v for k, v in record.items() if k not in {'items', 'source_labels'}}))
    value_count = sum(len(m['value']) for m in output['metrics'])
    audit = {
        'schema_version': 'api-metric-export-audit-1.0', 'requested_items': list(items),
        'source': deepcopy(report['source']),
        'taxonomy_source': {key: dictionary[key] for key in ('year', 'namespace', 'package_url', 'package_sha256', 'sources')},
        'checked_api_entries': len(records), 'included_api_entries': len(included),
        'excluded_api_entries': len(excluded), 'unique_values': value_count,
        'repeated_api_entries_merged': len(included) - value_count,
        'source_label_status': dict(Counter(label['status'] for m in output['metrics'] for v in m['value']
                                           for label in v['source_labels'])),
        'exclusion_counts': dict(Counter(e['reason'] for e in excluded)),
        'included_entries': included, 'excluded_entries': excluded,
    }
    if identity['evidence']:
        audit['source']['identity_metadata'] = identity['evidence']
    return catalogue, output, audit


def main(argv=None, *, metrics_filename='metrics_with_values.json'):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('xbrl_json', type=Path, help='Original API response/cache')
    parser.add_argument('--filing', type=Path, required=True, help='Matching original Inline XBRL filing')
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument('--filing-url', help='Provenance for raw API JSON')
    identity.add_argument('--accession', help='Provenance for raw API JSON')
    parser.add_argument('--items', nargs='+', choices=membership.SUPPORTED_ITEMS, type=str.upper,
                        default=list(membership.SUPPORTED_ITEMS))
    parser.add_argument('--output-dir', type=Path, help='Default: items_<selection>_metrics beside the input API file')
    parser.add_argument('--taxonomy-cache', type=Path, default=Path('data/taxonomy_cache'))
    parser.add_argument('--offline', action='store_true', help='Use the cached official taxonomy without downloading')
    parser.add_argument('--metrics-only', action='store_true', help='Write only metrics_with_values.json; run all checks in memory')
    parser.add_argument('--sec-cache', type=Path, default=Path('data/sec_cache'))
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''), help='SEC contact for incorporated report downloads')
    args = parser.parse_args(argv)
    try:
        if Path(metrics_filename).name != metrics_filename or not metrics_filename.endswith('.json'):
            raise ValueError('Metrics filename must be a JSON filename inside the output folder')
        items = membership.normalize_items(args.items)
        output_dir = args.output_dir or args.xbrl_json.parent / ('items_' + '_'.join(i.lower() for i in items) + '_metrics')
        report_name = ('item_8_membership_check.json' if items == ('8',) else
                       'items_' + '_'.join(i.lower() for i in items) + '_membership_check.json')
        paths = [output_dir / name for name in
                 (report_name, 'available_metrics.json', metrics_filename, 'metric_export_audit.json')]
        if len(set(paths)) != len(paths):
            raise ValueError('Metrics filename conflicts with another export filename')
        protected = {args.xbrl_json.resolve(), args.filing.resolve(),
                     args.filing.with_name(args.filing.stem + '-source.json').resolve()}
        write_paths = [paths[2]] if args.metrics_only else paths
        if any(p.exists() or p.resolve() in protected for p in write_paths):
            raise ValueError('Output files already exist or alias an input. Choose a new --output-dir.')
        original, data = args.xbrl_json.read_bytes(), args.filing.read_bytes()
        saved = api.read_json(original)
        parameters = api.request_parameters(args.filing_url, args.accession) if args.filing_url or args.accession else None
        payload, parameters = api.load_response(args.xbrl_json, parameters)
        binding = membership.bind_filing(saved, args.filing, data, parameters)
        cached = isinstance(saved, dict) and saved.get('schema_version') in {'sec-api-cache-1.0', 'sec-api-cache-2.0'}
        prefix = '/response' if cached else ''
        year = int(metadata.resolve(payload, data, parameters)['values']['DocumentFiscalYearFocus'])
        if not 1900 <= year <= 2200:
            raise ValueError('Use a four-digit fiscal year')
        print(f'Checking original API records against Items {", ".join(items)}...', flush=True)
        loader = filing_docs.ReportLoader(args.sec_cache.expanduser(), args.user_agent, args.offline)
        documents = filing_docs.resolve_documents(data, str(args.filing), parameters, items, loader)
        if any(path.resolve() == Path(entry['doc'].source).resolve() for path in write_paths for entry in documents.values()):
            raise ValueError('Output aliases an incorporated report')
        report = membership.check_response(payload, data, str(args.filing), parameters, year, prefix, items=items, documents=documents)
        report['source'].update(api_file=str(args.xbrl_json), api_file_sha256=membership.sha(original), filing_binding=binding)
        dictionary = taxonomy.load_dictionary(taxonomy_year(taxonomy.xml(data)), args.taxonomy_cache, args.offline)
        report_file = None if args.metrics_only else paths[0]
        catalogue, output, audit = build_metrics(payload, data, report, dictionary, args.xbrl_json, report_file, prefix, documents=documents)
        if not output['metrics']:
            raise ValueError(
                f'No verified numeric US-GAAP metrics in requested Items {", ".join(items)}; '
                f'{audit["checked_api_entries"]} API entries checked, '
                f'{audit["excluded_api_entries"]} excluded. No output written. '
                f'Exclusion counts: {audit["exclusion_counts"]}. '
                'Check section boundaries, fact matching and taxonomy coverage; '
                'the selected Items may also contain no eligible numeric facts.')
        if args.metrics_only:
            # Keep source hashes and exclusion counts in the one deliverable,
            # without dangling links to membership/audit files that do not exist.
            output['verification'] = {key: audit[key] for key in (
                'source', 'taxonomy_source', 'checked_api_entries', 'included_api_entries',
                'excluded_api_entries', 'unique_values', 'repeated_api_entries_merged',
                'source_label_status', 'exclusion_counts')}
        # Finish validation before creating any output files.
        if args.xbrl_json.read_bytes() != original or args.filing.read_bytes() != data:
            raise ValueError('API response or original filing changed during extraction')
        if any(Path(entry['doc'].source).read_bytes() != entry['doc'].data for entry in documents.values()):
            raise ValueError('Incorporated report changed during extraction')
        contents = [output] if args.metrics_only else [report, catalogue, output, audit]
        for content, path in zip(contents, write_paths):
            api.write_json(content, path)
        print(f'Wrote {paths[2]}')
        print(f'{len(output["metrics"])} metrics; {audit["unique_values"]} values; '
              f'{audit["included_api_entries"]} verified API entries included; {audit["excluded_api_entries"]} excluded '
              f'(see {"verification.exclusion_counts" if args.metrics_only else "audit"}).')
        return 0
    except (ValueError, OSError, KeyError, TypeError, etree.XMLSyntaxError, URLError, BadZipFile) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
