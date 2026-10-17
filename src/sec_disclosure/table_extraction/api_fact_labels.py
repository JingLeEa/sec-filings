"""Read source row/column labels for already matched API facts.

This module annotates source occurrences. It never supplies a financial amount
or infers a "Total" label from an empty XBRL dimension list.
"""
from __future__ import annotations

from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix


def label_text(text):
    return bool(text and len(text) <= 500 and any(c.isalpha() for c in text)
                and ix.measure_text(text) not in {'amount', 'dash'})


class FactLabelIndex:
    def __init__(self, data, source):
        self.doc = ix.Document(data, source)
        self.paths = {path: node for node, path in self.doc.paths.items()}
        self.tables = {}
        self.cell_labels = {}

    def table(self, table):
        if table in self.tables:
            return self.tables[table]
        doc = self.doc
        cells, grid, error = ix.table_cells(doc, table, {})
        by_id = {c['cell_id']: c for c in cells}
        by_path = {c['source_locator']: c for c in cells}
        nodes = {c['cell_id']: self.paths[c['source_locator']] for c in cells}
        numeric, explicit_columns, explicit_rows = set(), set(), set()
        for c in cells:
            node = nodes[c['cell_id']]
            scope = node.get('scope', '').lower()
            if scope in {'col', 'colgroup'} or any(a.tag == 'thead' and next(a.iterancestors('table'), None) is table
                                                  for a in node.iterancestors()):
                explicit_columns.add(c['cell_id'])
            if scope in {'row', 'rowgroup'}:
                explicit_rows.add(c['cell_id'])
            # Use numeric content only to separate header bands from data. No
            # amounts read here are returned to the metric exporter.
            tagged = any(ix.namespace(n) in ix.IX and ix.local(n) in {'nonFraction', 'fraction'}
                         and not doc.hidden[n] for n in node.iter())
            if (tagged or ix.measure_text(c['display_text']) in {'amount', 'dash'}) and c['cell_id'] not in explicit_columns:
                numeric.add(c['cell_id'])
        first_data = min((c['row'] for c in cells if c['cell_id'] in numeric), default=len(grid) + 1)
        header_ids = set(explicit_columns)
        data_columns = {}
        for c in cells:
            if c['cell_id'] in numeric and c['column'] is not None:
                data_columns.setdefault(c['row'], []).append(c['column'])
        for c in cells:
            text = c['display_text']
            node = nodes[c['cell_id']]
            if c['cell_id'] in explicit_rows or c['cell_id'] in numeric or not text:
                continue
            if c['header'] and c['cell_id'] not in explicit_columns and c['column'] is not None and any(
                    column > c['column'] for column in data_columns.get(c['row'], [])):
                explicit_rows.add(c['cell_id'])
            elif c['header'] and node.get('scope', '').lower() not in {'row', 'rowgroup'}:
                header_ids.add(c['cell_id'])
            elif c['row'] < first_data:
                # A full-width title/legend is not a column label. Preserve
                # specific headers and multi-level groups above data columns.
                if grid and c['column'] is not None and c['colspan'] < len(grid[0]):
                    header_ids.add(c['cell_id'])
        result = (cells, grid, error, by_id, by_path, nodes, numeric, header_ids, explicit_rows)
        self.tables[table] = result
        return result

    def labels(self, cell, table):
        if cell in self.cell_labels:
            return self.cell_labels[cell]
        cells, grid, error, by_id, by_path, nodes, numeric, headers, explicit_rows = self.table(table)
        target = by_path.get(self.doc.paths[cell])
        result = {'row_label': None, 'column_label': None, 'label_method': 'table_layout',
                  'status': 'unresolved'}
        if error or target is None or target['column'] is None:
            result.update(status='invalid_layout', issue=error or 'Source cell not in the table grid')
            self.cell_labels[cell] = result
            return result

        left = target['column']
        right = left + target['colspan'] - 1
        # Prefer explicit HTML header associations. A malformed reference must
        # not silently fall back to a guessed label.
        header_refs = cell.get('headers', '').split()
        row_headers, column_headers = [], []
        if header_refs:
            for identifier in header_refs:
                node = self.doc.ids.get(identifier)
                header = by_path.get(self.doc.paths.get(node))
                if header is None or header['cell_id'] in numeric:
                    result.update(issue='Invalid or non-header HTML headers reference')
                    self.cell_labels[cell] = result
                    return result
                scope = node.get('scope', '').lower()
                if scope in {'row', 'rowgroup'} or (header['column'] + header['colspan'] <= left
                                                   and header['row'] <= target['row'] < header['row'] + header['rowspan']):
                    row_headers.append(header)
                elif scope in {'col', 'colgroup'} or (header['row'] < target['row']
                                                     and header['column'] <= left
                                                     and header['column'] + header['colspan'] - 1 >= right):
                    column_headers.append(header)
                else:
                    result.update(issue='HTML header association has ambiguous row/column orientation')
                    self.cell_labels[cell] = result
                    return result
            result['label_method'] = 'html_headers'
        else:
            row_ids = list(dict.fromkeys(i for i in grid[target['row'] - 1][:left - 1] if i))
            candidates = [by_id[i] for i in row_ids if i not in numeric and label_text(by_id[i]['display_text'])
                          and (i not in headers or i in explicit_rows)]
            if candidates:
                # The nearest stub is the local row label in side-by-side
                # panels. A rowspan stub may originate on an earlier row.
                row_headers = [max(candidates, key=lambda c: c['column'])]
            column_headers = [by_id[i] for i in headers if by_id[i]['row'] < target['row']
                              and by_id[i]['column'] <= left
                              and by_id[i]['column'] + by_id[i]['colspan'] - 1 >= right
                              and by_id[i]['display_text']]
            # Two equally wide headers at different vertical positions may be
            # repeated headings. The closest one governs the current row.
            widths = {}
            for header in column_headers:
                width = header['colspan']
                if width not in widths or header['row'] > widths[width]['row']:
                    widths[width] = header
            column_headers = list(widths.values())

        def joined(headers):
            texts = list(dict.fromkeys(c['display_text'] for c in sorted(headers, key=lambda c: (c['row'], c['column']))
                                      if c['display_text']))
            return ' / '.join(texts) or None

        result['row_label'], result['column_label'] = joined(row_headers), joined(column_headers)
        result['status'] = ('resolved' if result['row_label'] and result['column_label'] else
                            'partial' if result['row_label'] or result['column_label'] else 'unresolved')
        self.cell_labels[cell] = result
        return result

    def for_entry(self, entry, selected_items):
        """Return all selected visible source occurrences, including prose."""
        result, seen = [], set()
        for evidence in entry['evidence']:
            for location in evidence['locations']:
                if not set(selected_items).intersection(location['referenced_items']):
                    continue
                point = self.paths.get(location['source_locator'])
                cell = next((n for n in point.iterancestors() if n.tag in {'td', 'th'}), None) if point is not None else None
                if point is not None and point.tag in {'td', 'th'}:
                    cell = point
                table = next(cell.iterancestors('table'), None) if cell is not None else None
                label = {'row_label': None, 'column_label': None, 'label_method': None,
                         'status': 'not_in_table' if point is not None else 'source_not_found'}
                if cell is not None and table is not None:
                    label = dict(self.labels(cell, table))
                locator = self.doc.paths[cell] if cell is not None else location['source_locator']
                identity = (evidence.get('source_fact_id'), locator, location['page'])
                if identity in seen:
                    continue
                seen.add(identity)
                result.append({**label, 'page': location['page'],
                               'source_fact_id': evidence.get('source_fact_id'), 'source_locator': locator})
        return result
