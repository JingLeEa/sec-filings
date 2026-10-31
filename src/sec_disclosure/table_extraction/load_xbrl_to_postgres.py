"""Store one yearly API/XBRL export in PostgreSQL, without SEC or API requests.

    python3 scripts/load_xbrl_to_postgres.py --company Intel --year 2025

Uses DATABASE_URL or POSTGRES_* settings, with the local Docker defaults.
Original filing/API caches are checked for issuer, year and hash consistency.
"""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from sec_disclosure.storage.artifacts import ArtifactPath as Path
import sys

from sec_disclosure.table_extraction import api_filing_metadata as metadata
from sec_disclosure.table_extraction import extract_item8_xbrl_api as api
from sec_disclosure.table_extraction import merge_api_metrics as merger


ROOT = Path(__file__).resolve().parents[3]
METRIC_FIELDS = {'concept', 'query_name', 'label', 'definition', 'period_type', 'scope',
                 'company_wide_annual_or_year_end_years', 'units', 'api_groups', 'items', 'value'}
VALUE_FIELDS = {'value', 'unit', 'status', 'period', 'dimensions', 'decimals', 'precision', 'items', 'source_labels'}
SOURCE_FIELDS = {'document_id', 'source', 'page', 'source_fact_id', 'source_locator',
                 'row_label', 'column_label', 'label_method', 'status'}
LABEL_STATUSES = {'resolved', 'partial', 'unresolved', 'not_in_table', 'source_not_found'}


class DatabaseSaveError(RuntimeError):
    """An actionable connection, schema or persistence failure."""


def json_text(value):
    # JSONB should retain decimal JSON numbers as numbers without float rounding.
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('JSON number must be finite')
        return str(value)
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(key, ensure_ascii=False) + ': ' + json_text(item)
                               for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(json_text(item) for item in value) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def extra(record, known):
    return json_text({key: value for key, value in record.items() if key not in known})


def check_items(items, allowed, description):
    if (not isinstance(items, list) or not items or any(not isinstance(item, str) for item in items)
            or len(set(items)) != len(items) or not set(items) <= set(allowed)):
        raise ValueError(f'{description}: invalid or inconsistent Item memberships')


def validate_export(data, year):
    merger.validate(data, year)
    if not isinstance(data.get('exporter_version'), str) or not data['exporter_version']:
        raise ValueError('Missing exporter_version')
    check_items(data['requested_items'], {'1', '1A', '7', '8'}, 'Export')
    scope_counts = {'company_wide': 0, 'dimension_only': 0}
    for metric in data['metrics']:
        concept = metric['concept']
        if (not isinstance(metric.get('label'), str) or metric.get('period_type') not in {'instant', 'duration'}
                or metric.get('scope') not in scope_counts
                or metric.get('definition') is not None and not isinstance(metric['definition'], str)):
            raise ValueError(f'{concept}: invalid metric metadata')
        for field in ('units', 'api_groups', 'company_wide_annual_or_year_end_years'):
            values = metric.get(field)
            expected = int if field == 'company_wide_annual_or_year_end_years' else str
            if not isinstance(values, list) or any(type(value) is not expected for value in values):
                raise ValueError(f'{concept}: invalid {field}')
        check_items(metric.get('items'), data['requested_items'], concept)
        for value in metric['value']:
            check_items(value['items'], metric['items'], concept)
            period = value['period']
            if set(period) == {'instant'}:
                date.fromisoformat(period['instant'])
            elif set(period) == {'startDate', 'endDate'}:
                if date.fromisoformat(period['startDate']) > date.fromisoformat(period['endDate']):
                    raise ValueError(f'{concept}: reversed duration')
            else:
                raise ValueError(f'{concept}: expected an instant or complete duration')
            if not isinstance(value['unit'], str) or not value['unit'].strip():
                raise ValueError(f'{concept}: missing unit')
            axes = set()
            for dimension in value['dimensions']:
                if (not isinstance(dimension, dict)
                        or any(not isinstance(dimension.get(key), str) or not dimension[key].strip()
                               for key in ('axis', 'member')) or dimension['axis'] in axes):
                    raise ValueError(f'{concept}: invalid or repeated dimension axis')
                axes.add(dimension['axis'])
            for field in ('decimals', 'precision'):
                if field in value and (isinstance(value[field], bool)
                                       or value[field] is not None and not isinstance(value[field], (str, int))):
                    raise ValueError(f'{concept}: invalid {field}')
            for source in value['source_labels']:
                if (not isinstance(source, dict) or source.get('status') not in LABEL_STATUSES
                        or any(source.get(key) is not None and not isinstance(source[key], str)
                               for key in SOURCE_FIELDS - {'status'})):
                    raise ValueError(f'{concept}: invalid source occurrence')
        actual_scope = 'company_wide' if any(not value['dimensions'] for value in metric['value']) else 'dimension_only'
        if metric['scope'] != actual_scope:
            raise ValueError(f'{concept}: scope disagrees with its dimensions')
        scope_counts[actual_scope] += 1
    for key, count in scope_counts.items():
        if data['counts'].get(key) != count:
            raise ValueError(f'{key}: count disagrees with metric records')
    count = sum(len(metric['value']) for metric in data['metrics'])
    if data['verification'].get('unique_values', count) != count:
        raise ValueError('Verified value count disagrees with value records')


def verified_identity(data, year, expected_cik=None):
    source = data['verification']['source']
    for path_key, hash_key in (('filing', 'filing_sha256'), ('api_file', 'api_file_sha256')):
        if not isinstance(source.get(path_key), str) or not isinstance(source.get(hash_key), str):
            raise ValueError(f'Missing verified {path_key} path/hash; use the current API metrics exporter')
        original = Path(source[path_key]).expanduser().read_bytes()
        if hashlib.sha256(original).hexdigest() != source[hash_key]:
            raise ValueError(f'Original {path_key} hash differs from the export')
    filing_bytes = Path(source['filing']).expanduser().read_bytes()
    payload, parameters = api.load_response(Path(source['api_file']).expanduser(), source.get('request'),
                                            source_sha256=source['filing_sha256'])
    if source.get('response_sha256') != api.digest(payload):
        raise ValueError('Original API response hash differs from the export')
    identity = metadata.resolve(payload, filing_bytes, parameters, year)['values']
    if ' '.join(data['company'].casefold().split()) != ' '.join(identity['EntityRegistrantName'].casefold().split()):
        raise ValueError('Export company differs from its verified source issuer')
    cik = str(int(identity['EntityCentralIndexKey']))
    if expected_cik is not None and cik != str(int(expected_cik)):
        raise ValueError('Export issuer CIK differs from --cik')
    return identity


def read_export(path, year, expected_cik=None):
    raw = Path(path).read_bytes()
    def invalid_number(value):
        raise ValueError(f'Invalid JSON number: {value}')
    data = json.loads(raw, parse_float=Decimal, parse_constant=invalid_number)
    try:
        validate_export(data, year)
        identity = verified_identity(data, year, expected_cik)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f'Malformed XBRL export: {exc}') from exc
    return data, hashlib.sha256(raw).hexdigest(), identity


def load_environment():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
    except ImportError:
        pass


def connect_database():
    load_environment()
    try:
        import psycopg
    except ImportError as exc:
        raise DatabaseSaveError('Install dependencies with: python -m pip install -r requirements.txt') from exc
    try:
        if os.environ.get('DATABASE_URL'):
            return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5)
        return psycopg.connect(host=os.environ.get('POSTGRES_HOST', 'localhost'),
                              port=os.environ.get('POSTGRES_PORT', '5432'),
                              user=os.environ.get('POSTGRES_USER', 'sec'),
                              password=os.environ.get('POSTGRES_PASSWORD', 'sec_local_pw'),
                              dbname=os.environ.get('POSTGRES_DB', 'sec_disclosure'), connect_timeout=5)
    except (psycopg.Error, ValueError) as exc:
        raise DatabaseSaveError('Cannot connect to PostgreSQL. Run docker compose up -d and check DATABASE_URL/POSTGRES_* settings.') from exc


def ensure_schema(cur):
    cur.execute("SELECT to_regclass('filings'), to_regclass('schema_info')")
    if not all(cur.fetchone()):
        raise DatabaseSaveError('Base database schema is missing; start the database with docker compose up -d.')
    cur.execute("SELECT value FROM schema_info WHERE key='xbrl_schema_version'")
    version = cur.fetchone()
    if version and version[0] != '1':
        raise DatabaseSaveError(f'Unsupported XBRL database schema version: {version[0]}')
    relations = ('xbrl_extractions', 'xbrl_metrics', 'xbrl_values', 'xbrl_value_sources', 'xbrl_value_context')
    cur.execute('SELECT to_regclass(name) FROM unnest(%s::text[]) AS name', (list(relations),))
    if version and all(row[0] for row in cur.fetchall()):
        return
    # Serialize initial schema creation; keep the migration inside our transaction.
    cur.execute('SELECT pg_advisory_xact_lock(40401001)')
    migration = ROOT / 'db/init/04_xbrl_data.sql'
    statements = '\n'.join(line for line in migration.read_text().splitlines()
                           if line.strip() not in {'BEGIN;', 'COMMIT;'})
    cur.execute(statements)


def prepare_database():
    """Check connectivity and install the XBRL tables before any paid extraction."""
    try:
        with connect_database() as conn, conn.cursor() as cur:
            ensure_schema(cur)
    except DatabaseSaveError:
        raise
    except Exception as exc:
        raise DatabaseSaveError(f'Cannot prepare XBRL database schema: {exc}') from exc


def metric_row(metric, extraction_id, index):
    return (extraction_id, metric['concept'], index, metric['query_name'], metric['label'], metric.get('definition'),
            metric['period_type'], metric['scope'], metric['company_wide_annual_or_year_end_years'],
            metric['units'], metric['api_groups'], metric['items'], extra(metric, METRIC_FIELDS))


def value_row(value, extraction_id, concept, index):
    period = value['period']
    return (extraction_id, concept, index, None if value['status'] == 'nil' else str(value['value']),
            value['unit'], value['status'], period.get('instant'), period.get('startDate'), period.get('endDate'),
            json_text(value['dimensions']), json_text(value['decimals']) if 'decimals' in value else None,
            json_text(value['precision']) if 'precision' in value else None, value['items'], extra(value, VALUE_FIELDS))


def source_row(source, extraction_id, concept, value_index, source_index):
    return (extraction_id, concept, value_index, source_index, source.get('document_id'), source.get('source'),
            source.get('page'), source.get('source_fact_id'), source.get('source_locator'), source.get('row_label'),
            source.get('column_label'), source.get('label_method'), source['status'], extra(source, SOURCE_FIELDS))


def store_export(cur, data, file_hash, identity, path, company, year):
    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (api.slug(company),))
    cur.execute("""
        INSERT INTO filings (company, fiscal_year, form_type, source, xbrl_taxonomy_version)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (company, fiscal_year, form_type) DO UPDATE
        SET xbrl_taxonomy_version = COALESCE(filings.xbrl_taxonomy_version, EXCLUDED.xbrl_taxonomy_version)
        RETURNING filing_id
    """, (api.slug(company), year, identity['DocumentType'], data['verification']['source']['request'].get('htm-url'),
          str(data['taxonomy_year'])))
    filing_id = cur.fetchone()[0]
    # An output-folder label must not silently combine different issuers across years.
    cur.execute('SELECT e.metadata FROM xbrl_extractions e JOIN filings f USING (filing_id) WHERE f.company=%s',
                (api.slug(company),))
    cik = str(int(identity['EntityCentralIndexKey']))
    for previous, in cur.fetchall():
        previous_cik = merger.issuer_cik(previous)
        if previous_cik is not None and previous_cik != cik:
            raise ValueError('Company label already contains XBRL exports for another issuer; use a different --company')
    cur.execute("""
        INSERT INTO xbrl_extractions
            (filing_id, file_path, file_sha256, export_schema_version, exporter_version, taxonomy_year, requested_items, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (filing_id, file_sha256) DO NOTHING RETURNING extraction_id
    """, (filing_id, str(path.resolve()), file_hash, data['schema_version'], data['exporter_version'], data['taxonomy_year'],
          data['requested_items'], json_text({key: value for key, value in data.items() if key != 'metrics'})))
    inserted = cur.fetchone()
    if inserted:
        extraction_id = inserted[0]
        metrics, values, sources = [], [], []
        for metric_index, metric in enumerate(data['metrics']):
            concept = metric['concept']
            metrics.append(metric_row(metric, extraction_id, metric_index))
            for value_index, value in enumerate(metric['value']):
                values.append(value_row(value, extraction_id, concept, value_index))
                sources.extend(source_row(source, extraction_id, concept, value_index, source_index)
                               for source_index, source in enumerate(value['source_labels']))
        cur.executemany('INSERT INTO xbrl_metrics (extraction_id, concept, metric_index, query_name, label, definition, '
                        'period_type, scope, company_wide_annual_or_year_end_years, units, api_groups, items, extra) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)', metrics)
        cur.executemany('INSERT INTO xbrl_values (extraction_id, concept, value_index, value_text, unit, status, '
                        'instant_date, period_start_date, period_end_date, dimensions, decimals, precision, items, extra) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s::jsonb)', values)
        if sources:
            cur.executemany('INSERT INTO xbrl_value_sources (extraction_id, concept, value_index, source_index, document_id, '
                            'source, page, source_fact_id, source_locator, row_label, column_label, label_method, label_status, extra) '
                            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)', sources)
    else:
        cur.execute('SELECT extraction_id FROM xbrl_extractions WHERE filing_id=%s AND file_sha256=%s', (filing_id, file_hash))
        extraction_id = cur.fetchone()[0]
    expected = (len(data['metrics']), sum(len(m['value']) for m in data['metrics']),
                sum(len(v['source_labels']) for m in data['metrics'] for v in m['value']))
    actual = []
    for table in ('xbrl_metrics', 'xbrl_values', 'xbrl_value_sources'):
        cur.execute(f'SELECT count(*) FROM {table} WHERE extraction_id=%s', (extraction_id,))
        actual.append(cur.fetchone()[0])
    if tuple(actual) != expected:
        raise DatabaseSaveError('Stored XBRL snapshot is incomplete; database counts differ from the export')
    return {'extraction_id': extraction_id, 'metrics': expected[0], 'values': expected[1], 'sources': expected[2],
            'already_present': inserted is None}


def load_export(path, company, year, *, expected_cik=None):
    path = Path(path).expanduser()
    if not api.slug(company):
        raise ValueError('Provide a nonempty company label')
    data, file_hash, identity = read_export(path, year, expected_cik)
    try:
        with connect_database() as conn, conn.cursor() as cur:
            ensure_schema(cur)
            result = store_export(cur, data, file_hash, identity, path, company, year)
            if hashlib.sha256(path.read_bytes()).hexdigest() != file_hash:
                raise ValueError('Export changed while it was being loaded; nothing committed')
    except DatabaseSaveError:
        raise
    except Exception as exc:
        # The connection context rolls back the whole snapshot on any failure.
        raise DatabaseSaveError(f'XBRL database save failed: {exc}') from exc
    detail = ' (already present)' if result['already_present'] else ''
    print(f'Saved {api.slug(company)} FY{year} to PostgreSQL: {result["metrics"]} metrics, '
          f'{result["values"]} values, {result["sources"]} source occurrences '
          f'(extraction_id {result["extraction_id"]}){detail}.')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--company', required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--file', type=Path, help='Override the default yearly JSON in data/table_output/<company>/')
    parser.add_argument('--cik', help='Require this verified issuer CIK')
    parser.add_argument('--dry-run', action='store_true', help='Validate the export and its cached sources without connecting to PostgreSQL')
    args = parser.parse_args(argv)
    path = args.file or Path('data/table_output') / api.slug(args.company) / f'{api.slug(args.company)}_{args.year}_api_metrics_with_values.json'
    try:
        if args.dry_run:
            data, _, _ = read_export(path, args.year, args.cik)
            print(f'Validated {len(data["metrics"])} metrics; no database changes.')
        else:
            load_export(path, args.company, args.year, expected_cik=args.cik)
        return 0
    except (ValueError, OSError, KeyError, TypeError, DatabaseSaveError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
