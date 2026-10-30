"""Export stored XBRL snapshots from Docker PostgreSQL, without filing/API requests.

    python3 scripts/export_xbrl_from_postgres.py --list
    python3 scripts/export_xbrl_from_postgres.py --company Intel --year 2025
    python3 scripts/export_xbrl_from_postgres.py --extraction-id 123 --format csv

Uses the same DATABASE_URL / POSTGRES_* settings as the XBRL loader.
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
from functools import partial
import json
from sec_disclosure.storage.artifacts import ArtifactPath as Path
import sys

from sec_disclosure.table_extraction import load_xbrl_to_postgres as database


METRIC_COLUMNS = tuple(sorted(database.METRIC_FIELDS - {'value'}))
SOURCE_COLUMNS = tuple(sorted(database.SOURCE_FIELDS - {'status'}))
CSV_COLUMNS = (
    'company', 'source_filing_year', 'form_type', 'extraction_id', 'taxonomy_year',
    'concept', 'query_name', 'label', 'definition', 'metric_scope', 'value_index',
    'value', 'unit', 'status', 'instant_date', 'period_start_date', 'period_end_date',
    'dimensions', 'decimals', 'precision', 'items', 'source_labels',
)


def select_snapshot(snapshots):
    if not snapshots:
        raise ValueError('No matching XBRL snapshot. Use --list to see what is stored.')
    if len(snapshots) > 1:
        ids = ', '.join(str(row['extraction_id']) for row in snapshots)
        raise ValueError(f'Multiple XBRL snapshots match (IDs: {ids}). Select one with --extraction-id.')
    return snapshots[0]


def restore_document(metadata, metric_rows, value_rows, source_rows):
    """Rebuild metrics[] / value[] / source_labels[] in their stored array order."""
    document = dict(metadata, metrics=[])
    metrics, values = {}, {}
    for row in metric_rows:
        metric = {**row['extra'], **{key: row[key] for key in METRIC_COLUMNS}, 'value': []}
        metrics[row['concept']] = metric
        document['metrics'].append(metric)
    for row in value_rows:
        period = ({'instant': row['instant_date'].isoformat()} if row['instant_date'] is not None
                  else {'startDate': row['period_start_date'].isoformat(),
                        'endDate': row['period_end_date'].isoformat()})
        value = {**row['extra'], 'value': row['value_text'], 'unit': row['unit'],
                 'status': row['status'], 'period': period, 'dimensions': row['dimensions'],
                 'items': row['items'], 'source_labels': []}
        for key in ('decimals', 'precision'):
            # SQL NULL means absent; JSON null means explicitly present with None.
            if row['has_' + key]:
                value[key] = row[key]
        values[(row['concept'], row['value_index'])] = value
        metrics[row['concept']]['value'].append(value)
    for row in source_rows:
        source = {**row['extra'], **{key: row[key] for key in SOURCE_COLUMNS},
                  'status': row['label_status']}
        values[(row['concept'], row['value_index'])]['source_labels'].append(source)
    return document


def check_schema(cur):
    relations = ['schema_info', 'filings', 'xbrl_extractions', 'xbrl_metrics',
                 'xbrl_values', 'xbrl_value_sources']
    cur.execute('SELECT to_regclass(name) AS relation FROM unnest(%s::text[]) AS name', (relations,))
    if not all(row['relation'] for row in cur.fetchall()):
        raise ValueError('XBRL database tables are missing. Load an export with scripts/load_xbrl_to_postgres.py first.')
    cur.execute("SELECT value FROM schema_info WHERE key='xbrl_schema_version'")
    version = cur.fetchone()
    if version is None or version['value'] != '1':
        raise ValueError('Expected XBRL database schema version 1.')


def read_database(*, company=None, year=None, extraction_id=None, list_only=False):
    """Read a consistent snapshot without installing schemas or changing any rows."""
    with database.connect_database() as conn:
        from psycopg.rows import dict_row
        from psycopg.types.json import set_json_loads

        set_json_loads(partial(json.loads, parse_float=Decimal), conn)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            check_schema(cur)
            clauses, parameters = [], []
            for column, value in (('f.company', database.api.slug(company) if company is not None else None),
                                  ('f.fiscal_year', year), ('e.extraction_id', extraction_id)):
                if value is not None:
                    clauses.append(column + ' = %s')
                    parameters.append(value)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            cur.execute('SELECT e.extraction_id, f.company, f.fiscal_year AS source_filing_year, '
                        'f.form_type, e.taxonomy_year, e.requested_items, e.loaded_at, e.file_path '
                        'FROM xbrl_extractions e JOIN filings f USING (filing_id)' + where +
                        ' ORDER BY f.company, f.fiscal_year, e.extraction_id', parameters)
            snapshots = cur.fetchall()
            if list_only:
                return snapshots, None
            snapshot = select_snapshot(snapshots)
            key = (snapshot['extraction_id'],)
            cur.execute('SELECT metadata FROM xbrl_extractions WHERE extraction_id=%s', key)
            metadata = cur.fetchone()['metadata']
            cur.execute('SELECT * FROM xbrl_metrics WHERE extraction_id=%s ORDER BY metric_index', key)
            metrics = cur.fetchall()
            cur.execute('SELECT *, decimals IS NOT NULL AS has_decimals, precision IS NOT NULL AS has_precision '
                        'FROM xbrl_values WHERE extraction_id=%s ORDER BY concept, value_index', key)
            values = cur.fetchall()
            cur.execute('SELECT * FROM xbrl_value_sources WHERE extraction_id=%s '
                        'ORDER BY concept, value_index, source_index', key)
            document = restore_document(metadata, metrics, values, cur.fetchall())
            database.validate_export(document, snapshot['source_filing_year'])
            return snapshots, document


def csv_rows(snapshot, document):
    """One row per value; keep source occurrences nested to avoid repeating amounts."""
    for metric in document['metrics']:
        for index, value in enumerate(metric['value']):
            period = value['period']
            row = {key: snapshot[key] for key in ('company', 'source_filing_year', 'form_type',
                                                   'extraction_id', 'taxonomy_year')}
            row.update({key: metric[key] for key in ('concept', 'query_name', 'label', 'definition')})
            row.update(metric_scope=metric['scope'], value_index=index,
                       **{key: value[key] for key in ('value', 'unit', 'status')},
                       instant_date=period.get('instant'), period_start_date=period.get('startDate'),
                       period_end_date=period.get('endDate'))
            for key in ('dimensions', 'decimals', 'precision', 'items', 'source_labels'):
                row[key] = database.json_text(value[key]) if key in value else ''
            yield row


def write_export(path, output_format, snapshot, document):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects existing exports and symlink targets.
    with path.open('x', encoding='utf-8', newline='') as output:
        try:
            if output_format == 'json':
                output.write(database.json_text(document) + '\n')
            else:
                writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(csv_rows(snapshot, document))
        except Exception:
            path.unlink()
            raise
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--company', help='Stored company slug/label, e.g. Intel (same label used when loading)')
    parser.add_argument('--year', type=int, help='Source filing fiscal year, not the comparative value year')
    parser.add_argument('--extraction-id', type=int, help='Select a specific stored snapshot')
    parser.add_argument('--list', action='store_true', help='List available snapshots; optionally filter by company/year')
    parser.add_argument('--format', choices=('json', 'csv'), default='json', help='Output format (default: json)')
    parser.add_argument('--output', type=Path, help='Output file; existing files are never overwritten')
    args = parser.parse_args(argv)
    if not args.list and args.extraction_id is None and (args.company is None or args.year is None):
        parser.error('Provide --company and --year, or --extraction-id; use --list to browse.')
    if args.company is not None and not args.company.strip():
        parser.error('Provide a nonempty company label.')
    try:
        snapshots, document = read_database(company=args.company, year=args.year,
                                            extraction_id=args.extraction_id, list_only=args.list)
        if args.list:
            if not snapshots:
                print('No matching XBRL snapshots.')
            for row in snapshots:
                print(f'{row["extraction_id"]}: {row["company"]} FY{row["source_filing_year"]} '
                      f'{row["form_type"]} | Items {", ".join(row["requested_items"])} | '
                      f'loaded {row["loaded_at"]} | {row["file_path"]}')
            return 0
        snapshot = snapshots[0]
        filename = (f'{snapshot["company"]}_{snapshot["source_filing_year"]}_'
                    f'extraction_{snapshot["extraction_id"]}_api_metrics_with_values.{args.format}')
        target = args.output or Path('data/table_output') / snapshot['company'] / 'db_exports' / filename
        path = write_export(target, args.format, snapshot, document)
        count = sum(len(metric['value']) for metric in document['metrics'])
        print(f'Exported extraction {snapshot["extraction_id"]}: {len(document["metrics"])} metrics, '
              f'{count} values to {path}')
        return 0
    except FileExistsError:
        print('Error: Output already exists. Use --output to select a new file.', file=sys.stderr)
    except (database.DatabaseSaveError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
    except Exception:
        # Connection/query diagnostics may contain credentials; keep the CLI error safe.
        print('Error: Cannot read XBRL data. Check PostgreSQL connectivity and the XBRL database schema.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
