"""Locate four 10-K Item bodies with model-selected source block ranges.

HTML parsing and printed-page mapping only prepare evidence. The model decides
which index tables, page spans, and block boundaries belong to an Item.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from sec_disclosure.agents.filing_item_fallback import (
    FallbackResult, InvalidFallbackResponse, _coerce_block_id, _request_json,
)
from sec_disclosure.llm.config import LLMConfig


ITEMS = ("1", "1A", "7", "8")


def _is_navigation_pointer(text: str) -> bool:
    return bool(re.search(
        r"\b(?:set forth|refer to|see|included in|appear(?:s)? on pages?|"
        r"located on pages?|on pages?\s+\d)\b", text, re.I,
    ))


def _explicit_item_label(text: str) -> str | None:
    from sec_disclosure.extraction.sec_10k_extractor import parse_item_heading

    if parse_item_heading(text) is None:
        return None
    match = re.match(r"\s*(?:part\s+[ivxlcdm]+\s*)?item\s+(\d{1,2}[A-Z]?)\b",
                     text, re.I)
    return match.group(1).upper() if match else None


def _ask(prompt: str, validate: Any, *, config: LLMConfig, timeout: float,
         max_tokens: int, retries: int) -> tuple[Any, int]:
    return _request_json(prompt, validate, config=config, timeout=timeout,
                         max_tokens=max_tokens, retries=retries)


def _index_tables(events: list[Any], *, config: LLMConfig, timeout: float,
                  max_tokens: int, retries: int) -> tuple[list[Any], int]:
    """Let the model choose navigation tables from a compact inventory of all tables."""
    tables = [event for event in events if event.kind == "table"]
    chosen: set[int] = set()
    attempts = 0
    for offset in range(0, len(tables), 45):
        window = tables[offset:offset + 45]
        ids = {table.index for table in window}
        inventory = [
            {"table_id": table.index, "rows": len(table.rows),
             "preview": [[cell[:100] for cell in row[:3]] for row in table.rows[:3]]}
            for table in window
        ]
        prompt = (
            "Identify tables that navigate to the substantive bodies of SEC 10-K "
            "Items 1, 1A, 7, or 8. Include cross-reference indexes and tables of "
            "contents even if they lack explicit Item headings. Financial data tables "
            "are not navigation tables. Return {\"table_ids\":[12,34]} or an empty list. "
            "Use only supplied IDs.\nTables: " + json.dumps(inventory, ensure_ascii=False)
        )

        def validate(value: Any) -> list[int]:
            raw = value.get("table_ids") if isinstance(value, dict) else None
            if not isinstance(raw, list) or any(type(number) is not int or number not in ids
                                                for number in raw):
                raise InvalidFallbackResponse("table_ids must be supplied table IDs")
            return raw

        try:
            selected, count = _ask(prompt, validate, config=config, timeout=timeout,
                                   max_tokens=max_tokens, retries=retries)
        except InvalidFallbackResponse as exc:
            print(f"Warning: filing table inventory could not be read: {exc}", file=sys.stderr)
            continue
        attempts += count
        chosen.update(selected)
    return [table for table in tables if table.index in chosen], attempts


def _index_references(tables: list[Any], targets: list[str], *, config: LLMConfig,
                      timeout: float, max_tokens: int, retries: int,
                      page_recovery: bool = False) -> tuple[list[dict], int]:
    from sec_disclosure.extraction.sec_10k_extractor import page_numbers_from_text

    references: list[dict] = []
    attempts = 0
    for table in tables:
        for offset in range(0, len(table.rows), 45):
            window = table.rows[offset:offset + 45]
            rows = {
                f"T{table.index}-R{offset + number}": [cell[:500] for cell in cells]
                for number, cells in enumerate(window)
            }
            prompt = (
                "Read this filing navigation table. For Items " + ", ".join(targets)
                + ", identify the row that gives each Item's substantive content "
                  "title and printed pages. Copy every page range, including disjoint "
                  "ranges. Return {\"references\":[{\"item\":\"7\","
                  "\"row_id\":\"T12-R3\",\"title\":\"Management's Discussion\","
                  "\"pages\":\"7-32, 70-129\"}]} or an empty list. Use only supplied "
                  "row IDs. Do not invent pages.\nRows: "
                + json.dumps(rows, ensure_ascii=False)
            )
            if page_recovery:
                prompt += ("\nReturn EVERY substantive row for each Item, including continuation rows and "
                           "disjoint references, not just its last row. Preserve prefixed page labels such as F-1–F-28. "
                           "A table of contents can give start pages only; do not invent a full range from a start page.")

            def validate(value: Any) -> list[dict]:
                raw = value.get("references") if isinstance(value, dict) else None
                if not isinstance(raw, list):
                    raise InvalidFallbackResponse("references must be a list")
                clean = []
                for ref in raw:
                    if not isinstance(ref, dict) or ref.get("item") not in targets:
                        raise InvalidFallbackResponse("invalid referenced Item")
                    row_id = ref.get("row_id")
                    title = ref.get("title")
                    pages = ref.get("pages", "")
                    if row_id not in rows or not isinstance(title, str) or not title.strip():
                        raise InvalidFallbackResponse("reference must cite a supplied row and title")
                    if page_recovery:
                        from sec_disclosure.agents.filing_page_locator import parse_printed_pages
                        if not isinstance(pages, str) or len(pages) > 256:
                            raise InvalidFallbackResponse("invalid printed page reference")
                        labels = parse_printed_pages(pages)
                        source_labels = set()
                        for cell in (cell for cells in rows.values() for cell in cells):
                            cell = re.sub(r"\s+(?:through|to)\s+", "-", cell.replace("–", "-"), flags=re.I)
                            for fragment in re.findall(r"(?:[A-Za-z]{1,3}-)?\d{1,4}(?:\s*-\s*(?:[A-Za-z]{1,3}-)?\d{1,4})?", cell):
                                try:
                                    source_labels.update(parse_printed_pages(fragment))
                                except ValueError:
                                    pass
                        if not labels.issubset(source_labels):
                            raise InvalidFallbackResponse("page reference is absent from supplied rows")
                        clean.append({"item": ref["item"], "title": title.strip()[:200],
                                      "pages": pages.strip().replace("–", "-"), "row_id": row_id})
                        continue
                    if not isinstance(pages, str) or len(pages) > 256 or (
                        pages and not re.fullmatch(
                            r"(?:pages?\s*)?\d{1,4}(?:\s*[-–]\s*\d{1,4})?"
                            r"(?:\s*[,;]\s*\d{1,4}(?:\s*[-–]\s*\d{1,4})?)*",
                            pages.strip(), re.I,
                        )
                    ):
                        raise InvalidFallbackResponse("invalid page reference")
                    # Some indexes split one Item's page list across following
                    # continuation rows (Citi does this for Item 1). Ground the
                    # numbers in the supplied window without requiring one row.
                    if pages and not set(page_numbers_from_text(pages)).issubset(
                        page_numbers_from_text(" ".join(
                            cell for cells in rows.values() for cell in cells
                        ))
                    ):
                        raise InvalidFallbackResponse("page reference is absent from supplied rows")
                    clean.append({"item": ref["item"], "title": title.strip()[:200],
                                  "pages": pages.strip().replace("–", "-"),
                                  "row_id": row_id})
                return clean

            try:
                found, count = _ask(prompt, validate, config=config, timeout=timeout,
                                    max_tokens=max_tokens, retries=retries)
            except InvalidFallbackResponse as exc:
                print(f"Warning: filing index rows could not be read: {exc}", file=sys.stderr)
                continue
            references.extend(found)
            attempts += count
    return references, attempts


def _page_key(page: Any) -> tuple[str, int]:
    from sec_disclosure.agents.filing_page_locator import page_parts
    return page_parts(str(page))


def _previous_page(page: Any) -> Any:
    from sec_disclosure.agents.filing_page_locator import previous_page
    return page - 1 if type(page) is int else previous_page(page)


def _block_pages(events: list[Any], blocks: list[Any]) -> tuple[dict[int, int], set[int]]:
    """Match generic parsed blocks to independently parsed printed folios."""
    from sec_disclosure.extraction.sec_10k_extractor import (
        event_page_numbers, validated_page_markers,
    )

    folios = validated_page_markers(events)
    pages = event_page_numbers(events, folios)
    if not pages:
        return {}, set()
    positions: dict[str, deque[int]] = defaultdict(deque)
    for position, event in enumerate(events):
        if event.kind != "table":
            positions[" ".join(event.text.split())].append(position)
    block_pages: dict[int, int] = {}
    folio_blocks: set[int] = set()
    folio_positions = {position for position, _ in folios}
    previous = -1
    for block in blocks:
        found = positions.get(" ".join(block.text.split()))
        if not found:
            return {}, set()
        position = found.popleft()
        if position <= previous:
            return {}, set()
        previous = position
        if position in pages:
            block_pages[block.index] = pages[position]
        if position in folio_positions:
            folio_blocks.add(block.index)
    return block_pages, folio_blocks


def _covers_index_pages(
    item: str, selected: list[Any], pages_by_item: dict[str, set[int]],
    block_pages: dict[int, int],
) -> bool:
    """Reject a small fragment when the index cites a substantial page span."""
    pages = pages_by_item[item]
    other_pages = set().union(*(value for key, value in pages_by_item.items() if key != item))
    expected = (pages - other_pages) & set(block_pages.values())
    if len(pages) < 2 or len(expected) < 4:
        return True
    covered = {block_pages[block.index] for block in selected
               if block.index in block_pages} & expected
    if len(covered) / len(expected) >= 0.6:
        return True
    print(f"Warning: Item {item} selection covers only {len(covered)}/{len(expected)} "
          "nonoverlapping indexed pages; leaving it unrecovered.", file=sys.stderr)
    return False


def _indexed_sections(
    blocks: list[Any], pages_by_item: dict[str, set[int]], references: list[dict],
    block_pages: dict[int, int], folio_blocks: set[int], *, config: LLMConfig,
    timeout: float, max_tokens: int, retries: int,
    available_pages: set | None = None,
) -> tuple[dict[str, list[Any]], int]:
    """Use model-selected index rows; ask the model who owns shared page spans."""
    from sec_disclosure.extraction.sec_10k_extractor import (
        is_boilerplate_or_nav, should_drop_line,
    )

    available = set(block_pages.values()) if available_pages is None else available_pages
    owners: dict[int, set[str]] = defaultdict(set)
    for item, pages in pages_by_item.items():
        if len(pages) > 1 and len(pages & available) / len(pages) >= 0.9:
            for page in pages:
                owners[page].add(item)
    if not owners:
        return {}, 0

    assigned = {page: next(iter(items)) for page, items in owners.items() if len(items) == 1}
    conflicts: list[tuple[set[int], set[str]]] = []
    for page in sorted(owners, key=_page_key):
        items = owners[page]
        if len(items) <= 1:
            continue
        if conflicts and _previous_page(page) in conflicts[-1][0] and items == conflicts[-1][1]:
            conflicts[-1][0].add(page)
        else:
            conflicts.append(({page}, set(items)))

    attempts = 0
    unresolved: set[str] = set()
    by_page: dict[int, list[Any]] = defaultdict(list)
    for block in blocks:
        if (page := block_pages.get(block.index)) is not None:
            by_page[page].append(block)
    for pages, choices in conflicts:
        sampled = []
        ordered_pages = sorted(pages, key=_page_key)
        for page in sorted({ordered_pages[0], ordered_pages[(len(ordered_pages) - 1) // 2], ordered_pages[-1]}, key=_page_key):
            entries = by_page.get(page, [])
            sampled.extend({"page": page, "block_id": block.index, "text": block.text[:160]}
                           for block in entries[:3])
        prompt = (
            "These printed filing pages are referenced by more than one SEC Item. "
            "Choose the single Item whose substantive body owns this shared page "
            "span. Use the index titles and source excerpts; return "
            "{\"owner\":\"7\"} or {\"owner\":null} if the pages cannot be "
            "assigned safely. Only the listed Items are valid owners.\n"
            "Page span: " + json.dumps([ordered_pages[0], ordered_pages[-1]])
            + "\nPossible Items: " + json.dumps(sorted(choices))
            + "\nIndex references: "
            + json.dumps([ref for ref in references if ref["item"] in choices], ensure_ascii=False)
            + "\nSource excerpts: " + json.dumps(sampled, ensure_ascii=False)
        )

        def validate(value: Any) -> str | None:
            owner = value.get("owner") if isinstance(value, dict) else object()
            if owner is not None and owner not in choices:
                raise InvalidFallbackResponse("owner must be one of the supplied Items or null")
            return owner

        try:
            owner, count = _ask(prompt, validate, config=config, timeout=timeout,
                                max_tokens=max_tokens, retries=retries)
        except InvalidFallbackResponse as exc:
            print(f"Warning: shared indexed pages could not be assigned: {exc}", file=sys.stderr)
            unresolved.update(choices)
            continue
        attempts += count
        if owner is not None:
            assigned.update({page: owner for page in pages})
        else:
            unresolved.update(choices)

    sections: dict[str, list[Any]] = {}
    for item, pages in pages_by_item.items():
        if len(pages) < 2 or item in unresolved:
            continue
        item_pages = {page for page, owner in assigned.items() if owner == item}
        if not item_pages:
            continue
        selected = [block for block in blocks
                    if block_pages.get(block.index) in item_pages
                    and block.index not in folio_blocks
                    and not should_drop_line(block.text)
                    and not is_boilerplate_or_nav(block.text)]
        if (len(selected) < 2
                or any((heading := _explicit_item_label(block.text)) is not None
                       and heading != item for block in selected)
                or not _covers_index_pages(item, selected, pages_by_item, block_pages)):
            continue
        sections[item] = selected
    return sections, attempts


def _global_heading_options(
    blocks: list[Any], item: str, references: list[dict] | None = None,
) -> list[dict]:
    """Offer compact boundary evidence from headings and index titles."""
    headings = [
        (position, block) for position, block in enumerate(blocks)
        if len(block.text) <= 180 and
        (block.bold or block.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
         or _explicit_item_label(block.text) is not None)
    ]
    if not headings:
        return []

    explicit = []
    for position, block in headings:
        match = re.match(r"\s*(?:part\s+[ivxlcdm]+\s*)?item\s+(\d{1,2}[A-Z]?)\b",
                         block.text, re.I)
        if match:
            explicit.append((position, match.group(1).upper()))
    starts = [position for position, label in explicit if label == item]
    windows: list[tuple[int, int]] = []
    for start in starts:
        end = next((position for position, _ in explicit if position > start), len(blocks))
        windows.append((start, end))
    if not starts:
        def rank(label: str) -> tuple[int, str]:
            match = re.fullmatch(r"(\d{1,2})([A-Z]?)", label)
            return (int(match.group(1)), match.group(2)) if match else (-1, "")

        target_rank = rank(item)
        for (start, before), (end, after) in zip(explicit, explicit[1:]):
            if rank(before) < target_rank < rank(after):
                windows.append((start, end))
    if not windows:
        if len(headings) > 300:
            return []
        selected = headings
    else:
        selected = [(position, block) for position, block in headings
                    if any(start <= position <= end for start, end in windows)]

    # A short labeled section that points to content elsewhere is a map, not
    # a body start. Keep the surrounding headings as context, but withhold the
    # pointer heading as a selectable boundary.
    pointer_starts = set()
    for start in starts:
        end = next((position for position, _ in explicit if position > start), len(blocks))
        body = " ".join(block.text for block in blocks[start + 1:end])
        if (end - start <= 4 and len(body) < 1000
                and _is_navigation_pointer(body)):
            pointer_starts.add(start)
    if item == "8" or pointer_starts:
        # A short Item pointer can refer to a report incorporated later in
        # the same filing. Supply that region as boundary evidence for the model.
        for start, label in explicit:
            if label == "15":
                end = next((position for position, _ in explicit if position > start), len(blocks))
                windows.append((start, end))
        selected = [(position, block) for position, block in headings
                    if any(start <= position <= end for start, end in windows)]
    selected = [(position, block) for position, block in selected
                if position not in pointer_starts]

    if len(selected) > 100:
        # Long descriptive sections can have hundreds of subheadings. Retain
        # their edges and the headings closest to the index's own title.
        titles = [ref["title"] for ref in references or []
                  if ref.get("item") == item and isinstance(ref.get("title"), str)]
        title_words = [set(re.findall(r"[a-z]{3,}", title.lower())) for title in titles]
        scores = []
        for index, (_, block) in enumerate(selected):
            words = set(re.findall(r"[a-z]{3,}", block.text.lower()))
            score = max((len(words & title) / len(title) for title in title_words if title),
                        default=0.0)
            scores.append((score, index))
        keep = set(range(min(16, len(selected))))
        keep.update(range(max(0, len(selected) - 12), len(selected)))
        for score, index in sorted(scores, reverse=True)[:8]:
            if score:
                keep.update(range(max(0, index - 6), min(len(selected), index + 7)))
        selected = [entry for index, entry in enumerate(selected) if index in keep]

    return [{"block_id": block.index, "text": block.text[:160]}
            for _, block in selected]


def _select_ranges(item: str, blocks: list[Any], options: list[dict], *, context: str,
                   used: set[int], config: LLMConfig, timeout: float,
                   max_tokens: int, retries: int) -> tuple[list[Any], int]:
    """Accept only model-chosen source ranges with supplied boundary IDs."""
    from sec_disclosure.extraction.sec_10k_extractor import (
        is_boilerplate_or_nav, should_drop_line,
    )

    if not options:
        return [], 0
    by_id = {block.index: position for position, block in enumerate(blocks)}
    sentinel = blocks[-1].index + 1
    allowed = {option["block_id"] for option in options}
    prompt = (
        f"Select the complete substantive body of SEC Item {item}. It may be under "
        "a descriptive heading, listed on multiple nonadjacent pages, or placed "
        "after an Item pointer. Return {\"ranges\":[{\"start_block\":123,"
        "\"end_block_exclusive\":456}]} or {\"ranges\":[]} if the evidence "
        "is insufficient. Every range must start and end at a supplied boundary ID. "
        "Copy IDs exactly from Boundary options; do not infer IDs between listed blocks. "
        "The supplied options are boundary candidates, not every intervening block; "
        "a selected range includes all source blocks between its two IDs. "
        "If an Item has no explicit heading, use the descriptive body heading that "
        "matches its index title and end at the next Item heading. "
        "End is exclusive. Include all relevant ranges; exclude index rows, pointers, "
        "other Items, and exhibit lists. Use surrounding titles and competing Item "
        "references to resolve overlapping page references. "
        "If Item 8 is a pointer, its financial report may start after Item 15 "
        "and end before the Exhibit Index. "
        "\nContext: "
        + context + "\nBoundary options: " + json.dumps(options, ensure_ascii=False)
    )

    def validate(value: Any) -> list[Any]:
        raw = value.get("ranges") if isinstance(value, dict) else None
        if not isinstance(raw, list):
            raise InvalidFallbackResponse("ranges must be a list")
        selected: list[Any] = []
        seen = set(used)
        previous_end = -1
        starts_at_item_heading = False
        for span in raw:
            if not isinstance(span, dict):
                raise InvalidFallbackResponse("range must be an object")
            start = _coerce_block_id(span.get("start_block"))
            end = _coerce_block_id(span.get("end_block_exclusive"))
            if start not in allowed or end not in allowed or start not in by_id:
                raise InvalidFallbackResponse("range boundaries must be supplied block IDs")
            start_position = by_id[start]
            end_position = len(blocks) if end == sentinel else by_id.get(end, -1)
            if start_position >= end_position or start_position < previous_end:
                raise InvalidFallbackResponse("ranges must be ordered and nonempty")
            start_heading = _explicit_item_label(blocks[start_position].text)
            if start_heading == item:
                starts_at_item_heading = True
                next_heading = next(
                    (position for position in range(start_position + 1, len(blocks))
                     if _explicit_item_label(blocks[position].text) is not None),
                    len(blocks),
                )
                end_is_nearby_section_heading = (
                    end_position + 1 == next_heading
                    and (blocks[end_position].bold or blocks[end_position].tag in {
                        "h1", "h2", "h3", "h4", "h5", "h6",
                    })
                )
                if end_position < next_heading and not end_is_nearby_section_heading:
                    raise InvalidFallbackResponse("range stops before the next explicit Item heading")
            previous_end = end_position
            for block in blocks[start_position:end_position]:
                heading = _explicit_item_label(block.text)
                if heading is not None and heading != item:
                    raise InvalidFallbackResponse(
                        f"Item {item} range crosses explicit Item {heading}"
                    )
                if block.index in seen:
                    raise InvalidFallbackResponse("range overlaps another Item or range")
                seen.add(block.index)
                if (block.text
                        and not should_drop_line(block.text)
                        and not is_boilerplate_or_nav(block.text)):
                    selected.append(block)
        preview = " ".join(block.text for block in selected)
        pointer_only = (starts_at_item_heading and len(selected) <= 4
                        and len(preview) < 1000 and _is_navigation_pointer(preview))
        if raw and (len(selected) < 2 or pointer_only):
            raise InvalidFallbackResponse("selected body is empty or only a pointer")
        return selected

    try:
        return _ask(prompt, validate, config=config, timeout=timeout,
                    max_tokens=max_tokens, retries=retries)
    except InvalidFallbackResponse as exc:
        print(f"Warning: Item {item} body ranges could not be validated: {exc}", file=sys.stderr)
        return [], 0


def _outline_locations(blocks: list[Any], targets: list[str], *, config: LLMConfig,
                       timeout: float, max_tokens: int, retries: int,
                       references: list[dict] | None = None) -> tuple[list[dict], int]:
    """Find body starts and ends when there is no usable page index."""
    candidates = [block for block in blocks if len(block.text) <= 180 or block.bold
                  or block.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}]
    if not candidates:
        candidates = blocks
    locations: list[dict] = []
    attempts = 0
    for offset in range(0, len(candidates), 120):
        window = candidates[offset:offset + 120]
        ids = {block.index for block in window}
        prompt = (
            "In this ordered filing outline, locate START and END boundaries "
            "for the substantive bodies of Items " + ", ".join(targets) + ". "
            "An explicit Item heading may be a pointer; its real body may start "
            "under a descriptive heading elsewhere. Other Item headings, including "
            "7A, 9 and 15, may be end boundaries, but are not requested outputs. "
            "For Item 8, a short Item 8 paragraph can point to financial statements "
            "after Item 15: mark the auditor report or financial report heading as "
            "Item 8's START and the first Exhibit Index/next Item heading as its END. "
            "A page number in an index can be a START page rather than the full body. "
            "Return {\"locations\":[{\"item\":\"7\",\"role\":\"start\","
            "\"block_id\":123},{\"item\":\"7\",\"role\":\"end\","
            "\"block_id\":456}]} or an empty list. Use only supplied IDs. "
            "Use IDs from the current Outline only; earlier locations are context. "
            "An end block is exclusive.\nIndex hints: "
            + json.dumps([ref for ref in (references or []) if ref["item"] in targets],
                         ensure_ascii=False)
            + "\nEarlier model-selected locations (context only): "
            + json.dumps(locations[-20:], ensure_ascii=False)
            + "\nOutline: "
            + json.dumps([{"block_id": block.index, "tag": block.tag,
                           "bold": block.bold, "text": block.text[:180]}
                          for block in window], ensure_ascii=False)
        )

        def validate(value: Any) -> list[dict]:
            raw = value.get("locations", []) if isinstance(value, dict) else None
            if not isinstance(raw, list):
                raise InvalidFallbackResponse("locations must be a list")
            clean = []
            for entry in raw:
                if (not isinstance(entry, dict) or entry.get("item") not in targets
                        or entry.get("role") not in {"start", "end"}):
                    raise InvalidFallbackResponse("invalid Item location")
                block_id = _coerce_block_id(entry.get("block_id"))
                if block_id not in ids:
                    raise InvalidFallbackResponse("location must cite a supplied block")
                clean.append({"item": entry["item"], "role": entry["role"],
                              "block_id": block_id})
            return clean

        try:
            found, count = _ask(prompt, validate, config=config, timeout=timeout,
                                max_tokens=max_tokens, retries=retries)
        except InvalidFallbackResponse as exc:
            print(f"Warning: filing outline window could not be read: {exc}", file=sys.stderr)
            continue
        locations.extend(found)
        attempts += count
    return locations, attempts


def locate_item_blocks(raw_html: str, blocks: list[Any], targets: list[str], *,
                       config: LLMConfig, timeout: float = 180, max_tokens: int = 1200,
                       retries: int = 3, page_recovery: bool = True,
                       page_cache_dir: Path | None = None) -> FallbackResult:
    """Locate only the four supported Items; never use deterministic Item sections."""
    from sec_disclosure.extraction.sec_10k_extractor import (
        html_to_structure_events, page_numbers_from_text,
    )

    if any(item not in ITEMS for item in targets):
        raise ValueError("The Item locator supports only 1, 1A, 7, and 8")
    if not targets or not blocks:
        return FallbackResult({}, 0)
    events = html_to_structure_events(raw_html)
    tables, attempts = _index_tables(events, config=config, timeout=timeout,
                                     max_tokens=max_tokens, retries=retries)
    reference_options = {"page_recovery": True} if page_recovery else {}
    references, count = _index_references(tables, targets, config=config,
                                         timeout=timeout, max_tokens=max_tokens,
                                         retries=retries, **reference_options)
    attempts += count
    block_pages, folio_blocks = _block_pages(events, blocks)
    pages_by_item: dict[str, set[int]] = defaultdict(set)
    for ref in references:
        if page_recovery:
            from sec_disclosure.agents.filing_page_locator import parse_printed_pages, page_parts
            if any(page_parts(label)[0] for label in parse_printed_pages(ref["pages"])):
                # F-1 is not numeric page 1 in the native integer-only map.
                continue
        pages_by_item[ref["item"]].update(page_numbers_from_text(ref["pages"]))
    sections, count = _indexed_sections(
        blocks, pages_by_item, references, block_pages, folio_blocks,
        config=config, timeout=timeout, max_tokens=max_tokens, retries=retries,
    )
    attempts += count
    used = {block.index for rows in sections.values() for block in rows}

    pending = [item for item in targets if item not in sections]
    if pending:
        context = json.dumps({"index_hints": [ref for ref in references
                                               if ref["item"] in pending]}, ensure_ascii=False)
        for item in list(pending):
            heading_options = _global_heading_options(blocks, item, references)
            if not heading_options:
                continue
            selected, count = _select_ranges(
                item, blocks, heading_options, context=context, used=used,
                config=config, timeout=timeout, max_tokens=max_tokens, retries=retries,
            )
            attempts += count
            if selected and _covers_index_pages(item, selected, pages_by_item, block_pages):
                sections[item] = selected
                used.update(block.index for block in selected)
        pending = [item for item in targets if item not in sections]
    if pending:
        locations, count = _outline_locations(blocks, pending, config=config,
                                              timeout=timeout, max_tokens=max_tokens,
                                              retries=retries, references=references)
        attempts += count
        for item in pending:
            starts = {entry["block_id"] for entry in locations
                      if entry["item"] == item and entry["role"] == "start"}
            ends = {entry["block_id"] for entry in locations
                    if entry["item"] == item and entry["role"] == "end"}
            if not starts or not ends:
                continue
            candidates = [block for block in blocks if block.index in starts | ends]
            options = [{"block_id": block.index, "text": block.text[:180]}
                       for block in candidates]
            selected, count = _select_ranges(
                item, blocks, options, context="Outline body boundaries selected by the model.",
                used=used, config=config, timeout=timeout,
                max_tokens=max_tokens, retries=retries,
            )
            attempts += count
            if selected and _covers_index_pages(item, selected, pages_by_item, block_pages):
                sections[item] = selected
                used.update(block.index for block in selected)
    pending = [item for item in targets if item not in sections]
    pointer_checks = set()
    if page_recovery:
        for pos, block in enumerate(blocks):
            item = _explicit_item_label(block.text)
            if item not in targets or item not in {"7", "8"}:
                continue
            end = next((p for p in range(pos + 1, len(blocks)) if _explicit_item_label(blocks[p].text)), len(blocks))
            body = " ".join(row.text for row in blocks[pos + 1:end])
            if len(body) < 1000 and _is_navigation_pointer(body) and re.search(r"\bpages?\s+(?:[A-Z]{1,3}-)?\d", body, re.I):
                pointer_checks.add(item)
    if page_recovery and (pending or pointer_checks):
        from sec_disclosure.agents.filing_page_locator import (
            bind_block_pages, locate_printed_pages, parse_printed_pages, resolve_page_pointers, page_boundaries, prefer_item_indexes,
        )
        try:
            references = prefer_item_indexes(events, references)
            mapping = locate_printed_pages(
                events, references, tables, config=config, timeout=timeout,
                max_tokens=max(2400, max_tokens), retries=min(retries, 1), cache_dir=page_cache_dir,
            )
            attempts += mapping.attempts
            mapping_attempts, mapping_hits = mapping.attempts, mapping.cache_hits
            resolved, count = resolve_page_pointers(
                events, references, [item for item in targets if item in {"7", "8"}], mapping.markers, config=config, timeout=timeout,
                max_tokens=max_tokens, cache_dir=page_cache_dir,
            )
            attempts += count
            if resolved:
                resolved_items = {ref["item"] for ref in resolved}
                references = [ref for ref in references if ref["item"] not in resolved_items] + resolved
                mapping = locate_printed_pages(events, references, tables, config=config, timeout=timeout,
                    max_tokens=max(2400, max_tokens), retries=min(retries, 1), cache_dir=page_cache_dir)
                attempts += mapping.attempts
                mapping_attempts += mapping.attempts
                mapping_hits += mapping.cache_hits
            print(f"Printed-page recovery: {len(mapping.markers)} footers; "
                  f"{mapping_attempts} requests; {mapping_hits} cache hits; "
                  f"{len(mapping.inferred_pages)} matched repeated layouts.", flush=True)
            for issue in mapping.issues:
                print("Warning: " + issue, file=sys.stderr)
            recovered_pages, recovered_folios = bind_block_pages(events, blocks, mapping.markers)
            label_pages = defaultdict(set)
            for ref in references:
                label_pages[ref["item"]].update(parse_printed_pages(ref["pages"]))
            # A verified pointer can expose a previously accepted highlights
            # fragment. Recheck its scope with the now-available physical map.
            for item in {ref["item"] for ref in resolved} & sections.keys():
                other_pages = set().union(*(pages for other, pages in label_pages.items() if other != item))
                expected = (label_pages[item] - other_pages) & set(recovered_pages.values())
                covered = {recovered_pages[block.index] for block in sections[item] if block.index in recovered_pages}
                if expected - covered or covered - label_pages[item]:
                    discarded = sections.pop(item)
                    used.difference_update(block.index for block in discarded)
                    if item not in pending:
                        pending.append(item)
                    print(f"Warning: Item {item} selection contradicted its verified body-page pointer; retrying its full scope.", file=sys.stderr)
            # A multi-page reference is a body scope. A single page from a TOC
            # remains a start hint, never a complete section by itself.
            available = set(page_boundaries(mapping.markers))
            complete = {}
            for item, pages in label_pages.items():
                others = set().union(*(value for key, value in label_pages.items() if key != item))
                if len(pages) > 1 and pages - others <= available:
                    complete[item] = pages
                elif item in pending:
                    print(f"Warning: Item {item} lacks complete printed-page boundaries; not recovering a fragment.", file=sys.stderr)
            recovered, count = _indexed_sections(
                blocks, complete, references, recovered_pages, recovered_folios,
                config=config, timeout=timeout, max_tokens=max_tokens, retries=retries, available_pages=available,
            )
            attempts += count
            for item in pending:
                selected = recovered.get(item, [])
                if selected and not any(block.index in used for block in selected):
                    sections[item] = selected
                    used.update(block.index for block in selected)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"Warning: printed-page recovery could not complete: {exc}", file=sys.stderr)
    return FallbackResult(sections, attempts)
