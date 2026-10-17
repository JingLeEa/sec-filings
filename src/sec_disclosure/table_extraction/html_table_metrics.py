"""HTML-only values and auditable table classification; no API amounts are used.

Shared readers supply verified Item locations and Inline XBRL tag detection.
All new parsing, classification and normalization lives in this module.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import re

from sec_disclosure.table_extraction.api_fact_labels import FactLabelIndex, label_text
from sec_disclosure.table_extraction import extract_10k_tables_xbrl as ix

VERSION = '1.2.0'
ITEMS = ('1', '1A', '7')
CLASSES = ('financial', 'narrative', 'review')
DATE_RE = re.compile(r'\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
                     r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
                     r'\.?\s+\d{1,2},?\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b', re.I)
NUM_RE = re.compile(r'^[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$|^[+\-−]?\.\d+$')
SCALES = {'thousand': 3, 'million': 6, 'billion': 9}
UNIT_LEGEND_RE = re.compile(
    r'^\(?\s*(?:(?:amounts|dollars|USD|US\$|\$)\s+)?(?:in\s+)?(?:USD\s+)?'
    r'(?:thousands?|millions?|billions?)(?:,?\s+except\s+per[- ]share\s+amounts)?\s*\)?[.:]?$', re.I)


def unique(values):
    return list(dict.fromkeys(values))


def dates(text):
    result = []
    for match in DATE_RE.finditer(text):
        raw = match.group().replace('.', '').replace(',', '')
        for fmt in ('%B %d %Y', '%b %d %Y', '%Y-%m-%d'):
            try:
                result.append(datetime.strptime(raw, fmt).date().isoformat())
                break
            except ValueError:
                pass
    return unique(result)


def amount(text):
    """Bounded display parser; percentages and scaling are applied separately."""
    text = ix.clean(text)
    if text in ix.html_tables.DASHES:
        return {'number': None, 'kind': 'dash', 'percent': False}
    # Parenthesized whole numbers are negatives; only trailing markers are removed.
    text = re.sub(r'(?<=[\d%)])\s*\([a-z0-9]{1,3}\)$', '', text, flags=re.I)
    text = re.sub(r'[†‡*]+$', '', text).strip()
    percent = '%' in text
    text = re.sub(r'^(?:US\$|USD|EUR|GBP|[$€£])\s*', '', text, flags=re.I)
    text = text.replace('%', '').strip()
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = text[1:-1].strip()
    if not NUM_RE.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(',', '').replace('−', '-'))
        if negative:
            number = number.copy_negate()
    except InvalidOperation:
        return None
    return {'number': ix.exact_decimal(number), 'kind': 'number', 'percent': percent}


def rows_of(cells):
    rows = defaultdict(list)
    for cell in cells:
        if cell['display_text']:
            rows[cell['row']].append(cell)
    return rows


def marker(cell, rows):
    row = rows[cell['row']]
    return (len(row) == 2 and row[0] is cell
            and re.fullmatch(r'\(?\d{1,2}\)?\.?|\([a-z]\)|[•▪■]', cell['display_text'])
            and amount(row[1]['display_text']) is None)


def graphics(node, doc):
    return [n for n in node.iter() if ix.local(n) in {'img', 'svg', 'object', 'canvas', 'embed'}
            and not doc.hidden.get(n, False)]


def image_only(table, node, doc):
    """Ignore a graphic with at most a short caption, never an HTML data grid."""
    if not graphics(node, doc):
        return False
    populated = [c for c in table['cells'] if c['display_text'] or c['fact_ids']]
    if any(c['fact_ids'] or amount(c['display_text'])
           or ix.measure_text(c['display_text']) == 'amount' for c in populated):
        return False
    return len(populated) <= 1 and all(len(c['display_text']) <= 200
                                      and len(c['display_text'].split()) <= 25 for c in populated)


def classification(table, facts, node, doc):
    """Classify structure, not the existence of a table tag or a tagged fact."""
    cells, rows = table['cells'], rows_of(table['cells'])
    measured = [c for c in cells if amount(c['display_text']) and not marker(c, rows)]
    evidence = {'populated_rows': len(rows), 'standalone_number_like_cells': len(measured),
                'long_prose_cells': sum(len(c['display_text'].split()) > 30 for c in cells),
                'images': len(graphics(node, doc))}
    if table['diagnostics']['layout_error']:
        return 'review', 'invalid_or_overlapping_cell_spans', evidence
    keep, reason = ix.classify_table(cells, facts)
    if keep:
        # Do not promote a label/paragraph table because a standalone footnote
        # or amount happens to accompany a long narrative cell.
        data_rows = [row for row in rows.values() if any(c in measured for c in row)]
        if data_rows and all(any(len(c['display_text'].split()) > 30 for c in row) for row in data_rows):
            return 'review', 'mixed_prose_and_measurements', evidence
        return 'financial', reason, evidence
    if evidence['images']:
        return 'review', 'image_content_requires_visual_review', evidence
    if reason == 'nonfinancial_roster':
        return 'narrative', reason, evidence
    uncertain = [c for c in measured if ix.measure_text(c['display_text']) != 'year']
    if uncertain or any(ix.measure_text(c['display_text']) == 'amount' for c in cells if not marker(c, rows)):
        return 'review', 'measurements_without_clear_table_relationship', evidence
    return 'narrative', reason, evidence


def meaningful_title(text):
    return (bool(text) and len(text) <= 200 and not dates(text) and re.search(r'[A-Za-z]', text)
            and not re.match(r'^(?:years? ended|as of|\(?in millions|\(?in thousands|[▪•])', text, re.I)
            and not re.fullmatch(r'\d+\s*\|.*|Table of Contents', text, re.I))


def title_for(table, node, layout):
    title, method = table['title'], table['title_source']
    if method in {'caption', 'table_heading', 'visible_heading'} and meaningful_title(title):
        return title, method
    for row in layout.doc.rows(node):
        populated = [cell for cell in row if layout.doc.display(cell)]
        if not populated:
            continue
        if len(populated) == 1:
            text = layout.doc.display(populated[0])
            if meaningful_title(text) and (ix.html_tables.bold_block(populated[0]) or ix.large_type(populated[0])):
                return text.rstrip(':'), 'table_heading'
        break
    # Date-only panel titles (Intel page 21) inherit the nearby visible section
    # heading on the same printed page. Never borrow headings across pages.
    for pos, text, block in reversed(layout.blocks):
        if pos >= layout.doc.order[node]:
            continue
        if layout.page(block) != table['page']:
            continue
        if meaningful_title(text) and (block.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
                                       or ix.html_tables.bold_block(block) or ix.large_type(block)):
            return text.rstrip(':'), 'visible_section_heading'
    if dates(title) and method == 'table_heading':
        return 'Untitled table', 'generated'
    return title, method


def annual_periods(doc):
    result = defaultdict(dict)
    for context_id, context in doc.contexts.items():
        p = context['period']
        if p.get('type') != 'duration':
            continue
        try:
            days = (date.fromisoformat(p['endDate']) - date.fromisoformat(p['startDate'])).days
        except (ValueError, KeyError):
            continue
        if 330 <= days <= 380:
            result[p['endDate']].setdefault(p['startDate'], context_id)
    return result


def resolve_period(headers, legend, nearby, calendar):
    evidence = headers + legend
    ends = unique(d for text in headers for d in dates(text))
    if not ends:
        ends = unique(d for text in legend for d in dates(text))
    if not ends:
        ends = unique(d for text in nearby for d in dates(text))
        evidence += nearby
    annual = bool(re.search(r'\byears? ended\b', ' '.join(evidence), re.I))
    # Year-only headers require a unique fiscal year-end in the source contexts.
    if not ends:
        years = unique(y for text in headers for y in re.findall(r'\b(?:19|20|21)\d{2}\b', text))
        if len(years) == 1:
            ends = [end for end in calendar if end.startswith(years[0] + '-')]
    if len(ends) != 1:
        return None, None, {'method': 'unresolved', 'text': evidence}, 'missing_or_ambiguous_period'
    end = ends[0]
    if annual:
        starts = calendar.get(end, {})
        if len(starts) != 1:
            return {'endDate': end}, 'duration', {'method': 'unresolved_annual_start', 'text': evidence}, 'unverified_period_start'
        start = next(iter(starts))
        return {'startDate': start, 'endDate': end}, 'duration', {
            'method': 'year_ended_header_and_unique_annual_context', 'context_ref': starts[start], 'text': evidence}, None
    # Other duration headers must not be silently interpreted as instant dates.
    if re.search(r'\b(?:months?|quarters?|weeks?|period)\s+ended\b', ' '.join(evidence), re.I):
        return {'endDate': end}, 'duration', {'method': 'unresolved_duration', 'text': evidence}, 'unverified_period_start'
    return {'instant': end}, 'instant', {'method': 'dated_column_or_as_of_text', 'text': evidence}, None


def currency_codes(doc):
    codes = set()
    for unit in doc.units.values():
        if not unit['denominator'] and len(unit['numerator']) == 1:
            measure = unit['numerator'][0]
            if measure['namespace'] == 'http://www.xbrl.org/2003/iso4217':
                codes.add(measure['local_name'])
    return codes


def normalize(parsed, text, row_label, headers, legends, symbols, codes):
    """Never assign a base-unit amount without evidence of the unit and scale."""
    evidence = unique([text] + headers + legends + symbols)
    header = ' '.join(headers)
    all_text = ' '.join(evidence)
    issues = []
    if parsed['percent'] or '%' in header or '%' in row_label or '%' in symbols:
        unit, exponent, basis = 'pure', -2, 'percent_display'
    elif re.search(r'\bsquare feet\b', all_text, re.I):
        unit, exponent, basis = 'square_feet', 0, 'square_feet_heading'
    else:
        explicit = set(re.findall(r'\b(?:USD|EUR|GBP|CAD|AUD|JPY)\b', all_text))
        if '€' in all_text:
            explicit.add('EUR')
        if '£' in all_text:
            explicit.add('GBP')
        if re.search(r'US\$|U\.S\. dollars', all_text, re.I):
            explicit.add('USD')
        if not explicit and '$' in all_text:
            explicit = codes & {'USD', 'CAD', 'AUD', 'NZD', 'HKD', 'SGD'}
        unit = next(iter(explicit)) if len(explicit) == 1 else None
        exponent, basis = 0, 'currency_heading_or_symbol_and_document_units'
        if re.search(r'\bper share\b', row_label, re.I) and unit:
            unit += '/shares'
        if unit is None:
            issues.append('missing_or_ambiguous_unit')
    if exponent != -2 and not (unit or '').endswith('/shares'):
        scales = {SCALES[m.group(1).lower()] for m in re.finditer(r'\b(thousand|million|billion)s?\b', all_text, re.I)}
        if len(scales) > 1:
            issues.append('conflicting_display_scales')
        elif scales:
            exponent = next(iter(scales))
    if parsed['kind'] == 'dash':
        issues.append('ambiguous_dash_not_assumed_zero_or_nil')
    elif parsed['kind'] == 'unsupported':
        issues.append('unsupported_numeric_display')
    value = None
    if not issues and parsed['number'] is not None:
        number = Decimal(parsed['number'])
        with localcontext() as ctx:
            ctx.prec = max(50, len(number.as_tuple().digits) + abs(exponent) + 5)
            value = ix.exact_decimal(number.scaleb(exponent))
    return value, unit, {'method': basis, 'scale_power': exponent, 'text': evidence}, issues


def nearby_text(layout, table, node):
    pos = layout.doc.order[node]
    previous = [(p, text) for p, text, block in layout.blocks if p < pos
                and layout.page(block) == table['page'] and text]
    # Only the closest sentence is inherited; broad section prose must not
    # accidentally supply another date or a different unit convention.
    return [ix.last_sentence(previous[-1][1])] if previous else []


def footnotes_for(table, node, layout):
    doc = layout.doc
    finish = max(doc.order[n] for n in node.iter() if isinstance(n.tag, str))
    next_table = min((doc.order[t] for t in doc.tables if doc.order[t] > finish), default=len(doc.nodes))
    notes = []
    for pos, text, block in layout.blocks:
        if not finish < pos < next_table or layout.page(block) != table['page']:
            continue
        if re.match(r'^(?:\(?\d{1,2}\)?[.\s]?|\([a-z]\)|[†‡*])\s*[A-Za-z]', text):
            notes.append({'text': text, 'source_locator': doc.paths[block]})
    return notes


class HtmlLabelIndex(FactLabelIndex):
    """Use the already parsed document; never change the API label reader."""
    def __init__(self, doc):
        self.doc = doc
        self.paths = {path: node for node, path in doc.paths.items()}
        self.tables, self.cell_labels = {}, {}


def shared_row_percent(row, labels, nodes, table_node):
    """A percent sign can govern comparable year columns in the same row.

    Require matching column wording after removing years, at least two distinct
    years, and no currency marker. Mixed amount/change panels are not inherited.
    """
    marked = [c for c in row if c['display_text'] == '%' or
              (amount(c['display_text']) and '%' in c['display_text'])]
    if not marked or any(re.search(r'[$€£]|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\b', c['display_text']) for c in row):
        return []
    wording, years = set(), set()
    for cell in row:
        if not amount(cell['display_text']):
            continue
        label = labels.labels(nodes[cell['cell_id']], table_node)['column_label'] or ''
        column_years = re.findall(r'\b(?:19|20|21)\d{2}\b', label)
        if len(set(column_years)) != 1:
            return []
        years.update(column_years)
        wording.add(re.sub(r'\b(?:19|20|21)\d{2}\b', '<year>', label))
    return [c['source_locator'] for c in marked] if len(wording) == 1 and len(years) >= 2 else []


def extract_values(table, node, doc, layout, labels):
    cells, grid, error, by_id, _, nodes, numeric, header_ids, _ = labels.table(node)
    if error:
        return [], {'tagged_value_cells_skipped': 0, 'issues': [error]}
    first_data = min((c['row'] for c in cells if c['cell_id'] in numeric), default=len(grid) + 1)
    # A literal amount such as 2025 is not a year heading when it follows a
    # period header and shares its row with a non-date descriptive stub.
    period_rows = {c['row'] for c in cells if dates(c['display_text']) or
                   re.search(r'\byears? ended\b', c['display_text'], re.I)}
    for cell in cells:
        if (ix.measure_text(cell['display_text']) == 'year'
                and any(r < cell['row'] for r in period_rows)
                and any(c['row'] == cell['row'] and c['column'] < cell['column']
                        and label_text(c['display_text']) and not dates(c['display_text'])
                        and not UNIT_LEGEND_RE.fullmatch(c['display_text']) for c in cells)):
            numeric.add(cell['cell_id'])
            first_data = min(first_data, cell['row'])
    header_ids.difference_update(c['cell_id'] for c in cells if c['row'] >= first_data
                                and nodes[c['cell_id']].get('scope', '').lower() not in {'col', 'colgroup'}
                                and not any(a.tag == 'thead' for a in nodes[c['cell_id']].iterancestors()))
    header_ids.difference_update(numeric)
    legends = unique(c['display_text'] for c in cells if c['display_text'] and c['row'] < first_data)
    # Currency markers in data cells also apply to unmarked amounts in the same
    # table. Multiple explicit currencies remain unresolved, never guessed.
    table_symbols = unique(c['display_text'] for c in cells
                           if re.fullmatch(r'[$€£]|USD|EUR|GBP|CAD|AUD|JPY', c['display_text']))
    calendar, codes = annual_periods(doc), currency_codes(doc)
    nearby = nearby_text(layout, table, node)
    legends += [text for text in nearby if UNIT_LEGEND_RE.fullmatch(text)]
    rows, values, tagged = rows_of(cells), [], 0
    percent_rows = {row_id: shared_row_percent(row, labels, nodes, node) for row_id, row in rows.items()}
    original = {c['cell_id']: c for c in table['cells']}
    for cell in cells:
        if cell['row'] < first_data or cell['cell_id'] in header_ids:
            continue
        parsed = amount(cell['display_text'])
        if parsed is None and ix.measure_text(cell['display_text']) == 'amount':
            parsed = {'number': None, 'kind': 'unsupported', 'percent': '%' in cell['display_text']}
        if parsed is None or marker(cell, rows):
            continue
        label = labels.labels(nodes[cell['cell_id']], node)
        if not label['row_label']:
            # A numeric header or orphan cell is recorded for review below.
            if ix.measure_text(cell['display_text']) == 'year':
                continue
        if original[cell['cell_id']]['fact_ids']:
            tagged += 1
            continue
        # A nested text-block tag does not imply the value itself is tagged;
        # only fact IDs actually attached to this cell exclude it above.
        row_label = label['row_label'] or None
        headers = label['column_label'].split(' / ') if label['column_label'] else []
        row = rows[cell['row']]
        position = row.index(cell)
        suffix = row[position + 1]['display_text'] if position + 1 < len(row) else ''
        percent_sources = percent_rows.get(cell['row'], [])
        symbols = table_symbols + (['%'] if suffix == '%' or percent_sources else [])
        value, unit, normalization, issues = normalize(parsed, cell['display_text'], row_label or '', headers,
                                                       legends, symbols, codes)
        if percent_sources and not parsed['percent'] and suffix != '%':
            normalization['row_percent_source_locators'] = percent_sources
        period, period_type, period_evidence, period_issue = resolve_period(headers, legends, nearby, calendar)
        if period_issue:
            issues.append(period_issue)
        if not row_label:
            issues.append('missing_row_label')
        if not label['column_label']:
            issues.append('missing_column_label')
        # Preserve segment/geographic headings without inventing XBRL axes.
        contexts = [h for h in headers if not dates(h) and not re.fullmatch(r'(?:19|20|21)\d{2}|Amount', h, re.I)]
        display = cell['display_text'] + ('%' if suffix == '%' and '%' not in cell['display_text'] else '')
        source_label = {**label, 'table_id': table['table_id'], 'table_title': table['title'],
                        'title_source': table['title_source'], 'page': table['page'],
                        'source_fact_id': None, 'source_locator': cell['source_locator'],
                        'document_id': table['document_id'], 'source': doc.source}
        record = {'value': value, 'unit': unit, 'period': period, 'dimensions': None,
                  'status': 'reported' if value is not None else 'unresolved',
                  'items': table['items'], 'source_type': 'html', 'display_text': display,
                  'context_labels': contexts, 'source_labels': [source_label],
                  'extraction_status': 'needs_review' if issues else 'resolved',
                  'issues': issues, 'normalization': normalization, 'period_evidence': period_evidence}
        values.append((row_label, period_type, record))
    return values, {'tagged_value_cells_skipped': tagged, 'issues': []}


def build(documents, company, year, items=ITEMS):
    items = tuple(unique(items))
    if not items or set(items) - set(ITEMS):
        raise ValueError('HTML metrics supports only Items 1, 1A and 7.')
    metrics, decisions, excluded, image_pages = [], [], Counter(), []
    ignored_images = []
    # Some document sets display report text using an explicit reference to a
    # hidden fact in the primary 10-K. Only a unique cross-document ID can be
    # followed. Local IDs retain precedence, including duplicate IDs in peers.
    fact_owners = defaultdict(list)
    for identifier, entry in documents.items():
        for fact_node in entry['doc'].fact_nodes:
            if fact_node.get('id'):
                fact_owners[fact_node.get('id')].append((identifier, entry['doc'], fact_node))
    reference_evidence = []
    for document_id, entry in documents.items():
        doc, layout = entry['doc'], entry['layout']
        def referenced_fact(identifier):
            candidates = fact_owners.get(identifier, [])
            if len(candidates) != 1:
                raise ValueError(f'Missing or ambiguous cross-document hidden fact reference: {identifier}')
            owner, owner_doc, node = candidates[0]
            fact = owner_doc.fact(node)
            evidence = {'referencing_document_id': document_id, 'fact_document_id': owner,
                        'source_fact_id': identifier, 'source_locator': owner_doc.paths[node]}
            if evidence not in reference_evidence:
                reference_evidence.append(evidence)
            return {**fact, 'fact_id': f'@document-{owner}/{fact["fact_id"]}', 'document_id': owner}
        tables, exported = ix.collect_tables(layout, document_id, company, year, items, financial_only=False,
                                             hidden_fact_resolver=referenced_fact)
        # Diagnostics here count the entire source document; only the candidate
        # table classifications below are used for requested-Item totals.
        excluded.update(exported['diagnostics']['excluded_tables'])
        image_pages.extend(exported['diagnostics']['image_only_pages'])
        paths = {path: node for node, path in doc.paths.items()}
        present = {t['source_locator'] for t in tables}
        # Inspect image layouts excluded by the shared reader so ignored
        # image-only tables can be counted separately from structural layouts.
        for ordinal, node in enumerate(doc.tables, 1):
            if doc.paths[node] in present or not graphics(node, doc):
                continue
            page, physical, members = layout.membership(node)
            if not set(items).intersection(members):
                continue
            cells, grid, error = ix.table_cells(doc, node, {})
            title, method = ix.table_title(layout, node, ordinal)
            tables.append({'table_id': f'{ix.html_tables.slug(company)}_{year}_{document_id}_{page or "unknown"}_image{ordinal}',
                           'document_id': document_id, 'referenced_items': members, 'page': page,
                           'title': title, 'title_source': method, 'source': doc.source,
                           'source_locator': doc.paths[node], 'cells': cells, 'grid': grid,
                           'fact_ids': [], 'diagnostics': {'layout_error': error}})
            if node in layout.excluded and excluded['heading_footer_or_index']:
                excluded['heading_footer_or_index'] -= 1
            elif not any(c['display_text'] for c in cells) and excluded['empty_table']:
                excluded['empty_table'] -= 1
        tables.sort(key=lambda t: doc.order[paths[t['source_locator']]])
        labels = HtmlLabelIndex(doc)
        for table in tables:
            node = paths[table['source_locator']]
            table['items'] = [i for i in items if i in table['referenced_items']]
            if image_only(table, node, doc):
                ignored_images.append(table['items'])
                continue
            table['title'], table['title_source'] = title_for(table, node, layout)
            facts = {fid: exported['facts'][fid] for fid in table['fact_ids']}
            category, reason, evidence = classification(table, facts, node, doc)
            decision = {k: table[k] for k in ('table_id', 'document_id', 'items', 'page', 'title', 'title_source', 'source', 'source_locator')}
            decision.update(classification=category, reason=reason, evidence=evidence,
                            contains_xbrl_facts=bool(table['fact_ids']), exported_values=0,
                            footnotes=footnotes_for(table, node, layout))
            if category == 'financial':
                extracted, diagnostics = extract_values(table, node, doc, layout, labels)
                decision.update(diagnostics)
                groups = {}
                for row_label, period_type, value in extracted:
                    # Scope each unidentified metric to its source table and
                    # row/measure kind; equal amounts alone never merge rows.
                    kind = 'percentage' if value['normalization']['method'] == 'percent_display' else 'amount'
                    key = (row_label, kind, period_type)
                    if key not in groups:
                        digest = hashlib.sha256(repr(key).encode()).hexdigest()[:12]
                        label = row_label or 'Unresolved row'
                        if kind == 'percentage' and '%' not in label:
                            label += ' (% column)'
                        groups[key] = {'metric_id': f'html:{table["table_id"]}:{digest}',
                            'concept': None, 'query_name': None, 'label': label, 'label_source': 'filing',
                            'definition': None, 'period_type': period_type, 'scope': 'unknown',
                            'company_wide_annual_or_year_end_years': [], 'units': [], 'api_groups': [],
                            'items': table['items'], 'value': []}
                    groups[key]['value'].append(value)
                    if value['unit'] and value['unit'] not in groups[key]['units']:
                        groups[key]['units'].append(value['unit'])
                metrics.extend(groups.values())
                decision['exported_values'] = len(extracted)
                decision['values_needing_review'] = sum(v['extraction_status'] == 'needs_review' for _, _, v in extracted)
            decisions.append(decision)
    values = [v for m in metrics for v in m['value']]
    counts = Counter(d['classification'] for d in decisions)
    summary = {'counting_unit': 'Unique HTML table candidates, excluding image-only tables; overlapping Item memberships count once in overall totals.',
               'total_tables': len(decisions), **{c: counts[c] for c in CLASSES},
               'ignored_image_only_tables': len(ignored_images),
               'ignored_image_only_by_item': {i: sum(i in members for members in ignored_images) for i in items},
               'by_item': {i: {'total_tables': sum(i in d['items'] for d in decisions),
                               **{c: sum(i in d['items'] and d['classification'] == c for d in decisions) for c in CLASSES}}
                           for i in items},
               'financial_tables_with_untagged_values': sum(d['classification'] == 'financial' and d['exported_values'] > 0 for d in decisions),
               'tagged_value_cells_skipped': sum(d.get('tagged_value_cells_skipped', 0) for d in decisions),
               'exported_values': len(values), 'values_needing_review': sum(v['extraction_status'] == 'needs_review' for v in values),
               'value_issue_counts': dict(Counter(issue for v in values for issue in v['issues'])),
               'structural_exclusions_document_wide': dict(excluded), 'image_only_pages': image_pages}
    return {'schema_version': 'html-metrics-1.0', 'exporter_version': VERSION,
            'source': documents['primary']['doc'].source, 'requested_items': list(items),
            'company': company, 'fiscal_year': year, 'taxonomy_year': None,
            'counts': {'metrics': len(metrics), 'verified_concepts': 0, 'company_wide': 0,
                       'dimension_only': 0, 'unknown_scope': len(metrics)},
            'notes': [
                'Amounts come only from visible untagged HTML table cells. Tagged cells are skipped, including hidden-fact references.',
                'Only financial/quantitative table records are exported. Narrative and review tables are omitted; classification_summary counts all classified candidates, including omitted tables.',
                'Concepts and definitions are not inferred. Null dimensions mean no verified XBRL dimensional context, not company-wide.',
                'Source XBRL contexts may verify fiscal dates and currency codes, but never supply exported amounts.',
                'Percentages use unit pure and a 0.01 display multiplier. Currency/physical quantities use the documented heading scale; per-share amounts are not multiplied by millions.',
                'Dashes are unresolved, never automatically zero or nil. Unresolved period/unit/label evidence is retained and counted.',
                'Metrics are grouped within each source table and row/measure kind. Repeated disclosures across tables are retained; do not sum them.',
                'Blank layout cells and year/column headers are not values. Image-only tables (including a short caption) are ignored and counted separately; tables with HTML measurements are still checked. No OCR is performed.',
                'Structural/navigation/empty tables are excluded before candidate classification. Document-wide exclusions are reported separately.',
            ], 'metrics': metrics,
            'table_classifications': [d for d in decisions if d['classification'] == 'financial'],
            'verification': {'amount_source': 'html', 'api_response_used_for_amounts': False,
                             'cross_document_hidden_references': reference_evidence,
                             'documents': {key: {'source': e['doc'].source, 'sha256': hashlib.sha256(e['doc'].data).hexdigest(),
                                                'sec_url': e.get('url'), 'item_page_references': e['layout'].references,
                                                'incorporated_sections': e['layout'].ranges,
                                                **({'report_discovery': e['report_discovery']} if e.get('report_discovery') else {}),
                                                **({'boundary_evidence': e['boundary_evidence']} if e.get('boundary_evidence') else {})}
                                           for key, e in documents.items()}},
            # This is deliberately the last JSON property, as requested.
            'classification_summary': summary}
