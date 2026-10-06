#!/usr/bin/env python3
"""Download a 10-K and its sec-api.io response, then export verified API metrics.

    python3 run_api_metrics_pipeline.py --ticker INTC --company Intel --year 2023

Default Items: 1, 1A, 7, 8. Live mode uses SEC_USER_AGENT and SEC_API_KEY.
Use --filing and --xbrl-json together to reuse saved inputs without an API key.
Each run writes only <company>_<year>_api_metrics_with_values.json;
existing exports are not replaced.
Default output folder: tests/for_table_development/<company>/, shared by all years.
Downloads are cached separately under data/sec_cache/; checks run in memory.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import compare_html_tables as sec
import export_api_metrics as export
import extract_item8_xbrl_api as api
import api_filing_metadata as metadata


def check_output(output, filename):
    target = output / filename
    if target.exists() or target.is_symlink():
        raise ValueError(f'Output file already exists: {target}. Choose a new --output-dir; existing results are preserved.')


def read_saved(response, filing, parameters=None):
    """Check original provenance before trusting or copying a cached response."""
    original, data = response.read_bytes(), filing.read_bytes()
    saved = api.read_json(original)
    payload, parameters = api.load_response(response, parameters)
    export.membership.bind_filing(saved, filing, data, parameters)
    if response.read_bytes() != original:
        raise ValueError('Saved API response changed while being read')
    return payload, parameters, data, original


def verify_year(payload, year, parameters, data):
    return metadata.resolve(payload, data, parameters, year)


def live_inputs(args):
    key = os.environ.get('SEC_API_KEY', '').strip()
    cache = args.sec_cache.expanduser()
    client = sec.SecClient(args.user_agent, cache)
    print(f'1/3 Selecting the original FY{args.year} 10-K from SEC submissions...', flush=True)
    _, filings = sec.discover_sec_filings(
        client, [args.year], ticker=args.ticker or '', cik=args.cik or '',
        accessions={args.year: args.accession} if args.accession else None)
    selected = filings[args.year]
    parameters = api.request_parameters(filing_url=selected['url'])
    folder = cache / 'api_metrics' / api.digest(parameters)
    filing, response = folder / 'original_filing.htm', folder / 'sec_api_xbrl.json'
    data = client.get(selected['url'])
    print(f'Selected {selected["accessionNumber"]}; report date {selected["reportDate"]}.', flush=True)
    # A failed downstream run may already have downloaded these inputs. Reuse
    # them only after checking the URL, accession and exact filing bytes.
    if response.exists():
        payload, _, cached_data, _ = read_saved(response, filing, parameters)
        if cached_data != data:
            raise ValueError('Cached source differs from the selected SEC filing. Choose a new --sec-cache.')
        print('2/3 Reusing the source-bound API response from the download cache.', flush=True)
    else:
        if not key:
            raise ValueError('Set SEC_API_KEY to your sec-api.io key, or use --filing with --xbrl-json.')
        if filing.exists() and filing.read_bytes() != data:
            raise ValueError('Cached source differs from the selected SEC filing. Choose a new --sec-cache.')
        print('2/3 Fetching the filing-wide XBRL response from sec-api.io...', flush=True)
        payload = api.fetch_xbrl_json(parameters, key)
        # Cache the original response before identity checks. A rejected
        # response remains inspectable and is revalidated on every retry.
        folder.mkdir(parents=True, exist_ok=True)
        if not filing.exists():
            with filing.open('xb') as stream:
                stream.write(data)
        api.write_json({'schema_version': 'sec-api-cache-2.0', 'request': parameters,
                        'source_sha256': export.membership.sha(data), 'response': payload}, response)
    audit = {'request': parameters, 'source_sha256': export.membership.sha(data),
             'response_sha256': api.digest(payload)}
    try:
        identity = verify_year(payload, args.year, parameters, data)
    except ValueError as exc:
        api.write_json({**audit, 'status': 'rejected', 'error': str(exc)}, folder / 'identity_validation.json')
        raise
    api.write_json({**audit, 'status': 'verified', 'metadata': identity}, folder / 'identity_validation.json')
    return filing, response, parameters


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--ticker', help='SEC ticker, e.g. INTC')
    source.add_argument('--cik', help='SEC issuer CIK instead of a ticker')
    source.add_argument('--filing', type=Path, help='Saved original Inline XBRL filing; requires --xbrl-json')
    parser.add_argument('--xbrl-json', type=Path, help='Saved API response/cache for --filing mode')
    parser.add_argument('--company', help='Output folder and filename label; issuer identity is verified from the sources')
    parser.add_argument('--year', type=int, required=True, help='Fiscal year, not filing submission year')
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument('--filing-url', help='SEC URL provenance for a saved raw API response')
    identity.add_argument('--accession', help='Select a live original 10-K, or supply provenance for a saved raw response')
    parser.add_argument('--items', nargs='+', type=str.upper, choices=export.membership.SUPPORTED_ITEMS,
                        default=list(export.membership.SUPPORTED_ITEMS))
    parser.add_argument('--output-dir', type=Path, help='Output folder; defaults to tests/for_table_development/<company>, shared by all years')
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''), help='SEC contact name/email; defaults to SEC_USER_AGENT')
    parser.add_argument('--sec-cache', type=Path, default=Path('data/sec_cache'), help='Internal SEC and API download cache, separate from the output folder')
    parser.add_argument('--taxonomy-cache', type=Path, default=Path('data/taxonomy_cache'))
    parser.add_argument('--offline', action='store_true', help='Require saved inputs and the cached official taxonomy; no downloads')
    args = parser.parse_args(argv)
    try:
        if not 1900 <= args.year <= 2200:
            raise ValueError('--year must be a four-digit fiscal year')
        if bool(args.filing) != bool(args.xbrl_json):
            raise ValueError('Use --filing and --xbrl-json together, or --ticker/--cik for live retrieval.')
        if args.offline and not args.filing:
            raise ValueError('--offline requires --filing and --xbrl-json; ticker/CIK lookup requires SEC access.')
        if args.filing_url and not args.filing:
            raise ValueError('--filing-url is only for saved inputs; live mode uses the selected SEC URL.')
        items = export.membership.normalize_items(args.items)
        label = args.company or args.ticker or args.cik or args.filing.stem
        filename = f'{api.slug(label)}_{args.year}_api_metrics_with_values.json'
        output = (args.output_dir or Path('tests/for_table_development') / api.slug(label)).expanduser()
        check_output(output, filename)  # Fail before network requests or paid API calls.
        if args.filing:
            filing, response = args.filing.expanduser(), args.xbrl_json.expanduser()
            parameters = api.request_parameters(args.filing_url, args.accession) if args.filing_url or args.accession else None
            print('1/3 Reading the saved original filing.\n2/3 Verifying the saved API response...', flush=True)
        else:
            filing, response, parameters = live_inputs(args)
        payload, parameters, data, _ = read_saved(response, filing, parameters)
        verify_year(payload, args.year, parameters, data)
        command = [str(response), '--filing', str(filing), '--items', *items,
                   '--output-dir', str(output), '--taxonomy-cache', str(args.taxonomy_cache.expanduser()),
                   '--metrics-only', '--sec-cache', str(args.sec_cache.expanduser()),
                   '--user-agent', args.user_agent]
        # Explicit provenance also lets the lower-level exporter accept raw JSON.
        command += ['--filing-url', parameters['htm-url']] if 'htm-url' in parameters else ['--accession', parameters['accession-no']]
        if args.offline:
            command.append('--offline')
        print('3/3 Checking Item membership and exporting official metrics, values and source labels...', flush=True)
        if export.main(command, metrics_filename=filename) != 0:
            return 1
        print(f'Pipeline complete. Final output: {output / filename}')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
