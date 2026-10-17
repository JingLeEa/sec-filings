#!/usr/bin/env python3
"""Add all verified values to an existing Item 8 metric catalogue.

python3 export_item8_metric_values.py \
  data/table_output/micron_2025_item_8_api_check/available_item8_metrics.json

Each metrics[] entry gains a value[] list. All existing fields are preserved.
Values include their unit, period, dimensions, accuracy and nil status, without
table associations. No annual-period filter or component reinterpretation is
applied. Only facts verified by the existing Item 8 membership check qualify.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys

from lxml import etree

from sec_disclosure.table_extraction import check_item8_api as membership
from sec_disclosure.table_extraction import extract_item8_xbrl_api as api
from sec_disclosure.table_extraction.query_item8_metrics import Filing


def resolve_pointer(document, pointer):
    """Resolve a JSON pointer without treating numeric object keys as indices."""
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('Expected an absolute JSON pointer')
    result = document
    for part in pointer.split('/')[1:]:
        part = part.replace('~1', '/').replace('~0', '~')
        result = result[int(part)] if isinstance(result, list) else result[part]
    return result


def value_record(fact, raw):
    """Format an already verified API fact without rescaling its amount."""
    normalized = api.normalise_fact(membership.matching_record(raw), fact['source']['api_pointer'])
    if normalized['status'] not in {'reported', 'nil'} or normalized['value'] != fact['value']:
        raise ValueError('Loaded fact no longer matches its original API record')
    record = {
        'value': raw.get('value') if fact['status'] != 'nil' else None,
        'unit': fact['unit'], 'period': deepcopy(fact['period']),
        'dimensions': deepcopy(normalized['dimensions']), 'status': fact['status'],
    }
    for key in ('decimals', 'precision'):
        if key in raw:
            record[key] = deepcopy(raw[key])
    return record


def value_key(fact, raw):
    """Merge repeats while retaining conflicts, contexts and accuracy variants."""
    return api.digest([fact['value'], fact['unit_signature'], fact['period'],
                       fact['dimensions'], fact['status'],
                       {k: raw[k] for k in ('decimals', 'precision') if k in raw}])


def value_sort_key(record):
    return (record['period'].get('instant', record['period'].get('endDate', '')),
            record['period'].get('startDate', ''), api.digest(record['dimensions']),
            record['unit'], api.digest(record))


def add_values(catalogue, filing):
    """Pure transformation: retain every metric, including empty definitions.

    An empty value list means no verified numeric record for that exact concept.
    Dimensional component balances remain under their reported concept; they
    are not moved to a different metric, inferred, summed, or annualised.
    """
    if not isinstance(catalogue, dict) or not isinstance(catalogue.get('metrics'), list):
        raise ValueError('Expected a metric catalogue with a metrics[] array')
    if catalogue.get('company') != filing.company or catalogue.get('taxonomy_year') != filing.taxonomy_year:
        raise ValueError('Catalogue company/taxonomy year differs from the source filing')
    names = set()
    for metric in catalogue['metrics']:
        if not isinstance(metric, dict):
            raise ValueError('Each metrics[] entry must be an object')
        name = metric.get('query_name')
        if not isinstance(name, str) or not name or metric.get('concept') != 'us-gaap:' + name:
            raise ValueError('Metric concept must match its US-GAAP query_name')
        if name in names:
            raise ValueError(f'Duplicate metric concept: {name}')
        if 'value' in metric:
            raise ValueError('Input already contains value fields; use the original catalogue')
        names.add(name)

    values = defaultdict(dict)
    for fact in filing.facts:
        if fact['concept'] not in names:
            continue
        raw = resolve_pointer(filing.named, fact['source']['json_pointer'])
        values[fact['concept']].setdefault(value_key(fact, raw), value_record(fact, raw))

    result = deepcopy(catalogue)
    for metric in result['metrics']:
        metric['value'] = sorted(values[metric['query_name']].values(), key=value_sort_key)
    # Contract: the only changes to the catalogue are metrics[].value fields.
    original = deepcopy(result)
    for metric in original['metrics']:
        del metric['value']
    if original != catalogue:
        raise ValueError('Catalogue preservation check failed')
    return result


def source_path(catalogue, catalogue_path, override=None):
    if not isinstance(catalogue, dict):
        raise ValueError('Expected a metric catalogue object')
    source = catalogue.get('source')
    if not isinstance(source, str) or not source:
        raise ValueError('Catalogue has no source named-JSON path')
    original = Path(source)
    if not original.exists():
        original = catalogue_path.parent / source
    if override is not None:
        supplied = Path(override)
        # Relocations are allowed; a different filing at the old path is not.
        if original.exists() and supplied.resolve() != original.resolve():
            if api.read_json(original.read_bytes()) != api.read_json(supplied.read_bytes()):
                raise ValueError('--named-json differs from the catalogue source')
        return supplied
    if not original.exists():
        sibling = catalogue_path.parent / Path(source).name
        if sibling.exists():
            return sibling
        raise ValueError('Named JSON source is missing; supply --named-json for a relocated file')
    return original


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('catalogue', type=Path, help='Existing available_item8_metrics.json')
    parser.add_argument('--named-json', type=Path, help='Override the named API source path')
    parser.add_argument('--membership', type=Path, help='Override its matching Item 8 membership report')
    parser.add_argument('--filing', type=Path, help='Override the matching original filing path')
    parser.add_argument('--output', type=Path, help='Defaults to item_8_metrics_with_values.json beside the catalogue')
    args = parser.parse_args(argv)
    try:
        # Regular JSON loading preserves the catalogue's existing field types.
        catalogue = json.loads(args.catalogue.read_bytes())
        named_path = source_path(catalogue, args.catalogue, args.named_json)
        filing = Filing(named_path, args.membership, args.filing)
        output = args.output or args.catalogue.with_name('item_8_metrics_with_values.json')
        if output.resolve() in filing.protected | {args.catalogue.resolve()} or output.exists():
            raise ValueError('--output must be a new file, separate from the catalogue and source files')
        result = add_values(catalogue, filing)
        api.write_json(result, output)
        metrics = result['metrics']
        print(f'Wrote {output}')
        print(json.dumps({
            'metrics': len(metrics), 'metrics_with_values': sum(bool(m['value']) for m in metrics),
            'unique_value_records': sum(len(m['value']) for m in metrics),
            'dimension_specific_records': sum(bool(r['dimensions']) for m in metrics for r in m['value']),
        }, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError, etree.XMLSyntaxError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
