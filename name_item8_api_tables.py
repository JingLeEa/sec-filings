#!/usr/bin/env python3
r"""Associate original sec-api.io groups with Item 8 filing table titles.

Values remain in their original API groups under groups[api_key].data. Filing
XHTML supplies only table/title/location metadata. A group may map to multiple
tables, or contain facts reported in prose rather than in a table.

python3 name_item8_api_tables.py \
  data/table_output/micron_2025_item_8_api_check/sec_api_xbrl.json \
  --filing data/source_cache/micron_2025_xbrl/mu-20250828.htm
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

import check_item8_api as membership
import extract_item8_xbrl_api as api
import extract_10k_tables_xbrl as ix

VERSION = '1.0.0'
NOTE = re.compile(r'^Note\s+\d+[A-Za-z]?\s*[.:–—-]\s*\S', re.I)


def noise(text):
    """Page furniture and unit/period labels are not titles."""
    return (not text or text.lower() == 'table of contents' or ix.ITEM_RE.match(text)
            or re.fullmatch(r'\d+(?:\s*\|.*)?', text)
            or re.fullmatch(r'\(?\s*(?:(?:all tabular amounts|amounts|dollars|\$)\s+)?in '
                            r'(?:millions|thousands|billions)\b[^.!?]*\)?', text, re.I)
            or re.match(r'^(?:years? ended|as of|quarter ended)\b', text, re.I)
            or re.fullmatch(r'(?:January|February|March|April|May|June|July|August|September|October|November|December|'
                            r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4}', text, re.I)
            or re.fullmatch(r'See accompanying notes\.?', text, re.I))


class TitleIndex:
    def __init__(self, data, source):
        self.doc = ix.Document(data, source)
        self.layout = ix.Layout(self.doc)
        self.paths = {path: node for node, path in self.doc.paths.items()}
        self.table_cache, self.title_cache = {}, {}
        self.hidden_references = defaultdict(list)
        self.data_tables = []
        for node in self.doc.nodes:
            if not self.doc.hidden[node]:
                for m in re.finditer(r'(?:^|;)\s*-sec-ix-hidden\s*:\s*([^;\s]+)', node.get('style', '')):
                    self.hidden_references[m[1]].append(node)
        # Use the existing financial-table classifier only to reject layout and
        # footnote tables. Cells and HTML-derived values are never exported.
        for table in self.doc.tables:
            if table in self.layout.excluded:
                continue
            facts_by_cell, facts = defaultdict(list), {}
            for node in table.iter():
                if ix.namespace(node) not in ix.IX or ix.local(node) not in ix.FACT_TAGS or self.doc.hidden[node]:
                    continue
                if next(node.iterancestors('table'), None) is not table:
                    continue
                cell = next((n for n in node.iterancestors() if n.tag in {'td', 'th'}), None)
                if cell is not None:
                    fact = self.doc.fact(node)
                    facts[fact['fact_id']] = fact
                    facts_by_cell[cell].append(fact['fact_id'])
            cells, _, _ = ix.table_cells(self.doc, table, facts_by_cell)
            keep, _ = ix.classify_table(cells, facts)
            if keep:
                self.data_tables.append(table)
        self.data_set = set(self.data_tables)
        self.table_positions = [self.doc.order[t] for t in self.data_tables]
        self.blocks = []
        for node in self.doc.nodes:
            if (node.tag not in ix.BLOCKS or self.doc.hidden[node]
                    or any(a.tag == 'table' for a in node.iterancestors())):
                continue
            # Includes headings preceding an embedded table in the same div.
            text = ix.before_table(self.doc, node)
            if text and len(text) <= 4000 and not noise(text):
                self.blocks.append((self.doc.order[node], text, node))
        # Intel also uses a one-cell layout table for large statement headings.
        # The shared layout reader already distinguishes these from data tables.
        for pos, text, node in self.layout.blocks:
            if node in self.layout.layout_headings and not noise(text):
                self.blocks.append((pos, text, node))
        # Note headings can be split across several cells (e.g. "Note 3",
        # ":", "Operating Segments"), so they are not one-cell headings.
        known_blocks = {n for _, _, n in self.blocks}
        for table in self.doc.tables:
            if table in self.data_set or table in known_blocks or self.doc.hidden[table]:
                continue
            values = [self.doc.display(c) for row in self.doc.rows(table) for c in row if self.doc.display(c)]
            text = ix.clean(' '.join(values))
            if 1 <= len(values) <= 4 and NOTE.match(text) and len(text) <= 180 and self.styled_heading(table):
                self.blocks.append((self.doc.order[table], text, table))
        self.blocks.sort(key=lambda entry: entry[0])
        self.block_positions = [p for p, _, _ in self.blocks]
        self.filing_hash = membership.sha(data)

    def styled_heading(self, node):
        # Formatting in subsequent table cells must not turn introductory
        # prose into a heading. Only inspect the prefix before the first table.
        for child in node.iter():
            if child is not node and child.tag == 'table':
                break
            if not isinstance(child.tag, str) or self.doc.hidden[child]:
                continue
            style = child.get('style', '')
            if (child.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'b', 'strong'}
                    or re.search(r'font-weight\s*:\s*(?:bold|[6-9]00)', style, re.I)
                    or any(float(m[1]) >= (12 if m[2].lower() == 'pt' else 16)
                           for m in re.finditer(r'font-size\s*:\s*(\d+(?:\.\d+)?)(pt|px)', style, re.I))):
                return True
        return False

    def heading(self, text, node):
        if NOTE.match(text):
            return len(text) <= 180
        return (len(text) <= 180 and not re.match(r'^[•▪●]', text)
                and not re.search(r'[.!?;]$', text) and self.styled_heading(node))

    def resolve_title(self, table):
        if table in self.title_cache:
            return self.title_cache[table]
        doc, pos = self.doc, self.doc.order[table]
        title, kind, locator, caption = None, 'unresolved', None, None
        cap = table.find('caption')
        if cap is not None and doc.display(cap):
            caption = doc.display(cap)
            title, kind, locator = caption, 'caption', doc.paths[cap]
        if title is None:
            for row in doc.rows(table):
                cells = [c for c in row if doc.display(c)]
                if not cells:
                    continue
                if len(cells) == 1:
                    text = doc.display(cells[0])
                    if (len(text) <= 180 and not noise(text) and re.search(r'[A-Za-z]', text)
                            and self.heading(text, cells[0]) and ix.measure_text(text) is None):
                        title, kind, locator = text, 'table_heading', doc.paths[cells[0]]
                break
        previous_index = bisect.bisect_left(self.table_positions, pos) - 1
        previous_table = self.table_positions[previous_index] if previous_index >= 0 else -1
        item_start = max((h['position'] for h in self.layout.headings if h['position'] < pos), default=-1)
        candidates = self.blocks[:bisect.bisect_left(self.block_positions, pos)]
        candidates = [(p, text, n) for p, text, n in candidates if p > item_start
                      and '8' in self.layout.membership(n)[2]]
        # Several nested divs can contain the same prefix. The deepest/latest
        # source node gives the most precise locator without joining headings.
        local = [(p, text, n) for p, text, n in candidates if p > previous_table]
        if title is None:
            for _, text, node in reversed(local):
                if self.heading(text, node):
                    title, kind, locator = text, ('note_heading' if NOTE.match(text) else 'visible_heading'), doc.paths[node]
                    break
        # Retain a current note heading when a later table has no local title.
        if title is None:
            for _, text, node in reversed(candidates):
                if NOTE.match(text) and len(text) <= 180:
                    title, kind, locator = text, 'note_heading', doc.paths[node]
                    break
        if title is None and local:
            _, text, node = local[-1]
            title, kind, locator = ix.last_sentence(text), 'preceding_sentence', doc.paths[node]
        result = {'title': title, 'title_source': kind, 'title_locator': locator, 'table_caption': caption}
        self.title_cache[table] = result
        return result

    def source_tables(self, evidence):
        """Follow the matched fact occurrence and its explicit continuations."""
        node = self.doc.ids.get(evidence.get('source_fact_id'))
        if node is None:
            node = self.paths.get(evidence.get('source_locator'))
        if node is None:
            return {}, ['Source fact location is unavailable']
        starts, visited, current, issues = [], set(), node, []
        while current is not None:
            if current in visited:
                issues.append('Cyclic source continuation')
                break
            visited.add(current)
            if self.doc.hidden[current]:
                starts.extend(self.hidden_references.get(current.get('id'), []))
            else:
                starts.append(current)
            ref = current.get('continuedAt')
            if not ref:
                break
            current = self.doc.ids.get(ref)
            if current is None or ix.namespace(current) not in ix.IX or ix.local(current) != 'continuation':
                issues.append('Missing source continuation')
                break
        result = {}
        block = ix.local(node) == 'nonNumeric' and (
            'TextBlock' in node.get('name', '') or node.get('escape') in {'true', '1'})
        for start in starts:
            if not block and '8' not in self.layout.membership(start)[2]:
                continue
            owner = next(start.iterancestors('table'), None)
            candidates = ([owner] if owner is not None else [])
            if block:
                candidates += [n for n in start.iter() if n.tag == 'table']
            for table in candidates:
                if table not in self.data_set:
                    continue
                page, physical, items = self.layout.membership(table)
                if '8' not in items:
                    continue
                result[table] = 'text_block_contains_table' if block else 'fact_occurs_in_table'
        return result, issues

    def describe(self, table):
        if table not in self.table_cache:
            page, physical, items = self.layout.membership(table)
            path = self.doc.paths[table]
            self.table_cache[table] = {
                'table_id': 'table_' + api.digest([self.filing_hash, path])[:16],
                **self.resolve_title(table), 'page': page, 'physical_item': physical,
                'referenced_items': items, 'source_locator': path,
            }
        return self.table_cache[table]


def name_response(payload, report, data, source):
    """Keep every original API value, with separate title associations."""
    if report['source']['response_sha256'] != api.digest(payload) or report['source']['filing_sha256'] != membership.sha(data):
        raise ValueError('Membership evidence does not belong to this API response and filing')
    index = TitleIndex(data, source)
    containers = {g['api_group']: g for g in report['groups']}
    containers.update({g['concept']: g for g in report.get('standalone_concepts', [])})
    named = {}
    for key, raw in payload.items():
        checked = containers[key]
        table_matches, unmatched = {}, []
        for entry in checked['entries']:
            pointer = entry['api_pointer']
            if entry['status'] in {'outside_item_8', 'unresolved'}:
                unmatched.append({'api_pointer': pointer, 'reason': entry['status'], 'issues': entry['issues']})
                continue
            matched, issues = {}, []
            for evidence in entry['evidence']:
                found, problems = index.source_tables(evidence)
                matched.update(found)
                issues.extend(problems)
            if not matched:
                unmatched.append({'api_pointer': pointer, 'reason': 'no_item_8_data_table_at_matched_locations',
                                  'issues': sorted(set(issues))})
            for table, basis in matched.items():
                desc = index.describe(table)
                association = table_matches.setdefault(desc['table_id'], {
                    **deepcopy(desc), 'matched_api_pointers': [], 'match_basis': [], 'issues': []})
                association['matched_api_pointers'].append(pointer)
                if basis not in association['match_basis']:
                    association['match_basis'].append(basis)
                association['issues'] = sorted(set(association['issues'] + issues))
        tables = sorted(table_matches.values(), key=lambda t: index.doc.order[index.paths[t['source_locator']]])
        status = ('excluded_metadata' if checked['status'] == 'excluded_metadata' else
                  'no_table_match' if not tables else 'multiple_tables' if len(tables) > 1 else
                  'single_table' if tables[0]['title'] is not None else 'title_unresolved')
        named[key] = {
            'title': tables[0]['title'] if len(tables) == 1 else None,
            'title_status': status, 'membership_status': checked['status'], 'tables': tables,
            'unmapped_entries': unmatched, 'data': deepcopy(raw),
        }
    unique_tables = {t['table_id']: t for g in named.values() for t in g['tables']}
    return {
        'schema_version': 'api-table-titles-1.0', 'mapper_version': VERSION,
        'source': deepcopy(report['source']), 'year': report['year'], 'item': '8',
        'value_convention': 'groups[api_key].data preserves the original API group/concept data. '
                            'No values are obtained from filing HTML.',
        'mapping_convention': 'tables[].matched_api_pointers identifies the associated subset of a group. '
                              'The entire group data must not be treated as belonging to each matched table. '
                              'One API record may legitimately occur in several tables. Text-block associations '
                              'locate the source concept; they do not independently verify the API block text.',
        'scope': 'Title associations are limited to verified Item 8 locations. All original API keys/data '
                 'are retained, including outside-Item-8, unresolved and metadata entries.',
        'groups': named,
        'summary': {'api_keys': len(named), 'title_status': dict(Counter(g['title_status'] for g in named.values())),
                    'unique_source_tables': len(unique_tables),
                    'title_sources': dict(Counter(t['title_source'] for t in unique_tables.values())),
                    'unmapped_api_entries': sum(len(g['unmapped_entries']) for g in named.values())},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('xbrl_json', type=Path)
    parser.add_argument('--filing', type=Path, required=True, help='Matching original Inline XBRL filing')
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument('--filing-url')
    identity.add_argument('--accession')
    parser.add_argument('--year', type=int)
    parser.add_argument('--output', type=Path, help='Defaults to item_8_named_api.json beside the API input')
    args = parser.parse_args(argv)
    try:
        api_bytes, data = args.xbrl_json.read_bytes(), args.filing.read_bytes()
        saved = api.read_json(api_bytes)
        parameters = api.request_parameters(args.filing_url, args.accession) if args.filing_url or args.accession else None
        payload, parameters = api.load_response(args.xbrl_json, parameters)
        binding = membership.bind_filing(saved, args.filing, data, parameters)
        cached = isinstance(saved, dict) and saved.get('schema_version') in {'sec-api-cache-1.0', 'sec-api-cache-2.0'}
        years = api.cover_values(payload.get('CoverPage', {}).get('DocumentFiscalYearFocus'))
        if args.year is None and (len(years) != 1 or not next(iter(years)).isdigit()):
            raise ValueError('Provide --year when the API response has no unique fiscal year')
        year = args.year if args.year is not None else int(next(iter(years)))
        if not 1900 <= year <= 2200:
            raise ValueError('Use a four-digit fiscal year')
        output = args.output or args.xbrl_json.with_name('item_8_named_api.json')
        protected = {args.xbrl_json.resolve(), args.filing.resolve(),
                     args.filing.with_name(args.filing.stem + '-source.json').resolve()}
        protected.update((args.xbrl_json.parent / n).resolve() for n in
                         ('item_7_html.json', 'item_8_xbrl.json', 'result_hybrid.json',
                          'item_8_membership_check.json', 'validation_summary.json'))
        if output.resolve() in protected:
            raise ValueError('--output must be a separate file, not an existing input/extraction/check output')
        print('Matching API records to Item 8 source tables and original titles...', flush=True)
        report = membership.check_response(payload, data, str(args.filing), parameters, year, '/response' if cached else '')
        report['source'].update(api_file=str(args.xbrl_json), api_file_sha256=membership.sha(api_bytes), filing_binding=binding)
        result = name_response(payload, report, data, str(args.filing))
        # This invariant prevents renaming/grouping from dropping or replacing
        # original API facts, including nil and untagged text-block contents.
        if {k: g['data'] for k, g in result['groups'].items()} != payload:
            raise ValueError('Original API data preservation check failed')
        api.write_json(result, output)
        print(f'Wrote {output}')
        print(json.dumps(result['summary'], indent=2))
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
