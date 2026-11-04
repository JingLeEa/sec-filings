#!/usr/bin/env python3
"""Extract FY2023, FY2024 and FY2025 API metrics for one company.

    python3 run_api_metrics_batch.py --ticker INTC --company Intel
    python3 run_api_metrics_batch.py --ticker WFC

Uses the existing single-year pipeline, SEC_USER_AGENT and SEC_API_KEY.
Only yearly metrics JSON files are written to the company output folder.
Yearly exports are saved to PostgreSQL by default. Existing output files are kept
and imported without downloading again. Use --no-db for the file-only workflow.
Failures are reported per year; other years are still attempted.
Up to three years run concurrently; use --workers 1 for sequential extraction.
Add --merge to also combine all yearly outputs by concept after a complete batch.
"""
from __future__ import annotations

import argparse
from sec_disclosure.storage.artifacts import ArtifactPath as Path
import sys

from sec_disclosure.table_extraction import run_api_metrics_pipeline as pipeline
from sec_disclosure.llm.concurrency import run_jobs


DEFAULT_YEARS = (2023, 2024, 2025)


def fiscal_year(value):
    try:
        year = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Use a four-digit fiscal year') from None
    if not 1900 <= year <= 2200:
        raise argparse.ArgumentTypeError('Fiscal year must be between 1900 and 2200')
    return year


def main(argv=None, *, on_year_saved=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--ticker', help='SEC ticker, e.g. INTC or WFC')
    source.add_argument('--cik', help='SEC issuer CIK instead of a ticker')
    parser.add_argument('--company', help='Optional output folder/filename label; defaults to ticker or CIK')
    parser.add_argument('--years', nargs='+', type=fiscal_year, default=DEFAULT_YEARS,
                        help='Optional fiscal years; defaults to 2023 2024 2025')
    parser.add_argument('--items', nargs='+', type=str.upper, choices=pipeline.export.membership.SUPPORTED_ITEMS,
                        default=list(pipeline.export.membership.SUPPORTED_ITEMS))
    parser.add_argument('--output-dir', type=Path, help='Defaults to data/table_output/<company>/')
    parser.add_argument('--user-agent', help='SEC contact name/email; otherwise uses SEC_USER_AGENT')
    parser.add_argument('--sec-cache', type=Path, help='Override the existing SEC/API download cache directory')
    parser.add_argument('--taxonomy-cache', type=Path, help='Override the official taxonomy cache directory')
    parser.add_argument('--merge', action='store_true', help='Also write one merged JSON by concept after all years are available')
    parser.add_argument('--workers', type=int, default=3, help='Concurrent financial-table years (default: 3)')
    parser.add_argument('--no-db', action='store_true', help='Write JSON only; skip PostgreSQL storage')
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error('--workers must be positive')

    source_value = (args.ticker or args.cik).strip()
    if not source_value:
        parser.error('Provide a nonempty ticker or CIK')
    if args.ticker:
        source_value = source_value.upper()
    label = args.company or source_value
    slug = pipeline.api.slug(label)
    output = (args.output_dir or Path('data/table_output') / slug).expanduser()
    years = sorted(set(args.years))
    common = ['--ticker' if args.ticker else '--cik', source_value,
              '--company', label, '--output-dir', str(output)]
    if args.no_db:
        common.append('--no-db')
    common += ['--items', *args.items]
    for option, value in (('--user-agent', args.user_agent), ('--sec-cache', args.sec_cache),
                          ('--taxonomy-cache', args.taxonomy_cache)):
        if value is not None:
            common += [option, str(value)]

    def run_year(year):
        target = output / f'{slug}_{year}_api_metrics_with_values.json'
        print(f'\nFY{year} — {label}', flush=True)
        if target.is_file() and not target.is_symlink():
            try:
                if not args.no_db:
                    pipeline.database.load_export(target, label, year, expected_cik=args.cik)
                print(f'Existing output kept{ " (not revalidated)" if args.no_db else " and saved to PostgreSQL"}: {target}', flush=True)
                return year, 'existing', target
            except (ValueError, OSError, KeyError, TypeError, pipeline.database.DatabaseSaveError) as exc:
                print(f'FY{year} database save failed: {exc}. Existing JSON kept: {target}', file=sys.stderr, flush=True)
                return year, 'failed', target
        try:
            code = pipeline.main([*common, '--year', str(year)])
        except Exception as exc:
            # Isolate an unexpected per-filing failure too; Ctrl-C still exits.
            print(f'FY{year} failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            code = 1
        return year, 'created' if code == 0 else 'failed', target

    results = []

    def finished(year, result):
        results.append(result)
        # Successful pipeline.main/load_export returns only after its database
        # transaction commits. Never infer a save from an output's existence.
        if result[1] != 'failed' and not args.no_db and on_year_saved is not None:
            on_year_saved(year)

    workers = min(args.workers, len(years))
    print(f'Financial tables: up to {workers} concurrent years.', flush=True)
    run_jobs(years, run_year, finished, workers)
    results.sort(key=lambda result: result[0])

    print('\nBatch summary:', flush=True)
    for year, status, target in results:
        detail = f' — {target}' if status != 'failed' else ' — see the error above; other years were still attempted'
        print(f'  FY{year}: {status}{detail}')
    print(f'{sum(s == "created" for _, s, _ in results)} created; '
          f'{sum(s == "existing" for _, s, _ in results)} existing files kept; '
          f'{sum(s == "failed" for _, s, _ in results)} failed.')
    if any(status == 'failed' for _, status, _ in results):
        if args.merge:
            print('Merge skipped: at least one requested year failed.', file=sys.stderr)
        return 1
    if args.merge:
        from sec_disclosure.table_extraction import merge_api_metrics
        merged_target = output / merge_api_metrics.merged_filename(label, years)
        if merged_target.is_file() and not merged_target.is_symlink():
            print(f'Existing merged output kept (not revalidated): {merged_target}')
            return 0
        return merge_api_metrics.main(['--company', label, '--years', *map(str, years), '--input-dir', str(output)])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
