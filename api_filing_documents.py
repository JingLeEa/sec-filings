"""Resolve explicitly incorporated Inline XBRL reports from the same accession.

Documents retain separate fact IDs, paths and layouts. Missing contexts/units
may be shared within the verified document set; ambiguous shared resources fail.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlparse

from lxml import html

import compare_html_tables as sec
import extract_10k_tables_xbrl as ix
import extract_item8_xbrl_api as api


def sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def unit_key(unit):
    return tuple(tuple(sorted(q['expanded_name'] for q in unit[key])) for key in ('numerator', 'denominator'))


def context_key(context):
    dimensions = []
    for d in context['dimensions']:
        dimensions.append((d['dimension']['expanded_name'], d['kind'], d['location'],
                           d['member']['expanded_name'] if d['kind'] == 'explicitMember' else d['typed_value_xml']))
    return api.digest({'entity': context['entity'], 'period': context['period'], 'dimensions': sorted(dimensions)})


def share_resources(documents):
    for attribute, key in [('contexts', context_key), ('units', unit_key)]:
        definitions = defaultdict(dict)
        for entry in documents.values():
            for identifier, value in getattr(entry['doc'], attribute).items():
                definitions[identifier][key(value)] = value
        for entry in documents.values():
            doc = entry['doc']
            resources = getattr(doc, attribute)
            needed = {n.get('contextRef' if attribute == 'contexts' else 'unitRef') for n in doc.fact_nodes} - {None}
            for identifier in needed - resources.keys():
                if identifier in doc.resource_errors:
                    raise ValueError(f'Invalid local XBRL resource {identifier}; cannot replace it from another document')
                alternatives = definitions.get(identifier, {})
                if len(alternatives) > 1:
                    raise ValueError(f'Ambiguous shared XBRL {attribute}: {identifier}')
                if alternatives:
                    resources[identifier] = next(iter(alternatives.values()))


def describe(documents):
    return {key: {'source': entry['doc'].source, 'sha256': sha(entry['doc'].data),
                  'sec_url': entry['url'], 'incorporated_sections': entry['layout'].ranges,
                  **({'incorporated_pages': entry['layout'].references}
                     if any(ref['method'] == 'external_report_pages' for ref in entry['layout'].references) else {}),
                  **({'report_discovery': entry['report_discovery']} if entry.get('report_discovery') else {})}
            for key, entry in documents.items()}


def filing_index_url(filing_url):
    api.sec_filing_url(filing_url)
    folder = filing_url.rsplit('/', 1)[0]
    accession = folder.rsplit('/', 1)[1]
    dashed = f'{accession[:10]}-{accession[10:12]}-{accession[12:]}'
    return f'{folder}/{dashed}-index.htm'


def report_from_document_list(data, filing_url):
    """Find the unique typed EX-13 on the selected accession's SEC index.

    Bind the index to the exact primary 10-K, resolve SEC viewer links to their
    original documents, and never infer the report filename from its spelling.
    """
    index_url = filing_index_url(filing_url)
    root = html.fromstring(data)
    rows = []
    for table in root.xpath('//table'):
        headers = [ix.clean(c.text_content()).lower() for c in table.xpath('./tr/th|./thead/tr/th|./tbody/tr/th')]
        if headers != ['seq', 'description', 'document', 'type', 'size']:
            continue
        for row in table.xpath('./tr|./tbody/tr'):
            cells = row.xpath('./td')
            if len(cells) != len(headers):
                continue
            kind = ix.clean(cells[3].text_content()).upper()
            if kind != '10-K' and not re.fullmatch(r'EX-13(?:\.\d+)?', kind):
                continue
            targets = set()
            for href in cells[2].xpath('.//a/@href'):
                target = urljoin(index_url, href)
                parsed = urlparse(target)
                if parsed.scheme == 'https' and parsed.netloc == 'www.sec.gov' and parsed.path == '/ix':
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if parsed.fragment or set(query) != {'doc'} or len(query['doc']) != 1:
                        raise ValueError('Invalid SEC document viewer link in the filing document list.')
                    target = urljoin('https://www.sec.gov/', query['doc'][0])
                target = api.sec_filing_url(target)
                if target.rsplit('/', 1)[0] != filing_url.rsplit('/', 1)[0]:
                    raise ValueError('Filing document list points outside the selected SEC accession.')
                targets.add(target)
            if len(targets) != 1:
                raise ValueError('Filing document list has a missing or ambiguous original HTML document link.')
            rows.append({'type': kind, 'url': targets.pop(), 'source_locator': root.getroottree().getpath(row)})
    primary = [row for row in rows if row['type'] == '10-K']
    if len(primary) != 1 or primary[0]['url'] != filing_url:
        raise ValueError('SEC filing document list does not identify the selected original 10-K.')
    reports = [row for row in rows if row['type'] != '10-K']
    if len(reports) != 1 or reports[0]['url'] == filing_url:
        raise ValueError('SEC filing document list must contain exactly one distinct HTML EX-13 report.')
    report = reports[0]
    return report['url'], {'method': 'sec_filing_document_list', 'index_url': index_url,
                           'index_sha256': sha(data), 'document_type': report['type'],
                           'source_locator': report['source_locator']}


def resolve_documents(data, source, parameters, items, loader=None, *, boundary_items=()):
    doc = ix.Document(data, source)
    layout = ix.Layout(doc)
    layout.validate(items)
    result = {'primary': {'doc': doc, 'layout': layout, 'url': parameters.get('htm-url')}}
    references = [ref for ref in layout.external if ref['item'] in items]
    # A selected section may need a neighbouring Item's explicit statement
    # index to prove its end. Resolve that evidence without exporting the
    # neighbouring Item's membership. Existing callers retain their scope.
    boundary_references = ([ref for ref in layout.external
                            if ref['item'] in boundary_items and ref['item'] not in items]
                           if references else [])
    needs_resources = any(n.get('contextRef') and n.get('contextRef') not in doc.contexts
                          or n.get('unitRef') and n.get('unitRef') not in doc.units for n in doc.fact_nodes)
    if not references and not (needs_resources and layout.external):
        return result
    if loader is None:
        names = ', '.join(sorted({ref['item'] for ref in references or layout.external}))
        raise ValueError(f'Items {names} incorporate an external report. Supply a verified report loader.')
    if 'htm-url' not in parameters:
        raise ValueError('External reports require --filing-url to verify their same-accession source.')
    # Resolve from the SEC URL, not the cache's renamed original_filing.htm.
    original_source = doc.source
    try:
        doc.source = api.sec_filing_url(parameters['htm-url'])
        target = ix.referenced_report_source(doc, allow_missing=True)
    finally:
        doc.source = original_source
    discovery = None
    if target is None:
        discover = getattr(loader, 'discover_report', None)
        if not callable(discover):
            raise ValueError('No linked HTML Exhibit 13; a SEC filing document-list loader is required.')
        target, discovery = discover(parameters['htm-url'])
    target = api.sec_filing_url(target)
    if target.rsplit('/', 1)[0] != parameters['htm-url'].rsplit('/', 1)[0] or target == parameters['htm-url']:
        raise ValueError('Incorporated report must be a distinct HTML document in the same SEC accession.')
    for ref in references + boundary_references:
        for entry in ref.get('linked_statement_index', []):
            linked = urlparse(urljoin(parameters['htm-url'], entry['href']))
            if linked._replace(fragment='').geturl() != target or linked.fragment != entry['anchor']:
                raise ValueError('Item 15 statement link does not belong to the verified Exhibit 13 report')
    report_data, report_source = loader(target, Path(source).parent / Path(urlparse(target).path).name)
    report = ix.Document(report_data, str(report_source))
    report_layout = ix.Layout(report, resolve_references=False)
    # Item membership comes only from verified incorporated sections/pages.
    report_layout.headings, report_layout.references, report_layout.ranges = [], [], []
    if references:
        ix.attach_report_ranges(report_layout, references + boundary_references)
    result['report1'] = {'doc': report, 'layout': report_layout, 'url': target}
    if boundary_references:
        result['report1']['boundary_evidence'] = {
            'items': sorted({ref['item'] for ref in boundary_references}),
            'ranges': [r for r in report_layout.ranges if r['item'] not in items],
            'references': [r for r in report_layout.references if r['item'] not in items]}
        report_layout.ranges = [r for r in report_layout.ranges if r['item'] in items]
        report_layout.references = [r for r in report_layout.references if r['item'] in items]
    if discovery:
        result['report1']['report_discovery'] = discovery
    for ref in references:
        for internal in ref.get('internal_item_references', []):
            headings = [h for h in layout.headings if h['item'] == internal]
            if len(headings) != 1:
                raise ValueError(f'Cannot resolve incorporated internal Item {internal}')
            start, end, _ = layout.body(headings[0])
            for item in (internal, ref['item']):
                layout.ranges.append({'item': item, 'start': start, 'end': end,
                                      'method': 'internal_item_reference', 'evidence': ref['reference_text']})
    share_resources(result)
    return result


class ReportLoader:
    """Cache report bytes and their verified SEC URL/hash, outside output folders."""
    def __init__(self, cache, user_agent='', offline=False):
        self.cache, self.user_agent, self.offline = Path(cache), user_agent, offline
        self.client = None

    def discover_report(self, filing_url):
        index_url = filing_index_url(filing_url)
        cached = self.cache / (sha(index_url.encode()) + '.html')
        if cached.is_file():
            data = cached.read_bytes()
        else:
            if self.offline:
                raise ValueError(f'SEC filing document list is not cached: {index_url}. Run once without --offline.')
            if self.client is None:
                self.client = sec.SecClient(self.user_agent, self.cache)
            print(f'Loading SEC filing document list: {index_url}', flush=True)
            data = self.client.get(index_url)
        return report_from_document_list(data, filing_url)

    def __call__(self, url, adjacent):
        api.sec_filing_url(url)
        folder = self.cache / 'incorporated_reports' / api.digest(url)
        cached = folder / 'original_report.htm'
        for path in dict.fromkeys((cached, Path(adjacent))):
            if not path.is_file():
                continue
            sidecars = [path.with_name(path.stem + '-source.json'), path.with_name(path.name + '.source.json')]
            for sidecar in sidecars:
                if not sidecar.is_file():
                    continue
                metadata = api.read_json(sidecar.read_bytes())
                data = path.read_bytes()
                if (metadata.get('original_sec_url') or metadata.get('sec_url')) != url or metadata.get('sha256') != sha(data):
                    raise ValueError(f'Incorporated report provenance mismatch: {path}')
                return data, str(path)
            if path == cached:
                raise ValueError(f'Incorporated report cache has no source metadata: {path}')
        if self.offline:
            raise ValueError(f'Incorporated report is not cached: {url}. Run once without --offline.')
        if self.client is None:
            self.client = sec.SecClient(self.user_agent, self.cache)
        print(f'Loading incorporated report: {url}', flush=True)
        data = self.client.get(url)
        ix.Document(data, url)  # Reject converted HTML/API error pages before caching.
        folder.mkdir(parents=True, exist_ok=True)
        with cached.open('xb') as stream:
            stream.write(data)
        api.write_json({'sec_url': url, 'sha256': sha(data)}, cached.with_name('original_report-source.json'))
        return data, str(cached)
