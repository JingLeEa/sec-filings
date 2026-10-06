#!/usr/bin/env python3
r"""Build financial statement/note tables directly from sec-api.io XBRL JSON.

No filing HTML, Inline XBRL parser, physical cells, or source fact IDs are used.
One API group becomes one logical table, with concept rows and period/unit/
dimension-specific facts. The provider does not supply verified Item boundaries.

python3 extract_item8_xbrl_api.py --company Intel --year 2023 \
    --accession 0000050863-24-000010

Live requests use SEC_API_KEY. --xbrl-json accepts an existing response cache.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = '1.1.0'
ENDPOINT = 'https://api.sec-api.io/xbrl-to-json'
STATEMENTS = {'StatementsOfIncome', 'StatementsOfComprehensiveIncome', 'BalanceSheets',
              'StatementsOfCashFlows', 'StatementsOfShareholdersEquity'}
EXCLUDED_GROUPS = {'CoverPage', 'AuditInformation'}


def slug(value):
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_') or 'company'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def pointer(*parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def label(value):
    return re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', value)).replace('_', ' ')


def read_json(data):
    def invalid(value):
        raise ValueError(f'Invalid JSON number: {value}')
    return json.loads(data, parse_float=str, parse_constant=invalid)


def sec_filing_url(value):
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or parsed.hostname != 'www.sec.gov' or parsed.query or parsed.fragment
            or parsed.username or parsed.port
            or not re.fullmatch(r'/Archives/edgar/data/\d+/\d{18}/[^/]+\.html?', parsed.path)):
        raise ValueError('Provide the original SEC filing HTTPS URL, ending in .htm or .html.')
    return value


def request_parameters(filing_url=None, accession=None):
    if bool(filing_url) == bool(accession):
        raise ValueError('Provide exactly one --filing-url or --accession for the API request.')
    if filing_url:
        return {'htm-url': sec_filing_url(filing_url)}
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
        raise ValueError('Expected an accession such as 0000050863-24-000010.')
    return {'accession-no': accession}


def validate_payload(payload):
    if (not isinstance(payload, dict) or not payload
            or any(k.lower() in {'error', 'message'} for k in payload)
            or not any(isinstance(v, dict) for v in payload.values())):
        raise ValueError('Expected financial statement groups from sec-api.io, not an API error response.')


def validate_request(parameters):
    if isinstance(parameters, dict) and set(parameters) == {'htm-url'}:
        return request_parameters(filing_url=parameters['htm-url'])
    if isinstance(parameters, dict) and set(parameters) == {'accession-no'}:
        return request_parameters(accession=parameters['accession-no'])
    raise ValueError('API request provenance must contain one filing URL or accession.')


def fetch_xbrl_json(parameters, api_key):
    parameters = validate_request(parameters)
    if not api_key.strip():
        raise ValueError('Set SEC_API_KEY to your sec-api.io key, or use --xbrl-json.')
    request = Request(ENDPOINT + '?' + urlencode(parameters), headers={
        'Authorization': api_key.strip(), 'Accept': 'application/json', 'Accept-Encoding': 'gzip'})
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read()
            encoding = (response.headers.get('Content-Encoding') or '').lower().strip()
    except HTTPError as exc:
        raise ValueError(f'sec-api.io returned HTTP {exc.code}; check your key, quota and filing availability.') from None
    except (URLError, TimeoutError, OSError):
        raise ValueError('Could not reach sec-api.io; retry when the service is available.') from None
    try:
        if encoding == 'gzip':
            body = gzip.decompress(body)
        elif encoding not in {'', 'identity'}:
            raise ValueError('Unsupported encoding')
        payload = read_json(body)
    except (ValueError, UnicodeDecodeError, OSError, EOFError):
        raise ValueError('sec-api.io did not return valid JSON.') from None
    validate_payload(payload)
    return payload


def request_accession(parameters):
    if 'accession-no' in parameters:
        return parameters['accession-no'].replace('-', '')
    return urlparse(parameters['htm-url']).path.split('/')[-2]


def load_response(path, parameters=None, source_sha256=None):
    saved = read_json(Path(path).read_bytes())
    if isinstance(saved, dict) and saved.get('schema_version') in {'sec-api-cache-1.0', 'sec-api-cache-2.0'}:
        recorded = validate_request(saved['request']) if 'request' in saved else request_parameters(filing_url=saved.get('filing_url'))
        if parameters and (request_accession(recorded) != request_accession(parameters)
                           or ('htm-url' in recorded and 'htm-url' in parameters and recorded != parameters)):
            raise ValueError('Saved API response belongs to a different filing.')
        if source_sha256 and saved.get('source_sha256') and source_sha256 != saved['source_sha256']:
            raise ValueError('Saved API response source hash differs from the Item 7 filing.')
        payload, parameters = saved.get('response'), parameters or recorded
    else:
        payload = saved
        if not parameters:
            raise ValueError('Raw API JSON requires --filing-url or --accession for provenance.')
    validate_payload(payload)
    return payload, parameters


def cover_values(raw):
    values = raw if isinstance(raw, list) else [raw]
    return {str(v.get('value') if isinstance(v, dict) else v) for v in values if v is not None}


IDENTITY_FIELDS = ('DocumentFiscalYearFocus', 'DocumentType', 'DocumentPeriodEndDate',
                   'EntityRegistrantName', 'EntityCentralIndexKey')


def identity_key(key, value):
    """Compare identities without changing their original API representation."""
    value = ' '.join(str(value).split())
    if not value or value == 'None':
        raise ValueError(f'Invalid identity metadata: {key}')
    if key == 'DocumentFiscalYearFocus' and not re.fullmatch(r'\d{4}', value):
        raise ValueError('Invalid DocumentFiscalYearFocus')
    if key == 'DocumentPeriodEndDate':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Invalid DocumentPeriodEndDate')
        date.fromisoformat(value)
    if key == 'EntityCentralIndexKey':
        if not re.fullmatch(r'\d{1,10}', value):
            raise ValueError('Invalid EntityCentralIndexKey')
        return value.lstrip('0') or '0'
    return value.casefold() if key == 'EntityRegistrantName' else value


def identity_field(payload, key):
    """Read supported cover/root metadata; conflicting or malformed fields fail."""
    if key not in IDENTITY_FIELDS:
        raise ValueError(f'Unsupported identity field: {key}')
    cover = payload.get('CoverPage', {})
    if not isinstance(cover, dict):
        raise ValueError('Invalid API CoverPage metadata')
    found = []
    for container, pointer_path in ((cover, '/CoverPage/' + key), (payload, '/' + key)):
        if key not in container:
            continue
        raw = container[key]
        records = raw if isinstance(raw, list) else [raw]
        if not records:
            raise ValueError(f'API metadata has no unique {key}')
        for record in records:
            if isinstance(record, dict) and (record.get('segment') not in (None, [], {})
                    or record.get('xsi:nil') not in (None, False, 'false', '0')):
                raise ValueError(f'Unsupported dimensioned or nil identity metadata: {key}')
            value = record.get('value') if isinstance(record, dict) else record
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f'API metadata has no unique {key}')
            found.append((str(value), pointer_path))
    if len({identity_key(key, value) for value, _ in found}) > 1:
        raise ValueError(f'Conflicting API metadata for {key}')
    return (found[0][0], [path for _, path in dict.fromkeys(found)]) if found else (None, [])


def verify_identity(payload, year, parameters):
    cover = payload.get('CoverPage', {})
    if not isinstance(cover, dict):
        raise ValueError('Invalid API CoverPage metadata')
    expected = {'DocumentFiscalYearFocus': {str(year)}, 'DocumentType': {'10-K', '10-K/A'}}
    checked = []
    for key, acceptable in expected.items():
        value, paths = identity_field(payload, key)
        if paths:
            if value not in acceptable:
                raise ValueError(f'API CoverPage {key} does not match the requested 10-K fiscal year.')
            checked.append(key)
    value, paths = identity_field(payload, 'EntityCentralIndexKey')
    if 'htm-url' in parameters and paths:
        expected_cik = urlparse(parameters['htm-url']).path.split('/')[4].lstrip('0') or '0'
        if identity_key('EntityCentralIndexKey', value) != expected_cik:
            raise ValueError('API CoverPage CIK differs from the requested filing URL.')
        checked.append('EntityCentralIndexKey')
    return checked


def decimal_value(value):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int)):
        raise ValueError('Expected an exact API decimal string or integer')
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', str(value)):
        raise ValueError('Invalid API number')
    number = Decimal(value)
    if not number.is_finite() or abs(number.as_tuple().exponent) > 10000:
        raise ValueError('Invalid or excessive API number')
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def normalise_fact(raw, path):
    structured = isinstance(raw, dict)
    record = raw if structured else {'value': raw}
    fact = {'fact_id': 'api_' + digest(path)[:20], 'value': None, 'value_type': None,
            'unit_ref': record.get('unitRef'), 'period': None, 'dimensions': None,
            'decimals': record.get('decimals'), 'precision': record.get('precision'),
            'status': 'reported', 'api_pointer': path, 'raw_record': deepcopy(raw), 'issues': []}
    try:
        period = record.get('period')
        if period is None:
            fact['issues'].append('missing_period')
        elif not isinstance(period, dict) or set(period) not in ({'instant'}, {'startDate', 'endDate'}):
            raise ValueError('Invalid API period')
        else:
            for value in period.values():
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError('Invalid API period date')
                date.fromisoformat(value)
            if period.get('startDate', '') > period.get('endDate', '9999'):
                raise ValueError('API period ends before it starts')
            fact['period'] = {'type': 'instant' if 'instant' in period else 'duration', **period}
        segment = record.get('segment', [])
        if isinstance(segment, dict):
            segment = [segment]
        if not isinstance(segment, list):
            raise ValueError('Invalid API dimensions')
        dimensions = []
        for dim in segment:
            if (not isinstance(dim, dict) or set(dim) != {'dimension', 'value'}
                    or not isinstance(dim['dimension'], str) or not isinstance(dim['value'], str)):
                raise ValueError('Unsupported API dimension representation')
            dimensions.append({'axis': dim['dimension'], 'member': dim['value']})
        if len({d['axis'] for d in dimensions}) != len(dimensions):
            raise ValueError('Duplicate dimension axes')
        fact['dimensions'] = sorted(dimensions, key=lambda d: (d['axis'], d['member'])) if structured else None
        if record.get('xsi:nil') not in (None, 'true', 'false', '1', '0', True, False):
            raise ValueError('Invalid API nil marker')
        nil = record.get('xsi:nil') in ('true', '1', True)
        value = record.get('value')
        if nil:
            if value not in (None, ''):
                raise ValueError('Nil API fact also contains a value')
            fact.update(value_type='nil', status='nil')
        elif value is None:
            raise ValueError('API fact has no value or explicit nil marker')
        elif fact['unit_ref'] is not None:
            if not isinstance(fact['unit_ref'], str) or not fact['unit_ref']:
                raise ValueError('Invalid API unit reference')
            fact.update(value=decimal_value(value), value_type='number')
        elif isinstance(value, (str, bool, int)):
            value_type = 'boolean' if isinstance(value, bool) else ('duration' if isinstance(value, str) and re.fullmatch(r'-?P[0-9YMDTHS.]+', value) else 'text')
            fact.update(value=str(value).lower() if isinstance(value, bool) else str(value), value_type=value_type)
        else:
            raise ValueError('Unsupported API value representation')
        for key in ('decimals', 'precision'):
            if fact[key] is not None and not re.fullmatch(r'INF|[+-]?\d+', str(fact[key])):
                raise ValueError(f'Invalid API {key}')
        if fact['issues']:
            fact['status'] = 'incomplete'
    except (ValueError, InvalidOperation) as exc:
        fact.update(status='invalid', value=None)
        fact['issues'].append(str(exc))
    return fact


def is_text_block(concept, raw):
    value = raw.get('value') if isinstance(raw, dict) else raw
    return ('TextBlock' in concept or isinstance(value, str) and bool(re.search(
        r'<(?:div|p|table|span|ul|ol|br)\b', value, re.I)))


def context_variants(facts):
    grouped = defaultdict(list)
    for fact in facts:
        if fact['period'] is not None and fact['status'] in {'reported', 'nil'}:
            grouped[digest([fact['period'], fact['unit_ref'], fact['dimensions']])].append(fact)
    return [{'fact_ids': [f['fact_id'] for f in values],
             'kind': 'same_value' if len({f['value'] for f in values}) == 1 else 'multiple_reported_values',
             'note': 'All API occurrences retained; do not sum repeated contexts.'}
            for values in grouped.values() if len(values) > 1]


def extract_item8(payload, company, year, parameters, groups=None):
    """Pure JSON transformation. No HTML access or source-fact matching occurs."""
    validate_payload(payload)
    parameters = validate_request(parameters)
    if not 1900 <= year <= 2200:
        raise ValueError('Use a four-digit fiscal year')
    checked = verify_identity(payload, year, parameters)
    if groups and set(groups) - payload.keys():
        raise ValueError('Requested API groups are absent: ' + ', '.join(sorted(set(groups) - payload.keys())))
    tables, excluded, skipped = [], [], []
    for name, concepts in payload.items():
        if name in EXCLUDED_GROUPS or groups and name not in groups:
            excluded.append({'api_group': name, 'reason': 'cover_or_audit_metadata' if name in EXCLUDED_GROUPS else 'not_selected'})
            continue
        if not isinstance(concepts, dict):
            excluded.append({'api_group': name, 'reason': 'unsupported_group_shape'})
            continue
        rows, structured_count = [], 0
        for concept, content in concepts.items():
            raw_values = content if isinstance(content, list) else [content]
            facts = []
            for index, raw in enumerate(raw_values):
                path = pointer(name, concept, index) if isinstance(content, list) else pointer(name, concept)
                if is_text_block(concept, raw):
                    skipped.append({'api_group': name, 'concept': concept, 'api_pointer': path, 'reason': 'narrative_text_block'})
                    continue
                facts.append(normalise_fact(raw, path))
                structured_count += isinstance(raw, dict) and ('period' in raw or 'unitRef' in raw)
            if facts:
                rows.append({'row_id': 'row_' + digest(concept)[:16], 'concept': concept,
                             'label': label(concept), 'label_source': 'generated_from_api_concept',
                             'facts': facts, 'context_variants': context_variants(facts)})
        if not structured_count:
            excluded.append({'api_group': name, 'reason': 'no_structured_financial_facts'})
            continue
        kind = 'financial_statement' if name in STATEMENTS else (
            'statement_parenthetical' if name.removesuffix('Parenthetical') in STATEMENTS else 'disclosure_group')
        tables.append({'table_id': f'{slug(company)}_{year}_item8_{slug(name)}_{digest(name)[:8]}',
                       'api_group': name, 'title': label(name), 'title_source': 'generated_from_api_group',
                       'table_type': kind, 'rows': rows})
    if not tables:
        raise ValueError('No structured financial statement/note groups found; no output written.')
    facts = [f for t in tables for r in t['rows'] for f in r['facts']]
    return {'schema_version': 'xbrl-api-groups-1.0', 'extractor_version': VERSION,
            'company': company, 'year': year, 'item': '8', 'extraction_method': 'sec-api.io_xbrl_groups',
            'scope': {'basis': 'provider_financial_statement_and_disclosure_groups',
                      'item_boundaries_verified': False, 'physical_tables_reconstructed': False,
                      'selected_groups': list(groups) if groups else None,
                      'note': 'The API has no SEC Item membership. These are financial statement/note candidates for Item 8, '
                              'including facts that may also appear elsewhere in the filing.'},
            'value_convention': 'API decimal strings are already scaled values. unit_ref is the provider unit identifier. '
                                'No HTML display scale, taxonomy namespace, missing period or dimension is inferred.',
            'source': {'provider': 'sec-api.io', 'endpoint': ENDPOINT, 'request': parameters,
                       'response_sha256': digest(payload), 'cover_page_identity_checks': checked},
            'tables': tables,
            'summary': {'tables': len(tables), 'table_types': dict(Counter(t['table_type'] for t in tables)),
                        'rows': sum(len(t['rows']) for t in tables), 'facts': len(facts),
                        'numeric_facts': sum(f['value_type'] == 'number' for f in facts),
                        'fact_status': dict(Counter(f['status'] for f in facts)),
                        'context_variant_groups': sum(len(r['context_variants']) for t in tables for r in t['rows'])},
            'diagnostics': {'excluded_groups': excluded, 'excluded_text_blocks': skipped}}


def check_strict(result):
    counts = result['summary']['fact_status']
    if counts.get('invalid', 0) or counts.get('incomplete', 0):
        raise ValueError('Strict extraction failed: invalid API facts or missing metadata; outputs were not replaced.')


def write_json(value, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--filing-url')
    source.add_argument('--accession')
    parser.add_argument('--company', required=True)
    parser.add_argument('--year', required=True, type=int)
    parser.add_argument('--xbrl-json', type=Path, help='Cached/raw API JSON; no HTML or network required for a cache')
    parser.add_argument('--groups', nargs='+', help='Optional exact API group names')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args(argv)
    try:
        if not 1900 <= args.year <= 2200:
            raise ValueError('Use a four-digit fiscal year')
        parameters = request_parameters(args.filing_url, args.accession) if args.filing_url or args.accession else None
        if args.xbrl_json:
            payload, parameters = load_response(args.xbrl_json, parameters)
        else:
            parameters = parameters or request_parameters()
            payload = fetch_xbrl_json(parameters, os.environ.get('SEC_API_KEY', ''))
        result = extract_item8(payload, args.company, args.year, parameters, args.groups)
        result['source']['response_mode'] = 'saved' if args.xbrl_json else 'live'
        if args.strict:
            check_strict(result)
        output = args.output_dir or Path('data/table_output') / f'{slug(args.company)}_{args.year}_item_8_xbrl_api_tables'
        write_json(result, output / 'item_8_xbrl.json')
        write_json({'schema_version': 'sec-api-cache-2.0', 'request': parameters, 'response': payload}, output / 'sec_api_xbrl.json')
        print(f'Wrote {output / "item_8_xbrl.json"}\n' + json.dumps(result['summary']))
        return 0
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
