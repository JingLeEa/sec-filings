#!/usr/bin/env python3
"""Import stage metadata once for results already stored in PostgreSQL.

Validates existing normalized rows before publishing any metadata. Does not
download anything, call models, or replace normalized records.
"""
import argparse
import importlib.util
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sec_disclosure.storage.disclosures import connection_arguments, disclosure_row, read_disclosures
from sec_disclosure.storage.alignments import alignment_row, member_rows, read_alignments
from sec_disclosure.storage.pipeline_snapshots import (
    ALIGNMENT_FILES, DISCLOSURE_FILES, file_document, folder_documents, publish,
)


def verify(expected, actual, keys):
    def identity(row):
        return tuple(row[k] for k in keys)
    wanted, stored = ({identity(row): row for row in rows} for rows in (expected, actual))
    if wanted.keys() != stored.keys():
        raise ValueError('Local and PostgreSQL IDs differ; complete the existing stage import first.')
    for key, row in wanted.items():
        for field, value in row.items():
            if field in {'extra', 'sources', 'verification', 'change_analysis', 'evidence', 'grouping', 'validation_errors'} and isinstance(value, str):
                value = json.loads(value)
            found = stored[key].get(field)
            if isinstance(found, (int, Decimal)) and isinstance(value, str):
                try:
                    value = Decimal(value)
                except InvalidOperation:
                    pass
            if value != found:
                raise ValueError(f'Local and PostgreSQL {field} differ for {key}; no snapshot imported.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', default='AMD')
    parser.add_argument('--years', type=int, nargs='+', default=[2023, 2024, 2025])
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    company, years = args.ticker.lower(), sorted(set(args.years))
    spec = importlib.util.spec_from_file_location('snapshot_raw_loader', ROOT / 'scripts/load_to_postgres.py')
    raw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(raw)
    import psycopg
    from psycopg.rows import dict_row
    snapshots = []
    try:
        with psycopg.connect(**connection_arguments(args.env_file), row_factory=dict_row) as conn:
            conn.read_only = args.dry_run
            conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
            with conn.cursor() as cur:
                for year in years:
                    cur.execute("SELECT filing_id FROM filings WHERE company=%s AND fiscal_year=%s AND form_type='10-K'", (company, year))
                    filing = cur.fetchone()
                    if filing is None:
                        raise ValueError(f'No PostgreSQL filing for {company} {year}')
                    filing_id = filing['filing_id']
                    folder = args.data_dir / 'raw' / company / str(year)
                    docs = folder_documents(folder, [f'{year}_chunks.json', f'{year}_chunk_sentences.json'])
                    for suffix, table, mapper, key in (('chunks', 'chunks', raw.chunk_row, 'chunk_id'),
                                                       ('chunk_sentences', 'sentences', raw.sentence_row, 'sentence_id')):
                        records = docs[f'{year}_{suffix}.json']['payload']
                        cur.execute(f'SELECT * FROM {table} WHERE filing_id=%s', (filing_id,))
                        verify([mapper(row, filing_id) for row in records], cur.fetchall(), [key])
                    snapshots.append(('raw', year, year, docs))
                    folder = args.data_dir / 'disclosures' / company / str(year)
                    groups = read_disclosures(folder, company, year, require_complete=True)
                    cur.execute('SELECT * FROM disclosures WHERE filing_id=%s', (filing_id,))
                    verify([disclosure_row(row, filing_id, status) for status, records in groups for row in records],
                           cur.fetchall(), ['disclosure_id'])
                    docs = folder_documents(folder, DISCLOSURE_FILES)
                    if not set(DISCLOSURE_FILES[:-1]) <= docs.keys():
                        raise ValueError(f'Missing extraction audit/usage metadata for {year}')
                    snapshots.append(('disclosures', year, year, docs))
                for previous, current in zip(years, years[1:]):
                    folder = args.data_dir / 'alignments' / company / f'{previous}-{current}'
                    reports = read_alignments(folder, company, previous, current)
                    cur.execute('SELECT run_id FROM alignment_runs WHERE company=%s AND previous_year=%s AND current_year=%s', (company, previous, current))
                    run = cur.fetchone()
                    if run is None:
                        raise ValueError('Missing PostgreSQL alignment run')
                    run_id = run['run_id']
                    records = [(name, row) for name, report in reports.items() for row in report['alignments']]
                    cur.execute('SELECT * FROM alignments WHERE run_id=%s', (run_id,))
                    verify([alignment_row(row, run_id, name) for name, row in records], cur.fetchall(), ['match_id'])
                    cur.execute('SELECT * FROM alignment_members WHERE run_id=%s', (run_id,))
                    verify([member for _, row in records for member in member_rows(row, run_id)], cur.fetchall(),
                           ['match_id', 'side', 'disclosure_id'])
                    docs = folder_documents(folder, ALIGNMENT_FILES, include_change_cache=True)
                    if not set(ALIGNMENT_FILES[:3]) <= docs.keys():
                        raise ValueError('Missing alignment report/manifest metadata')
                    snapshots.append(('alignments', previous, current, docs))
                if not args.dry_run:
                    for kind, previous, current, docs in snapshots:
                        publish(cur, company, kind, previous, current, docs)
        print(f"{'Validated' if args.dry_run else 'Imported'} {len(snapshots)} PostgreSQL stage snapshots for {company.upper()}; normalized records unchanged.")
        return 0
    except Exception as error:
        print(f'Snapshot import failed ({type(error).__name__}); transaction rolled back.', file=sys.stderr)
        if isinstance(error, (ValueError, KeyError, FileNotFoundError)):
            print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
