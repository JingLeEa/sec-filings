"""HTML input provenance and identity, using the API pipeline's source readers.

No provider request or provider amount is needed. SEC selection, original DEI
parsing, incorporated reports and resource sharing use the existing helpers.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import api_filing_documents as documents
import api_filing_metadata as metadata
import compare_html_tables as sec
import extract_item8_xbrl_api as api


def sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass
class FilingInput:
    data: bytes
    source: str
    parameters: dict
    provenance: dict
    watched: dict = field(default_factory=dict)

    def verify_unchanged(self, resolved):
        expected = {Path(e['doc'].source): sha(e['doc'].data) for e in resolved.values()}
        expected.update(self.watched)
        for path, digest in expected.items():
            if sha(path.read_bytes()) != digest:
                raise ValueError(f'Filing input changed during extraction: {path}')


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or not all(isinstance(value, str) for value in parameters.values()):
        raise ValueError('Invalid saved filing request provenance.')
    return api.validate_request(parameters)


def merge_parameters(left, right):
    if not left:
        return validate_parameters(right) if right else {}
    left = validate_parameters(left)
    if not right:
        return left
    right = validate_parameters(right)
    if (api.request_accession(left) != api.request_accession(right)
            or 'htm-url' in left and 'htm-url' in right and left != right):
        raise ValueError('Filing URL/accession differs from the saved source provenance.')
    return right if 'htm-url' in right else left


def read_sidecars(path, data, parameters):
    watched, selection = {}, None
    for sidecar in (path.with_name(path.stem + '-source.json'), path.with_name(path.name + '.source.json')):
        if not sidecar.is_file():
            continue
        raw = sidecar.read_bytes()
        saved = json.loads(raw)
        if not isinstance(saved, dict):
            raise ValueError(f'Expected a source provenance object: {sidecar}')
        if saved.get('sha256') != sha(data):
            raise ValueError(f'Filing sidecar hash mismatch: {sidecar}')
        url = saved.get('original_sec_url') or saved.get('sec_url')
        if url:
            parameters = merge_parameters(parameters, {'htm-url': url})
        if saved.get('sec_selection'):
            if not isinstance(saved['sec_selection'], dict):
                raise ValueError('Invalid saved SEC filing selection.')
            if selection and selection != saved['sec_selection']:
                raise ValueError('Conflicting saved SEC filing selections.')
            selection = saved['sec_selection']
        watched[sidecar] = sha(raw)
    return parameters, watched, selection


def read_source(args):
    if args.api_metrics or args.filing:
        watched, evidence = {}, {}
        parameters = api.request_parameters(filing_url=args.filing_url) if args.filing_url else {}
        if args.api_metrics:
            export_path = args.api_metrics.expanduser()
            raw = export_path.read_bytes()
            provenance = json.loads(raw)['verification']['source']
            path = Path(provenance['filing']).expanduser()
            if not path.is_file():
                path = export_path.parent / path
            data = path.read_bytes()
            if sha(data) != provenance['filing_sha256']:
                raise ValueError('Original filing hash differs from the API export provenance.')
            parameters = merge_parameters(parameters, provenance.get('request', {}))
            watched[export_path] = sha(raw)
            evidence = {'binding': 'api_export_source_sha256', 'api_metrics': str(export_path)}
        else:
            path = args.filing.expanduser()
            data = path.read_bytes()
            evidence = {'binding': 'explicit_filing_url' if parameters else 'local_filing'}
        parameters, sidecars, selection = read_sidecars(path, data, parameters)
        watched.update(sidecars)
        watched[path] = sha(data)
        if sidecars:
            evidence['sidecars'] = [str(p) for p in sidecars]
        if selection:
            evidence['sec_selection'] = selection
        return FilingInput(data, str(path), parameters, evidence, watched)

    client = sec.SecClient(args.user_agent, args.sec_cache.expanduser())
    try:
        issuer, filings = sec.discover_sec_filings(
            client, [args.year], ticker=args.ticker or '', cik=args.cik or '',
            accessions={args.year: args.accession} if args.accession else None)
    except ValueError as exc:
        # Shared selection also serves a two-year comparison CLI. Use the
        # equivalent option names exposed by this single-filing command.
        raise ValueError(str(exc).replace('--previous-accession or --current-accession', '--accession')) from exc
    selected = filings[args.year]
    parameters = api.request_parameters(filing_url=selected['url'])
    data = client.get(selected['url'])
    print(f'Selected {selected["accessionNumber"]}; report date {selected["reportDate"]}.', flush=True)
    # Use SecClient's existing immutable document cache instead of another
    # download tree. A source sidecar also makes offline retries reproducible.
    cache = args.sec_cache.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (sha(selected['url'].encode()) + '.html')
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Cached source differs from the selected SEC filing. Choose a new --sec-cache.')
    else:
        with path.open('xb') as stream:
            stream.write(data)
    parameters, watched, _ = read_sidecars(path, data, parameters)
    sidecar = path.with_name(path.stem + '-source.json')
    if not sidecar.exists():
        raw = json.dumps({'sec_url': selected['url'], 'sha256': sha(data),
                          'sec_selection': selected}, indent=2).encode()
        with sidecar.open('xb') as stream:
            stream.write(raw)
        watched[sidecar] = sha(raw)
    watched[path] = sha(data)
    return FilingInput(data, str(path), parameters,
                       {'binding': 'sec_submissions_selection', 'sec_selection': selected, 'issuer': issuer}, watched)


def verify_identity(resolved, expected_year, parameters=None, selection=None):
    """Check the whole verified document set, including shared DEI contexts."""
    fields, entities = {}, set()
    parameters = validate_parameters(parameters) if parameters else {}
    for document_id, entry in resolved.items():
        doc = entry['doc']
        reported, _ = metadata.filing_fields(doc.data)
        for name, facts in reported.items():
            for fact in facts:
                context = doc.contexts.get(fact['context_ref'])
                if context is None or context['dimensions']:
                    raise ValueError(f'{name} has no verified undimensioned context in {document_id}.')
                fields.setdefault(name, []).append({**fact, 'document_id': document_id,
                                                    'context_available_in_document_set': True})
        entities.update(sec.normalize_cik(c['entity']['identifier']) for c in doc.contexts.values()
                        if c['entity']['scheme'] in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'})
    values = {}
    for name, facts in fields.items():
        keys = {api.identity_key(name, f['value']) for f in facts}
        if len(keys) != 1:
            raise ValueError(f'Conflicting {name} across the filing and incorporated reports.')
        values[name] = facts[0]['value']
    year = values.get('DocumentFiscalYearFocus')
    if year is None:
        raise ValueError('No verified DocumentFiscalYearFocus in the original filing or its incorporated reports; '
                         'cannot verify --year from the requested year or filename alone.')
    if int(year) != expected_year:
        raise ValueError(f'Verified filing fiscal year is {year}, expected {expected_year}.')
    if values.get('DocumentType') not in {None, '10-K'}:
        raise ValueError('Verified document type is not an original 10-K.')
    if 'DocumentType' not in values and not selection:
        raise ValueError('No verified DocumentType or SEC original 10-K selection.')
    expected_cik = sec.normalize_cik(urlparse(parameters['htm-url']).path.split('/')[4]) if 'htm-url' in parameters else None
    declared = sec.normalize_cik(values['EntityCentralIndexKey']) if 'EntityCentralIndexKey' in values else None
    all_ciks = entities | ({expected_cik} if expected_cik else set()) | ({declared} if declared else set())
    if not entities or len(all_ciks) != 1:
        raise ValueError('Original filing/report XBRL entities differ from the requested CIK or cannot be verified.')
    if selection:
        if (selection.get('form') != '10-K' or selection.get('url') != parameters.get('htm-url')
                or str(selection.get('accessionNumber', '')).replace('-', '') != api.request_accession(parameters)
                or sec.normalize_cik(selection.get('cik', '')) != next(iter(all_ciks))):
            raise ValueError('SEC filing selection differs from the verified document identity.')
        if values.get('DocumentPeriodEndDate') and values['DocumentPeriodEndDate'] != selection.get('reportDate'):
            raise ValueError('DocumentPeriodEndDate differs from the SEC report date.')
        if selection.get('declaredFiscalYear') not in (None, expected_year):
            raise ValueError('SEC selection fiscal-year evidence differs from the verified document.')
    return {'fiscal_year': {'value': int(year), 'method': 'filing_dei', 'facts': fields['DocumentFiscalYearFocus']},
            'cik': next(iter(all_ciks)), 'document_type': values.get('DocumentType'),
            'report_date': values.get('DocumentPeriodEndDate'), 'facts': fields,
            'document_type_evidence': 'filing_dei' if 'DocumentType' in fields else
                                      'sec_submissions' if selection else 'not_reported'}


def resolve_source(source, items, loader):
    # Item 8's statement inventory can establish where Item 7 ends. It supplies
    # boundary evidence only; HTML values remain restricted to requested Items.
    resolved = documents.resolve_documents(source.data, source.source, source.parameters, items, loader,
                                           boundary_items=('8',) if '7' in items else ())
    return resolved
