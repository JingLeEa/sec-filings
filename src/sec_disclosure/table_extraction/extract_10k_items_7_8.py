#!/usr/bin/env python3
r"""Extract Item 7 with HTML and Item 8 independently from sec-api.io XBRL JSON.

Outputs: item_7_html.json, item_8_xbrl.json, and a reusable sec_api_xbrl.json cache.
Item 8 uses API statement/note groups, concepts and dimensional facts. It does
not use HTML cells, pages, Inline XBRL fact IDs, or local value verification.

    python3 extract_10k_items_7_8.py --ticker INTC --company Intel --year 2023 \
        --user-agent "Your name your-email@example.com"

Live requests require SEC_API_KEY. Use --xbrl-json for an existing response.
For Item 8 alone without any filing HTML, use extract_item8_xbrl_api.py.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from sec_disclosure.comparison import compare_html_tables as html
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix
from sec_disclosure.table_extraction import extract_item8_xbrl_api as api

VERSION = '2.0.0'


def html_value(text):
    """Displayed values only: no inherited units, periods or XBRL conversions."""
    kind, value = html.numeric(text)
    if kind == 'text':
        suffix = re.fullmatch(r'(.+?)\s*([KMB])', text, re.I)
        if suffix:
            kind, value = html.numeric(suffix[1])
            if kind == 'number':
                return {'kind': kind, 'value': value, 'unit': None,
                        'display_scale': {'K': 'thousands', 'M': 'millions', 'B': 'billions'}[suffix[2].upper()]}
    return {'kind': kind, 'value': value if kind == 'number' else None,
            'unit': '%' if '%' in text and kind == 'number' else None, 'display_scale': None}


def extract_item7(data, company, year, source='', report_loader=None, report_source=None):
    base = ix.extract_tables(data, company, year, source, items=('7',),
                             report_loader=report_loader, report_source=report_source)
    tables = deepcopy(base['tables'])
    for table in tables:
        table['source_table_id'] = table['table_id']
        table['item'] = '7'
        table['position_id'] = f'{html.slug(company)}*{year}7*{table["page"] or "unknown"}*{table["page_table_index"]}'
        table['extraction_method'] = 'html'
        for key in ('fact_ids', 'enclosing_xbrl_concepts'):
            table.pop(key, None)
        for key in ('fact_errors', 'untagged_number_like_cells'):
            table['diagnostics'].pop(key, None)
        for cell in table['cells']:
            cell.pop('fact_ids', None)
            cell.pop('tagging', None)
            cell['html_value'] = html_value(cell['display_text'])
        table['status'] = 'needs_review' if table['diagnostics']['layout_error'] else 'extracted'
    used = {t['document_id'] for t in tables}
    documents = {}
    for doc_id, original in base['documents'].items():
        scope = {key: [e for e in entries if e.get('item') == '7'] for key, entries in original['scope'].items()}
        if doc_id in used or any(scope.values()):
            documents[doc_id] = {'source': original['source'], 'sha256': original['sha256'], 'scope': scope}
    return {'extractor_version': VERSION, 'company': company, 'year': year,
            'table_filter': base['table_filter'], 'schema_version': 'html-item-tables-1.0',
            'item': '7', 'requested_items': ['7'], 'extraction_method': 'html',
            'documents': documents, 'tables': tables,
            'value_convention': 'html_value.value is a displayed, unscaled decimal string. No table-wide unit or scale is assumed.',
            'summary': {'tables': len(tables),
                        'layout_errors': sum(bool(t['diagnostics']['layout_error']) for t in tables),
                        'tables_without_verified_page': sum(t['page'] is None for t in tables)}}


def local_filing_url(source, data):
    path = Path(source)
    sidecar = path.with_name(path.stem + '-source.json')
    if sidecar.exists():
        metadata = api.read_json(sidecar.read_bytes())
        if metadata.get('sha256') != hashlib.sha256(data).hexdigest():
            raise ValueError('Cached filing does not match its source metadata hash.')
        candidate = metadata.get('original_sec_url') or metadata.get('sec_url')
        if candidate:
            return api.sec_filing_url(candidate)
    return None


def write_item_results(item7, item8, output, strict=False):
    if strict:
        if item7['summary']['layout_errors']:
            raise ValueError('Strict extraction failed: Item 7 table layout errors; outputs were not replaced.')
        api.check_strict(item8)
    paths = {'7': Path(output) / 'item_7_html.json', '8': Path(output) / 'item_8_xbrl.json'}
    api.write_json(item7, paths['7'])
    api.write_json(item8, paths['8'])
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', nargs='?', help='Original filing XHTML for Item 7, or SEC URL')
    issuer = parser.add_mutually_exclusive_group()
    issuer.add_argument('--ticker')
    issuer.add_argument('--cik')
    parser.add_argument('--company')
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--accession', help='Original 10-K accession for ticker/CIK mode')
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''))
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--report-source')
    parser.add_argument('--filing-url', help='SEC filing URL for local input without a verified source sidecar')
    parser.add_argument('--xbrl-json', type=Path, help='Saved sec-api.io response; skips the live API call')
    parser.add_argument('--item8-groups', nargs='+', help='Optional exact API financial group names')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--strict', action='store_true', help='Refuse both outputs on Item 7 layout errors or invalid/incomplete Item 8 API facts')
    args = parser.parse_args(argv)
    try:
        if not 1900 <= args.year <= 2200:
            raise ValueError('--year must be a four-digit fiscal year')
        if bool(args.source) == bool(args.ticker or args.cik):
            raise ValueError('Provide either a source or --ticker/--cik.')
        if args.accession and args.source:
            raise ValueError('--accession requires --ticker/--cik.')
        if not args.xbrl_json and not os.environ.get('SEC_API_KEY', '').strip():
            raise ValueError('Set SEC_API_KEY to your sec-api.io key, or provide --xbrl-json.')
        client = None
        def load(source):
            nonlocal client
            if urlparse(source).scheme in {'http', 'https'}:
                if client is None:
                    client = html.SecClient(args.user_agent, args.cache_dir)
                return client.get(source)
            return Path(source).expanduser().read_bytes()
        selection = None
        if args.source:
            source = args.source
            company = args.company or Path(urlparse(source).path).stem.split('-')[0]
        else:
            client = html.SecClient(args.user_agent, args.cache_dir)
            print(f'Looking up the original FY{args.year} 10-K...', flush=True)
            issuer_data, filings = html.discover_sec_filings(client, [args.year], ticker=args.ticker or '',
                cik=args.cik or '', accessions={args.year: args.accession} if args.accession else None)
            selection = {'issuer': issuer_data, 'filing': filings[args.year]}
            source = filings[args.year]['url']
            company = args.company or args.ticker or issuer_data['name']
        data = load(source)
        discovered = api.sec_filing_url(source) if urlparse(source).scheme else local_filing_url(source, data)
        if args.filing_url and discovered and args.filing_url != discovered:
            raise ValueError('--filing-url conflicts with the selected/cached filing.')
        parameters = api.request_parameters(filing_url=args.filing_url or discovered or '')
        source_hash = hashlib.sha256(data).hexdigest()
        print('Extracting Item 7 HTML tables...', flush=True)
        item7 = extract_item7(data, company, args.year, source, load, args.report_source)
        if args.xbrl_json:
            payload, parameters = api.load_response(args.xbrl_json, parameters, source_hash)
        else:
            print('Requesting XBRL financial groups from sec-api.io...', flush=True)
            payload = api.fetch_xbrl_json(parameters, os.environ['SEC_API_KEY'])
        item8 = api.extract_item8(payload, company, args.year, parameters, args.item8_groups)
        item8['source']['response_mode'] = 'saved' if args.xbrl_json else 'live'
        if selection:
            item7['sec_selection'] = selection
        output = args.output_dir or Path('data/table_output') / f'{html.slug(company)}_{args.year}_items_7_8_hybrid_tables'
        paths = write_item_results(item7, item8, output, args.strict)
        api.write_json({'schema_version': 'sec-api-cache-2.0', 'request': parameters,
                        'source_sha256': source_hash, 'response': payload}, output / 'sec_api_xbrl.json')
        print(f'Wrote {paths["7"]} ({item7["summary"]["tables"]} HTML tables)')
        print(f'Wrote {paths["8"]} ({item8["summary"]["tables"]} XBRL API groups)')
        print(json.dumps(item8['summary']))
        return 0
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
