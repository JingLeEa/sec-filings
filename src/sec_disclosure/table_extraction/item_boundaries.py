"""Locate the text preprocessor's selected Item sections in the original DOM.

The text extractor owns heading recognition, closing Items and longest-section
selection. This adapter only records where its HTML blocks came from; tables
and XBRL nodes stay in their original document coordinate system.
"""
from __future__ import annotations

import re

from sec_disclosure.extraction import sec_10k_extractor as text


class _LocatedParser:
    """Add DOM locations to the existing text parsers without changing them."""
    def __init__(self, doc):
        super().__init__()
        self.doc = doc
        self.node_index = 0
        self.locations = {}
        self.table_node = None

    def _node(self, tag):
        if self.node_index >= len(self.doc.nodes):
            raise ValueError('Text Item boundaries cannot be mapped to the original XHTML.')
        node = self.doc.nodes[self.node_index]
        self.node_index += 1
        if node.tag.rsplit('}', 1)[-1].lower() != tag.rsplit(':', 1)[-1].lower():
            raise ValueError('Text Item boundary tags disagree with the original XHTML.')
        return node

    def handle_starttag(self, tag, attrs):
        node = self._node(tag)
        stack_size = len(self.block_stack)
        table_depth = getattr(self, 'table_depth', 0)
        super().handle_starttag(tag, attrs)
        if len(self.block_stack) > stack_size:
            self.block_stack[-1]['dom_node'] = node
        if not table_depth and getattr(self, 'table_depth', 0):
            self.table_node = node

    def handle_startendtag(self, tag, attrs):
        self._node(tag)
        super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag):
        results = self.blocks if hasattr(self, 'blocks') else self.events
        count = len(results)
        block_node = self.block_stack[-1].get('dom_node') if self.block_stack else None
        table_node = self.table_node
        super().handle_endtag(tag)
        for result in results[count:]:
            node = table_node if getattr(result, 'kind', None) == 'table' else block_node
            if node is None:
                raise ValueError('A text Item block has no original XHTML location.')
            self.locations[result.index] = node


class _Blocks(_LocatedParser, text.FilingBlockExtractor):
    pass


class _Events(_LocatedParser, text.FilingStructureExtractor):
    pass


def _read(parser, html):
    parser.feed(html)
    parser.close()
    if parser.node_index != len(parser.doc.nodes):
        raise ValueError('Text Item boundaries do not cover the original XHTML nodes.')
    return parser


def _heading_allowed(doc, node, legacy_positions):
    if doc.hidden[node] or any(a.tag == 'table' for a in node.iterancestors()):
        return False
    if node.tag == 'table':
        return doc.order[node] in legacy_positions
    # Index links inside an embedded table are not links on the heading.
    for child in node.iter():
        if child.tag == 'table':
            break
        if child.tag == 'a' and child.get('href', '').startswith('#'):
            if not (child.get('href') == '#' and (child.get('id') or child.get('name'))
                    and not doc.display(child) and not child.xpath('.//img')):
                return False
    return True


def _styled_heading(node, value):
    if value.isupper():
        return True
    for child in node.iter():
        if child.tag == 'table':
            break
        if (child.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'b', 'strong'}
                or text.is_bold_style(child.get('style', ''))):
            return True
    return False


def detect(doc, legacy_headings):
    """Return canonical headings and selected half-open original-DOM ranges.

    Items with no retained narrative (e.g. a table-only section) have no text
    selection. The caller retains its verified table-heading fallback for those.
    """
    html = doc.data.decode('utf-8', errors='replace')
    parsed = _read(_Blocks(doc), html)
    blocks = text.item_blocks_with_layout_headings(html, parsed.blocks)
    locations = parsed.locations
    if blocks is not parsed.blocks:
        locations = _read(_Events(doc), html).locations
    legacy_positions = {h['position'] for h in legacy_headings}
    legacy_positions.update(doc.order[parent] for h in legacy_headings
                            for parent in doc.nodes[h['position']].iterancestors('table'))
    headings, parsed_headings = [], {}
    for index, block in enumerate(blocks):
        heading = text.parse_item_heading(block.text)
        if heading is None:
            continue
        parsed_headings[index] = heading[0]
        # The unchanged text matcher currently accepts Item 10 as Item 1.
        # Never promote a different full Item number into table membership.
        number = re.match(r'^\s*(?:part\s+[ivxlcdm]+\s*)?item\s+(\d{1,2}[a-c]?)\b', block.text, re.I)
        node = locations[block.index]
        if (number is None or number[1].upper() != heading[0]
                or not _heading_allowed(doc, node, legacy_positions)):
            continue
        headings.append({'item': heading[0], 'position': doc.order[node], 'text': block.text,
                         'source_locator': doc.paths[node], 'block_position': index})

    selected = text.extract_section_blocks(blocks, items=tuple(text.ITEM_ENDS))
    block_positions = {id(block): index for index, block in enumerate(blocks)}
    boundaries = []
    for item, chosen in selected.items():
        chosen_positions = [block_positions[id(block)] for block in chosen]
        for heading in headings:
            if heading['item'] != item or heading['block_position'] >= chosen_positions[0]:
                continue
            start = heading['block_position']
            closing = [i for i, value in parsed_headings.items()
                       if i > start and value in text.ITEM_ENDS[item]]
            if item == '15':
                closing += [i for i in range(start + 1, len(blocks))
                            if text.is_terminal_section_heading(blocks[i].text)]
            end = min(closing) if closing else len(blocks) if item == '15' else None
            if end is None:
                continue
            # Recover the original span of the already-selected block list.
            # Selection itself is performed by the text extractor above.
            retained = [i for i in range(start + 1, end) if blocks[i].text
                        and not text.should_drop_line(blocks[i].text)
                        and not text.is_boilerplate_or_nav(blocks[i].text)
                        and i not in parsed_headings]
            if retained == chosen_positions:
                boundaries.append({'item': item, 'start': heading['position'],
                    'end': doc.order[locations[blocks[end].index]] if end < len(blocks) else len(doc.nodes),
                    'method': 'text_preprocessing', 'source_locator': heading['source_locator']})
                break
    edges = {b[key] for b in boundaries for key in ('start', 'end')}
    headings = [h for h in headings if h['position'] in edges | legacy_positions
                or _styled_heading(doc.nodes[h['position']], h['text'])]
    for heading in headings:
        heading.pop('block_position')
    return headings, boundaries
