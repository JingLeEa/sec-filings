#!/usr/bin/env python3
r"""Check which Items contain the original sec-api.io records.

Reads the saved API JSON and the SAME filing's original Inline XBRL XHTML.
The filing supplies section/tag locations, never replacement output values.
Writes a separate annotation report; neither extraction output is modified.

python3 check_item8_api.py \
  data/table_output/intel_2023_items_7_8_hybrid_tables/sec_api_xbrl.json \
  --filing data/source_cache/intc-20231230.htm

Add --items 1 1A 7 8 to check all four Items in one report. Omitting --items
preserves the Item 8 report used by the existing metric-export pipeline.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import extract_item8_xbrl_api as api
import extract_10k_tables_xbrl as ix
import api_filing_documents as filing_docs
import api_filing_metadata as metadata

VERSION = '1.11.0'
SUPPORTED_ITEMS = ('1', '1A', '7', '8')
STATUS_HELP = {
    'item_8': 'All entries have Item 8 location evidence; they may also be referenced by other Items.',
    'partial': 'Some entries have Item 8 evidence; other entries or source locations are unresolved.',
    'mixed': 'Contains both Item 8 content and content located only outside Item 8.',
    'outside_item_8': 'All entries have known locations outside Item 8.',
    'unresolved': 'Insufficient evidence to establish membership for the complete group/entry.',
    'excluded_metadata': 'Provider cover-page/audit metadata, not a financial table group.',
}
MULTI_STATUS_HELP = {
    'selected_items': 'All entries have evidence in the requested Items; this does not mean every entry belongs to every Item.',
    'partial': 'Some requested-Item evidence exists, but other entries or locations are unresolved.',
    'mixed': 'Contains requested-Item content and content located only outside the requested Items.',
    'outside_selected_items': 'All entries have known locations outside the requested Items.',
    'unresolved': STATUS_HELP['unresolved'],
    'excluded_metadata': STATUS_HELP['excluded_metadata'],
}
ITEM_STATUS_HELP = {
    'in_item': 'All entries have evidence in this Item; exact facts may also appear in other Items.',
    'outside_item': 'All entries have known locations outside this Item.',
    'mixed': 'Contains content in this Item and content located only outside it.',
    'partial': 'Some evidence exists in this Item, but other entries or locations are unresolved.',
    'unresolved': STATUS_HELP['unresolved'],
    'excluded_metadata': STATUS_HELP['excluded_metadata'],
}


def normalize_items(items):
    items = tuple(dict.fromkeys(str(item).upper() for item in items))
    if not items or any(item not in SUPPORTED_ITEMS for item in items):
        raise ValueError('Select one or more Items from 1, 1A, 7, 8')
    return items


def sha(data):
    return hashlib.sha256(data).hexdigest()


def combine(statuses, inside='item_8', outside='outside_item_8'):
    statuses = set(statuses)
    if 'mixed' in statuses or (inside in statuses and outside in statuses):
        return 'mixed'
    if 'partial' in statuses or (inside in statuses and 'unresolved' in statuses):
        return 'partial'
    return next(iter(statuses)) if len(statuses) == 1 else 'unresolved'


def location_status(locations, items, error=None, hidden_numeric=False,
                    inside='in_item', outside='outside_item'):
    statuses = [inside if set(items).intersection(p['referenced_items']) else
                outside if p['referenced_items'] else 'unresolved' for p in locations]
    if error:
        statuses.append('unresolved')
    # Several visible references to a hidden numeric fact are occurrences of
    # one value, rather than a text block spanning multiple Items.
    if hidden_numeric and not error and inside in statuses:
        return inside
    return combine(statuses, inside, outside)


def occurrence_status(statuses, exact_fact, inside='in_item', outside='outside_item'):
    statuses = list(statuses)
    # One complete exact occurrence proves inclusion, not exclusivity. Text
    # blocks/contextless concepts still require their full occurrence set.
    return inside if exact_fact and inside in statuses else combine(statuses, inside, outside)


def entries(payload, prefix):
    """Yield pointers into the input file, including singleton and text records."""
    for group, concepts in payload.items():
        if not isinstance(concepts, dict):
            pairs = [(group, concepts, prefix + api.pointer(group))]
        else:
            pairs = [(concept, raw, prefix + api.pointer(group, concept)) for concept, raw in concepts.items()]
        for concept, raw, base in pairs:
            if isinstance(raw, list):
                if not raw:
                    yield group, concept, base, raw
                for i, record in enumerate(raw):
                    yield group, concept, base + '/' + str(i), record
            else:
                yield group, concept, base, raw


def period_key(period):
    return api.digest({k: v for k, v in period.items() if k != 'type'})


def matching_record(raw):
    """Adapt the provider's alternate explicit-member syntax on a copy only.

    Intel uses {dimension, value}; Micron also uses the XML-shaped form
    {explicitMember: {dimension, $t}} (or an array of those members).
    Preserve all dimensions, and reject unknown/mixed forms rather than
    dropping them and incorrectly matching an entity-wide fact.
    """
    if not isinstance(raw, dict):
        return raw
    segment = raw.get('segment')
    if not isinstance(segment, dict) or 'explicitMember' not in segment:
        return raw
    if set(segment) != {'explicitMember'}:
        raise ValueError('Unsupported mixed API dimension representation')
    members = segment['explicitMember']
    members = members if isinstance(members, list) else [members]
    if not members:
        raise ValueError('Empty API explicitMember representation')
    dimensions = []
    for member in members:
        if not isinstance(member, dict) or set(member) != {'dimension', '$t'}:
            raise ValueError('Unsupported API explicitMember representation')
        dimensions.append({'dimension': member['dimension'], 'value': member['$t']})
    return {**raw, 'segment': dimensions}


class LocationIndex:
    """Index XBRL occurrences, without extracting HTML table cells or grids."""
    def __init__(self, data, source, items=('8',), documents=None):
        self.items = normalize_items(items)
        self.legacy = self.items == ('8',)
        self.inside = 'item_8' if self.legacy else 'selected_items'
        self.outside = 'outside_item_8' if self.legacy else 'outside_selected_items'
        self.documents = documents or filing_docs.resolve_documents(data, source, {}, self.items)
        self.doc = self.documents['primary']['doc']
        self.layout = self.documents['primary']['layout']
        if self.doc.data != data or self.doc.source != source:
            raise ValueError('Resolved document set belongs to a different primary filing')
        self.owners = {node: identifier for identifier, entry in self.documents.items() for node in entry['doc'].nodes}
        self.by_name = defaultdict(list)
        self.namespaces = defaultdict(set)
        self.locations = {}
        self.signatures = {}
        self.hidden_references = defaultdict(list)
        for identifier, entry in self.documents.items():
            doc = entry['doc']
            for node in doc.nodes:
                if doc.hidden[node]:
                    continue
                for match in re.finditer(r'(?:^|;)\s*-sec-ix-hidden\s*:\s*([^;\s]+)', node.get('style', '')):
                    self.hidden_references[(identifier, match[1])].append(node)
            for node in doc.fact_nodes:
                try:
                    name = ix.qname(node, node.get('name'))
                except ValueError:
                    continue
                self.by_name[name['local_name']].append(node)
                self.namespaces[name['local_name']].add(name['namespace'])

    def candidates(self, concept):
        if not isinstance(concept, str):
            return [], 'API group is not a concept object'
        # Bare names are common in this API. Never silently choose a namespace
        # when a custom and standard concept have the same local name.
        if ':' in concept:
            prefix, local = concept.split(':', 1)
            uris = {entry['doc'].root.nsmap.get(prefix) for entry in self.documents.values()} - {None}
            if len(uris) != 1:
                return [], 'API concept prefix cannot be resolved in this filing'
            uri = next(iter(uris))
            return [n for n in self.by_name[local] if ix.qname(n, n.get('name'))['namespace'] == uri], None
        if len(self.namespaces[concept]) > 1:
            return [], 'API stripped a concept namespace that is ambiguous in this filing'
        return self.by_name[concept], None

    def dimension_key(self, node, dimensions, from_api=False):
        values = []
        if from_api:
            for d in dimensions:
                values.append((ix.qname(node, d['axis'])['expanded_name'],
                               ix.qname(node, d['member'])['expanded_name']))
        else:
            for d in dimensions:
                if d['kind'] != 'explicitMember':
                    raise ValueError('Typed dimensions are not matched by this checker')
                values.append((d['dimension']['expanded_name'], d['member']['expanded_name']))
        return tuple(sorted(values))

    def signature(self, node):
        if node in self.signatures:
            return self.signatures[node]
        doc = self.documents[self.owners[node]]['doc']
        fact = doc.fact(node)
        result = None
        if fact['status'] in {'ok', 'nil'}:
            context = doc.contexts[fact['context_ref']]
            try:
                result = (period_key(context['period']),
                          self.dimension_key(node, context['dimensions']), fact['unit_ref'],
                          'nil' if fact['nil'] else 'value', fact['value'])
            except ValueError:
                pass
        self.signatures[node] = result
        return result

    def location(self, node):
        if node in self.locations:
            return self.locations[node]
        document_id = self.owners[node]
        doc, layout = (self.documents[document_id][key] for key in ('doc', 'layout'))
        starts, seen, current = [], set(), node
        error = None
        while current is not None:
            if current in seen:
                error = 'Cyclic Inline XBRL continuation'
                break
            seen.add(current)
            if doc.hidden[current]:
                starts.extend(self.hidden_references.get((document_id, current.get('id')), []))
            else:
                starts.append(current)
            ref = current.get('continuedAt')
            if not ref:
                break
            current = doc.ids.get(ref)
            if current is None or ix.namespace(current) not in ix.IX or ix.local(current) != 'continuation':
                error = 'Missing or invalid Inline XBRL continuation'
                break
        points = []
        for start in starts:
            # Text blocks can wrap several pages or continue in another Item.
            # Inspect all visible content positions, not just the opening tag.
            content = [n for n in start.iter() if isinstance(n.tag, str) and not doc.hidden[n]
                       and ix.clean(n.text or '') and ix.local(n) != 'exclude'
                       and not any(ix.namespace(a) in ix.IX and ix.local(a) == 'exclude'
                                   for a in n.iterancestors())]
            points.extend(content or [start])
        evidence = {}
        for point in points:
            page, physical, items = layout.membership(point)
            key = (page, physical, tuple(items))
            evidence.setdefault(key, {'page': page, 'physical_item': physical, 'referenced_items': items,
                                      'source_locator': doc.paths[point]})
        hidden_numeric = doc.hidden[node] and ix.local(node) == 'nonFraction'
        status = location_status(evidence.values(), self.items, error, hidden_numeric, self.inside, self.outside)
        result = {'source_fact_id': node.get('id'), 'source_locator': doc.paths[node],
                  'concept': ix.qname(node, node.get('name'))['expanded_name'],
                  'status': status, 'locations': list(evidence.values())}
        if len(self.documents) > 1:
            result.update(document_id=document_id, source=doc.source)
            for point in result['locations']:
                point.update(document_id=document_id, source=doc.source)
        if not self.legacy:
            result['item_membership'] = {item: location_status(evidence.values(), (item,), error, hidden_numeric)
                                         for item in self.items}
        if error or not points:
            result['issue'] = error or 'Hidden fact has no explicit visible reference'
        elif any(not p['referenced_items'] for p in evidence.values()):
            result['issue'] = 'A visible content location has no verified Item membership'
        self.locations[node] = result
        return result

    def check(self, concept, raw, pointer, standalone=False):
        block = api.is_text_block(concept or '', raw)
        record = {'api_pointer': pointer, 'concept': concept,
                  'kind': 'text_block' if block else 'fact', 'status': 'unresolved',
                  'match_basis': None, 'value_checked': False, 'evidence': [], 'issues': []}
        if not self.legacy:
            record.update(item_membership={item: 'unresolved' for item in self.items}, matched_items=[])
        candidates, issue = self.candidates(concept)
        if issue or not candidates:
            record['issues'].append(issue or 'Concept not found in the original filing')
            return record
        concept_only = block or (standalone and not isinstance(raw, dict))
        if concept_only:
            # API text blocks frequently omit the context. Classify the tagged
            # concept's complete occurrence set, without parsing HTML values.
            selected = candidates
            record['match_basis'] = ('all_source_occurrences_of_text_block_concept' if block
                                     else 'all_source_occurrences_of_standalone_concept')
        else:
            try:
                normalized = api.normalise_fact(matching_record(raw), pointer)
            except ValueError as exc:
                record['issues'].append(str(exc))
                return record
            if normalized['status'] in {'invalid', 'incomplete'}:
                record['issues'].extend(normalized['issues'])
                return record
            selected = []
            for node in candidates:
                try:
                    signature = (period_key(normalized['period']),
                                 self.dimension_key(node, normalized['dimensions'], from_api=True),
                                 normalized['unit_ref'],
                                 'nil' if normalized['value_type'] == 'nil' else 'value', normalized['value'])
                    if self.signature(node) == signature:
                        # Distinguish precision/rounding variants of one fact.
                        if all(normalized[k] is None or str(normalized[k]) == node.get(k)
                               for k in ('decimals', 'precision')):
                            selected.append(node)
                except ValueError:
                    continue
            record['match_basis'] = 'concept_period_dimensions_unit_value_accuracy'
            if not selected:
                record['issues'].append('No exact supported XBRL occurrence matches this API record')
                facts = [self.documents[self.owners[n]]['doc'].fact(n) for n in candidates]
                errors = {fact.get('error') for fact in facts if fact['status'] == 'error'}
                record['issues'].extend(sorted(e for e in errors if e))
                return record
            record['value_checked'] = True
        record['evidence'] = [self.location(n) for n in selected]
        statuses = [e['status'] for e in record['evidence']]
        record['status'] = occurrence_status(statuses, not concept_only, self.inside, self.outside)
        outside_key = 'also_found_outside_item_8' if self.legacy else 'also_found_outside_selected_items'
        record[outside_key] = any(p['referenced_items'] and not set(self.items).intersection(p['referenced_items'])
                                  for e in record['evidence'] for p in e['locations'])
        record['also_referenced_by_items'] = sorted({i for e in record['evidence'] for p in e['locations']
                                                    for i in p['referenced_items'] if i not in self.items})
        if not self.legacy:
            record['item_membership'] = {
                item: occurrence_status((e['item_membership'][item] for e in record['evidence']), not concept_only)
                for item in self.items}
            record['matched_items'] = [item for item in self.items
                                       if any(item in p['referenced_items'] for e in record['evidence']
                                              for p in e['locations'])]
        record['issues'].extend(sorted({e['issue'] for e in record['evidence'] if 'issue' in e}))
        return record


def check_response(payload, data, source, parameters, year, prefix='/response', *, items=('8',), documents=None):
    api.verify_identity(payload, year, parameters)
    identity = metadata.resolve(payload, data, parameters, year,
                                required=('DocumentFiscalYearFocus', 'DocumentType'))
    index = LocationIndex(data, source, items, documents)
    # A same-company different-year filing must not supply location evidence.
    for entry in index.documents.values():
        doc = entry['doc']
        for node in doc.fact_nodes:
            if node.get('name', '').split(':')[-1] == 'DocumentFiscalYearFocus':
                qn = ix.qname(node, node.get('name'))
                if qn['namespace'].startswith('http://xbrl.sec.gov/dei/'):
                    if ix.clean(doc.content(node)) != str(year):
                        raise ValueError('Original filing fiscal year differs from the API response')
    expected_ciks = {identity['values']['EntityCentralIndexKey']} if 'EntityCentralIndexKey' in identity['values'] else set()
    if expected_ciks:
        expected_ciks = {c.lstrip('0') or '0' for c in expected_ciks}
        reported = {c['entity']['identifier'].lstrip('0') or '0' for entry in index.documents.values()
                    for c in entry['doc'].contexts.values()
                    if c['entity']['scheme'] in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}}
        if not reported or not reported <= expected_ciks:
            raise ValueError('Original filing XBRL entities differ from API CoverPage CIK')
    groups = {name: {'api_group': name, 'api_pointer': prefix + api.pointer(name),
                     'status': 'unresolved', 'entries': []} for name in payload}
    evidence_key = 'has_item_8_evidence' if index.legacy else 'has_selected_item_evidence'
    for group, concept, pointer, raw in entries(payload, prefix):
        if group in api.EXCLUDED_GROUPS:
            groups[group]['status'] = 'excluded_metadata'
            continue
        groups[group]['entries'].append(index.check(concept, raw, pointer, not isinstance(payload[group], dict)))
    for group in groups.values():
        if group['api_group'] in api.EXCLUDED_GROUPS:
            group['status'] = 'excluded_metadata'
        else:
            group['status'] = combine((e['status'] for e in group['entries']), index.inside, index.outside)
        group['entry_counts'] = dict(Counter(e['status'] for e in group['entries']))
        name = group['api_group']
        kinds = {e['kind'] for e in group['entries']}
        group['content_kind'] = ('metadata' if name in api.EXCLUDED_GROUPS else
                                 'financial_statement' if name in api.STATEMENTS else
                                 'statement_parenthetical' if name.removesuffix('Parenthetical') in api.STATEMENTS else
                                 'text_block_group' if kinds == {'text_block'} else 'disclosure_group')
        group[evidence_key] = any(e['status'] in {index.inside, 'partial', 'mixed'} for e in group['entries'])
        if not index.legacy:
            group['item_membership'] = {
                item: 'excluded_metadata' if name in api.EXCLUDED_GROUPS else
                combine((e['item_membership'][item] for e in group['entries']), 'in_item', 'outside_item')
                for item in index.items}
            group['matched_items'] = [item for item in index.items
                                      if any(item in e['matched_items'] for e in group['entries'])]
        group['also_referenced_by_items'] = sorted({i for e in group['entries']
                                                   for i in e.get('also_referenced_by_items', [])})
    all_entries = [e for g in groups.values() for e in g['entries']]
    api_groups = [g for g in groups.values() if isinstance(payload[g['api_group']], dict)]
    standalone = [{**{k: v for k, v in g.items() if k != 'api_group'}, 'concept': g['api_group']}
                  for g in groups.values() if not isinstance(payload[g['api_group']], dict)]
    layout = index.layout
    result = {
        'schema_version': 'api-item-membership-1.1', 'checker_version': VERSION, 'item': '8', 'year': year,
        'method': 'original_api_records_checked_against_filing_xbrl_locations',
        'status_definitions': STATUS_HELP,
        'limitations': [
            'API groups are not necessarily individual printed tables. This report checks content membership, not table boundaries.',
            'Text blocks are checked using ALL occurrences of their tagged concept; API text-block values are not parsed or verified.',
            'Standalone contextless concepts are checked using ALL source occurrences; their values are not independently verified.',
            'Unmatched/unsupported records remain unresolved. Missing matches do not prove exclusion from Item 8.',
            'Exact numeric matches use period, dimensions, unitRef, value and supplied accuracy; values are never replaced.',
            'Section membership follows detected headings and explicit references; referenced pages may belong to multiple Items.',
        ],
        'source': {'request': parameters, 'response_sha256': api.digest(payload),
                   'filing': source, 'filing_sha256': sha(data)},
        'item_8_scope': {'headings': [h for h in layout.headings if h['item'] == '8'],
                         'page_references': [r for r in layout.references if r['item'] == '8'],
                         'incorporated_sections': [r for r in layout.ranges if r['item'] == '8']},
        'summary': {'top_level_entries': len(groups), 'groups': len(api_groups),
                    'group_status': dict(Counter(g['status'] for g in api_groups)),
                    'standalone_concepts': len(standalone),
                    'standalone_concept_status': dict(Counter(g['status'] for g in standalone)),
                    'checked_entries': len(all_entries), 'entry_status': dict(Counter(e['status'] for e in all_entries)),
                    'item_8_groups': [g['api_group'] for g in api_groups if g['status'] == index.inside],
                    'groups_with_item_8_evidence': [g['api_group'] for g in api_groups if g[evidence_key]],
                    'groups_requiring_review': [g['api_group'] for g in api_groups
                                               if g['status'] in {'mixed', 'partial', 'unresolved'}]},
        'groups': api_groups, 'standalone_concepts': standalone,
    }
    if not index.legacy:
        result.update(schema_version='api-items-membership-1.0', requested_items=list(index.items),
                      status_definitions=MULTI_STATUS_HELP, item_status_definitions=ITEM_STATUS_HELP)
        result.pop('item')
        result.pop('item_8_scope')
        result['limitations'][3] = 'Unmatched/unsupported records remain unresolved. Missing matches do not prove exclusion from the requested Items.'
        result['limitations'].extend([
            'Only records present in the API response are checked. Untagged prose/tables and image-only content are not extracted.',
            'Per-Item counts overlap when the same record belongs to multiple Items; do not add them to count unique entries.',
            'matched_items records any location evidence; use item_membership to distinguish complete, mixed and partial evidence.',
        ])
        result['item_scopes'] = {
            item: {'headings': [h for h in layout.headings if h['item'] == item],
                   'page_references': [r for r in layout.references if r['item'] == item],
                   'incorporated_sections': [r for r in layout.ranges if r['item'] == item],
                   'image_only_pages': [g for g in layout.image_gaps if any(
                       r['item'] == item and int(g['inferred_page']) in r['pages'] for r in layout.references)]}
            for item in index.items}
        summary = result['summary']
        summary['selected_item_groups'] = summary.pop('item_8_groups')
        summary['groups_with_selected_item_evidence'] = summary.pop('groups_with_item_8_evidence')
        summary['by_item'] = {
            item: {'entry_status': dict(Counter(e['item_membership'][item] for e in all_entries)),
                   'entries_with_evidence': sum(item in e['matched_items'] for e in all_entries),
                   'group_status': dict(Counter(g['item_membership'][item] for g in api_groups)),
                   'standalone_concept_status': dict(Counter(g['item_membership'][item] for g in standalone))}
            for item in index.items}
    if len(index.documents) > 1:
        result['source']['documents'] = filing_docs.describe(index.documents)
        scopes = result.get('item_scopes', {'8': result.get('item_8_scope')})
        for item, scope in scopes.items():
            scope['page_references'] = [dict(reference, document_id=identifier)
                for identifier, entry in index.documents.items() for reference in entry['layout'].references
                if reference['item'] == item]
            scope['incorporated_sections'] = [dict(section, document_id=identifier)
                for identifier, entry in index.documents.items() for section in entry['layout'].ranges
                if section['item'] == item]
    if identity['evidence']:
        result['source']['identity_metadata'] = identity['evidence']
    return result


def bind_filing(saved, path, data, parameters):
    """Require evidence that the local file belongs to this exact API filing."""
    expected = saved.get('source_sha256') if isinstance(saved, dict) and 'response' in saved else None
    if expected:
        if expected != sha(data):
            raise ValueError('Original filing hash differs from the API cache source_sha256')
        return 'api_cache_source_sha256'
    sidecar = path.with_name(path.stem + '-source.json')
    if sidecar.is_file():
        metadata = api.read_json(sidecar.read_bytes())
        if metadata.get('sha256') != sha(data):
            raise ValueError('Original filing hash differs from its source sidecar')
        other = api.request_parameters(filing_url=metadata.get('original_sec_url') or metadata.get('sec_url'))
        if api.request_accession(other) != api.request_accession(parameters):
            raise ValueError('Original filing accession differs from the API response')
        if 'htm-url' in parameters and other != parameters:
            raise ValueError('Original filing URL differs from the API response')
        return 'filing_source_sidecar'
    raise ValueError('Cannot bind this local filing to the API response. Use the API cache with source_sha256, '
                     'or the original filing with its matching *-source.json provenance sidecar.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('xbrl_json', type=Path, help='Original API response or sec_api_xbrl.json cache')
    parser.add_argument('--filing', type=Path, required=True, help='Same filing, original Inline XBRL XHTML')
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument('--filing-url', help='Provenance for raw API JSON')
    identity.add_argument('--accession', help='Provenance for raw API JSON')
    parser.add_argument('--year', type=int, help='Defaults to verified API/filing DocumentFiscalYearFocus')
    parser.add_argument('--items', nargs='+', type=str.upper, choices=SUPPORTED_ITEMS, default=['8'],
                        help='Items to verify, e.g. --items 1 1A 7 8. Default: 8 (legacy report).')
    parser.add_argument('--output', type=Path, help='Defaults to an Item-specific membership report beside input')
    parser.add_argument('--sec-cache', type=Path, default=Path('data/sec_cache'))
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''))
    parser.add_argument('--offline', action='store_true', help='Require incorporated reports to be cached')
    args = parser.parse_args(argv)
    try:
        original = args.xbrl_json.read_bytes()
        saved = api.read_json(original)
        parameters = api.request_parameters(args.filing_url, args.accession) if args.filing_url or args.accession else None
        payload, parameters = api.load_response(args.xbrl_json, parameters)
        cached = isinstance(saved, dict) and saved.get('schema_version') in {'sec-api-cache-1.0', 'sec-api-cache-2.0'}
        data = args.filing.read_bytes()
        binding = bind_filing(saved, args.filing, data, parameters)
        identity = metadata.resolve(payload, data, parameters, args.year,
                                    required=('DocumentFiscalYearFocus', 'DocumentType'))
        year = int(identity['values']['DocumentFiscalYearFocus'])
        if not 1900 <= year <= 2200:
            raise ValueError('Use a four-digit fiscal year')
        items = normalize_items(args.items)
        filename = ('item_8_membership_check.json' if items == ('8',) else
                    'items_' + '_'.join(item.lower() for item in items) + '_membership_check.json')
        output = args.output or args.xbrl_json.with_name(filename)
        protected = {args.xbrl_json.resolve(), args.filing.resolve()}
        protected.add(args.filing.with_name(args.filing.stem + '-source.json').resolve())
        protected.update((args.xbrl_json.parent / n).resolve() for n in
                         ('item_7_html.json', 'item_8_xbrl.json', 'result_hybrid.json', 'item_8_named_api.json',
                          'available_item8_metrics.json', 'item_8_metrics_with_values.json'))
        if items != ('8',):
            protected.add(args.xbrl_json.with_name('item_8_membership_check.json').resolve())
        if output.resolve() in protected:
            raise ValueError('--output must be a separate report, not an existing extraction input/output')
        print(f'Checking API groups against Items {", ".join(items)} section and XBRL-tag locations...', flush=True)
        documents = filing_docs.resolve_documents(data, str(args.filing), parameters, items,
            filing_docs.ReportLoader(args.sec_cache.expanduser(), args.user_agent, args.offline))
        if any(output.resolve() == Path(entry['doc'].source).resolve() for entry in documents.values()):
            raise ValueError('--output must not replace an incorporated report')
        result = check_response(payload, data, str(args.filing), parameters, year, '/response' if cached else '',
                                items=items, documents=documents)
        result['source'].update(api_file=str(args.xbrl_json), api_file_sha256=sha(original), filing_binding=binding)
        if args.xbrl_json.read_bytes() != original or any(
                Path(entry['doc'].source).read_bytes() != entry['doc'].data for entry in documents.values()):
            raise ValueError('API response or filing documents changed during verification')
        api.write_json(result, output)
        print(f'Wrote {output}')
        print(json.dumps({k: v for k, v in result['summary'].items() if not isinstance(v, list)}, indent=2))
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
