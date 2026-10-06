#!/usr/bin/env python3
"""Extract untagged financial HTML table metrics for Items 1, 1A and 7.

python3 extract_html_metrics.py --ticker INTC --company Intel --year 2025
python3 extract_html_metrics.py --api-metrics tests/for_table_development/intel/intel_2025_api_metrics_with_values.json --company Intel --year 2025 --offline

No sec-api.io request or API key is needed. --api-metrics only locates and
hash-verifies the original filing already used by that export; amounts come
from HTML. Existing output files are never overwritten.
Narrative/review table records are omitted; their summary counts are retained.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import api_filing_documents as filing_docs
import compare_html_tables as sec
import html_table_metrics as html_metrics
import html_filing_source as filing_source


def verify_fiscal_year(documents, expected):
    # Retained for callers of the original helper; the CLI verifies all identity
    # fields and the selected SEC issuer, rather than the year alone.
    return filing_source.verify_identity(documents, expected)['fiscal_year']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--filing', type=Path, help='Original Inline XBRL XHTML filing')
    source.add_argument('--api-metrics', type=Path, help='Existing final API metrics JSON; used only for source provenance')
    source.add_argument('--ticker', help='Download original 10-K from SEC; no sec-api.io call')
    source.add_argument('--cik', help='SEC issuer CIK instead of ticker lookup')
    parser.add_argument('--accession', help='Select an explicit original 10-K in live ticker/CIK mode')
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--company', help='Folder/filename label, e.g. Intel')
    parser.add_argument('--items', nargs='+', choices=html_metrics.ITEMS, type=str.upper, default=list(html_metrics.ITEMS))
    parser.add_argument('--filing-url', help='Original SEC URL provenance for --filing')
    parser.add_argument('--output-dir', type=Path, help='Defaults to tests/for_table_development/<company>/')
    parser.add_argument('--sec-cache', type=Path, default=Path('data/sec_cache'))
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''))
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    try:
        if not 1900 <= args.year <= 2200:
            raise ValueError('--year must be a four-digit fiscal year.')
        if args.filing_url and not args.filing:
            raise ValueError('--filing-url requires --filing.')
        if args.accession and not (args.ticker or args.cik):
            raise ValueError('--accession requires --ticker or --cik; saved filings use --filing-url or source provenance.')
        if args.accession:
            filing_source.api.request_parameters(accession=args.accession)
        if args.offline and not (args.filing or args.api_metrics):
            raise ValueError('--offline requires --filing or --api-metrics.')
        label = args.company or args.ticker or args.cik or (args.filing or args.api_metrics).stem
        slug = sec.slug(label)
        output = (args.output_dir or Path('tests/for_table_development') / slug).expanduser()
        target = output / f'{slug}_{args.year}_html_metrics_with_values.json'
        if target.exists() or target.is_symlink():
            raise ValueError(f'Output already exists: {target}. Use a different --output-dir.')
        args.items = list(dict.fromkeys(args.items))
        print('Reading original filing and resolving Items ' + ', '.join(args.items) + '...', flush=True)
        source = filing_source.read_source(args)
        loader = filing_docs.ReportLoader(args.sec_cache.expanduser(), args.user_agent, args.offline)
        documents = filing_source.resolve_source(source, tuple(args.items), loader)
        identity = filing_source.verify_identity(documents, args.year, source.parameters,
                                                 source.provenance.get('sec_selection'))
        print('Classifying tables and extracting untagged HTML values...')
        result = html_metrics.build(documents, label, args.year, tuple(args.items))
        result['verification']['fiscal_year'] = identity['fiscal_year']
        result['verification']['identity'] = identity
        result['verification']['source'] = {**source.provenance, 'request': source.parameters,
                                             'filing': source.source, 'filing_sha256': filing_source.sha(source.data)}
        source.verify_unchanged(documents)
        # Serialize fully before creating a new output file. Exclusive creation
        # prevents overwriting a result or a symlink created during extraction.
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        output.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            stream.write(serialized)
        summary = result['classification_summary']
        print(f'Wrote {target}')
        print(f'Tables: {summary["financial"]} financial retained; '
              f'{summary["narrative"]} narrative and {summary["review"]} review omitted; '
              f'{summary["ignored_image_only_tables"]} image-only tables ignored; '
              f'{summary["exported_values"]} HTML values ({summary["values_needing_review"]} need review).')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
