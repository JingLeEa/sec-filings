#!/usr/bin/env python3
"""Extract tables with genuine Inline XBRL facts from a single SEC 10-K.

HTML supplies table positions and display text; XBRL supplies concept, context,
dimensions, unit, sign and scale. Untagged cells remain untagged. Decimal values
are exact strings in the XBRL unit (USD, not USD millions). This is a separate
schema and output directory; the existing HTML extractor is not modified.
Financial/quantitative tables are retained by default; prose and layout blocks
are filtered. Use --include-layout-tables to inspect the broader HTML inventory.

Python 3.10+ and lxml; keep compare_html_tables.py and sec_10k_extractor.py nearby.
This is a bounded Inline XBRL reader, not a taxonomy/calculation validator.
Unsupported transformations/fractions are preserved with explicit errors.

    python3 extract_10k_tables_xbrl.py --ticker INTC --company Intel --year 2023 \
        --user-agent "Your name your-email@example.com"
    python3 extract_10k_tables_xbrl.py filing.htm --company Intel --year 2023
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlparse, urldefrag

from lxml import etree
import compare_html_tables as html_tables
import sec_10k_extractor as narrative

VERSION = '1.9.0'
DEFAULT_ITEMS = ('1', '1A', '7', '8')
XHTML = 'http://www.w3.org/1999/xhtml'
XBRLI = 'http://www.xbrl.org/2003/instance'
XBRLDI = 'http://xbrl.org/2006/xbrldi'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
IX = {'http://www.xbrl.org/2013/inlineXBRL', 'http://www.xbrl.org/2008/inlineXBRL'}
REGISTRIES = {'http://www.xbrl.org/inlineXBRL/transformation/' + date
              for date in ('2010-04-20', '2011-07-31', '2015-02-26', '2020-02-12', '2022-02-16')}
BLOCKS = {'div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
ITEM_RE = re.compile(r'^ITEM\s+(\d{1,2}[A-C]?)\s*[.:–—-]?\s*(.*)$', re.I)
FACT_TAGS = {'nonFraction', 'fraction', 'nonNumeric'}


def clean(value):
    return re.sub(r'\s+', ' ', value.replace('\u200b', '')).strip()


def local(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ''


def namespace(node):
    return etree.QName(node).namespace if isinstance(node.tag, str) else ''


def qname(node, value):
    if not value:
        raise ValueError('Missing QName')
    prefix, name = value.split(':', 1) if ':' in value else (None, value)
    uri = node.nsmap.get(prefix)
    if not uri or not re.fullmatch(r'[A-Za-z_][\w.\-]*', name):
        raise ValueError(f'Unresolved or invalid QName: {value}')
    return {'name': value, 'namespace': uri, 'local_name': name, 'expanded_name': f'{{{uri}}}{name}'}


def exact_decimal(value):
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def transformed_number(node, raw):
    """A deliberately bounded set of official numeric transformations.

    Unknown registry namespaces or formats never fall back to guessing from
    display text. 'decimals' records accuracy; it is not the scaling exponent.
    """
    fmt = qname(node, node.get('format')) if node.get('format') else None
    s = raw.strip()
    if fmt:
        name = fmt['local_name']
        if fmt['namespace'] not in REGISTRIES:
            raise ValueError(f'Unsupported transformation registry: {fmt["namespace"]}')
        modern = fmt['namespace'].endswith(('2020-02-12', '2022-02-16'))
        if name == 'fixed-zero' and modern:
            s = '0'
        elif name == 'zerodash' and fmt['namespace'].endswith('2015-02-26'):
            # Registry 3, zerodashType: one of the explicitly enumerated
            # Unicode dashes. An untagged dash is never inferred to be zero.
            # https://www.xbrl.org/Specification/inlineXBRL-transformationRegistry/REC-2015-02-26/inlineXBRL-transformationRegistry-REC-2015-02-26.html
            if s not in set('-֊־‐‑‒–—―﹘﹣－'):
                raise ValueError(f'Invalid input for {name}: {raw!r}')
            s = '0'
        elif name in ({'num-dot-decimal', 'num-comma-decimal'} if modern else {'numdotdecimal', 'numcommadecimal'}):
            comma = 'comma' in name
            decimal, grouping = (',', '.') if comma else ('.', ',')
            integer = rf'(?:[0-9]+|[0-9]{{1,3}}(?:[{re.escape(grouping)}\s][0-9]{{3}})+)'
            pattern = rf'(?:{integer}(?:{re.escape(decimal)}[0-9]+)?|{re.escape(decimal)}[0-9]+)'
            if not re.fullmatch(pattern, s):
                raise ValueError(f'Invalid input for {name}: {raw!r}')
            s = re.sub(r'\s', '', s).replace(grouping, '').replace(decimal, '.')
        elif name == 'numdash' and not modern and s in {'-', '–', '—'}:
            s = '0'
        else:
            raise ValueError(f'Unsupported numeric transformation: {node.get("format")}')
    if not re.fullmatch(r'\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', s):
        raise ValueError(f'Invalid unsigned XBRL decimal: {raw!r}')
    scale = node.get('scale', '0')
    if not re.fullmatch(r'[+-]?\d+', scale) or abs(int(scale)) > 10000:
        raise ValueError(f'Invalid or excessive scale: {scale}')
    sign = node.get('sign')
    if sign not in {None, '-'}:
        raise ValueError(f'Invalid sign: {sign}')
    number = Decimal(s)
    parts = number.as_tuple()
    # Constructing the decimal tuple avoids the default 28-digit arithmetic
    # precision silently rounding unusually large/high-precision source facts.
    value = Decimal((int(sign == '-'), parts.digits, parts.exponent + int(scale)))
    return exact_decimal(value)


def transformed_text(node, raw):
    if node.get('escape') in {'true', '1'}:
        raise ValueError('Escaped nonNumeric XML retained as raw text; not normalized')
    if not node.get('format'):
        return raw
    fmt = qname(node, node.get('format'))
    modern = fmt['namespace'] in REGISTRIES and fmt['namespace'].endswith(('2020-02-12', '2022-02-16'))
    english_date = ((modern and fmt['local_name'] == 'date-monthname-day-year-en') or
                    (fmt['namespace'] == 'http://www.xbrl.org/inlineXBRL/transformation/2015-02-26'
                     and fmt['local_name'] == 'datemonthdayyearen'))
    if english_date:
        # Bounded English-month/four-digit-year subset of the official date
        # transforms. Unknown text, ambiguous dates and registries still fail.
        months = ('January February March April May June July August September October November December').split()
        names = {name.lower(): i for i, name in enumerate(months, 1)}
        names.update({name[:3].lower(): i for i, name in enumerate(months, 1)})
        match = re.fullmatch(r'\s*([A-Za-z]+)\.?\s+(\d{1,2})\s*,?\s+(\d{4})\s*', raw)
        if match and match[1].lower() in names:
            return date(int(match[3]), names[match[1].lower()], int(match[2])).isoformat()
    if modern:
        fixed = {'fixed-true': 'true', 'fixed-false': 'false', 'fixed-empty': ''}
        if fmt['local_name'] in fixed:
            return fixed[fmt['local_name']]
        orders = {'date-month-day-year': ('month', 'day', 'year'),
                  'date-day-month-year': ('day', 'month', 'year'),
                  'date-year-month-day': ('year', 'month', 'day')}
        if fmt['local_name'] in orders:
            # Deliberately support four-digit years only; unusual lexical forms
            # remain explicit errors instead of being interpreted heuristically.
            parts = re.fullmatch(r'\s*([0-9]+)[^0-9]+([0-9]+)[^0-9]+([0-9]+)\s*', raw)
            if parts:
                values = dict(zip(orders[fmt['local_name']], parts.groups()))
                if len(values['year']) == 4 and len(values['month']) <= 2 and len(values['day']) <= 2:
                    return date(**{k: int(v) for k, v in values.items()}).isoformat()
    raise ValueError(f'Unsupported nonNumeric transformation: {node.get("format")} (raw text retained)')


class Document:
    def __init__(self, data, source):
        self.source, self.data = source, data
        if b'<!ENTITY' in data.upper():
            raise ValueError('Entity declarations are not supported in filing input.')
        try:
            self.root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
        except etree.XMLSyntaxError as exc:
            raise ValueError('Expected well-formed Inline XBRL XHTML. Use the original SEC HTML, '
                             'not a PDF or converted HTML mirror. ' + str(exc)) from exc
        self.nodes = [n for n in self.root.iter() if isinstance(n.tag, str)]
        self.order = {n: i for i, n in enumerate(self.nodes)}
        self.paths = {n: n.getroottree().getpath(n) for n in self.nodes}
        self.ids, self.hidden = {}, {}
        for n in self.nodes:
            if n.get('id'):
                if n.get('id') in self.ids:
                    raise ValueError(f'Duplicate XML ID: {n.get("id")}')
                self.ids[n.get('id')] = n
            style = re.sub(r'\s+', '', n.get('style', '')).lower()
            self.hidden[n] = (self.hidden.get(n.getparent(), False) or
                              (namespace(n) in IX and local(n) in {'header', 'hidden'}) or
                              local(n) in {'script', 'style', 'noscript'} or n.get('hidden') is not None or
                              'display:none' in style or 'visibility:hidden' in style)
        self.fact_nodes = [n for n in self.nodes if namespace(n) in IX and local(n) in FACT_TAGS]
        if not self.fact_nodes:
            raise ValueError('No Inline XBRL facts found. Converted HTML may have lost its XBRL tags.')
        self.contexts, self.units = {}, {}
        self.resource_errors = {}
        for n in self.nodes:
            if n.tag in {f'{{{XBRLI}}}context', f'{{{XBRLI}}}unit'}:
                key = n.get('id')
                try:
                    if not key:
                        raise ValueError('XBRL resource without an ID')
                    if local(n) == 'context':
                        self.contexts[key] = self.read_context(n)
                    else:
                        self.units[key] = self.read_unit(n)
                except ValueError as exc:
                    self.resource_errors[key] = str(exc)
        # Only normalize XHTML element names for HTML layout helpers. XBRL
        # namespaces, original paths, resources and original fact nodes remain.
        for n in self.nodes:
            if namespace(n) == XHTML:
                n.tag = local(n)
        self._rows, self._texts, self._facts = {}, {}, {}
        # Include outer tables only when they own facts outside their nested
        # tables. Otherwise they are wrappers, not a second copy of the data.
        owners = {next(n.iterancestors('table'), None) for n in self.fact_nodes if not self.hidden[n]}
        self.tables = [n for n in self.nodes if n.tag == 'table' and not self.hidden[n]
                       and (n in owners or not any(x.tag == 'table' for x in n.iterdescendants()))]
        self.blocks = [n for n in self.nodes if n.tag in BLOCKS and not self.hidden[n]
                       and not any(a.tag == 'table' for a in n.iterancestors())
                       and not any(x.tag in BLOCKS for x in n.iterdescendants())]

    def display(self, node):
        if node in self._texts:
            return self._texts[node]
        def visit(n):
            if self.hidden.get(n, False):
                return ''
            parts = [n.text or '']
            for child in n:
                if isinstance(child.tag, str):
                    parts.append(' ' if child.tag == 'br' else visit(child))
                parts.append(child.tail or '')
            return ''.join(parts)
        self._texts[node] = clean(visit(node))
        return self._texts[node]

    def rows(self, table):
        if table not in self._rows:
            trs = [n for n in table.iter('tr') if next(n.iterancestors('table'), None) is table]
            self._rows[table] = [[n for n in tr if n.tag in {'td', 'th'} and not self.hidden.get(n, False)] for tr in trs]
        return self._rows[table]

    def read_context(self, node):
        identifier = node.find(f'{{{XBRLI}}}entity/{{{XBRLI}}}identifier')
        period_node = node.find(f'{{{XBRLI}}}period')
        if identifier is None or period_node is None:
            raise ValueError('Context is missing its entity or period')
        period = {local(n): clean(''.join(n.itertext())) for n in period_node if isinstance(n.tag, str)}
        if set(period) == {'instant'}:
            period['type'] = 'instant'
        elif set(period) == {'startDate', 'endDate'}:
            period['type'] = 'duration'
        elif set(period) == {'forever'}:
            period['type'] = 'forever'
        else:
            raise ValueError('Invalid context period')
        dimensions = []
        for n in node.iter():
            if namespace(n) != XBRLDI or local(n) not in {'explicitMember', 'typedMember'}:
                continue
            d = {'dimension': qname(n, n.get('dimension')), 'kind': local(n),
                 'location': local(n.getparent())}
            if local(n) == 'explicitMember':
                d['member'] = qname(n, clean(''.join(n.itertext())))
            else:
                d['typed_value_xml'] = ''.join(etree.tostring(c, encoding='unicode', with_tail=False) for c in n)
            dimensions.append(d)
        return {'entity': {'scheme': identifier.get('scheme'), 'identifier': clean(''.join(identifier.itertext()))},
                'period': period, 'dimensions': dimensions, 'source_locator': self.paths[node],
                'raw_xml': etree.tostring(node, encoding='unicode', with_tail=False)}

    def read_unit(self, node):
        divide = node.find(f'{{{XBRLI}}}divide')
        if divide is None:
            num = node.findall(f'{{{XBRLI}}}measure')
            den = []
        else:
            num = divide.findall(f'{{{XBRLI}}}unitNumerator/{{{XBRLI}}}measure')
            den = divide.findall(f'{{{XBRLI}}}unitDenominator/{{{XBRLI}}}measure')
            if not den:
                raise ValueError('Unit divide has no denominator')
        if not num:
            raise ValueError('Unit has no measure')
        numerator = [qname(n, clean(''.join(n.itertext()))) for n in num]
        denominator = [qname(n, clean(''.join(n.itertext()))) for n in den]
        label = '*'.join(n['name'] for n in numerator)
        if denominator:
            label += '/' + '*'.join(n['name'] for n in denominator)
        return {'numerator': numerator, 'denominator': denominator, 'label': label,
                'source_locator': self.paths[node]}

    def content(self, node):
        def visit(n):
            if namespace(n) in IX and local(n) == 'exclude':
                return ''
            result = [n.text or '']
            for child in n:
                if isinstance(child.tag, str):
                    result.append(visit(child))
                result.append(child.tail or '')
            return ''.join(result)
        parts, seen, current = [], set(), node
        while current is not None:
            if current in seen:
                raise ValueError('Cyclic Inline XBRL continuation')
            seen.add(current)
            parts.append(visit(current))
            ref = current.get('continuedAt')
            if not ref:
                break
            current = self.ids.get(ref)
            if current is None or namespace(current) not in IX or local(current) != 'continuation':
                raise ValueError(f'Missing Inline XBRL continuation: {ref}')
        return ''.join(parts)

    def fact(self, node):
        if node in self._facts:
            return self._facts[node]
        identifier = node.get('id') or f'@node-{self.order[node]}'
        value = {'fact_id': identifier, 'source_id': node.get('id'), 'kind': local(node),
                 'concept': None, 'context_ref': node.get('contextRef'), 'unit_ref': node.get('unitRef'),
                 'decimals': node.get('decimals'), 'precision': node.get('precision'),
                 'scale': node.get('scale'), 'sign': node.get('sign'), 'format': node.get('format'),
                 'target': node.get('target', ''), 'nil': node.get(f'{{{XSI}}}nil') in {'true', '1'},
                 'raw_text': None, 'value': None, 'status': 'error', 'source_locator': self.paths[node]}
        try:
            value['raw_text'] = self.content(node)
            value['concept'] = qname(node, node.get('name'))
            if node.get(f'{{{XSI}}}nil') not in {None, 'true', 'false', '1', '0'}:
                raise ValueError('Invalid xsi:nil boolean')
            if node.get('target'):
                raise ValueError('Multiple-target Inline XBRL requires a document-set processor')
            if node.get('tupleRef') or any(namespace(n) in IX and local(n) == 'tuple' for n in node.iterancestors()):
                raise ValueError('Tuple facts require a document-set processor')
            if value['context_ref'] not in self.contexts:
                raise ValueError('Missing/invalid context: ' + str(value['context_ref']))
            value['period'] = self.contexts[value['context_ref']]['period']
            if local(node) in {'nonFraction', 'fraction'}:
                if value['unit_ref'] not in self.units:
                    raise ValueError('Missing/invalid unit: ' + str(value['unit_ref']))
                value['unit'] = self.units[value['unit_ref']]['label']
                if local(node) == 'fraction':
                    raise ValueError('ix:fraction is preserved but not evaluated by this extractor')
            if value['nil']:
                value['status'] = 'nil'
            elif local(node) == 'nonFraction':
                if node.get('continuedAt'):
                    raise ValueError('A nonFraction fact cannot use continuedAt')
                for attr in ('decimals', 'precision'):
                    if node.get(attr) and not re.fullmatch(r'INF|[+-]?\d+', node.get(attr)):
                        raise ValueError(f'Invalid {attr}')
                if node.get('decimals') and node.get('precision'):
                    raise ValueError('Both decimals and precision are specified')
                value['value'] = transformed_number(node, value['raw_text'])
                value['status'] = 'ok'
            else:
                value['value'], value['status'] = transformed_text(node, value['raw_text']), 'ok'
        except (ValueError, InvalidOperation) as exc:
            value['error'] = str(exc)
        self._facts[node] = value
        return value


def before_table(doc, node):
    """Visible heading/prose before an embedded table, without its cell text."""
    parts = []
    def visit(n):
        if n.tag == 'table':
            return False
        if doc.hidden.get(n, False):
            return True
        parts.append(n.text or '')
        for child in n:
            if isinstance(child.tag, str) and not visit(child):
                return False
            parts.append(child.tail or '')
        return True
    visit(node)
    return clean(''.join(parts))


def large_type(node):
    return any(float(m.group(1)) >= (12 if m.group(2).lower() == 'pt' else 16)
               for n in node.iter() for m in re.finditer(
                   r'font-size\s*:\s*(\d+(?:\.\d+)?)(pt|px)', n.get('style', ''), re.I))


PAGE_LABEL = r'(?:[A-Za-z]{1,3}[-–—])?\d{1,4}'


def page_parts(value):
    match = re.fullmatch(r'([A-Za-z]{1,3}[-–—])?(\d{1,4})', str(value))
    if not match:
        raise ValueError(f'Invalid printed-page label: {value}')
    return (match.group(1) or '').upper().replace('–', '-').replace('—', '-'), int(match.group(2))


def page_key(value):
    prefix, number = page_parts(value)
    return f'{prefix}{number}' if prefix else number


def page_ranges(value):
    pages = []
    for span in value.split(','):
        match = re.fullmatch(r'\s*(' + PAGE_LABEL + r')(?:\s*(?:[-–—]|through|to)\s*(' + PAGE_LABEL + r'))?\s*', span, re.I)
        if not match:
            raise ValueError(f'Invalid printed-page range: {value}')
        prefix, lo = page_parts(match.group(1))
        end_prefix, hi = page_parts(match.group(2) or match.group(1))
        if prefix != end_prefix or not 0 < lo <= hi <= 2000:
            raise ValueError(f'Invalid printed-page range: {value}')
        pages.extend(f'{prefix}{n}' if prefix else n for n in range(lo, hi + 1))
    return sorted(set(pages), key=page_parts)


def external_report_pages(body, *, report_identified=False):
    """Explicit incorporation of printed pages in a shareholder report.

    Require the report qualifier so a reference to pages of this 10-K does
    not become an external reference. The exhibit link is verified separately.
    """
    incorporated = (r'\b(?:is|are)\s+(?:hereby\s+)?incorporated\s+'
                    r'(?:(?:herein|into\s+(?:this|the)\s+report)\s+)?by\s+reference\b')
    if not re.search(incorporated, body, re.I):
        return None
    span = PAGE_LABEL + r'(?:\s*(?:[-–—]|through|to)\s*' + PAGE_LABEL + r')?'
    pattern = (r'\bpages?\s+(' + span + r'(?:\s*(?:,\s*(?:and\s+)?|and\s+)' + span + r')*)'
               r'\s+of\s+[^.;]{0,100}?\b(?:annual|financial)\s+report\b')
    # An Annual Report without "to Shareholders" must be identified as
    # Exhibit 13 here or in the filing's exhibit index. Its source is verified
    # separately, including when the index entry contains no hyperlink.
    if not (report_identified
            or re.search(r'\b(?:annual|financial)\s+report\s+to\s+(?:shareholders|stockholders)\b', body, re.I)
            or re.search(r'\bExhibit\s+(?:13\b|\(13\))', body, re.I)):
        return None
    # Some Items name the report before its pages, then affirm incorporation
    # in the following sentence. Keep the report qualifier attached to the
    # range so incidental page references cannot broaden the Item.
    report_first = (r'\b(?:annual|financial)\s+report(?:\s+to\s+(?:shareholders|stockholders))?'
                    r'\s+on\s+pages?\s+(' + span + r'(?:\s*(?:,\s*(?:and\s+)?|and\s+)' + span + r')*)')
    matches = re.findall(pattern, body, re.I) + re.findall(report_first, body, re.I)
    if not matches:
        return None
    # Prefer pages in the affirmative incorporation sentence. A descriptive
    # "set forth" sentence may include extra, unincorporated report material.
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z“"])', body)
    explicit = [m for sentence in sentences if re.search(incorporated, sentence, re.I)
                for m in re.findall(pattern, sentence, re.I) + re.findall(report_first, sentence, re.I)]
    selected = explicit or matches
    ranges = {tuple(page_ranges(re.sub(r',?\s*\band\b', ',', m, flags=re.I))) for m in selected}
    if len(ranges) > 1:
        # Separate, affirmative clauses may incorporate different disclosures
        # (statements plus quarterly data). Overlapping, unequal ranges are
        # conflicting scope claims, not a reason to select the wider range.
        ordered = list(ranges)
        if not explicit or any(set(left) & set(right) for i, left in enumerate(ordered) for right in ordered[i + 1:]):
            raise ValueError('Multiple external report page references in one Item; cannot resolve scope uniquely.')
    pages = sorted({page for span in ranges for page in span}, key=page_parts)
    return {'reference_text': body, 'section_paths': [], 'pages': pages,
            'internal_item_references': []}


def unquoted_report_reference(body):
    """Recognize explicit Exhibit 13 incorporation even without quoted titles.

    An unresolved title is retained so the report resolver fails visibly,
    rather than treating a short incorporation paragraph as the complete Item.
    """
    if not (re.search(r'\b(?:annual|financial) report\b', body, re.I)
            and re.search(r'\bExhibit\s+(?:13\b|\(13\))', body, re.I)
            and re.search(r'\b(?:is|are)\s+(?:hereby\s+)?incorporated\s+(?:herein\s+)?by\s+reference\b', body, re.I)):
        return None
    titles = re.findall(r'\binformation\s+(?:contained|appearing|included)\s+in\s+(?:the\s+)?'
                        r'([^.;]{3,180}?)\s+section\b', body, re.I)
    return {'reference_text': body, 'section_paths': [[title] for title in titles],
            'unquoted': True, 'internal_item_references': []}


class Layout:
    """Printed-page and Item evidence, independent of XBRL value decoding."""
    def __init__(self, doc, resolve_references=True):
        self.doc = doc
        self.headings, self.references, self.ranges, self.external = [], [], [], []
        self.excluded, self.layout_headings, self.footers = set(), [], []
        verified_table_titles = set()
        self.breaks = [doc.order[n] for n in doc.nodes if not doc.hidden[n] and re.search(
            r'(?:page-break-(?:before|after)|break-(?:before|after))\s*:\s*(?:always|page)',
            n.get('style', ''), re.I)]
        self.blocks = [(doc.order[b], before_table(doc, b), b) for b in doc.blocks]
        for table in doc.tables:
            rows = doc.rows(table)
            populated = [c for row in rows for c in row if doc.display(c)]
            values = [doc.display(c) for c in populated]
            bottom = any(re.search(r'(?:^|;)\s*bottom\s*:', n.get('style', ''), re.I)
                         for n in [table, *list(table.iterancestors())[:4]])
            printed = (re.fullmatch(r'(\d{1,4})\s+.{1,100}\b\d{4}\s+Annual Report', values[0], re.I)
                       or re.fullmatch(r'.{1,100}\b\d{4}\s+Annual Report\s+(\d{1,4})', values[0], re.I)) if len(values) == 1 else None
            if bottom and printed and not any(namespace(n) in IX for n in table.iterdescendants()):
                self.footers.append((doc.order[table], str(page_key(printed[1]))))
                self.excluded.add(table)
                continue
            # An Item title can head a multi-row contents/heading table. It
            # must be the first populated row, standalone, styled and unlinked;
            # ordinary TOC entries and numerical grids are not body headings.
            first_row = next(([c for c in row if doc.display(c)] for row in rows
                              if any(doc.display(c) for c in row)), [])
            title = doc.display(first_row[0]) if len(first_row) == 1 else ''
            item_title = ITEM_RE.match(title)
            links = first_row[0].xpath('.//a/@href') if len(first_row) == 1 else []
            # A title may link to its own immediately preceding anchor. A
            # link to a later body section is a TOC entry, never a heading.
            self_links = all(href.startswith('#') and href[1:] in doc.ids
                             and 0 <= doc.order[table] - doc.order[doc.ids[href[1:]]] <= 8
                             and not doc.display(doc.ids[href[1:]]) for href in links)
            if (len(values) > 1 and item_title and item_title[2]
                    and len(title) <= 260 and not re.match(r'(?:see|refer)\b', item_title[2], re.I)
                    and self_links
                    and not any(namespace(n) in IX and local(n) in {'nonFraction', 'fraction'}
                                for n in table.iterdescendants())
                    and (html_tables.bold_block(first_row[0]) or large_type(first_row[0]))):
                self.layout_headings.append(table)
                self.blocks.append((doc.order[first_row[0]], title, first_row[0]))
                verified_table_titles.add(first_row[0])
                self.excluded.add(table)
                continue
            if not values or len(values) > 4:
                continue
            bottom = any(re.search(r'(?:^|;)\s*bottom\s*:', n.get('style', ''), re.I)
                         for n in [table, *list(table.iterancestors())[:4]])
            pages = [s for s in values if re.fullmatch(PAGE_LABEL, s)]
            # Some filers put the page label and its page break in a tiny
            # table, without absolute-position/bottom CSS. Require a dedicated
            # footer (only the label and an optional TOC link), never a grid.
            embedded_break = any(doc.order[n] in self.breaks for n in table.iterdescendants())
            footer_only = (len(pages) == 1 and all(v == pages[0] or v.lower() == 'table of contents' for v in values)
                           and all(doc.display(c) == pages[0] or c.xpath('.//a[starts-with(@href,"#")]') for c in populated))
            if ((bottom or (embedded_break and footer_only)) and len(pages) == 1 and sum(map(len, values)) <= 250
                    and sum(any(doc.display(c) for c in row) for row in rows) == 1
                    and not any(n in doc.fact_nodes for n in table.iterdescendants())):
                self.footers.append((doc.order[table], str(page_key(pages[0]))))
                self.excluded.add(table)
            elif (len(values) == 2 and re.fullmatch(r'ITEM\s+\d{1,2}[A-C]?\.', values[0], re.I)
                  and len(values[1]) < 220 and (values[1].isupper() or html_tables.bold_block(table))):
                self.layout_headings.append(table)
                self.blocks.append((doc.order[table], ' '.join(values), table))
                self.excluded.add(table)
            elif (len(values) == 1 and len(values[0]) <= 180
                  and not any(namespace(n) in IX and local(n) in FACT_TAGS for n in table.iterdescendants())
                  and (html_tables.bold_block(populated[0]) or large_type(populated[0]))):
                self.layout_headings.append(table)
                self.blocks.append((doc.order[table], values[0], table))
                self.excluded.add(table)
        self.blocks.sort(key=lambda b: b[0])
        last_block_position = max((pos for pos, value, _ in self.blocks if value), default=-1)
        for pos, value, node in self.blocks:
            match = ITEM_RE.match(value)
            # Empty named anchors mark a location; they are not TOC links.
            navigation = [a for a in node.xpath('.//a[starts-with(@href,"#")]')
                          if not (a.get('href') == '#' and (a.get('id') or a.get('name'))
                                  and not doc.display(a) and not a.xpath('.//img'))]
            if match and len(value) <= 260 and (node in verified_table_titles or not navigation):
                title = match.group(2)
                if ((html_tables.bold_block(node) or title.isupper() or large_type(node))
                        and not re.match(r'(?:see|refer)\b', title, re.I)):
                    self.headings.append({'item': match.group(1).upper(), 'position': pos,
                                          'text': value, 'source_locator': doc.paths[node]})
            match = re.fullmatch(r'(' + PAGE_LABEL + r')(?:\s*\|\s*\d{4}\s+10-K)?', value, re.I)
            report_footer = False
            if not match:
                # Publisher footers can put the printed page before or after
                # the issuer/year/report label (alternating left/right pages).
                match = re.fullmatch(r'(\d{1,4})\s+.{1,100}\b\d{4}\s+Annual Report', value, re.I)
                if not match:
                    match = re.fullmatch(r'.{1,100}\b\d{4}\s+Annual Report\s+(\d{1,4})', value, re.I)
                report_footer = bool(match)
            if not match:
                # Publisher abbreviations beside a page number are accepted
                # only inside an explicitly positioned bottom footer.
                bottom = any(re.search(r'(?:^|;)\s*bottom\s*:', a.get('style', ''), re.I)
                             for a in [node, *list(node.iterancestors())[:3]])
                if bottom:
                    decorated = re.fullmatch(r'(\d{1,4})\s+[A-Za-z][A-Za-z .&]{1,55}', value)
                    if not decorated:
                        decorated = re.fullmatch(r'[A-Za-z][A-Za-z .&]{1,55}\s+(\d{1,4})', value)
                    match = decorated
            if (match and node.tag != 'table'
                    and not any(namespace(n) in IX and local(n) in {'nonFraction', 'fraction'} for n in node.iter())):
                following = bisect.bisect_right(self.breaks, pos)
                near = following < len(self.breaks) and self.breaks[following] - pos < 35
                bottom = any(re.search(r'bottom\s*:', n.get('style', ''), re.I)
                             for n in [node, *list(node.iterancestors())[:3]])
                if near or bottom or '10-K' in value or (report_footer and pos == last_block_position):
                    self.footers.append((pos, str(page_key(match.group(1)))))
        self.footers.sort()
        self.footer_positions = [p for p, _ in self.footers]
        self.index_references = self.read_index() if not self.headings else []
        self.references.extend(self.index_references)
        self.image_gaps = self.read_image_gaps()
        if resolve_references:
            self.read_body_references()
            self.read_primary_financial_appendix()
            self.read_financial_index()

    def page(self, node):
        pos = self.doc.order[node]
        fi = bisect.bisect_right(self.footer_positions, pos)
        bi = bisect.bisect_right(self.breaks, pos)
        if fi < len(self.footers) and (bi == len(self.breaks) or self.footers[fi][0] < self.breaks[bi]):
            return self.footers[fi][1]
        return None

    def read_index(self):
        indexes = []
        blocks = [(pos, text) for pos, text, _ in self.blocks if text]
        positions = [pos for pos, _ in blocks]
        for table in self.doc.tables:
            previous = bisect.bisect_left(positions, self.doc.order[table]) - 1
            numbered = bool(previous >= 0 and re.fullmatch(
                r'FORM\s+10-K\s+CROSS[\s-]REFERENCE\s+INDEX', blocks[previous][1], re.I))
            if numbered:
                entries = self.read_numbered_index(table)
                if entries:
                    indexes.append(entries)
                    self.excluded.add(table)
                    continue
                # An explicit cross-reference index may still use the older
                # Item-prefixed, Page/Pages layout (including continuation
                # rows). If the bare-number parser found no usable index,
                # let the existing parser inspect the same table.
            entries, current = [], None
            for row in self.doc.rows(table):
                values = [self.doc.display(c) for c in row]
                if len(values) < 2:
                    continue
                match = re.fullmatch(r'Item\s+(\d{1,2}[A-C]?)\s*[.:]?', values[0], re.I)
                if match:
                    current = {'item': match.group(1).upper(), 'pages': [], 'evidence': [],
                               'source_locator': self.doc.paths[table],
                               'method': 'item_page_index'}
                    entries.append(current)
                elif values[0]:
                    current = None
                if current is not None and len(values) >= 3:
                    pages = re.fullmatch(r'Pages?\s+(\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*)'
                                         r'(?:\s*\([a-z]\))?', values[-1], re.I)
                    if pages:
                        current['pages'].extend(page_ranges(pages.group(1)))
                        current['evidence'].append(' | '.join(values))
            if len(entries) >= 3 and sum(bool(e['pages']) for e in entries) >= 2:
                if len({e['item'] for e in entries}) != len(entries):
                    raise ValueError('Repeated Items in the page index; cannot resolve scope uniquely.')
                for entry in entries:
                    entry['pages'] = sorted(set(entry['pages']))
                indexes.append(entries)
                self.excluded.add(table)
        if len(indexes) > 1:
            raise ValueError('Multiple Item/page indexes; cannot resolve scope uniquely.')
        return indexes[0] if indexes else []

    def read_numbered_index(self, table):
        """Explicit Form 10-K cross-reference index, including wrapped ranges."""
        entries, current = [], None
        for row in self.doc.rows(table):
            values = [self.doc.display(c) for c in row if self.doc.display(c)]
            if not values:
                continue
            match = re.fullmatch(r'(?:Item\s+)?(\d{1,2}[A-C]?)\.', values[0], re.I)
            if match and len(values) in {2, 3} and re.search(r'[A-Za-z]', values[1]):
                current = {'item': match[1].upper(), 'pages': [], 'evidence': [' | '.join(values)],
                           'source_locator': self.doc.paths[table], 'method': 'numbered_item_page_index',
                           'fragments': [values[2]] if len(values) == 3 else []}
                entries.append(current)
            elif (current is not None and len(values) == 1 and current['fragments']
                  and current['fragments'][-1].endswith(',')
                  and re.fullmatch(r'[\d,\s–—-]+', values[0])):
                current['fragments'].append(values[0])
                current['evidence'].append(values[0])
            else:
                current = None
        if len(entries) < 3:
            return []
        if len({e['item'] for e in entries}) != len(entries):
            raise ValueError('Repeated Items in the page index; cannot resolve scope uniquely.')
        for entry in entries:
            text = ' '.join(entry.pop('fragments'))
            if text and re.fullmatch(r'[\d,\s–—-]+', text):
                entry['pages'] = page_ranges(text)
            elif text and text.lower() != 'not applicable':
                # Other Items can point to a proxy statement rather than
                # filing pages. Retain the unresolved reference and reject
                # it only when that Item is requested.
                entry['unresolved_page_reference'] = text
        return entries if sum(bool(e['pages']) for e in entries) >= 2 else []

    def read_image_gaps(self):
        gaps, doc = [], self.doc
        for (left_pos, left), (right_pos, right) in zip(self.footers, self.footers[1:]):
            # Image-only inference currently applies only to numeric indexes;
            # prefixed pages must have their actual printed labels verified.
            if not left.isdigit() or not right.isdigit():
                continue
            expected = list(range(int(left) + 1, int(right)))
            if not expected or not any(set(expected) & set(e['pages']) for e in self.references):
                continue
            first, last = bisect.bisect_right(self.breaks, left_pos), bisect.bisect_right(self.breaks, right_pos)
            if last - first - 1 != len(expected):
                continue
            images = []
            for page, boundary in zip(expected, range(first, last - 1)):
                nodes = [n for n in doc.nodes[self.breaks[boundary] + 1:self.breaks[boundary + 1]] if not doc.hidden[n]]
                candidates = [n for n in nodes if n.tag == 'img']
                if len(candidates) != 1 or any(n.tag == 'table' or clean(n.text or '') or clean(n.tail or '') for n in nodes):
                    break
                img, sizes = candidates[0], {}
                for key in ('width', 'height'):
                    size = re.search(rf'(?:^|;)\s*{key}\s*:\s*(\d+(?:\.\d+)?)(px|pt)\b', img.get('style', ''), re.I)
                    if size:
                        sizes[key] = float(size.group(1)) * (4 / 3 if size.group(2).lower() == 'pt' else 1)
                    elif re.fullmatch(r'\d+(?:\.\d+)?', img.get(key, '')):
                        sizes[key] = float(img.get(key))
                if sizes.get('width', 0) < 400 or sizes.get('height', 0) < 600 or not img.get('src'):
                    break
                images.append({'inferred_page': str(page), 'image_src': img.get('src'), 'source_locator': doc.paths[img]})
            if len(images) == len(expected):
                gaps.extend(images)
        return gaps

    def body(self, heading):
        start = heading['position']
        end = next((h['position'] for h in self.headings if h['position'] > start), len(self.doc.nodes))
        # Text-node scanning also includes inline prose following layout-table
        # Item headings, which ordinary paragraph-only extraction loses.
        parts = []
        for n in self.doc.nodes[start + 1:end]:
            if self.doc.hidden[n]:
                continue
            if n.tag != 'table' and not any(a.tag == 'table' for a in n.iterancestors()):
                parts.append(n.text or '')
            if not any(a.tag == 'table' for a in n.iterancestors()):
                parts.append(n.tail or '')
        return start, end, clean(' '.join(parts))

    def read_body_references(self):
        subjects = {'7': r"management[’']s discussion and analysis", '8': r'(?:the )?consolidated financial statements'}
        report_identified = next(report_exhibit_rows(self.doc), None) is not None
        for heading in self.headings:
            start, end, body = self.body(heading)
            if len(body) > 1800:
                continue
            item = heading['item']
            if (item == '8' and report_identified and re.search(r'\bItem\s+15\b', body, re.I)
                    and re.search(r'incorporated\s+(?:herein\s+)?by\s+reference', body, re.I)):
                delegated = self.delegated_report_pages()
                if delegated:
                    self.external.append({'item': item, 'reference_text': body, 'section_paths': [],
                        'pages': delegated['pages'], 'index_evidence': delegated['entries'],
                        'source_locator': heading['source_locator'], 'internal_item_references': []})
                    continue
            if (item == '8' and report_identified and re.search(r'listed under Item 15', body, re.I)
                    and re.search(r'Annual Report.*incorporated herein by reference', body, re.I)):
                self.external.append({'item': item, 'reference_text': body, 'section_paths': [],
                    'linked_statement_index': self.linked_statement_index(),
                    'selected_financial_data': bool(re.search(r'Selected Financial Data', body, re.I)),
                    'source_locator': heading['source_locator'], 'internal_item_references': []})
                continue
            tables = [t for t in self.doc.tables if start < self.doc.order[t] < end and t not in self.excluded]
            if tables:
                inventory = self.item_statement_inventory(tables, body) if item == '8' else []
                if inventory:
                    reference = narrative.report_reference([narrative.FilingBlock(start, 'p', body)]) if re.search(r'["“].+["”]', body) else None
                    paths = [[entry['title']] for entry in inventory]
                    paths += [path for path in (reference or {}).get('section_paths', []) if path not in paths]
                    self.external.append({'item': item, 'reference_text': body, 'section_paths': paths,
                        'internal_item_references': [], 'index_evidence': inventory,
                        'source_locator': heading['source_locator'], 'statement_inventory': True})
                    self.excluded.update(tables)
                elif (item == '8' and re.search(r'\bfollowing consolidated financial statements\b', body, re.I)
                      and unquoted_report_reference(body)):
                    raise ValueError('Item 8 incorporates a statement inventory, but its table cannot be verified.')
                continue
            if item == '7' and report_identified:
                md = re.search(r'\bis set forth in the MD&A(?:\s+and\s+Notes?\s+([\d,\s]+(?:and\s+\d+)?)'
                    r'\s+of the Notes to Consolidated Financial Statements)?\s+in the Annual Report,?\s+'
                    r'which portions? (?:is|are) incorporated (?:herein )?by reference', body, re.I)
                if md:
                    paths = [['MD&A']]
                    paths += [['Notes to Consolidated Financial Statements', 'Note ' + n]
                              for n in dict.fromkeys(re.findall(r'\d+', md[1] or ''))]
                    self.external.append({'item': item, 'reference_text': body, 'section_paths': paths,
                        'toc_hierarchy': True, 'source_locator': heading['source_locator'], 'internal_item_references': []})
                    continue
            reference = external_report_pages(body, report_identified=report_identified)
            if reference:
                self.external.append({'item': item, **reference, 'source_locator': heading['source_locator']})
                continue
            if item in subjects:
                matches = []
                for pos, value, node in self.blocks:
                    if not start < pos < end or not re.match(subjects[item] + r'\b', value, re.I):
                        continue
                    m = re.search(r'\b(?:appears?|(?:is|are) (?:set forth|included|presented))\s+on\s+pages?\s+(\d+\s*[-–—]\s*\d+)\b', value, re.I)
                    if m:
                        matches.append({'item': item, 'pages': page_ranges(m.group(1)), 'evidence': value,
                                        'source_locator': self.doc.paths[node], 'method': 'body_page_reference'})
                if len(matches) > 1:
                    raise ValueError(f'Multiple primary page references in Item {item}.')
                self.references.extend(matches)
                if matches:
                    continue
            # Named external report incorporation (e.g. Exhibit 13). In-document
            # Item 8 references are resolved separately through their anchors.
            if re.search(r'["“].+["”]', body) and re.search(r'incorporat\w*.*reference', body, re.I):
                reference = narrative.report_reference([narrative.FilingBlock(start, 'p', body)])
                if reference:
                    self.external.append({'item': item, **reference, 'source_locator': heading['source_locator']})
            else:
                reference = unquoted_report_reference(body)
                if reference:
                    if item == '8' and re.search(r'consolidated financial statements', body, re.I):
                        inventory = self.external_statement_inventory()
                        reference['section_paths'] = [[entry['title']] for entry in inventory]
                        reference['index_evidence'] = inventory
                    self.external.append({'item': item, **reference, 'source_locator': heading['source_locator']})

    def delegated_report_pages(self):
        """Item 8 delegates to an Item 15 inventory explicitly using report pages."""
        headings = [h for h in self.headings if h['item'] == '15']
        if len(headings) != 1:
            return None
        start, end, body = self.body(headings[0])
        if not (re.search(r'page numbers refer to pages of the annual report', body, re.I)
                and re.search(r'incorporated by reference', body, re.I)):
            return None
        candidates = []
        for table in self.doc.tables:
            if not start < self.doc.order[table] < end:
                continue
            if any(namespace(n) in IX for n in table.iterdescendants()):
                continue
            entries = []
            for row in self.doc.rows(table):
                cells = [c for c in row if self.doc.display(c)]
                if len(cells) != 2:
                    continue
                title, pages = map(self.doc.display, cells)
                if not re.match(r'^(?:Consolidated |Notes to (?:the )?Consolidated Financial Statements|'
                                r'Report of Independent Registered Public Accounting Firm)', title, re.I):
                    continue
                if not re.fullmatch(PAGE_LABEL + r'(?:\s*[-–—]\s*' + PAGE_LABEL + r')?', pages):
                    raise ValueError('Item 15 incorporated statement inventory has an invalid page range')
                entries.append({'title': title, 'pages': page_ranges(pages),
                                'source_locator': self.doc.paths[cells[0]]})
            if (sum(e['title'].lower().startswith('consolidated ') for e in entries) >= 3
                    and sum(e['title'].lower().startswith('notes to ') for e in entries) == 1):
                candidates.append(entries)
        if len(candidates) != 1:
            raise ValueError('Item 15 must identify one incorporated statement/page inventory')
        entries = candidates[0]
        if any(page_parts(a['pages'][-1]) >= page_parts(b['pages'][0]) for a, b in zip(entries, entries[1:])):
            raise ValueError('Item 15 incorporated statement page ranges overlap or are out of order')
        return {'entries': entries, 'pages': [p for e in entries for p in e['pages']]}

    def item_statement_inventory(self, tables, body):
        """An explicitly incorporated statement list is not a financial grid."""
        if (not re.search(r'\bfollowing consolidated financial statements\b', body, re.I)
                or not unquoted_report_reference(body)):
            return []
        entries = []
        for table in tables:
            if any(namespace(n) in IX for n in table.iterdescendants()):
                return []
            for row in self.doc.rows(table):
                cells = [c for c in row if self.doc.display(c)]
                if not cells:
                    continue
                if len(cells) != 1:
                    return []
                value = self.doc.display(cells[0])
                title = re.split(r'\s+(?:at|as of)\s+\w+\s+\d|\s+for (?:the )?years? ended\b', value, flags=re.I)[0].strip()
                if not re.fullmatch(r"(?:Management[’']s Annual Report on Internal Control Over Financial Reporting|"
                        r'Reports? of Independent Registered Public Accounting Firm|'
                        r'Consolidated (?:Statements of [A-Za-z\s’\x27(),-]+|Balance Sheets)|'
                        r'Notes to (?:the )?Consolidated Financial Statements)', title, re.I):
                    return []
                entries.append({'title': title, 'evidence': value, 'source_locator': self.doc.paths[cells[0]],
                                'document_id': 'primary', 'source': self.doc.source})
        statements = [e for e in entries if e['title'].lower().startswith('consolidated ')]
        notes = [e for e in entries if e['title'].lower().startswith('notes to ')]
        auditors = [e for e in entries if re.match(r'Reports? of Independent', e['title'], re.I)]
        return entries if len(statements) >= 3 and len(notes) == 1 and len(auditors) == 1 else []

    def linked_statement_index(self):
        """Item 8 explicitly delegates its statement list to Item 15."""
        headings = [h for h in self.headings if h['item'] == '15']
        if len(headings) != 1:
            raise ValueError('Cannot verify the incorporated Item 15 statement index')
        start, end, _ = self.body(headings[0])
        candidates = []
        for table in self.doc.tables:
            if not start < self.doc.order[table] < end:
                continue
            entries = []
            for row in self.doc.rows(table):
                cells = [c for c in row if self.doc.display(c)]
                if not cells:
                    continue
                if len(cells) != 2 or not re.fullmatch(PAGE_LABEL, self.doc.display(cells[1])):
                    break
                title = self.doc.display(cells[0])
                title = re.split(r'\s+(?:as of|at)\s+\w+\s+\d|\s+for (?:the )?years? ended\b|\s+\(', title, flags=re.I)[0]
                links = set(cells[0].xpath('.//a/@href'))
                if (len(links) != 1 or not re.match(r'^(?:Consolidated (?:Balance Sheets|Statements of )|'
                        r'Notes to (?:the )?Consolidated Financial Statements|Report of Independent)', title, re.I)):
                    break
                href = links.pop()
                report, anchor = urldefrag(href)
                if not anchor or not re.search(r'\.html?$', report, re.I):
                    break
                entries.append({'title': title, 'page': str(page_key(self.doc.display(cells[1]))),
                    'href': href, 'anchor': anchor, 'source_locator': self.doc.paths[cells[0]]})
            else:
                if (sum(e['title'].lower().startswith('consolidated ') for e in entries) >= 3
                        and sum(e['title'].lower().startswith('notes to ') for e in entries) == 1
                        and entries[-1]['title'].lower().startswith('notes to ')
                        and len({urldefrag(e['href'])[0] for e in entries}) == 1):
                    candidates.append(entries)
        if len(candidates) != 1:
            raise ValueError('Cannot verify one linked Item 15 statement index')
        return candidates[0]

    def external_statement_inventory(self):
        """Use Item 15's explicit report references to expand the statement set."""
        headings = [h for h in self.headings if h['item'] == '15']
        if len(headings) != 1:
            return []
        start, end, _ = self.body(headings[0])
        inventory = []
        for pos, value, node in self.blocks:
            if not start < pos < end:
                continue
            parts = re.split(r'\s+[-–—]\s+Incorporated\s+by\s+reference\s+from\b', value, flags=re.I)
            if len(parts) != 2 or not re.search(r'\bAnnual Report\b', parts[1], re.I):
                continue
            title = re.split(r'\s+(?:as of|for (?:the )?(?:years? ended|years? ending))\b|\s*\(PCAOB\b', parts[0], flags=re.I)[0].strip()
            if re.match(r'^(?:Consolidated (?:Balance Sheets|Statements of )|Notes to (?:the )?Consolidated Financial Statements|Reports? of Independent Registered Public Accounting Firm)', title, re.I):
                inventory.append({'title': title, 'evidence': value, 'source_locator': self.doc.paths[node],
                                  'document_id': 'primary', 'source': self.doc.source})
        statements = [e for e in inventory if re.match(r'Consolidated (?:Balance Sheets|Statements of )', e['title'], re.I)]
        notes = [e for e in inventory if re.match(r'Notes to ', e['title'], re.I)]
        return inventory if len(statements) >= 3 and len(notes) == 1 else []

    def primary_index_target(self, anchor, page, title, *, heading_spans=False):
        """Require a unique visible fragment, its printed page and nearby title."""
        matches = [n for n in self.doc.nodes if n.get('id') == anchor or n.get('name') == anchor]
        if len(matches) != 1 or self.doc.hidden[matches[0]]:
            raise ValueError('Primary appendix index has a missing, duplicate or hidden target')
        position = self.doc.order[matches[0]]
        limit = position + 12
        while position < len(self.doc.nodes) and self.page(self.doc.nodes[position]) != page:
            n = self.doc.nodes[position]
            if position >= limit or clean(n.text or '') or clean(n.tail or '') or namespace(n) in IX:
                raise ValueError('Primary appendix link disagrees with its printed page')
            position += 1
        if position == len(self.doc.nodes):
            raise ValueError('Primary appendix link has no printed page')
        # Dates and units are presentation suffixes, not statement identities.
        title = re.split(r'\s+(?:at|as of|for)\b|\s+(?:twelve|12) months ended\b',
                         title, maxsplit=1, flags=re.I)[0]
        compact = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
        def title_key(text):
            text = re.sub(r'\s*\(in (?:millions|thousands|billions)'
                          r'(?:,? except (?:per )?share (?:data|amounts))?\)\s*$', '', text, flags=re.I)
            return compact(text)
        nearby = [text for pos, text, n in self.blocks
                  if position - 30 <= pos < position + 70 and self.page(n) == page]
        if heading_spans:
            # Some statement headings wrap both a title span and a table;
            # the leaf-block inventory intentionally omits that wrapper.
            nearby += [self.doc.display(n) for n in self.doc.nodes[position:position + 70]
                       if n.tag == 'span' and not self.doc.hidden[n] and self.page(n) == page
                       and html_tables.bold_block(n) and len(self.doc.display(n)) <= 300]
        banners = [compact(self.doc.display(n)) for n in self.doc.fact_nodes
                   if (n.get('name') or '').split(':')[-1] == 'EntityRegistrantName']
        expected = title_key(title)
        prefixes = {''} | {b + suffix for b in banners for suffix in ('', 'andsubsidiaries')}
        actual = {title_key(t) for t in nearby}
        if any(prefix + expected in actual for prefix in prefixes):
            return position
        # A known truncated index title still needs the exact fragment, page
        # and one unambiguous comprehensive-income heading at its destination.
        if expected == 'consolidatedstatementsofcomprehensive':
            suffixes = ('income', 'loss', 'incomeloss', 'lossincome')
            matching = {key for key in actual if any(key == prefix + expected + suffix
                        for prefix in prefixes for suffix in suffixes)}
            if len(matching) == 1:
                return position
        raise ValueError(f'Primary appendix target title cannot be verified: {title}')

    def custom_financial_note(self, node):
        """A custom TextBlock needs a visible, numbered note heading as evidence."""
        try:
            concept = qname(node, node.get('name'))
        except ValueError:
            return False
        if not concept['local_name'].endswith('TextBlock') or any(
                token in concept['local_name'] for token in ('TableTextBlock', 'ScheduleOf')):
            return False
        position = self.doc.order[node]
        for pos, text, block in self.blocks:
            match = re.fullmatch(r'Note\s+\d+[.\s:–—-]+(.+)', text, re.I)
            if (match and 0 <= position - pos <= 12 and self.page(block) == self.page(node)
                    and (html_tables.bold_block(block) or large_type(block))
                    and clean(match[1]).casefold() == self.doc.display(node).casefold()
                    and node in block.iterdescendants()):
                return True
        return False

    def read_primary_financial_appendix(self):
        """Verify an Item 8 appendix in this filing, even after Item 16.

        An explicit page range or Item 15 statement index supplies authority.
        Links, printed pages and titles must agree; physical document order
        alone never turns a later appendix into Item 8.
        """
        h8 = [h for h in self.headings if h['item'] == '8']
        if len(h8) != 1 or any(e['item'] == '8' for e in self.external + self.references + self.ranges):
            return
        _, _, evidence = self.body(h8[0])
        if len(evidence) > 1800 or re.search(r'\b(?:not|no)\s+(?:filed|included|incorporated|presented)\b', evidence, re.I):
            return
        span = '(' + PAGE_LABEL + r'\s*(?:through|to|[-–—])\s*' + PAGE_LABEL + ')'
        explicit = re.findall(r'\bincluded in this (?:Annual Report|Form 10-K) on pages\s+' + span,
                              evidence, re.I)
        explicit += re.findall(r'\b(?:appear|are included|are presented)\s+(?:at|on)\s+pages\s+' + span
                               + r'\s+of this (?:Annual Report|Form 10-K)', evidence, re.I)
        if explicit:
            if len(set(explicit)) != 1:
                raise ValueError('Item 8 has conflicting primary financial statement page ranges')
            pages = page_ranges(explicit[0])
            labels = Counter(page_key(p) for _, p in self.footers)
            selected = [(pos, page_key(p)) for pos, p in self.footers if page_key(p) in pages]
            if ([p for _, p in selected] != pages or any(labels[p] != 1 for p in pages)
                    or any(bisect.bisect_right(self.breaks, b[0]) - bisect.bisect_right(self.breaks, a[0]) != 1
                           for a, b in zip(selected, selected[1:]))):
                raise ValueError('Item 8 primary appendix pages are missing, duplicate or discontinuous')
            self.references.append({'item': '8', 'pages': pages, 'evidence': evidence,
                                    'source_locator': h8[0]['source_locator'], 'method': 'primary_appendix_pages'})
            return
        h15 = [h for h in self.headings if h['item'] == '15']
        h16 = [h for h in self.headings if h['item'] == '16']
        if (len(h15) != 1 or len(h16) != 1
                or not re.search(r'\bItem\s+15\b', evidence, re.I)
                or not re.search(r'financial statements', evidence, re.I)
                or not re.search(r'this (?:Annual Report|Form 10-K)', evidence, re.I)
                or not re.search(r'\b(?:are (?:filed|included)|filed as part|See Part IV)\b', evidence, re.I)):
            return
        start15, end15, body15 = self.body(h15[0])
        # The explicit cited-index-page resolver is stricter and already
        # handles this separate layout (including its linked end boundary).
        if re.search(r'Index to .*Financial Statements.*\bon\s+Page\b', body15, re.I):
            return
        candidates = []
        for table in self.doc.tables:
            if not start15 < self.doc.order[table] < end15 or any(
                    namespace(n) in IX and local(n) in {'nonFraction', 'fraction'} for n in table.iter()):
                continue
            entries = []
            for row in self.doc.rows(table):
                cells = [c for c in row if self.doc.display(c)]
                title = ' '.join(self.doc.display(c) for c in cells
                                 if not re.fullmatch(PAGE_LABEL, self.doc.display(c)))
                kind = ('statement' if re.match(r'^Consolidated (?:Balance Sheets|Statements of )', title, re.I)
                        else 'notes' if re.match(r'^Notes to (?:the )?Consolidated Financial Statements\b', title, re.I)
                        else None)
                if kind:
                    entries.append({'title': title, 'kind': kind,
                        'links': {a.get('href') for c in cells for a in c.iter('a') if a.get('href')},
                        'pages': [str(page_key(self.doc.display(c))) for c in cells
                                  if re.fullmatch(PAGE_LABEL, self.doc.display(c))]})
            if sum(e['kind'] == 'statement' for e in entries) >= 3 and sum(e['kind'] == 'notes' for e in entries) == 1:
                candidates.append((table, entries))
        if not candidates:
            return
        if len(candidates) != 1:
            raise ValueError('Item 15 has multiple candidate primary statement indexes')
        table, entries = candidates[0]
        # Leave ordinary, pre-Item-16 statement indexes to the existing path.
        targets = [self.doc.ids.get(next(iter(e['links']))[1:]) for e in entries
                   if len(e['links']) == 1 and next(iter(e['links'])).startswith('#')]
        if targets and all(n is not None and self.doc.order[n] < h16[0]['position'] for n in targets):
            return
        verified = []
        for entry in entries:
            if len(entry['links']) != 1 or not next(iter(entry['links'])).startswith('#') or len(entry['pages']) != 1:
                raise ValueError('Primary appendix index requires unique internal links and printed pages')
            anchor, page = next(iter(entry['links']))[1:], entry['pages'][0]
            position = self.primary_index_target(anchor, page, re.split(r'\s*[—–]\s*', entry['title'])[0])
            verified.append({'title': entry['title'], 'kind': entry['kind'], 'anchor': anchor,
                             'page': page, 'position': position})
        positions = [e['position'] for e in verified]
        if (positions != sorted(set(positions)) or positions[0] <= h16[0]['position']
                or verified[-1]['kind'] != 'notes'):
            raise ValueError('Primary appendix targets are repeated, out of order or outside the appendix')
        # The first subsequent standalone schedule/exhibit heading closes the
        # notes. An appendix ending the file instead needs a closed XBRL note
        # continuation chain and no later visible content beyond its footer.
        boundary = next(((pos, n) for pos, text, n in self.blocks if pos > positions[-1]
                         and len(text) <= 180 and (html_tables.bold_block(n) or large_type(n) or text.isupper())
                         and re.match(r'^(?:Schedule\s+[IVX]+\s*[-–—.:]|Index to Exhibits$|Exhibit Index$|Signatures$)', text, re.I)), None)
        schedule_in_item8 = False
        if boundary and re.search(r'Financial Statement Schedule\s+listed\b.{0,120}\bItem\s+15\s*\(a\)\s*\(2\)', evidence, re.I):
            # An explicit Item 8 incorporation can include the schedule too.
            # Verify its separate Item 15 index entry; a nearby schedule alone
            # is insufficient authority (the default boundary stays outside).
            schedule_entries = []
            for candidate in self.doc.tables:
                if not start15 < self.doc.order[candidate] < end15:
                    continue
                for row in self.doc.rows(candidate):
                    cells = [c for c in row if self.doc.display(c)]
                    title = ' '.join(self.doc.display(c) for c in cells
                                     if not re.fullmatch(PAGE_LABEL, self.doc.display(c)))
                    if not re.match(r'^Schedule\s+[IVX]+\s*[-–—.:]', title, re.I):
                        continue
                    links = {a.get('href') for c in cells for a in c.iter('a') if a.get('href')}
                    pages = [str(page_key(self.doc.display(c))) for c in cells if re.fullmatch(PAGE_LABEL, self.doc.display(c))]
                    if len(links) == 1 and next(iter(links)).startswith('#') and len(pages) == 1:
                        target = self.doc.ids.get(next(iter(links))[1:])
                        if (target is not None and not self.doc.hidden[target]
                                and 0 <= boundary[0] - self.doc.order[target] < 40
                                and self.page(boundary[1]) == pages[0]
                                and styled_title_key(re.split(r'\s+for\b', title, maxsplit=1, flags=re.I)[0])
                                    == styled_title_key(before_table(self.doc, boundary[1]))):
                            schedule_entries.append({'title': title, 'page': pages[0], 'anchor': next(iter(links))[1:]})
            if len(schedule_entries) != 1:
                raise ValueError('Item 8 incorporates a schedule whose index destination cannot be verified')
            schedule_in_item8 = True
            boundary = None
        if boundary:
            last = boundary[0]
            for parent in boundary[1].iterancestors():
                pos = self.doc.order[parent]
                if (namespace(parent) in IX and local(parent) == 'nonNumeric'
                        and not any(clean(n.text or '') for n in self.doc.nodes[pos:last])):
                    last = pos
            end_method = 'following_schedule_or_exhibits'
        else:
            notes = [n for n in self.doc.fact_nodes if self.doc.order[n] >= positions[-1] and not self.doc.hidden[n]
                     and local(n) == 'nonNumeric' and (n.get('name') or '').endswith('TextBlock')
                     and 'TableTextBlock' not in n.get('name', '')
                     and (schedule_in_item8 or self.custom_financial_note(n) or ('ScheduleOf' not in n.get('name', '')
                          and re.fullmatch(r'https?://fasb.org/us-gaap/\d{4}',
                                           n.nsmap.get(n.get('name', '').split(':')[0], ''))))]
            if not notes:
                raise ValueError('Primary appendix has no verifiable final note boundary')
            current, chain = max(notes, key=self.doc.order.get), []
            while current is not None:
                if current in chain:
                    raise ValueError('Cyclic primary appendix note continuation')
                chain.append(current)
                ref = current.get('continuedAt')
                current = self.doc.ids.get(ref) if ref else None
                if ref and (current is None or namespace(current) not in IX or local(current) != 'continuation'):
                    raise ValueError('Invalid primary appendix note continuation')
            last = max(self.doc.order[n] for part in chain for n in part.iter() if n in self.doc.order) + 1
            final_page = self.page(self.doc.nodes[last - 1])
            footer = next((pos for pos, p in self.footers if p == final_page and pos >= last), None)
            footer_nodes = set(self.doc.nodes[footer].iter()) if footer is not None else set()
            if footer is None or any(not self.doc.hidden[n] and
                    (n in self.doc.fact_nodes or '-sec-ix-hidden' in n.get('style', '')
                     or (n not in footer_nodes and clean(n.text or '')) or clean(n.tail or ''))
                    for n in self.doc.nodes[last:]):
                raise ValueError('Unverified content follows the final primary appendix note')
            end_method = 'explicit_schedule_continuation' if schedule_in_item8 else 'final_note_continuation'
        verify_report_span(self, positions[0], last)
        self.ranges.append({'item': '8', 'start': positions[0], 'end': last,
                            'method': 'primary_appendix_index', 'evidence': evidence,
                            'source_locator': self.doc.paths[table], 'index_entries': verified,
                            'end_method': end_method})
        if schedule_in_item8:
            self.ranges[-1]['schedule_index_entry'] = schedule_entries[0]
        self.excluded.add(table)
        # An appendix may restart numbering with Item 1 Financial Statements.
        # Suppress only that precise nested heading inside this verified range.
        self.headings = [h for h in self.headings if not (
            h['item'] == '1' and positions[0] - 30 <= h['position'] < last
            and self.page(self.doc.nodes[h['position']]) == verified[0]['page']
            and re.fullmatch(r'Item\s+1[.\s]+Financial Statements[.\s]*', h['text'], re.I)
            and any(old['item'] == '1' and old['position'] < h8[0]['position'] for old in self.headings))]

    def read_financial_index(self):
        h8 = [h for h in self.headings if h['item'] == '8']
        h15 = [h for h in self.headings if h['item'] == '15']
        if len(h8) != 1 or len(h15) != 1 or any(e['item'] == '8' for e in self.external + self.references + self.ranges):
            return
        start, end, evidence = self.body(h8[0])
        if len(evidence) > 1800 or any(start < self.doc.order[t] < end for t in self.doc.tables if t not in self.excluded):
            return
        refers_to_item15 = re.search(
            r'\b(?:are|is)\s+(?:filed|included|presented|set forth)\b[^.!?]{0,180}'
            r'\b(?:under|in)\s+Item\s+15\b', evidence, re.I)
        if not ((refers_to_item15 or re.search(r'(?:required by this item|incorporat\w*.*reference)', evidence, re.I))
                and re.search(r'consolidated financial statements', evidence, re.I)
                and re.search(r'(?:this annual report|this form 10-k)', evidence, re.I)):
            return
        if self.read_referenced_financial_index(h15[0], evidence):
            return
        pstart, pend, _ = self.body(h15[0])
        anchors = defaultdict(list)
        for n in self.doc.nodes:
            for anchor in {n.get('id'), n.get('name')} - {None, ''}:
                anchors[anchor].append(self.doc.order[n])
        for table in self.doc.tables:
            if not pstart < self.doc.order[table] < pend:
                continue
            entries = []
            for row in self.doc.rows(table):
                label = ' '.join(self.doc.display(c) for c in row if not re.fullmatch(r'\d+', self.doc.display(c)))
                refs = {a.get('href')[1:] for c in row for a in c.iter('a') if a.get('href', '').startswith('#')}
                if len(refs) == 1:
                    entries.append({'label': label.strip(), 'anchor': next(iter(refs))})
            statements = [e for e in entries if re.match(r'consolidated (?:statements|balance sheets)', e['label'], re.I)]
            notes = [e for e in entries if re.match(r'notes to (?:the )?consolidated financial statements', e['label'], re.I)]
            if len(statements) < 3 or len(notes) != 1:
                continue
            boundary = next((e for e in entries[entries.index(notes[0]) + 1:] if re.search(
                r'^(?:schedule\b|exhibits\b)|index to exhibits', e['label'], re.I)), None)
            if not boundary or any(len(anchors[e['anchor']]) != 1 for e in statements + notes + [boundary]):
                continue
            first, last = min(anchors[e['anchor']][0] for e in statements), anchors[boundary['anchor']][0]
            if self.doc.order[table] < first < anchors[notes[0]['anchor']][0] < last <= pend:
                self.ranges.append({'item': '8', 'start': first, 'end': last, 'method': 'financial_index',
                                    'evidence': evidence, 'source_locator': self.doc.paths[table]})
                self.excluded.add(table)
        if not self.ranges and self.read_pagelink_financial_index(pstart, pend, evidence):
            return
        if len(self.ranges) != 1:
            raise ValueError('Item 8 incorporates financial statements, but their linked boundaries could not be verified.')

    def read_pagelink_financial_index(self, start, end, evidence):
        """Fallback for a statement index containing extra links on date text.

        The page-number link and statement-title link must agree. Only date
        links can differ; all destinations, titles and the closing schedule
        are independently checked before assigning any Item 8 membership.
        """
        candidates = []
        for table in self.doc.tables:
            if not start < self.doc.order[table] < end or any(
                    namespace(n) in IX and local(n) in {'nonFraction', 'fraction'} for n in table.iter()):
                continue
            entries = []
            for row in self.doc.rows(table):
                cells = [c for c in row if self.doc.display(c)]
                pages = [c for c in cells if re.fullmatch(PAGE_LABEL, self.doc.display(c))]
                title = ' '.join(self.doc.display(c) for c in cells if c not in pages)
                kind = ('statement' if re.match(r'^Consolidated (?:Balance Sheets|Statements of )', title, re.I)
                        else 'notes' if re.match(r'^Notes to (?:the )?Consolidated Financial Statements\b', title, re.I)
                        else 'boundary' if re.match(r'^(?:Schedule\b|Exhibits\b|Index to Exhibits\b)', title, re.I)
                        else None)
                if kind and any(a.get('href') for c in cells for a in c.iter('a')):
                    entries.append({'title': title, 'kind': kind, 'cells': cells, 'pages': pages})
            if sum(e['kind'] == 'statement' for e in entries) >= 3 and sum(e['kind'] == 'notes' for e in entries) == 1:
                candidates.append((table, entries))
        if not candidates:
            return False
        if len(candidates) != 1:
            raise ValueError('Item 15 has multiple candidate page-linked financial indexes')
        table, entries = candidates[0]
        note_index = next(i for i, e in enumerate(entries) if e['kind'] == 'notes')
        if (not all(e['kind'] == 'statement' for e in entries[:note_index])
                or len(entries) <= note_index + 1 or entries[note_index + 1]['kind'] != 'boundary'):
            raise ValueError('Page-linked financial index has no unique ordered closing boundary')
        verified = []
        for entry in entries[:note_index + 2]:
            if len(entry['pages']) != 1:
                raise ValueError('Page-linked financial index requires one printed page per entry')
            page_cell = entry['pages'][0]
            links = {a.get('href') for a in page_cell.iter('a') if a.get('href')}
            if len(links) != 1 or not next(iter(links)).startswith('#'):
                raise ValueError('Page-linked financial index requires one internal page link')
            href = next(iter(links))
            title_links = [a for c in entry['cells'] if c is not page_cell for a in c.iter('a') if a.get('href')]
            if not any(a.get('href') == href and re.match(
                    r'^(?:Consolidated |Notes to |Schedule |Exhibits|Index to Exhibits)', self.doc.display(a), re.I)
                       for a in title_links):
                raise ValueError('Page-linked financial index title and page links disagree')
            date_links = [a for a in title_links if a.get('href') != href]
            if date_links:
                # Preserve the original spacing: a publisher may split even
                # the word "and" across adjacent anchors to the same target.
                text = clean(''.join(''.join(a.itertext()) + (a.tail or '') for a in date_links))
                remainder = re.sub(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December|and)\b', '', text, flags=re.I)
                if (len({a.get('href') for a in date_links}) != 1
                        or any(self.doc.hidden[a] or not a.get('href').startswith('#') for a in date_links)
                        or not re.search(r'\b\d{4}\b', text)
                        or not re.fullmatch(r'[\d\s,.]+', remainder)):
                    raise ValueError('Page-linked financial index contains conflicting non-date links')
            page = str(page_key(self.doc.display(page_cell)))
            position = self.primary_index_target(href[1:], page, entry['title'], heading_spans=True)
            verified.append({'title': entry['title'], 'kind': entry['kind'], 'anchor': href[1:],
                             'page': page, 'position': position})
        positions = [e['position'] for e in verified]
        if (positions != sorted(set(positions)) or not self.doc.order[table] < positions[0]
                or positions[-1] >= end):
            raise ValueError('Page-linked financial index targets are repeated, out of order or outside Item 15')
        verify_report_span(self, positions[0], positions[-1] + 1)
        self.ranges.append({'item': '8', 'start': positions[0], 'end': positions[-1],
                            'method': 'financial_index_page_links', 'evidence': evidence,
                            'source_locator': self.doc.paths[table], 'index_entries': verified})
        self.excluded.add(table)
        return True

    def read_referenced_financial_index(self, heading, evidence):
        """Follow Item 15's explicit page citation to an internal statement index.

        Financial appendices may follow Item 16 and signatures. Their scope
        comes from the cited index and verified destinations, not the preceding
        Item heading. A schedule/exhibit entry must close the notes range.
        """
        start, _, body = self.body(heading)
        citations = re.findall(
            r'\bIndex to (?:the )?(?:Consolidated )?Financial Statements'
            r'(?: and (?:Financial Statement )?Schedules?)?\s+on\s+Page\s+('
            + PAGE_LABEL + r')\b', body, re.I)
        if not citations:
            return False
        pages = {str(page_key(p)) for p in citations}
        if len(pages) != 1:
            raise ValueError('Item 15 cites multiple financial statement index pages')
        page = pages.pop()
        if sum(p == page for _, p in self.footers) != 1:
            raise ValueError('Item 15 financial statement index page is missing or ambiguous')
        statement = r'^Consolidated (?:Balance Sheets|Statements of )'
        notes = r'^Notes to (?:the )?Consolidated Financial Statements\b'
        boundary = r'^(?:Schedule\b|Exhibits\b|Index to Exhibits\b)'
        candidates = []
        for table in self.doc.tables:
            if self.doc.order[table] <= start or self.page(table) != page:
                continue
            entries = []
            for row in self.doc.rows(table):
                cells = [c for c in row if self.doc.display(c)]
                labels = [self.doc.display(c) for c in cells
                          if not re.fullmatch(PAGE_LABEL, self.doc.display(c))]
                title = ' '.join(labels)
                kind = ('statement' if re.match(statement, title, re.I) else
                        'notes' if re.match(notes, title, re.I) else
                        'boundary' if re.match(boundary, title, re.I) else None)
                if kind is None:
                    continue
                links = {a.get('href') for c in cells for a in c.iter('a') if a.get('href')}
                printed = [str(page_key(self.doc.display(c))) for c in cells
                           if re.fullmatch(PAGE_LABEL, self.doc.display(c))]
                entries.append({'title': title, 'kind': kind, 'links': links, 'pages': printed})
            if (sum(e['kind'] == 'statement' for e in entries) >= 3
                    and sum(e['kind'] == 'notes' for e in entries) == 1):
                candidates.append((table, entries))
        if len(candidates) != 1:
            raise ValueError('Cannot uniquely verify the financial statement index on the Item 15 cited page')
        table, entries = candidates[0]
        note = next(e for e in entries if e['kind'] == 'notes')
        stop = next((e for e in entries[entries.index(note) + 1:] if e['kind'] == 'boundary'), None)
        if stop is None:
            raise ValueError('Referenced financial statement index has no verified end boundary after notes')
        selected = [e for e in entries if e['kind'] == 'statement'] + [note, stop]
        verified = []
        for entry in selected:
            if (len(entry['links']) != 1 or not next(iter(entry['links'])).startswith('#')
                    or len(entry['pages']) != 1):
                raise ValueError('Financial statement index needs unique internal links and printed pages')
            anchor, target_page = next(iter(entry['links']))[1:], entry['pages'][0]
            position = report_anchor(self, anchor, target_page)
            # Dates in statement-index labels are not part of the body title.
            title = entry['title']
            if entry['kind'] == 'statement':
                title = re.split(r'\s*[—–]\s*|\s+(?:as of|for (?:the )?years? ended|years? ended)\b',
                                 title, maxsplit=1, flags=re.I)[0]
            key = re.sub(r'[^a-z0-9]', '', title.lower())
            # A notes link may point at Note 1 immediately after the section
            # title. Stay on the cited page and close to the exact anchor.
            nearby = [text for pos, text, node in self.blocks
                      if position - 30 <= pos < position + 50 and self.page(node) == target_page]
            if key not in {re.sub(r'[^a-z0-9]', '', text.lower()) for text in nearby}:
                raise ValueError(f'Financial statement index target title cannot be verified: {title}')
            verified.append({'title': entry['title'], 'kind': entry['kind'], 'anchor': anchor,
                             'page': target_page, 'position': position,
                             'source_locator': self.doc.paths[self.doc.nodes[position]]})
        positions = [e['position'] for e in verified]
        if positions != sorted(set(positions)) or self.doc.order[table] >= positions[0]:
            raise ValueError('Referenced financial statement index targets are repeated or out of order')
        first, last = positions[0], positions[-1]
        # A schedule's text-block tag can open immediately before its heading.
        # Keep that wrapper outside Item 8 too, without trimming preceding text.
        for parent in self.doc.nodes[last].iterancestors():
            pos = self.doc.order[parent]
            if (namespace(parent) in IX and local(parent) == 'nonNumeric'
                    and self.page(parent) == verified[-1]['page']
                    and not any(clean(n.text or '') for n in self.doc.nodes[pos:last])):
                last = pos
        verify_report_span(self, first, last)
        self.ranges.append({'item': '8', 'start': first, 'end': last,
                            'method': 'referenced_financial_index', 'evidence': evidence,
                            'source_locator': self.doc.paths[table],
                            'index_reference': {'item': '15', 'page': page, 'evidence': body,
                                                'source_locator': heading['source_locator']},
                            'index_entries': verified})
        self.excluded.add(table)
        return True

    def validate(self, items):
        counts = Counter(h['item'] for h in self.headings)
        available = set(counts) | {e['item'] for e in self.references + self.ranges}
        missing = set(items) - available
        if missing:
            raise ValueError(f'Cannot resolve Items {sorted(missing)}. Use original filing XHTML, or --all-items to inspect all tables.')
        for item in items:
            if counts[item] > 1:
                raise ValueError(f'Multiple body headings for Item {item}; cannot resolve scope uniquely.')
        labels = Counter(page_key(page) for _, page in self.footers)
        images = {int(g['inferred_page']) for g in self.image_gaps}
        for reference in self.references:
            if reference['item'] not in items:
                continue
            if reference.get('unresolved_page_reference'):
                raise ValueError(f'Item {reference["item"]} has an unverified page reference: '
                                 + reference['unresolved_page_reference'])
            missing = set(reference['pages']) - set(labels) - images
            duplicates = {p for p in reference['pages'] if labels[p] > 1}
            if missing or duplicates:
                raise ValueError(f'Item {reference["item"]} pages are not uniquely verified: missing={sorted(missing, key=page_parts)}, repeated={sorted(duplicates, key=page_parts)}.')

    def membership(self, node):
        pos, page = self.doc.order[node], self.page(node)
        physical = next((h['item'] for h in reversed(self.headings) if h['position'] < pos), None)
        referenced = [e['item'] for e in self.references if page and page_key(page) in e['pages']]
        referenced += [e['item'] for e in self.ranges if e['start'] <= pos < e['end']]
        members = list(dict.fromkeys(referenced or ([physical] if physical else [])))
        return page, physical, members


def report_exhibit_rows(doc):
    """Recognize Exhibit 13 across all indexes, including unlinked listings."""
    for table in doc.tables:
        for row in doc.rows(table):
            populated = [c for c in row if doc.display(c)]
            if not populated:
                continue
            label = re.sub(r'[*†‡]', '', doc.display(populated[0])).strip(' \t-–—:')
            if not re.fullmatch(r'(?:(?:EX-|Exhibit\s+)?13(?:\.\d+)?|(?:Exhibit\s+)?\(13(?:\.\d+)?\))', label, re.I):
                continue
            if not re.search(r'(?:annual|financial) report', ' '.join(doc.display(c) for c in populated), re.I):
                continue
            yield populated


def referenced_report_source(doc, *, allow_missing=False):
    """Only an explicitly linked Exhibit 13 in the same filing is followed.

    The API pipeline may handle a genuinely absent link using the SEC filing
    document list. An ambiguous or invalid link never triggers that fallback.
    """
    candidates = {a.get('href') for row in report_exhibit_rows(doc) for c in row for a in c.iter('a') if a.get('href')}
    if not candidates and allow_missing:
        return None
    sources = set()
    for href in candidates:
        parsed = urlparse(href)
        if parsed.query or parsed.fragment or not re.search(r'\.x?html?$', parsed.path, re.I):
            continue
        if urlparse(doc.source).scheme in {'http', 'https'}:
            source = urljoin(doc.source, href)
            if source.rsplit('/', 1)[0] != doc.source.rsplit('/', 1)[0]:
                continue
        else:
            if parsed.scheme or parsed.netloc or '/' in parsed.path or '\\' in parsed.path:
                continue
            source = str(Path(doc.source).resolve().parent / parsed.path)
        sources.add(source)
    if len(sources) != 1:
        raise ValueError('Named report incorporation needs one linked HTML Exhibit 13 in the same filing. '
                         'Use --report-source with the verified original Inline XBRL report.')
    return sources.pop()


def attach_report_ranges(layout, references):
    indexed = [ref for ref in references if ref.get('linked_statement_index')]
    for ref in indexed:
        attach_linked_statement_ranges(layout, ref)
    references = [ref for ref in references if ref not in indexed] + [
        {**ref, 'section_paths': [['Selected Financial Data']]} for ref in indexed if ref.get('selected_financial_data')]
    page_refs = [ref for ref in references if ref.get('pages')]
    for ref in page_refs:
        layout.references.append({'item': ref['item'], 'pages': ref['pages'],
                                  'method': 'external_report_pages', 'evidence': ref['reference_text'],
                                  **({'index_evidence': ref['index_evidence']} if ref.get('index_evidence') else {})})
    if page_refs:
        # These are labels in the external document, never page ordinals or
        # labels in the primary 10-K. Missing/repeated labels must fail closed.
        layout.validate([ref['item'] for ref in page_refs])
        for ref in page_refs:
            previous = None
            for page in ref['pages']:
                pos = next(pos for pos, label in layout.footers if page_key(label) == page)
                boundary = bisect.bisect_right(layout.breaks, pos)
                peers = [label for other, label in layout.footers
                         if bisect.bisect_right(layout.breaks, other) == boundary]
                if len(peers) != 1 or (not boundary and page_parts(page)[1] != 1):
                    raise ValueError(f'External report page {page} has no unique page boundary.')
                if previous is not None:
                    old_page, old_pos, old_boundary = previous
                    prefix, number = page_parts(page)
                    old_prefix, old_number = page_parts(old_page)
                    if pos <= old_pos or (prefix == old_prefix and number == old_number + 1 and boundary != old_boundary + 1):
                        raise ValueError('External report pages are out of order or have unverified gaps.')
                previous = page, pos, boundary
    unquoted = [ref for ref in references if ref.get('unquoted')]
    if unquoted:
        attach_unquoted_report_ranges(layout, unquoted)
    references = [ref for ref in references if not ref.get('pages') and not ref.get('unquoted')]
    if not references:
        return
    doc = layout.doc
    events, positions = [], []
    elements = sorted(set(doc.tables + doc.blocks), key=doc.order.get)
    for node in elements:
        if node.tag == 'table':
            rows = doc.rows(node)
            event = narrative.FilingStructureEvent(doc.order[node], 'table', doc.display(node),
                        tuple(tuple(doc.display(c) for c in row) for row in rows),
                        cell_bold=tuple(tuple(html_tables.bold_block(c) for c in row) for row in rows))
        else:
            event = narrative.FilingStructureEvent(doc.order[node], node.tag, before_table(doc, node),
                                                    bold=html_tables.bold_block(node))
        events.append(event)
        positions.append(doc.order[node])
    paths = [path for ref in references for path in ref['section_paths']]
    try:
        outline = narrative.report_outline(events, paths)
    except ValueError:
        # Some reports have a page-based TOC but use styled divs, split titles
        # and abbreviated TOC labels instead of semantic HTML headings.
        if not attach_hierarchical_linked_toc_ranges(layout, references) and not attach_linked_toc_ranges(layout, references):
            attach_styled_toc_ranges(layout, references)
        return
    for ref in references:
        for section in narrative.referenced_section_ranges(outline, ref['section_paths']):
            layout.ranges.append({'item': ref['item'], 'start': positions[section['start']],
                                  'end': positions[section['end']] if section['end'] < len(positions) else len(doc.nodes),
                                  'method': 'external_report_section', 'section': section['title'],
                                  'evidence': ref['reference_text']})


def verify_report_span(layout, start, end):
    doc = layout.doc
    first, last = layout.page(doc.nodes[start]), layout.page(doc.nodes[end - 1])
    if first is None or last is None or end <= start:
        raise ValueError('Linked report section has no verified printed-page span')
    pages = page_ranges(f'{first} through {last}')
    labels = Counter(page_key(page) for _, page in layout.footers)
    footers = [(pos, page_key(page)) for pos, page in layout.footers if page_key(page) in pages]
    if ([page for _, page in footers] != pages or any(labels[p] != 1 for p in pages)
            or any(bisect.bisect_right(layout.breaks, b[0]) - bisect.bisect_right(layout.breaks, a[0]) != 1
                   for a, b in zip(footers, footers[1:]))):
        raise ValueError('Linked report section has missing, duplicate or discontinuous pages')


def report_anchor(layout, anchor, page):
    node = layout.doc.ids.get(anchor)
    if node is None or layout.doc.hidden[node] or layout.page(node) != page:
        raise ValueError('Report link target disagrees with its referenced printed page')
    return layout.doc.order[node]


def attach_linked_statement_ranges(layout, ref):
    """Explicit Item 15 document fragments establish each statement's start."""
    doc = layout.doc
    entries = ref['linked_statement_index']
    starts = [report_anchor(layout, e['anchor'], e['page']) for e in entries]
    if starts != sorted(set(starts)):
        raise ValueError('Linked statement index is repeated or out of document order')
    for entry, start in zip(entries, starts):
        candidates = [before_table(doc, n) for n in doc.blocks if start <= doc.order[n] < start + 50]
        if styled_title_key(entry['title']) not in {styled_title_key(t) for t in candidates}:
            raise ValueError(f'Linked statement title cannot be verified: {entry["title"]}')
    # A final note text block explicitly closes via its continuation chain.
    # Require the final printed notes page to be labelled as a notes page and
    # reject any later visible XBRL fact/reference, rather than including the
    # report's subsequent staff/shareholder material in the financial notes.
    notes = [n for n in doc.fact_nodes if doc.order[n] >= starts[-1] and not doc.hidden[n]
             and local(n) == 'nonNumeric' and n.get('name', '').endswith('TextBlock')
             and re.match(r'NOTE\s+[A-Z0-9]+\s*[-–—.:]', clean(doc.content(n)), re.I)]
    if not notes:
        raise ValueError('Cannot verify the final incorporated note text block')
    final = max(notes, key=doc.order.get)
    chain, current = [], final
    while current is not None:
        if current in chain:
            raise ValueError('Cyclic final-note continuation')
        chain.append(current)
        ref_id = current.get('continuedAt')
        current = doc.ids.get(ref_id) if ref_id else None
        if ref_id and (current is None or namespace(current) not in IX or local(current) != 'continuation'):
            raise ValueError('Invalid final-note continuation')
    end = max(doc.order[n] for part in chain for n in part.iter() if n in doc.order) + 1
    last_page = layout.page(doc.nodes[end - 1])
    running = [n for n in doc.blocks if layout.page(n) == last_page and doc.order[n] < end
               and styled_title_key(before_table(doc, n)) == styled_title_key(entries[-1]['title'])]
    if (not running or any(not doc.hidden[n] and (n in doc.fact_nodes or '-sec-ix-hidden' in n.get('style', ''))
                           for n in doc.nodes[end:])):
        raise ValueError('Cannot verify the final incorporated notes boundary')
    for entry, start, stop in zip(entries, starts, starts[1:] + [end]):
        verify_report_span(layout, start, stop)
        layout.ranges.append({'item': '8', 'start': start, 'end': stop, 'method': 'external_report_linked_index',
            'section': entry['title'], 'source_locator': doc.paths[doc.nodes[start]],
            'end_source_locator': doc.paths[doc.nodes[stop - 1]], 'evidence': ref['reference_text'],
            'index_evidence': entry, 'end_method': 'next_statement_link' if stop != end else 'final_note_continuation'})


def report_section_key(title):
    key = styled_title_key(title.rstrip(' ,:.;'))
    if key in {styled_title_key('MD&A'), styled_title_key(
            "Management's Discussion and Analysis of Financial Condition and Results of Operations")}:
        return 'md&a'
    return key


def attach_hierarchical_linked_toc_ranges(layout, references):
    """Use a report's bold TOC groups and page links to verify nested sections.

    Unnumbered bold group rows supply hierarchy; linked leaf rows supply exact
    boundaries. Repeated page numbers are allowed, but fragment positions must
    increase. Nothing is assigned from a title match alone.
    """
    if not any(ref.get('toc_hierarchy') or any(len(p) > 1 for p in ref['section_paths']) for ref in references):
        return False
    doc = layout.doc
    if not layout.footers:
        return False
    entries = []
    for table in doc.tables:
        if doc.order[table] >= layout.footers[0][0]:
            continue
        if any(namespace(n) in IX for n in table.iterdescendants()):
            continue
        rows, links = [], 0
        for row in doc.rows(table):
            cells = [c for c in row if doc.display(c)]
            if not cells or (len(cells) == 1 and doc.display(cells[0]).lower() == 'page'):
                continue
            title = doc.display(cells[0])
            if not 3 <= len(title) <= 240 or not title[0].isalpha():
                rows = []
                break
            bold = entirely_bold(doc, cells[0])
            entry = {'title': title.rstrip(':'), 'key': report_section_key(title),
                     'toc_locator': doc.paths[cells[0]], 'toc_position': doc.order[table], 'group': len(cells) == 1,
                     'level': 0 if bold else (1 if len(cells) == 1 else 2)}
            if len(cells) == 1 and title.endswith(':'):
                rows.append(entry)
                continue
            hrefs = set(h for c in cells for h in c.xpath('.//a/@href'))
            if len(cells) != 2 or not re.fullmatch(PAGE_LABEL, doc.display(cells[1])) or len(hrefs) != 1:
                rows = []
                break
            href = hrefs.pop()
            if not href.startswith('#'):
                rows = []
                break
            entry.update(anchor=href[1:], page=str(page_key(doc.display(cells[1]))))
            rows.append(entry)
            links += 1
        if rows and links >= 4 and any(e['group'] and e['level'] == 0 for e in rows):
            entries.extend(rows)
    if not entries:
        return False
    leaves = [e for e in entries if not e['group']]
    for entry in leaves:
        node = doc.ids.get(entry['anchor'])
        if node is None or doc.hidden[node]:
            raise ValueError('Report TOC has a missing or hidden fragment target')
        start = doc.order[node]
        # Empty fragment anchors sometimes precede the page-break element.
        # Only empty markup may be crossed to reach the stated printed page.
        limit = start + 12
        while start < len(doc.nodes) and layout.page(doc.nodes[start]) != entry['page']:
            n = doc.nodes[start]
            if start >= limit or clean(n.text or '') or clean(n.tail or '') or namespace(n) in IX:
                raise ValueError('Report TOC fragment disagrees with its printed page')
            start += 1
        if start >= len(doc.nodes):
            raise ValueError('Report TOC fragment has no verified printed page')
        entry['start'] = start
    if [e['start'] for e in leaves] != sorted({e['start'] for e in leaves}):
        raise ValueError('Report TOC fragments are repeated or out of order')
    if max(e['toc_position'] for e in entries) >= leaves[0]['start']:
        raise ValueError('Report TOC does not precede its sections')
    for index, entry in enumerate(entries):
        following = entries[index + 1:]
        if entry['group']:
            children = []
            for child in following:
                if child['level'] <= entry['level']:
                    break
                if not child['group']:
                    children.append(child)
            if not children:
                raise ValueError('Report TOC group has no linked child sections')
            entry['start'] = children[0]['start']
        next_entry = next((e for e in following if e['level'] <= entry['level']), None)
        # Group starts are derived above; find the next actual leaf when the
        # boundary is another unnumbered group.
        if next_entry is not None:
            boundary_index = entries.index(next_entry)
            next_leaf = next(e for e in entries[boundary_index:] if not e['group'])
            entry['end'] = next_leaf['start']
            entry['end_entry'] = next_leaf
    pending = []
    for ref in references:
        for path in ref['section_paths']:
            parent = None
            for title in path:
                note = re.fullmatch(r'Note\s+(\d+)', title, re.I)
                matches = [e for e in entries if (e['key'] == report_section_key(title)
                           or (note and re.match(r'Note\s+' + note[1] + r'\b', e['title'], re.I)))
                           and (parent is None or parent['start'] <= e['start'] < parent.get('end', -1))]
                if len(matches) != 1:
                    raise ValueError(f'Cannot uniquely verify report TOC hierarchy: {" / ".join(path)}')
                parent = matches[0]
            entry = parent
            if entry is None or 'end' not in entry or entry['end'] <= entry['start']:
                raise ValueError('Report TOC section has no verified following boundary')
            first_leaf = next(e for e in leaves if e['start'] == entry['start'])
            for boundary in (first_leaf, entry['end_entry']):
                titles = [text for pos, text, node in layout.blocks
                          if boundary['start'] <= pos < boundary['start'] + 60
                          and text and len(text) <= 240]
                if report_section_key(boundary['title']) not in {report_section_key(t) for t in titles}:
                    raise ValueError(f'Report TOC target title cannot be verified: {boundary["title"]}')
            end = entry['end']
            while end > entry['start'] and layout.page(doc.nodes[end - 1]) is None:
                node = doc.nodes[end - 1]
                footer = next((pos for pos, _ in reversed(layout.footers)
                               if pos <= end - 1 <= max(doc.order[n] for n in doc.nodes[pos].iter() if n in doc.order)
                               and not any(namespace(n) in IX for n in doc.nodes[pos].iter())), None)
                if footer is not None:
                    end = footer
                    continue
                if clean(node.text or '') or clean(node.tail or '') or namespace(node) in IX:
                    raise ValueError('Report section ends in unnumbered visible content')
                end -= 1
            verify_report_span(layout, entry['start'], end)
            pending.append({'item': ref['item'], 'start': entry['start'], 'end': entry['end'],
                'method': 'external_report_hierarchical_toc', 'section': entry['title'],
                'section_path': path, 'source_locator': doc.paths[doc.nodes[entry['start']]],
                'end_source_locator': doc.paths[doc.nodes[entry['end']]],
                'toc_evidence': {k: v for k, v in entry.items() if k != 'end_entry'},
                'end_method': 'next_linked_toc_section', 'evidence': ref['reference_text']})
    layout.ranges.extend(pending)
    return True


def attach_linked_toc_ranges(layout, references):
    """A report's exact TOC links work even when visible headings are split."""
    if any(len(path) != 1 for ref in references for path in ref['section_paths']):
        return False
    doc, tables = layout.doc, []
    for table in doc.tables:
        entries = []
        for row in doc.rows(table):
            cells = [c for c in row if doc.display(c)]
            if len(cells) != 2 or not re.fullmatch(PAGE_LABEL, doc.display(cells[1])):
                continue  # Roman-numbered front matter cannot supply a numeric boundary.
            links = set(cells[0].xpath('.//a/@href'))
            if len(links) == 1 and next(iter(links)).startswith('#'):
                entries.append({'title': doc.display(cells[0]), 'anchor': links.pop()[1:],
                                'page': str(page_key(doc.display(cells[1])))})
        if len(entries) >= 3:
            tables.append((table, entries))
    def matches(title, requested):
        return (styled_title_key(title) == styled_title_key(requested) or
                (requested == 'Selected Financial Data' and re.fullmatch(
                    r'Selected (?:Consolidated )?Financial (?:and Other )?Data', title, re.I)))
    selections = []
    for ref in references:
        for path in ref['section_paths']:
            found = [(table, rows, e) for table, rows in tables for e in rows if matches(e['title'], path[0])]
            if not found:
                return False
            if len(found) != 1:
                raise ValueError('Ambiguous linked report contents entry')
            selections.append((ref, *found[0]))
    for ref, table, rows, entry in selections:
        start = report_anchor(layout, entry['anchor'], entry['page'])
        if doc.order[table] >= start:
            raise ValueError('Report contents link does not precede its section')
        later = rows[rows.index(entry) + 1:]
        stops = [report_anchor(layout, later[0]['anchor'], later[0]['page'])] if later else []
        stops += [r['start'] for r in layout.ranges if r['start'] > start]
        if not stops or min(stops) <= start:
            raise ValueError('Cannot verify the end of linked report section')
        end = min(stops)
        verify_report_span(layout, start, end)
        layout.ranges.append({'item': ref['item'], 'start': start, 'end': end,
            'method': 'external_report_toc_link', 'section': entry['title'],
            'source_locator': doc.paths[doc.nodes[start]], 'end_source_locator': doc.paths[doc.nodes[end]],
            'toc_evidence': {**entry, 'source_locator': doc.paths[table]}, 'evidence': ref['reference_text']})
    return bool(selections)


def entirely_bold(doc, node):
    """A bold lead-in followed by ordinary prose is not a standalone heading."""
    def walk(current, inherited=False):
        if not isinstance(current.tag, str) or doc.hidden.get(current, False):
            return []
        bold = inherited or current.tag in {'b', 'strong', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
        weight = re.search(r'font-weight\s*:\s*(bold|normal|[1-9]00)', current.get('style', ''), re.I)
        if weight:
            bold = weight[1].lower() == 'bold' or (weight[1].isdigit() and int(weight[1]) >= 600)
        chunks = [bold] if clean(current.text or '') else []
        for child in current:
            chunks.extend(walk(child, bold))
            if clean(child.tail or ''):
                chunks.append(bold)
        return chunks
    chunks = walk(node)
    return bool(chunks) and all(chunks)


def styled_title_key(title):
    title = re.sub(r'\s*\(PCAOB\b[^)]*\)', '', title, flags=re.I)
    # The primary statement inventory may use the dual income/loss caption.
    if re.match(r'Consolidated Statements of ', title, re.I):
        title = re.sub(r'\bIncome\s*\(Loss\)', 'Income', title, flags=re.I)
    return re.sub(r'^reports of ', 'report of ', narrative.report_title_key(title))


def attach_styled_toc_ranges(layout, references):
    """Verify explicit titles using a printed-page TOC and styled headings.

    Adjacent centered title fragments may be joined, but never across a page
    or intervening content. Individual statements may sit within a TOC's
    financial-statements group. No entire-report or fuzzy-title fallback.
    """
    doc = layout.doc
    if any(len(path) != 1 for ref in references for path in ref['section_paths']):
        raise ValueError('Cannot locate referenced report headings with a verified hierarchy.')
    tocs = []
    for table in doc.tables:
        # A TOC may tag metadata such as the auditor's PCAOB identifier.
        # Numeric financial grids still cannot supply the report outline.
        if any(namespace(n) in IX and local(n) in {'nonFraction', 'fraction'} for n in table.iterdescendants()):
            continue
        rows = []
        for row in doc.rows(table):
            values = [doc.display(c) for c in row if doc.display(c)]
            if not values:
                continue
            if (len(values) != 2 or not 3 <= len(values[0]) <= 220
                    or not values[0][0].isalpha() or not re.fullmatch(PAGE_LABEL, values[1])):
                rows = []
                break
            rows.append({'title': values[0], 'key': styled_title_key(values[0]),
                         'page': str(page_key(values[1]))})
        if len(rows) < 4 or len({r['key'] for r in rows}) != len(rows):
            continue
        if (any(page_parts(a['page'])[0] != page_parts(b['page'])[0]
                or page_parts(a['page'])[1] >= page_parts(b['page'])[1] for a, b in zip(rows, rows[1:]))
                or any(sum(page == r['page'] for _, page in layout.footers) != 1 for r in rows)):
            continue
        first_footer = next(pos for pos, page in layout.footers if page == rows[0]['page'])
        boundary = bisect.bisect_right(layout.breaks, first_footer)
        if not boundary or doc.order[table] >= layout.breaks[boundary - 1]:
            continue
        tocs.append((table, rows))
    if len(tocs) != 1:
        raise ValueError('Cannot locate referenced report headings: one verified printed-page TOC is required.')
    toc, rows = tocs[0]
    requested = {styled_title_key(path[0]) for ref in references for path in ref['section_paths']}
    wanted = requested | {r['key'] for r in rows}
    candidates, atomic = [], []
    for node in doc.blocks:
        pos, title = doc.order[node], before_table(doc, node)
        if (pos <= doc.order[toc] or not 3 <= len(title) <= 240 or not title[0].isalpha()
                or not entirely_bold(doc, node) or any(namespace(n) in IX for n in node.iterdescendants())):
            continue
        tagged = [a for a in node.iterancestors() if namespace(a) in IX]
        if tagged:
            # A specifically referenced heading may open its own XBRL text
            # block. Arbitrary headings inside notes/continuations cannot
            # become section boundaries merely because they are bold.
            if (len(tagged) != 1 or local(tagged[0]) != 'nonNumeric'
                    or not tagged[0].get('name', '').endswith('TextBlock')
                    or styled_title_key(title) not in requested
                    or not doc.display(tagged[0]).startswith(title)
                    or pos - doc.order[tagged[0]] > 4):
                continue
        page = layout.page(node)
        if page is None:
            continue
        candidate = {'title': title, 'key': styled_title_key(title), 'position': pos, 'page': page,
                     'source_locator': doc.paths[node], 'heading_locators': [doc.paths[node]]}
        candidates.append(candidate)
        atomic.append((node, candidate))
    by_node = dict(atomic)
    for node, first in atomic:
        if not re.search(r'text-align\s*:\s*center', node.get('style', ''), re.I):
            continue
        title, locators, following = first['title'], list(first['heading_locators']), node
        for _ in range(2):
            following = following.getnext()
            other = by_node.get(following)
            if (not other or other['page'] != first['page']
                    or not re.search(r'text-align\s*:\s*center', following.get('style', ''), re.I)):
                break
            title += ' ' + other['title']
            locators += other['heading_locators']
            key = styled_title_key(title)
            if key in wanted:
                candidates.append({**first, 'title': title, 'key': key, 'heading_locators': list(locators)})
            if not any(k.startswith(key + ' ') for k in wanted):
                break
    def near_page_start(candidate):
        boundary = bisect.bisect_right(layout.breaks, candidate['position'])
        return boundary and candidate['position'] - layout.breaks[boundary - 1] <= 20
    def bucket(candidate):
        prefix, number = page_parts(candidate['page'])
        return next((i for i in range(len(rows) - 1, -1, -1)
                     if page_parts(rows[i]['page'])[0] == prefix and page_parts(rows[i]['page'])[1] <= number), None)
    matched = {}
    for ref in references:
        inventory = {styled_title_key(e['title']) for e in ref.get('index_evidence', [])}
        for path in ref['section_paths']:
            key = styled_title_key(path[0])
            occurrences = sorted((c for c in candidates if c['key'] == key), key=lambda c: c['position'])
            if not occurrences:
                raise ValueError(f'Cannot locate referenced report headings: {path[0]}')
            first = occurrences[0]
            group = bucket(first)
            grouped_statement = (group is not None and key in inventory and key.startswith('consolidated ')
                                 and rows[group]['key'] == 'consolidated financial statements')
            if (group is None or not near_page_start(first)
                    or (first['page'] != rows[group]['page'] and not grouped_statement)
                    or any(bucket(c) != group for c in occurrences)):
                raise ValueError(f'Referenced report heading disagrees with its TOC pages: {path[0]}')
            matched[key] = occurrences
    anchors = sorted({c['position'] for cs in matched.values() for c in cs})
    for key, occurrences in matched.items():
        first, last = occurrences[0], occurrences[-1]
        own = {c['position'] for c in occurrences}
        if any(first['position'] < pos < last['position'] and pos not in own for pos in anchors):
            raise ValueError(f'Ambiguous repeated report heading: {first["title"]}')
    for ref in references:
        for path in ref['section_paths']:
            first, last = matched[styled_title_key(path[0])][0], matched[styled_title_key(path[0])][-1]
            group = bucket(first)
            peers = [c for cs in matched.values() for c in cs if c['position'] > last['position']]
            if group + 1 < len(rows):
                next_row = rows[group + 1]
                boundaries = [c for c in candidates if c['page'] == next_row['page'] and near_page_start(c)
                              and (c['key'] == next_row['key'] or c['key'] in matched)]
                if not boundaries:
                    raise ValueError(f'Cannot verify the next report TOC boundary: {next_row["title"]}')
                peers.append(min(boundaries, key=lambda c: c['position']))
            if peers:
                end, end_method = min(peers, key=lambda c: c['position']), 'verified_heading'
            else:
                # Only the final TOC section may close at the report's final
                # printed footer, with no visible content after that footer.
                pos, page = layout.footers[-1]
                footer = doc.nodes[pos]
                after = max(doc.order[n] for n in footer.iter()) + 1
                if (group != len(rows) - 1 or pos <= last['position']
                        or any(not doc.hidden[n] and (clean(n.text or '') or clean(n.tail or '')) for n in doc.nodes[after:])):
                    raise ValueError(f'Cannot verify the end of report section: {path[0]}')
                end = {'position': pos, 'page': page, 'source_locator': doc.paths[footer]}
                end_method = 'final_report_footer'
            pages = page_ranges(f'{first["page"]} through {end["page"]}')
            labels = Counter(page_key(page) for _, page in layout.footers)
            end_footer = next(pos for pos, page in layout.footers if page == end['page'])
            footers = [(pos, page_key(page)) for pos, page in layout.footers if first['position'] <= pos <= end_footer]
            if ([page for _, page in footers] != pages or any(labels[p] != 1 for p in pages)
                    or any(bisect.bisect_right(layout.breaks, b[0]) - bisect.bisect_right(layout.breaks, a[0]) != 1
                           for a, b in zip(footers, footers[1:]))):
                raise ValueError(f'Report section has missing, repeated or unordered printed pages: {path[0]}')
            layout.ranges.append({'item': ref['item'], 'start': first['position'], 'end': end['position'],
                'method': 'external_report_toc_section', 'section': first['title'],
                'source_locator': first['source_locator'], 'heading_locators': first['heading_locators'],
                'end_source_locator': end['source_locator'], 'end_method': end_method,
                'toc_evidence': {**rows[group], 'source_locator': doc.paths[toc]},
                'evidence': ref['reference_text'], 'index_evidence': ref.get('index_evidence', [])})


def attach_unquoted_report_ranges(layout, references):
    """Resolve unquoted titles against standalone report headings.

    The primary filing supplies the titles, including its Item 15 inventory.
    Repeated running headings form one section only when they do not cross
    another referenced section. Printed pages and a subsequent peer heading
    establish the end; an unbounded or ambiguous section is rejected.
    """
    doc = layout.doc
    def key(title):
        return re.sub(r'^reports of ', 'report of ', narrative.report_title_key(title))
    candidates = []
    for node in sorted(set(doc.blocks + doc.tables), key=doc.order.get):
        title = doc.display(node) if node.tag == 'table' else before_table(doc, node)
        cells = [c for row in doc.rows(node) for c in row if doc.display(c)] if node.tag == 'table' else []
        centered_title = len(cells) == 1 and bool(re.search(r'text-align\s*:\s*center', cells[0].get('style', ''), re.I))
        if (not 3 <= len(title) <= 200 or title.endswith(('.', ':'))
                or not (entirely_bold(doc, node) or centered_title)
                or any(namespace(n) in IX for n in [*node.iterancestors(), *node.iter()])):
            continue
        # Non-heading numerical grids never supply a section boundary.
        if node.tag == 'table' and len(cells) != 1:
            continue
        candidates.append({'title': title, 'key': key(title), 'position': doc.order[node],
                           'page': layout.page(node), 'source_locator': doc.paths[node]})
    matched = {}
    for ref in references:
        if not ref['section_paths'] or any(len(path) != 1 for path in ref['section_paths']):
            raise ValueError(f'Item {ref["item"]} incorporates an external report, but its unquoted sections cannot be resolved. '
                             'A named section or explicit Item 15 statement inventory is required.')
        for path in ref['section_paths']:
            title = path[0]
            wanted = key(title)
            keys = {c['key'] for c in candidates if c['key'] == wanted}
            if not keys and len(wanted.split()) >= 4:
                keys = {c['key'] for c in candidates if c['key'].startswith(wanted + ' ')}
            if len(keys) != 1:
                raise ValueError(f'Cannot uniquely locate unquoted report heading: {title}')
            chosen = next(iter(keys))
            occurrences = [c for c in candidates if c['key'] == chosen]
            if any(c['page'] is None for c in occurrences):
                raise ValueError(f'Unquoted report heading has no verified printed page: {title}')
            matched[wanted] = occurrences
    anchors = sorted({c['position'] for rows in matched.values() for c in rows})
    def page_opening(candidate):
        pos = candidate['position']
        boundary = bisect.bisect_right(layout.breaks, pos)
        return (candidate['page'] is not None and boundary and pos - layout.breaks[boundary - 1] <= 12
                and candidate['title'][0].isalpha() and not candidate['title'].isupper()
                and not re.match(r'Note\s+\d+\b', candidate['title'], re.I))
    for ref in references:
        for path in ref['section_paths']:
            occurrences = matched[key(path[0])]
            first, last = occurrences[0], occurrences[-1]
            own = {c['position'] for c in occurrences}
            if any(first['position'] < pos < last['position'] and pos not in own for pos in anchors):
                raise ValueError(f'Ambiguous repeated report heading: {path[0]}')
            if any(first['position'] < c['position'] < last['position'] and c['position'] not in own
                   and page_opening(c) for c in candidates):
                raise ValueError(f'Ambiguous intervening report heading: {path[0]}')
            peers = [c for rows in matched.values() for c in rows if c['position'] > last['position']]
            if not peers:
                # A new, unnumbered page-opening heading closes the final
                # referenced section. Note subheads and headings inside XBRL
                # text blocks cannot close the containing financial section.
                for candidate in candidates:
                    pos, page = candidate['position'], candidate['page']
                    if (pos > last['position'] and page is not None
                            and page_parts(page)[0] == page_parts(last['page'])[0]
                            and page_parts(page)[1] > page_parts(last['page'])[1]
                            and page_opening(candidate)):
                        peers.append(candidate)
            if not peers:
                raise ValueError(f'Cannot verify the end of unquoted report section: {path[0]}')
            end = min(peers, key=lambda c: c['position'])
            # Validate every printed page between the two boundary headings.
            labels = Counter(page_key(page) for _, page in layout.footers)
            pages = page_ranges(f'{first["page"]} through {end["page"]}')
            if not pages or any(labels[p] != 1 for p in pages):
                raise ValueError(f'Unquoted report section has missing or repeated printed pages: {path[0]}')
            end_footer = next(pos for pos, page in layout.footers if page_key(page) == page_key(end['page']))
            footers = [(pos, page_key(page)) for pos, page in layout.footers if first['position'] <= pos <= end_footer]
            if [page for _, page in footers] != pages or any(
                    bisect.bisect_right(layout.breaks, right[0]) - bisect.bisect_right(layout.breaks, left[0]) != 1
                    for left, right in zip(footers, footers[1:])):
                raise ValueError(f'Unquoted report section has unordered pages or unverified gaps: {path[0]}')
            layout.ranges.append({'item': ref['item'], 'start': first['position'], 'end': end['position'],
                'method': 'external_report_unquoted_section', 'section': first['title'],
                'source_locator': first['source_locator'], 'end_source_locator': end['source_locator'],
                'evidence': ref['reference_text'], 'index_evidence': ref.get('index_evidence', [])})


def last_sentence(value):
    start = 0
    for match in re.finditer(r'''[.!?]["'”’)]*\s+(?=\S)''', value):
        prefix = value[:match.start() + 1]
        if not re.search(r'\b(?:[A-Za-z]\.){2,}$|\b(?:Inc|Corp|Ltd|Co|No|Dr|Mr|Ms|vs|e\.g|i\.e)\.$', prefix, re.I):
            start = match.end()
    return value[start:]


def table_title(layout, table, ordinal, ignored_tables=()):
    doc, pos = layout.doc, layout.doc.order[table]
    caption = table.find('caption')
    if caption is not None and doc.display(caption):
        return doc.display(caption), 'caption'
    # A single bold, nonnumeric first row is an internal visible title.
    for row in doc.rows(table):
        cells = [c for c in row if doc.display(c)]
        if not cells:
            continue
        if len(cells) == 1 and html_tables.bold_block(cells[0]) and len(doc.display(cells[0])) <= 180:
            text = doc.display(cells[0])
            if html_tables.numeric(text)[0] == 'text' and not re.search(r'\b(?:in millions|in thousands|years? ended)\b', text, re.I):
                return text, 'table_heading'
        break
    previous_table = max((doc.order[t] for t in doc.tables if doc.order[t] < pos
                          and t not in layout.excluded and t not in ignored_tables), default=-1)
    scope_start = max((h['position'] for h in layout.headings if h['position'] < pos), default=-1)
    previous = [(p, value, n) for p, value, n in layout.blocks if max(scope_start, previous_table) < p < pos]
    prose = []
    for _, value, node in reversed(previous):
        if (not value or ITEM_RE.match(value) or re.fullmatch(r'\d+(?:\s*\|.*)?', value)
                or value.lower() == 'table of contents'
                or re.fullmatch(r'\(?\s*(?:(?:amounts|dollars|\$)\s+)?in (?:millions|thousands|billions)\b[^.!?]*\)?', value, re.I)):
            continue
        if len(value) <= 180 and (node.tag == 'table' or html_tables.bold_block(node)):
            return value.rstrip(':'), 'visible_heading'
        prose.append(value)
    if prose:
        return last_sentence(prose[0]), 'preceding_sentence'
    return f'Untitled table {ordinal}', 'generated'


def table_cells(doc, table, facts_by_cell):
    """Physical cells occur once; grid coordinates refer back to their IDs."""
    rows, cells, grid, error = doc.rows(table), [], [], None
    for ri, row in enumerate(rows):
        for ci, node in enumerate(row):
            facts = list(dict.fromkeys(facts_by_cell.get(node, [])))
            cells.append({'cell_id': f'r{ri + 1}c{ci + 1}', 'row': ri + 1, 'source_cell_index': ci + 1,
                          'column': None, 'rowspan': node.get('rowspan', '1'), 'colspan': node.get('colspan', '1'),
                          'header': node.tag == 'th', 'display_text': doc.display(node),
                          'fact_ids': facts, 'tagging': 'tagged' if facts else 'untagged',
                          'source_locator': doc.paths[node]})
    try:
        grid = [[] for _ in rows]
        offset = 0
        for ri, row in enumerate(rows):
            column = 0
            for node in row:
                cell = cells[offset]
                offset += 1
                while column < len(grid[ri]) and grid[ri][column] is not None:
                    column += 1
                width, height = int(node.get('colspan', '1')), int(node.get('rowspan', '1'))
                if height == 0:  # HTML's remaining rows in this row group.
                    group = node.getparent().getparent()
                    height = 1
                    for later in rows[ri + 1:]:
                        if later and later[0].getparent().getparent() is group:
                            height += 1
                        else:
                            break
                if not 1 <= width <= 500 or not 1 <= height <= 2000 or column + width > 1000:
                    raise ValueError('Invalid or excessive HTML row/column span')
                if height > len(rows) - ri:
                    raise ValueError('HTML rowspan extends past the table')
                cell['column'], cell['rowspan'], cell['colspan'] = column + 1, height, width
                for rr in range(ri, ri + height):
                    grid[rr].extend([None] * max(0, column + width - len(grid[rr])))
                    for cc in range(column, column + width):
                        if grid[rr][cc] is not None:
                            raise ValueError('Overlapping HTML cell spans')
                        grid[rr][cc] = cell['cell_id']
                column += width
        width = max(map(len, grid), default=0)
        for row in grid:
            row.extend([None] * (width - len(row)))
    except ValueError as exc:
        error, grid = str(exc), []
        # Source positions remain available even when layout cannot expand.
        for cell in cells:
            cell['column'] = None
    return cells, grid, error


def measure_text(value):
    """Recognize a standalone display amount, never a number inside prose.

    This is classification only. XBRL normalization still uses the source fact;
    untagged cells do not gain inferred facts or calculated values.
    """
    value = clean(value)
    # Footnote markers after an amount are not part of that amount. A complete
    # '(12)' remains a negative number, not a stripped footnote marker.
    value = re.sub(r'(?<=[\d%)KMB])(?:\s*\([a-z0-9]{1,3}\))+\s*$', '', value, flags=re.I)
    value = re.sub(r'[†‡*]+$', '', value).strip()
    if value in html_tables.DASHES:
        return 'dash'
    if re.fullmatch(r'(?:19|20|21)\d{2}', value):
        return 'year'
    if re.fullmatch(r'(?:19|20|21)\d{2}(?:\s*[-–—,]\s*(?:19|20|21)\d{2})+', value):
        return 'period'
    number = r'[+\-−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)'
    currency = r'(?:US\$|USD|EUR|GBP|JPY|[$€£¥])?'
    suffix = r'(?:%|[KMB]|thousands?|millions?|billions?|trillions?|bps|basis points)?'
    amount = rf'{currency}\s*\(?\s*{number}\s*\)?\s*{suffix}'
    # Percent ranges and short ranges of amounts remain valid data cells.
    return 'amount' if re.fullmatch(rf'{amount}(?:\s*(?:[-–—]|to)\s*{amount})?', value, re.I) else None


def classify_table(cells, facts):
    """Separate quantitative disclosure tables from HTML prose/layout blocks.

    Tag presence alone is insufficient: a tagged number embedded in a sentence
    is still prose. Conversely, a separate untagged amount is valid table data.
    """
    populated = [c for c in cells if c['display_text'] or c['fact_ids']]
    if not populated:
        return False, 'empty_table'
    rows = defaultdict(list)
    for cell in populated:
        rows[cell['row']].append(cell)
    short_texts = [c['display_text'] for c in populated if len(c['display_text']) <= 180]
    # Ages in an executive roster are numbers but are not financial measures.
    headings = {re.sub(r'[^a-z ]', '', s.lower()).strip() for s in short_texts}
    if 'age' in headings and any('name' in s for s in headings) and any(
            word in s for s in headings for word in ('position', 'title', 'experience')):
        return False, 'nonfinancial_roster'

    values, labels, years, dashes = [], [], [], []
    page_headers = [c for c in populated if len(c['display_text']) < 90 and
                    (re.fullmatch(r'pages?', c['display_text'], re.I) or
                     re.search(r'page\s*references?', c['display_text'], re.I))]
    for cell in populated:
        text = cell['display_text']
        kind = measure_text(text)
        numeric = [facts[fid] for fid in cell['fact_ids'] if facts[fid]['kind'] in {'nonFraction', 'fraction'}]
        # Allow nil/unsupported facts and multiple facts in a cell, provided the
        # surrounding content is merely punctuation, currency, or footnotes.
        remainder = text
        for fact in numeric:
            raw = clean(fact['raw_text'] or '')
            if raw:
                remainder = remainder.replace(raw, '', 1)
        remainder = re.sub(r'\([a-z0-9]{1,3}\)', '', remainder, flags=re.I)
        standalone_fact = bool(numeric) and not re.sub(r'[\s$€£¥%(),.+\-−–—/†‡*]', '', remainder)
        row = rows[cell['row']]
        column = cell['column'] or cell['source_cell_index']
        if any(h['row'] < cell['row'] and (h['column'] or h['source_cell_index']) <= column <
               (h['column'] or h['source_cell_index']) + (int(h['colspan']) if str(h['colspan']).isdigit() else 1)
               for h in page_headers):
            continue
        # Enumerated footnotes, including '(1)', must not become data just
        # because the neighboring explanation mentions dollars or percentages.
        marker = (cell is row[0] and len(row) == 2 and not numeric
                  and re.fullmatch(r'(?:\(?\d{1,2}\)?\.?|\([a-z]\)|[•▪■+\-–—])', text)
                  and measure_text(row[1]['display_text']) is None)
        if marker:
            continue
        if standalone_fact or kind == 'amount':
            values.append(cell)
        elif kind == 'year':
            years.append(cell)
        elif kind == 'dash':
            dashes.append(cell)
        elif kind != 'dash' and 1 < len(text) <= 180 and re.search(r'[A-Za-z]', text):
            labels.append(cell)

    # A year-shaped amount (e.g. revenue of 2,025) is only excluded as a header
    # when it is a bare year. An earlier period header or a tagged value supplies
    # the evidence needed to recognize a bare 2025 in a data row instead.
    period_rows = {c['row'] for c in populated if re.search(
        r'\b(?:years? ended|as of|quarter ended|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b',
        c['display_text'], re.I)}
    year_rows = Counter(c['row'] for c in years)
    period_rows.update(row for row, count in year_rows.items() if count >= 2 and not any(
        c['row'] == row and c['display_text'].lower() not in {'year', 'years', 'period'} for c in labels))
    for cell in years:
        if any(row < cell['row'] for row in period_rows) and any(c['row'] == cell['row'] for c in labels):
            values.append(cell)
    units = any(re.fullmatch(r'[$€£¥]', s) or re.search(r'\bin (?:thousands|millions|billions)\b|\bper share\b', s, re.I)
                for s in short_texts)
    financial_labels = any(re.search(r'\b(?:cash|revenue|income|earnings|assets|liabilities|equity|debt|expenses?|balances?|profit|loss|dividends?|tax)\b',
                                     c['display_text'], re.I) for c in labels)
    if units or financial_labels:
        values.extend(c for c in dashes if any(label['row'] == c['row'] for label in labels))
    if not values:
        # Credit-rating matrices are financial data despite categorical values.
        rating = r'(?:Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca|C|(?:AAA|AA|A|BBB|BB|B|CCC|CC|C|D)[+-]?|P-[123]|A-[123][+]?|F[123][+]?)'
        ratings = [c for c in populated if re.fullmatch(rating, c['display_text'])]
        rating_headers = any(re.fullmatch(r'(?:long|short)[- ]term|(?:credit )?ratings?', s, re.I) for s in short_texts)
        if rating_headers and len(ratings) >= 2 and len({c['row'] for c in ratings}) >= 2:
            return True, 'credit_rating_matrix'
        return False, 'text_or_header_only'

    # Require distinct data cells and labels/headers. This excludes single-cell
    # prose wrappers even when they contain genuine numeric Inline XBRL tags.
    def aligned(value, label):
        vc, lc = value['column'] or value['source_cell_index'], label['column'] or label['source_cell_index']
        width = int(label['colspan']) if str(label['colspan']).isdigit() else 1
        return lc <= vc < lc + width
    if labels and (any(v['row'] == label['row'] or aligned(v, label) for v in values for label in labels)
                   or len(values) >= 2):
        return True, 'separate_numeric_data_cells'
    # A continued numeric panel can inherit labels from an earlier HTML table.
    # Require several standalone tagged values rather than a single stray fact.
    if len(values) >= 2 and all(c['fact_ids'] for c in values):
        return True, 'tagged_numeric_panel'
    return False, 'no_tabular_data_relationship'


def collect_tables(layout, document_id, company, year, items, financial_only=True, *, hidden_fact_resolver=None):
    doc = layout.doc
    by_table, by_cell = defaultdict(list), defaultdict(list)
    for fact in doc.fact_nodes:
        if doc.hidden[fact]:
            continue
        table = next(fact.iterancestors('table'), None)
        cell = next((n for n in fact.iterancestors() if n.tag in {'td', 'th'}), None)
        if table is not None:
            by_table[table].append(doc.fact(fact))
            if cell is not None:
                by_cell[cell].append(doc.fact(fact)['fact_id'])
    # SEC permits visible text to refer to a fact in ix:hidden through this
    # explicit CSS property. Do not infer a connection just from matching text.
    for node in doc.nodes:
        if doc.hidden[node]:
            continue
        match = re.search(r'(?:^|;)\s*-sec-ix-hidden\s*:\s*([\w.:-]+)\s*(?:;|$)', node.get('style', ''))
        if not match:
            continue
        fact = doc.ids.get(match.group(1))
        if fact is None and hidden_fact_resolver is not None:
            record = hidden_fact_resolver(match.group(1))
        elif fact is not None and namespace(fact) in IX and local(fact) in FACT_TAGS:
            record = doc.fact(fact)
        else:
            record = None
        if record is None:
            raise ValueError(f'Unresolved -sec-ix-hidden fact reference: {match.group(1)}')
        table = next(node.iterancestors('table'), None)
        cell = next((n for n in [node, *node.iterancestors()] if n.tag in {'td', 'th'}), None)
        if table is not None and cell is not None:
            by_table[table].append(record)
            by_cell[cell].append(record['fact_id'])
    tables, selected_facts, counts = [], {}, Counter()
    excluded, filtered, filtered_nodes = Counter(), [], set()
    for ordinal, table in enumerate(doc.tables, 1):
        if table in layout.excluded and not by_table[table]:
            excluded['heading_footer_or_index'] += 1
            continue
        rows = doc.rows(table)
        if not any(doc.display(c) for row in rows for c in row) and not by_table[table]:
            excluded['empty_table'] += 1
            continue
        # HTML-only navigation tables are not disclosure data tables.
        if not by_table[table] and sum(bool(c.xpath('.//a[starts-with(@href,"#")]')) for row in rows for c in row) >= 3:
            excluded['navigation_table'] += 1
            continue
        page, physical, members = layout.membership(table)
        counts[page] += 1  # Before Item filtering, so IDs remain stable.
        cells, grid, error = table_cells(doc, table, by_cell)
        table_facts = {f['fact_id']: f for f in by_table[table]}
        keep, reason = classify_table(cells, table_facts)
        if financial_only and not keep:
            filtered_nodes.add(table)
        if items is not None and not set(members).intersection(items):
            excluded['outside_requested_items'] += 1
            continue
        primary = members[0] if members else None
        index = counts[page]
        table_id = f'{html_tables.slug(company)}_{year}_{document_id}_{page or "unknown"}_t{index}'
        facts = list(dict.fromkeys(f['fact_id'] for f in by_table[table]))
        if financial_only and not keep:
            excluded[reason] += 1
            filtered.append({'table_id': table_id, 'page': page, 'source_locator': doc.paths[table],
                             'reason': reason, 'excluded_fact_ids': facts})
            continue
        selected_facts.update(table_facts)
        title, title_source = table_title(layout, table, ordinal, filtered_nodes)
        errors = [f for f in facts if selected_facts[f]['status'] == 'error']
        numeric_untagged = [c['cell_id'] for c in cells if not c['fact_ids']
                            and html_tables.numeric(c['display_text'])[0] in {'number', 'dash'}]
        enclosing = [{'concept': n.get('name'), 'context_ref': n.get('contextRef'), 'source_locator': doc.paths[n]}
                     for n in table.iterancestors() if namespace(n) in IX and local(n) in FACT_TAGS]
        tables.append({'table_id': table_id,
                       'position_id': f'{html_tables.slug(company)}*{year}{primary or "unknown"}*{page or "unknown"}*{index}',
                       'document_id': document_id, 'company': company, 'year': year,
                       'item': primary, 'referenced_items': members, 'physical_item': physical,
                       'page': page, 'page_table_index': index,
                       'title': title, 'title_source': title_source,
                       'source': doc.source, 'source_locator': doc.paths[table],
                       'cells': cells, 'grid': grid, 'fact_ids': facts, 'enclosing_xbrl_concepts': enclosing,
                       'status': 'needs_review' if error or errors else ('extracted' if facts else 'untagged'),
                       'diagnostics': {'layout_error': error, 'fact_errors': errors,
                                       'untagged_number_like_cells': numeric_untagged}})
    used_contexts = {f['context_ref'] for f in selected_facts.values()}
    used_units = {f['unit_ref'] for f in selected_facts.values()}
    exported = {'source': doc.source, 'sha256': hashlib.sha256(doc.data).hexdigest(),
                'namespaces': {k or 'html': v for k, v in doc.root.nsmap.items()},
                'contexts': {k: v for k, v in doc.contexts.items() if k in used_contexts},
                'units': {k: v for k, v in doc.units.items() if k in used_units}, 'facts': selected_facts,
                'scope': {'headings': layout.headings, 'page_references': layout.references,
                          'incorporated_sections': layout.ranges, 'external_references': layout.external},
                'diagnostics': {'total_inline_facts': len(doc.fact_nodes), 'exported_table_facts': len(selected_facts),
                                'excluded_tables': dict(excluded), 'image_only_pages': layout.image_gaps,
                                'filtered_tables': filtered,
                                'invalid_resources': doc.resource_errors}}
    return tables, exported


def extract_tables(data, company, year, source='', items=DEFAULT_ITEMS, report_loader=None, report_source=None,
                   financial_only=True):
    """Extract tables and document-scoped facts; ``items=None`` inspects all Items.

    report_loader(source) returns bytes. It is called only for an explicit
    named report incorporation; offline callers can supply Path.read_bytes.
    Set financial_only=False to include prose/header/layout tables as well.
    """
    doc = Document(data, source)
    fiscal_years = set()
    for node in doc.fact_nodes:
        if node.get('name', '').split(':')[-1] != 'DocumentFiscalYearFocus':
            continue
        concept = qname(node, node.get('name'))
        if concept['namespace'].startswith('http://xbrl.sec.gov/dei/'):
            fiscal_years.add(clean(doc.content(node)))
    if fiscal_years and fiscal_years != {str(year)}:
        raise ValueError(f'Requested FY{year}, but DocumentFiscalYearFocus is {sorted(fiscal_years)}.')
    layout = Layout(doc, resolve_references=items is not None)
    if items is not None:
        layout.validate(items)
    layouts = [('primary', layout)]
    references = [e for e in layout.external if e['item'] in set(DEFAULT_ITEMS) | set(items or [])]
    if items is not None and not any(e['item'] in items for e in references):
        references = []
    if references:
        if report_loader is None:
            raise ValueError('The filing incorporates an external report. Provide report_loader or use the CLI.')
        target = report_source or referenced_report_source(doc)
        report = Document(report_loader(target), target)
        def entities(document):
            return {(c['entity']['scheme'], c['entity']['identifier'].lstrip('0') or '0')
                    for c in document.contexts.values()}
        if entities(doc) and entities(report) and not entities(doc).intersection(entities(report)):
            raise ValueError('The referenced report has a different XBRL entity from the primary filing.')
        report_layout = Layout(report)
        attach_report_ranges(report_layout, references)
        layouts.append(('report1', report_layout))
    documents, tables = {}, []
    for document_id, current in layouts:
        found, exported = collect_tables(current, document_id, company, year, items, financial_only)
        tables.extend(found)
        documents[document_id] = exported
    if not tables and not any(d['diagnostics']['filtered_tables'] for d in documents.values()):
        raise ValueError('No disclosure tables found in the requested scope; no output written.')
    facts = [f for d in documents.values() for f in d['facts'].values()]
    stats = {'tables': len(tables), 'tables_with_facts': sum(bool(t['fact_ids']) for t in tables),
             'filtered_tables': sum(len(d['diagnostics']['filtered_tables']) for d in documents.values()),
             'untagged_tables': sum(not t['fact_ids'] for t in tables),
             'facts': len(facts), 'numeric_facts': sum(f['kind'] in {'nonFraction', 'fraction'} for f in facts),
             'fact_status': dict(Counter(f['status'] for f in facts)),
             'layout_errors': sum(bool(t['diagnostics']['layout_error']) for t in tables),
             'tables_without_verified_page': sum(t['page'] is None for t in tables),
             'untagged_number_like_cells': sum(len(t['diagnostics']['untagged_number_like_cells']) for t in tables)}
    return {'schema_version': 'xbrl-tables-1.0', 'extractor_version': VERSION,
            'company': company, 'year': year, 'requested_items': list(items) if items is not None else None,
            'table_filter': 'financial' if financial_only else 'all',
            'value_convention': 'Exact decimal strings in XBRL base units after transformation, sign and scale. '
                                'Untagged cells are display text only; number-like diagnostics also include headers.',
            'summary': stats, 'documents': documents, 'tables': tables}


def write_result(result, output, strict=False):
    if strict and (result['summary']['fact_status'].get('error', 0) or result['summary']['layout_errors']):
        raise ValueError('Strict extraction failed: unsupported/invalid facts or table layouts. '
                         'Run without --strict to retain raw data and review diagnostics. Existing output was not replaced.')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents a failed extraction/serialization truncating
    # a previously successful result.
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', nargs='?', help='Original Inline XBRL .htm/.html path or SEC HTTPS URL')
    issuer = parser.add_mutually_exclusive_group()
    issuer.add_argument('--ticker')
    issuer.add_argument('--cik')
    parser.add_argument('--company', help='Company label for IDs and the output directory')
    parser.add_argument('--year', required=True, type=int, help='Fiscal year, not filing year')
    parser.add_argument('--accession', help='Explicit original 10-K accession in API mode')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--items', nargs='+', default=list(DEFAULT_ITEMS))
    scope.add_argument('--all-items', action='store_true', help='Inspect the primary document without requiring Item boundaries')
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''))
    parser.add_argument('--cache-dir', type=Path, help='Optional SEC request cache')
    parser.add_argument('--report-source', help='Verified original Inline XBRL Exhibit 13 path/URL override')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--include-layout-tables', action='store_true',
                        help='Also retain prose/header/layout tables for inspection (default: financial tables only)')
    parser.add_argument('--strict', action='store_true', help='Fail before writing on fact/layout errors; untagged cells are still allowed')
    args = parser.parse_args(argv)
    try:
        if not 1900 <= args.year <= 2200:
            raise ValueError('--year must be a four-digit fiscal year')
        if bool(args.source) == bool(args.ticker or args.cik):
            raise ValueError('Provide either one local/SEC source or --ticker/--cik.')
        if args.accession and args.source:
            raise ValueError('--accession requires --ticker/--cik.')
        items = None if args.all_items else tuple(dict.fromkeys(item.upper() for item in args.items))
        if items and any(not re.fullmatch(r'\d{1,2}[A-C]?', item) for item in items):
            raise ValueError('--items expects Item numbers such as 1 1A 7 8')
        client = None
        def load(source):
            nonlocal client
            if urlparse(source).scheme in {'http', 'https'}:
                if client is None:
                    client = html_tables.SecClient(args.user_agent, args.cache_dir)
                return client.get(source)
            return Path(source).expanduser().read_bytes()
        metadata = None
        if args.source:
            source = args.source
            company = args.company or Path(urlparse(source).path).stem.split('-')[0]
        else:
            client = html_tables.SecClient(args.user_agent, args.cache_dir)
            print(f'Looking up the original FY{args.year} 10-K through the SEC submissions API...', flush=True)
            issuer_data, filings = html_tables.discover_sec_filings(client, [args.year], ticker=args.ticker or '',
                cik=args.cik or '', accessions={args.year: args.accession} if args.accession else None)
            metadata = {'issuer': issuer_data, 'filing': filings[args.year]}
            source = filings[args.year]['url']
            company = args.company or args.ticker or issuer_data['name']
            print(f'Selected {filings[args.year]["accessionNumber"]}; report date {filings[args.year]["reportDate"]}.', flush=True)
        result = extract_tables(load(source), company, args.year, source, items, load, args.report_source,
                                financial_only=not args.include_layout_tables)
        if metadata:
            result['sec_selection'] = metadata
        label = 'all' if items is None else 'items_' + '_'.join(i.lower() for i in items)
        output = (args.output_dir or Path('data/table_output') / f'{html_tables.slug(company)}_{args.year}_{label}_xbrl_tables') / 'result_xbrl.json'
        write_result(result, output, args.strict)
        s = result['summary']
        print(f'Wrote {output}\n{s["tables"]} tables; {s["facts"]} XBRL facts; '
              f'{s["filtered_tables"]} prose/layout tables filtered; '
              f'{s["untagged_tables"]} untagged tables; {s["fact_status"].get("error", 0)} fact errors; '
              f'{s["layout_errors"]} layout errors.')
        return 0
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
