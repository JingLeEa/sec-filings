#!/usr/bin/env python3
"""Versioned metric dictionary built from official FASB US-GAAP packages.

The cache contains the unmodified package and a searchable JSON index. XLink
arcs, not resource-name conventions, associate labels with schema elements.
No synonym list or company-specific concept names are supplied by this module.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import io
from pathlib import Path
import re
from urllib.request import Request, urlopen
from zipfile import ZipFile

from lxml import etree

import extract_item8_xbrl_api as api

VERSION = '1.0.0'
XS = 'http://www.w3.org/2001/XMLSchema'
XI = 'http://www.xbrl.org/2003/instance'
LINK = 'http://www.xbrl.org/2003/linkbase'
XL = 'http://www.w3.org/1999/xlink'
LANG = '{http://www.w3.org/XML/1998/namespace}lang'
ROLE = 'http://www.xbrl.org/2003/role/'
MAX_PACKAGE = 30 * 1024 * 1024
MAX_XML = 50 * 1024 * 1024


def xml(data):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError('DTD declarations are not supported')
    return root


def words(text):
    return re.findall(r'[a-z0-9]+', api.label(text).lower())


def text_key(text):
    return ' '.join(words(text))


def build_dictionary(package: bytes, year: int):
    """Read only three known members; never extract ZIP paths to the filesystem."""
    if len(package) > MAX_PACKAGE:
        raise ValueError('Taxonomy package exceeds size limit')
    sources, docs = [], {}
    with ZipFile(io.BytesIO(package)) as archive:
        for name in (f'us-gaap-{year}.xsd', f'us-gaap-lab-{year}.xml', f'us-gaap-doc-{year}.xml'):
            suffix = '/elts/' + name
            matches = [n for n in archive.namelist() if n.endswith(suffix)]
            if len(matches) != 1:
                raise ValueError(f'Expected one official taxonomy file: {name}')
            if archive.getinfo(matches[0]).file_size > MAX_XML:
                raise ValueError('Taxonomy XML exceeds size limit')
            data = archive.read(matches[0])
            docs[name] = xml(data)
            sources.append({'url': f'https://xbrl.fasb.org/us-gaap/{year}/elts/{name}',
                            'sha256': hashlib.sha256(data).hexdigest(), 'zip_member': matches[0]})
    schema = docs[f'us-gaap-{year}.xsd']
    namespace = f'http://fasb.org/us-gaap/{year}'
    if schema.tag != f'{{{XS}}}schema' or schema.get('targetNamespace') != namespace:
        raise ValueError('US-GAAP schema namespace/version mismatch')
    concepts, ids = {}, {}
    for node in schema.findall(f'{{{XS}}}element'):
        name = node.get('name')
        if not name or not node.get('id'):
            continue
        ids[node.get('id')] = name
        concepts[name] = {
            'concept': 'us-gaap:' + name, 'namespace': namespace,
            'period_type': node.get(f'{{{XI}}}periodType'), 'data_type': node.get('type'),
            'balance': node.get(f'{{{XI}}}balance'), 'abstract': node.get('abstract') == 'true',
            'labels': [], 'definition': None,
        }
    for filename, root in docs.items():
        if filename.endswith('.xsd'):
            continue
        for link in root.findall(f'{{{LINK}}}labelLink'):
            locs = {}
            for node in link.findall(f'{{{LINK}}}loc'):
                href = node.get(f'{{{XL}}}href', '')
                file, _, fragment = href.partition('#')
                if file.rsplit('/', 1)[-1] == f'us-gaap-{year}.xsd' and fragment in ids:
                    locs[node.get(f'{{{XL}}}label')] = ids[fragment]
            resources = defaultdict(list)
            for node in link.findall(f'{{{LINK}}}label'):
                if node.get(LANG, '').lower() not in {'en', 'en-us'}:
                    continue
                resources[node.get(f'{{{XL}}}label')].append({
                    'text': ' '.join(''.join(node.itertext()).split()),
                    'role': node.get(f'{{{XL}}}role'), 'language': node.get(LANG),
                })
            for arc in link.findall(f'{{{LINK}}}labelArc'):
                if arc.get('use') == 'prohibited':
                    continue
                name = locs.get(arc.get(f'{{{XL}}}from'))
                if name is None:
                    continue
                for label in resources.get(arc.get(f'{{{XL}}}to'), []):
                    entry = concepts[name]
                    if label['role'] == ROLE + 'documentation':
                        entry['definition'] = label['text']
                    elif label not in entry['labels']:
                        entry['labels'].append(label)
    for entry in concepts.values():
        entry['label'] = next((l['text'] for l in entry['labels'] if l['role'] == ROLE + 'label'), None)
        entry['deprecated'] = any('deprecated' in l['text'].lower() for l in entry['labels'])
    if not concepts or not any(c['definition'] for c in concepts.values()):
        raise ValueError('Taxonomy labels/definitions are missing')
    return {
        'schema_version': 'official-metric-dictionary-1.0', 'builder_version': VERSION,
        'taxonomy': 'us-gaap', 'year': year, 'namespace': namespace,
        'package_url': f'https://xbrl.fasb.org/us-gaap/{year}/us-gaap-{year}.zip',
        'package_sha256': hashlib.sha256(package).hexdigest(),
        'sources': sources, 'concepts_sha256': api.digest(concepts), 'concepts': concepts,
    }


def load_dictionary(year, cache=Path('data/taxonomy_cache'), offline=False):
    if not isinstance(year, int) or not 2011 <= year <= 2200:
        raise ValueError('Unsupported US-GAAP taxonomy year')
    cache = Path(cache)
    package_path = cache / f'us-gaap-{year}.zip'
    index_path = cache / f'us-gaap-{year}-dictionary.json'
    if not package_path.exists():
        if offline:
            raise ValueError(f'Taxonomy cache missing: {package_path}. Run once without --offline.')
        url = f'https://xbrl.fasb.org/us-gaap/{year}/us-gaap-{year}.zip'
        request = Request(url, headers={'User-Agent': 'SEC-disclosure-metric-query/1.0'})
        with urlopen(request, timeout=60) as response:
            if not response.url.startswith('https://xbrl.fasb.org/'):
                raise ValueError('Unexpected taxonomy download host')
            package = response.read(MAX_PACKAGE + 1)
        result = build_dictionary(package, year)
        cache.mkdir(parents=True, exist_ok=True)
        # Do not save failed/partial downloads as valid cached packages.
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(dir=cache, delete=False) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(package)
        temp_path.replace(package_path)
        api.write_json(result, index_path)
        return result
    package = package_path.read_bytes()
    if index_path.exists():
        try:
            result = api.read_json(index_path.read_bytes())
            if (result.get('schema_version') == 'official-metric-dictionary-1.0'
                    and result.get('builder_version') == VERSION and result.get('year') == year
                    and result.get('namespace') == f'http://fasb.org/us-gaap/{year}'
                    and result.get('package_sha256') == hashlib.sha256(package).hexdigest()
                    and result.get('concepts_sha256') == api.digest(result.get('concepts'))):
                return result
        except (ValueError, TypeError, AttributeError):
            pass  # Rebuild an incomplete/corrupted index from the cached package.
    result = build_dictionary(package, year)
    api.write_json(result, index_path)
    return result


def metric_concept(entry):
    """Exclude table/axis/member/abstract/text concepts from numeric queries."""
    return (not entry['abstract'] and entry['period_type'] in {'instant', 'duration'}
            and bool(re.search(r'(?:monetary|shares|perShare|percent|decimal|integer|nonNegativeInteger|pure)ItemType$',
                               entry['data_type'] or '', re.I)))


def forms(entry):
    name = entry['concept'].split(':', 1)[1]
    yield text_key(name), 'concept_name', 100
    for label in entry['labels']:
        if label['role'] not in {ROLE + 'label', ROLE + 'terseLabel', ROLE + 'verboseLabel'}:
            continue
        text = label['text']
        yield text_key(text), 'official_label', 100
        shortened = re.sub(r'\([^)]*\)', '', text)
        if shortened != text:
            yield text_key(shortened), 'label_without_parenthetical', 95
            # E.g. NetIncomeLoss with label "Net Income (Loss) Attributable to
            # Parent" can be searched as "net income". This is a search
            # heuristic; it does not declare other profit concepts equivalent.
            name_words = text_key(name)
            for parenthetical in re.findall(r'\(([^)]*)\)', text):
                part = text_key(parenthetical)
                if part:
                    name_words = re.sub(r'(?<!\w)' + re.escape(part) + r'(?!\w)', '', name_words)
            yield ' '.join(name_words.split()), 'concept_without_label_parenthetical', 92


def search(dictionary, query, available=None, limit=12):
    """Rank concepts; definitions are discovery hints, never auto-selection."""
    query_key, query_words = text_key(query.removeprefix('us-gaap:')), set(words(query.removeprefix('us-gaap:')))
    if not query_words:
        raise ValueError('Provide a nonempty metric name')
    candidates = []
    for name, entry in dictionary['concepts'].items():
        if not metric_concept(entry) or available is not None and name not in available:
            continue
        score, basis, matched = 0, None, None
        for form, kind, exact_score in forms(entry):
            tokens = set(form.split())
            value = exact_score if form == query_key else (55 + 25 * len(query_words) / len(tokens)
                                                         if query_words <= tokens else 0)
            if value > score:
                score, basis, matched = value, kind if form == query_key else 'partial_' + kind, form
        definition = entry['definition'] or ''
        if query_words <= set(words(definition)) and score < 35:
            score, basis, matched = 35, 'definition_tokens', query_key
        if score:
            candidates.append({
                'concept': entry['concept'], 'label': entry['label'], 'definition': definition,
                'period_type': entry['period_type'], 'score': round(score, 2),
                'match_basis': basis, 'matched_text': matched, 'deprecated': entry['deprecated'],
            })
    return sorted(candidates, key=lambda c: (-c['score'], c['deprecated'], c['concept']))[:limit]
