"""Resolve API identity metadata against its already-bound original filing.

Callers must bind the filing bytes to the request URL/accession before using
the fallback. No API response, monetary value or command-line identity is
rewritten. Only official DEI facts and SEC entity contexts supply fallbacks.
"""
import re
from urllib.parse import urlparse

from lxml import etree

from sec_disclosure.table_extraction import extract_item8_xbrl_api as api
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix


def filing_fields(data):
    if b'<!ENTITY' in data.upper():
        raise ValueError('Entity declarations are not supported in filing input.')
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise ValueError('Expected original Inline XBRL XHTML for identity verification') from exc
    nodes = [n for n in root.iter() if isinstance(n.tag, str)]
    identities, contexts, ids = {}, {}, {}
    for node in nodes:
        if node.get('id'):
            ids.setdefault(node.get('id'), []).append(node)
        if node.tag == f'{{{ix.XBRLI}}}context':
            contexts.setdefault(node.get('id'), []).append(node)
    def content(node):
        if ix.namespace(node) in ix.IX and ix.local(node) == 'exclude':
            return ''
        return (node.text or '') + ''.join(
            (content(child) if isinstance(child.tag, str) else '') + (child.tail or '') for child in node)
    def text(node):
        parts, seen = [], set()
        while node is not None:
            if node in seen:
                raise ValueError('Cyclic metadata continuation')
            seen.add(node)
            parts.append(content(node))
            ref = node.get('continuedAt')
            if not ref:
                break
            matches = ids.get(ref, [])
            if len(matches) != 1 or ix.namespace(matches[0]) not in ix.IX or ix.local(matches[0]) != 'continuation':
                raise ValueError('Missing or ambiguous metadata continuation')
            node = matches[0]
        return ''.join(parts)
    for node in nodes:
        if ix.namespace(node) not in ix.IX or ix.local(node) != 'nonNumeric':
            continue
        name = node.get('name', '').split(':')[-1]
        if name not in api.IDENTITY_FIELDS:
            continue
        qn = ix.qname(node, node.get('name'))
        if not re.fullmatch(r'https?://xbrl.sec.gov/dei/\d{4}', qn['namespace']):
            continue
        if (not node.get('contextRef') or node.get(f'{{{ix.XSI}}}nil') not in (None, 'false', '0') or node.get('target')
                or node.get('tupleRef') or any(ix.namespace(a) in ix.IX and ix.local(a) == 'tuple' for a in node.iterancestors())):
            raise ValueError(f'Unsupported filing identity fact: {name}')
        matches = contexts.get(node.get('contextRef'), [])
        if len(matches) > 1 or any(n.tag in {f'{{{ix.XBRLDI}}}explicitMember', f'{{{ix.XBRLDI}}}typedMember'}
                                  for c in matches for n in c.iter()):
            raise ValueError(f'Ambiguous or dimensioned filing identity context: {name}')
        value = ix.clean(ix.transformed_text(node, text(node)))
        identities.setdefault(name, []).append({'value': value, 'source_locator': node.getroottree().getpath(node),
            'source_fact_id': node.get('id'), 'context_ref': node.get('contextRef'),
            'context_available': len(matches) == 1, 'method': 'filing_dei'})
    ciks = []
    for matches in contexts.values():
        for context in matches:
            entity = context.find(f'{{{ix.XBRLI}}}entity/{{{ix.XBRLI}}}identifier')
            if entity is not None and entity.get('scheme') in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}:
                value = ix.clean(''.join(entity.itertext()))
                api.identity_key('EntityCentralIndexKey', value)
                ciks.append({'value': value, 'source_locator': entity.getroottree().getpath(entity),
                             'method': 'filing_entity_context'})
    for name, evidence in identities.items():
        if len({api.identity_key(name, e['value']) for e in evidence}) != 1:
            raise ValueError(f'Conflicting filing metadata for {name}')
    return identities, ciks


def resolve(payload, data, parameters, year=None, required=api.IDENTITY_FIELDS):
    """API CoverPage -> API root -> verified filing; conflicts never fall back."""
    parameters = api.validate_request(parameters)
    reported, contexts = filing_fields(data)
    resolved, evidence = {}, {}
    for name in api.IDENTITY_FIELDS:
        api_value, paths = api.identity_field(payload, name)
        facts = reported.get(name, [])
        if api_value is not None and facts and api.identity_key(name, api_value) != api.identity_key(name, facts[0]['value']):
            raise ValueError(f'Original filing {name} differs from the API response')
        if api_value is not None:
            resolved[name] = api_value
            evidence[name] = {'value': api_value, 'method': 'api', 'api_paths': paths}
        elif facts:
            resolved[name] = facts[0]['value']
            evidence[name] = {'value': facts[0]['value'], 'method': 'filing_dei', 'facts': facts}
    cik = 'EntityCentralIndexKey'
    entity_ciks = {api.identity_key(cik, c['value']) for c in contexts}
    if cik not in resolved and len(entity_ciks) == 1:
        resolved[cik] = next(iter(entity_ciks))
        evidence[cik] = {'value': resolved[cik], 'method': 'filing_entity_context', 'facts': contexts}
    expected_cik = urlparse(parameters['htm-url']).path.split('/')[4].lstrip('0') if 'htm-url' in parameters else None
    if cik not in resolved and expected_cik:
        # Some primary 10-Ks keep their DEI entity fact and contexts in
        # Exhibit 13. The already-bound original SEC URL identifies the
        # issuer; do not use the accession's submitting-agent CIK or --company.
        resolved[cik] = expected_cik
        evidence[cik] = {'value': expected_cik, 'method': 'filing_request_url', 'sec_url': parameters['htm-url']}
    declared_cik = api.identity_key(cik, resolved[cik]) if cik in resolved else None
    if ((expected_cik and declared_cik and expected_cik != declared_cik)
            or (entity_ciks and not entity_ciks <= {declared_cik or expected_cik})):
        raise ValueError('Original filing XBRL entities differ from the API/request CIK')
    fallback = any(e['method'] != 'api' for e in evidence.values())
    if fallback and not entity_ciks and cik not in reported and not expected_cik:
        raise ValueError('Filing metadata fallback requires a verified issuer CIK')
    missing = [name for name in required if name not in resolved]
    if missing:
        raise ValueError('No unique API or filing identity metadata: ' + ', '.join(missing))
    actual_year = resolved.get('DocumentFiscalYearFocus')
    if actual_year is not None and (not 1900 <= int(actual_year) <= 2200 or year is not None and int(actual_year) != year):
        raise ValueError('Verified fiscal year differs from --year or is outside the supported range')
    if resolved.get('DocumentType') not in {None, '10-K', '10-K/A'}:
        raise ValueError('Verified document type is not a 10-K')
    api.verify_identity(payload, int(actual_year) if actual_year else year, parameters)
    # Existing CoverPage exports retain their metadata shape. Nonstandard
    # sources get field-level evidence separate from the untouched response.
    nonstandard = fallback or any(not p.startswith('/CoverPage/') for e in evidence.values() for p in e.get('api_paths', []))
    return {'values': resolved, 'evidence': evidence if nonstandard else None}
