#!/usr/bin/env python3
"""Convert narrative comparison JSON to a 20-column TSV with original paragraphs.

Example:
    python3 scripts/export_disclosure_annotations.py \
        --input data/comparison/mu/2024_vs_2025/all_items_diff.json \
        --company Micron --industry Semiconductors

Reads all_items_diff.json or item_*_diff.json from the lexical comparison step.
One row contains a suggested sentence pair or one unmatched sentence.
Matching follows filing_sentence_annotator: combined token/character similarity,
candidate search across each Item, and one-to-one pairing at a 0.55 threshold.
Equivalent section headings receive a ranking bonus; Item 8 headings ignore
note-number prefixes. Original source headings are retained.
Original paragraphs come from the corresponding extraction chunk JSON files.
Taxonomy, materiality, and rationale cells remain blank for annotation. No downloads,
LLM calls, third-party packages, or changes to other scripts are required.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from html import escape
from pathlib import Path
from typing import Any


COLUMNS = [
    "Annotator", "Company", "Industry", "Split", "Filing Form",
    "Previous Fiscal Year", "Current Fiscal Year", "Item",
    "Previous Section / Subsection", "Previous Paragraph / Chunk ID",
    "Previous Disclosure Text", "Current Section / Subsection",
    "Current Paragraph / Chunk ID", "Current Disclosure Text",
    "Change Taxonomy", "Content Taxonomy", "Materiality",
    "Rationale / Evidence", "Previous Original Paragraph", "Current Original Paragraph",
]
DEFAULT_MATCH_THRESHOLD = 0.55
STOP = set("a an and are as at be been by for from has have in into is it its of on or our that the their these this to was we were which will with us".split())


def normalize_item(value: str) -> str:
    value = re.sub(r"^item\s*", "", value.strip(), flags=re.IGNORECASE).upper()
    if not re.fullmatch(r"\d+[A-Z]?", value):
        raise ValueError(f"Invalid Item number: {value!r}")
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_comparison(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(
            "Expected all_items_diff.json or item_*_diff.json from the lexical comparison step. "
            "For numerical table result.json, use export_table_annotations.py."
        )
    for field in ("old_year", "new_year"):
        if not re.fullmatch(r"\d{4}", str(data.get(field, ""))):
            raise ValueError(f"Input must contain a four-digit {field}.")
    if str(data["old_year"]) == str(data["new_year"]):
        raise ValueError("Previous and current fiscal years must differ.")
    return data


def validate_sentences(occurrences: Any, context: str) -> list[dict[str, Any]]:
    """Validate occurrences without merging, deduplicating, or changing text."""
    if not isinstance(occurrences, list):
        raise ValueError(f"{context}: expected a sentence list.")
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            raise ValueError(f"{context}: expected a sentence object.")
        source_id, sentence = occurrence.get("source_id"), occurrence.get("sentence")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError(f"{context}: a sentence is missing its source_id.")
        if not isinstance(sentence, str) or not sentence.strip():
            raise ValueError(f"{context}: a sentence is missing its text.")
    return occurrences


def section_title_key(item: str, title: str) -> str:
    """Ignore heading presentation, while retaining the actual section topic."""
    title = normalize_context_text(title)
    if normalize_item(item) == "8":
        without_note = re.sub(
            r"^note\s*(?:no\.?\s*)?(?:\(\s*\d+\s*\)|\d+)(?:\s*[.:\-]\s*|\s+)(?=\S)",
            "", title,
        )
        # A bare "Note 25." has no topic to match against another note.
        if any(character.isalpha() for character in without_note):
            title = without_note
    return title.rstrip(" .:")


def equivalent_section_groups(
    item: str, groups: list[dict[str, Any]], old_year: str, new_year: str, default_title: str,
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    combined: dict[tuple[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for position, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"Item {item}: expected a section object.")
        previous = validate_sentences(group.get(f"{old_year}_only"), f"Item {item}, {old_year}")
        current = validate_sentences(group.get(f"{new_year}_only"), f"Item {item}, {new_year}")
        fallback = group.get("item_title") or default_title
        old_title = str(group.get("old_item_title") or fallback)
        new_title = str(group.get("new_item_title") or fallback)
        old_key, new_key = section_title_key(item, old_title), section_title_key(item, new_title)
        # Explicitly paired different topics retain their existing relationship.
        # Never let automatic aliases override a manually verified title map.
        if group.get("title_match") == "manual" or (previous and current and old_key != new_key):
            key = ("existing_pair", str(position))
        else:
            key = ("equivalent_title", old_key if previous else new_key)
        old_entries, new_entries = combined.setdefault(key, ([], []))
        # Keep the exact title on each occurrence, even when several display
        # variants contribute to one logical group. The input JSON is unchanged.
        old_entries.extend({**entry, "_section_title": old_title, "_match_section": key} for entry in previous)
        new_entries.extend({**entry, "_section_title": new_title, "_match_section": key} for entry in current)
    return list(combined.values())


def remove_equivalent_unchanged(
    previous: list[dict[str, Any]], current: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    old_count = Counter(normalize_context_text(entry["sentence"]) for entry in previous)
    new_count = Counter(normalize_context_text(entry["sentence"]) for entry in current)
    shared = old_count & new_count

    def remaining(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        to_remove = shared.copy()
        kept = []
        for entry in entries:
            key = normalize_context_text(entry["sentence"])
            if to_remove[key]:
                to_remove[key] -= 1
            else:
                kept.append(entry)
        return kept

    return remaining(previous), remaining(current), sum(shared.values())


def sentence_tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text.lower())


def similarity(previous: str, current: str) -> float:
    """Use the same lexical score as filing_sentence_annotator/compare_filings.py."""
    token_score = SequenceMatcher(None, sentence_tokens(previous), sentence_tokens(current), autojunk=False).ratio()
    char_score = SequenceMatcher(None, previous.lower(), current.lower(), autojunk=False).ratio()
    return 0.8 * token_score + 0.2 * char_score


class CandidateIndex:
    """The reference annotator's word index, including its top-12 candidate limit."""

    def __init__(self, texts: list[str]):
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        terms = [Counter(word for word in re.findall(r"\w+", text.lower())
                         if len(word) > 1 and word not in STOP) for text in texts]
        frequencies = Counter(word for row in terms for word in row)
        for index, row in enumerate(terms):
            vector = {word: (1 + math.log(count)) * (1 + math.log((1 + len(texts)) / (1 + frequencies[word])))
                      for word, count in row.items()}
            length = math.sqrt(sum(weight * weight for weight in vector.values())) or 1
            for word, weight in vector.items():
                self.postings[word].append((index, weight / length))

    def top(self, text: str, limit: int = 12) -> list[int]:
        scores: dict[int, float] = defaultdict(float)
        for word, count in Counter(re.findall(r"\w+", text.lower())).items():
            if word not in STOP:
                for index, weight in self.postings.get(word, []):
                    scores[index] += weight * (1 + math.log(count))
        return sorted(scores, key=lambda index: (-scores[index], index))[:limit]


def pair_sentences(
    previous: list[dict[str, Any]], current: list[dict[str, Any]], threshold: float,
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Port the reference annotator's exact-first, greedy one-to-one matching.

    The caller supplies one Item, with section keys used only for ranking.
    Exact pairs are returned too, so the caller can omit/count unchanged rows.
    Preserve this exporter's previous-side ordering and every occurrence;
    unmatched current sentences follow the previous-side rows.
    """
    if not 0 <= threshold <= 1:
        raise ValueError("--match-threshold must be between 0 and 1.")

    def section(entry: dict[str, Any]) -> Any:
        return entry.get("_match_section", entry.get("_section_title", ""))

    exact: dict[str, list[int]] = defaultdict(list)
    for index, old in enumerate(previous):
        exact[old["sentence"]].append(index)
    used_old: set[int] = set()
    matches: dict[int, int] = {}
    # Reserve unchanged sentences before any edited sentence can consume them.
    for new_index, new in enumerate(current):
        options = [index for index in exact.get(new["sentence"], []) if index not in used_old]
        if options:
            old_index = max(options, key=lambda index: (
                section(previous[index]) == section(new),
                -abs(index / max(len(previous), 1) - new_index / max(len(current), 1)),
            ))
            used_old.add(old_index)
            matches[new_index] = old_index

    index = CandidateIndex([entry["sentence"] for entry in previous])
    candidates: list[tuple[float, float, int, int]] = []
    for new_index, new in enumerate(current):
        if new_index in matches:
            continue
        for old_index in index.top(new["sentence"]):
            if old_index in used_old:
                continue
            score = similarity(previous[old_index]["sentence"], new["sentence"])
            if score >= threshold:
                bonus = 0.025 if section(previous[old_index]) == section(new) else 0.0
                candidates.append((score + bonus, score, old_index, new_index))
    for _, _, old_index, new_index in sorted(candidates, reverse=True):
        if old_index not in used_old and new_index not in matches:
            matches[new_index] = old_index
            used_old.add(old_index)

    old_matches = {old_index: new_index for new_index, old_index in matches.items()}
    pairs = [(old, current[old_matches[index]] if index in old_matches else None)
             for index, old in enumerate(previous)]
    pairs.extend((None, new) for index, new in enumerate(current) if index not in matches)
    return pairs


def make_annotations(
    document: dict[str, Any], *, items: list[str] | None = None,
    annotator: str | None = None, company: str | None = None,
    industry: str | None = None, split: str | None = None,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    stats: dict[str, int] | None = None,
) -> list[dict[str, str]]:
    if not 0 <= match_threshold <= 1:
        raise ValueError("--match-threshold must be between 0 and 1.")
    old_year, new_year = str(document["old_year"]), str(document["new_year"])
    selected = {normalize_item(item) for item in items} if items else None
    present: set[str] = set()
    records: list[dict[str, str]] = []
    if stats is not None:
        stats["unchanged_sentence_pairs_removed"] = 0
    metadata = {
        "Annotator": annotator if annotator is not None else document.get("annotator", ""),
        "Company": company if company is not None else document.get("company", ""),
        "Industry": industry if industry is not None else document.get("industry", ""),
        "Split": split if split is not None else document.get("split", ""),
        "Filing Form": document.get("filing_form", "10-K"),
        "Previous Fiscal Year": old_year, "Current Fiscal Year": new_year,
    }
    for item_data in document["items"]:
        if not isinstance(item_data, dict):
            raise ValueError("Each items entry must be an object.")
        item = normalize_item(str(item_data.get("item", "")))
        present.add(item)
        if selected is not None and item not in selected:
            continue
        groups = item_data.get("item_titles")
        if not isinstance(groups, list):
            raise ValueError(f"Item {item}: expected an item_titles list.")
        item_previous, item_current = [], []
        for previous, current in equivalent_section_groups(item, groups, old_year, new_year, item_data.get("item_default_title", "")):
            previous, current, removed = remove_equivalent_unchanged(previous, current)
            if stats is not None:
                stats["unchanged_sentence_pairs_removed"] += removed
            item_previous.extend(previous)
            item_current.extend(current)
        for old, new in pair_sentences(item_previous, item_current, match_threshold):
            if old is not None and new is not None and old["sentence"] == new["sentence"]:
                if stats is not None:
                    stats["unchanged_sentence_pairs_removed"] += 1
                continue
            record = dict.fromkeys(COLUMNS, "")
            record.update({key: "" if value is None else str(value) for key, value in metadata.items()})
            record["Item"] = item
            for side, entry in (("Previous", old), ("Current", new)):
                if entry is not None:
                    record[f"{side} Section / Subsection"] = entry["_section_title"]
                    record[f"{side} Paragraph / Chunk ID"] = entry["source_id"]
                    record[f"{side} Disclosure Text"] = entry["sentence"]
            records.append(record)
    if selected is not None and selected - present:
        raise ValueError(f"Requested Item(s) absent from input: {', '.join(sorted(selected - present))}")
    return records


def chunk_json_path(source: Path, document: dict[str, Any], year: str, chunks_root: Path | None) -> Path:
    company = str(document.get("company") or "").strip().lower()
    if not company or company in (".", "..") or "/" in company or "\\" in company:
        raise ValueError("Input needs a company folder name to locate chunks; supply --previous-json and --current-json explicitly.")
    if chunks_root is None:
        # Works for data/comparison/<company>/<pair>/ and the runner's custom --data-dir.
        comparison_root = source.resolve().parent.parent.parent
        chunks_root = comparison_root.parent / "raw" if comparison_root.name == "comparison" else Path("data/raw")
    return chunks_root / company / year / f"{year}_chunks.json"


def load_paragraphs(path: Path, year: str, company: str) -> dict[str, dict[str, str]]:
    try:
        chunks = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except FileNotFoundError:
        raise ValueError(f"Chunk JSON not found: {path}. Supply --chunks-root or --previous-json/--current-json from the same extraction as the comparison.") from None
    if not isinstance(chunks, list):
        raise ValueError(f"Expected a list of extraction chunks: {path}")
    index: dict[str, dict[str, str]] = {}
    blocks: dict[tuple[str, str, int], list[str]] = {}
    keys: dict[str, tuple[str, str, int]] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError(f"Expected a chunk object in {path}.")
        chunk_id, text = chunk.get("id"), chunk.get("text")
        if not isinstance(chunk_id, str) or not chunk_id.strip() or not isinstance(text, str) or not text.strip():
            raise ValueError(f"Each chunk needs an id and nonempty text: {path}")
        if chunk_id in index:
            raise ValueError(f"Duplicate chunk ID {chunk_id!r} in {path}.")
        if chunk.get("year") is not None and str(chunk["year"]) != year:
            raise ValueError(f"Chunk {chunk_id} has the wrong fiscal year in {path}.")
        if company and chunk.get("company") is not None and str(chunk["company"]).strip().casefold() != company.strip().casefold():
            raise ValueError(f"Chunk {chunk_id} belongs to a different company in {path}.")
        item = normalize_item(str(chunk.get("item", "")))
        index[chunk_id] = {"item": item, "text": text, "paragraph": text}
        block = chunk.get("source_block_index")
        if type(block) is int and block > 0:
            key = (item, str(chunk.get("source", "")), block)
            keys[chunk_id] = key
            blocks.setdefault(key, []).append(text)
    # The extractor writes a block's split chunks in source order. Group only
    # with explicit block metadata; adjacent IDs alone do not prove a paragraph.
    paragraphs = {key: " ".join(parts) for key, parts in blocks.items()}
    for chunk_id, key in keys.items():
        index[chunk_id]["paragraph"] = paragraphs[key]
    return index


def normalize_context_text(text: str) -> str:
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"}))
    text = re.sub(r"\s*•\s*", " • ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def fill_original_paragraphs(
    records: list[dict[str, str]], document: dict[str, Any], source: Path,
    *, chunks_root: Path | None = None, previous_json: Path | None = None, current_json: Path | None = None,
) -> None:
    for side, year_key, explicit_path in (("Previous", "old_year", previous_json), ("Current", "new_year", current_json)):
        id_column, paragraph_column = f"{side} Paragraph / Chunk ID", f"{side} Original Paragraph"
        for record in records:
            record[paragraph_column] = ""
        populated = [record for record in records if record[id_column]]
        if not populated:
            continue
        year = str(document[year_key])
        path = explicit_path if explicit_path is not None else chunk_json_path(source, document, year, chunks_root)
        paragraphs = load_paragraphs(path, year, str(document.get("company") or ""))
        for record in populated:
            chunk_id = record[id_column]
            chunk = paragraphs.get(chunk_id)
            if chunk is None:
                raise ValueError(f"{side} chunk {chunk_id!r} was not found in {path}. Use the chunk files that produced this comparison.")
            if chunk["item"] != record["Item"]:
                raise ValueError(f"{side} chunk {chunk_id!r} has a different Item in {path}.")
            if normalize_context_text(record[f"{side} Disclosure Text"]) not in normalize_context_text(chunk["text"]):
                raise ValueError(f"{side} sentence does not match chunk {chunk_id!r} in {path}. The comparison and extraction may be from different runs.")
            record[paragraph_column] = chunk["paragraph"]


def safe_cell(value: str) -> str:
    # Keep tabs, line breaks and quotes; TSV quoting carries them in one cell.
    # A leading apostrophe prevents disclosure text from becoming a formula.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def quoted_tsv(rows: list[list[str]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerows(rows)
    result = stream.getvalue()
    parsed = list(csv.reader(io.StringIO(result, newline=""), delimiter="\t", strict=True))
    if parsed != rows or any(len(row) != len(COLUMNS) for row in parsed):
        raise ValueError(f"TSV verification failed: expected exactly {len(COLUMNS)} unchanged cells per row.")
    return result


def clipboard_page(rows: list[list[str]]) -> str:
    table_rows = "".join(
        '<tr>' + "".join('<td style="white-space:pre-wrap;mso-number-format:\\@">' + escape(cell) + '</td>' for cell in row) + '</tr>'
        for row in rows
    )
    header = '<tr>' + "".join('<th>' + escape(column) + '</th>' for column in COLUMNS) + '</tr>'
    payload = {}
    for key, included_header in (("rows", ""), ("with_headers", header)):
        payload[key] = {
            "html": '<html><body><table>' + included_header + table_rows + '</table></body></html>',
            "text": quoted_tsv(([COLUMNS] if included_header else []) + rows),
        }
    encoded = json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Copy disclosure annotations</title><style>
*{box-sizing:border-box}body{margin:0;padding:32px;background:#f3f6fa;color:#17304b;font:16px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:auto;padding:28px;background:white;border:1px solid #d8e1ec;border-radius:12px}
h1{font-size:26px;margin-top:0}button{padding:10px 16px;font:inherit;cursor:pointer;border:1px solid #245683;border-radius:6px;background:white;color:#174a78}
button:focus-visible{outline:3px solid #dc9d16}#copy{background:#174a78;color:white}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}
#status{padding:12px;background:#edf4fb}.scroll{overflow:auto;max-height:420px;margin-top:12px}table{border-collapse:collapse;font-size:13px}
td,th{border:1px solid #d8e1ec;padding:8px;min-width:130px;max-width:450px;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}th{background:#edf4fb;text-align:left}
</style></head><body><main><h1>Copy disclosure annotations</h1>
<p>''' + str(len(rows)) + ''' sentence comparison rows, with ''' + str(len(COLUMNS)) + ''' columns per row.</p>
<p>Each populated side contains one source sentence, its chunk ID, and the full extracted paragraph in the Original Paragraph column. Suggested pairs use the filing_sentence_annotator matching method and can cross subsections within the same Item; review them before annotation. Equivalent headings receive a ranking bonus, ignoring capitalization and Item 8 note numbers. Original heading labels are shown. A blank side means no match was assigned, not a confirmed addition or removal. Taxonomy, materiality, and rationale cells are blank.</p>
<ol><li>Choose whether to include headers, then click <strong>Copy rows</strong>.</li>
<li>In Google Sheets, single-click column <strong>A</strong> in an empty row, with enough empty rows below.</li>
<li>Paste normally with <strong>Cmd+V / Ctrl+V</strong>. Line breaks stay within their disclosure cells.</li></ol>
<label><input id="headers" type="checkbox"> Include column headers (for a new sheet)</label>
<div class="actions"><button id="copy" type="button">Copy rows</button><button id="select" type="button">Select rows for manual copy</button><button id="download" type="button">Download TSV</button></div>
<p id="status" role="status" aria-live="polite">Ready. Existing annotation sheets usually need rows without headers.</p>
<p>Use normal paste, rather than cell-edit mode or “Split text to columns.” If copying is blocked, select the rows below and press Cmd+C / Ctrl+C. You can also import the TSV with Tab as the separator.</p>
<details id="preview"><summary>Preview annotation rows</summary><div class="scroll"><table id="table"><thead id="column-headings">''' + header + '''</thead><tbody id="rows">''' + table_rows + '''</tbody></table></div></details>
<p>This page works offline. It writes to the clipboard only when you click Copy or copy the selected rows.</p>
</main><script id="clipboard-data" type="application/json">''' + encoded + '''</script><script>
const data = JSON.parse(document.getElementById('clipboard-data').textContent);
const status = document.getElementById('status');
const headers = document.getElementById('headers');
const payload = () => data[headers.checked ? 'with_headers' : 'rows'];
let selectedNode = null;
document.getElementById('copy').addEventListener('click', async () => {
  try {
    if (!navigator.clipboard || !navigator.clipboard.write || !window.ClipboardItem) throw new Error('Unavailable');
    const current = payload();
    await navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([current.html], {type:'text/html'}),
      'text/plain': new Blob([current.text], {type:'text/plain'})
    })]);
    status.textContent = 'Copied. Single-click column A in Google Sheets, then paste with Cmd+V / Ctrl+V.';
  } catch (error) {
    status.textContent = 'Automatic copy is unavailable. Click Select rows for manual copy, then press Cmd+C / Ctrl+C.';
  }
});
document.getElementById('select').addEventListener('click', () => {
  document.getElementById('preview').open = true;
  selectedNode = document.getElementById(headers.checked ? 'table' : 'rows');
  const range = document.createRange(); range.selectNode(selectedNode);
  const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
  status.textContent = 'Rows selected. Press Cmd+C / Ctrl+C, then paste into column A of your sheet.';
});
headers.addEventListener('change', () => { selectedNode = null; window.getSelection().removeAllRanges(); });
document.addEventListener('copy', (event) => {
  const selection = window.getSelection();
  if (!selectedNode || !event.clipboardData || !selection || !selection.containsNode(selectedNode, true)) return;
  const current = payload();
  event.clipboardData.setData('text/html', current.html);
  event.clipboardData.setData('text/plain', current.text);
  event.preventDefault();
});
document.getElementById('download').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([payload().text], {type:'text/tab-separated-values;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url;
  link.download = headers.checked ? 'disclosure_annotations.tsv' : 'paste_into_sheets.tsv'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});
</script></body></html>
'''


def export_annotations(records: list[dict[str, str]], output_dir: Path, source: Path) -> list[Path]:
    rows = [[safe_cell(record[column]) for column in COLUMNS] for record in records]
    outputs = {
        "disclosure_annotations.tsv": quoted_tsv([COLUMNS, *rows]),
        "paste_into_sheets.tsv": quoted_tsv(rows),
        "paste_into_sheets.html": clipboard_page(rows),
    }
    if any((output_dir / name).resolve() == source.resolve() for name in outputs):
        raise ValueError("An output path would overwrite the input JSON. Choose another output directory.")
    output_dir.mkdir(parents=True, exist_ok=True)
    # Finish serialization and stage every output before replacing an earlier export.
    with tempfile.TemporaryDirectory(prefix=".disclosure_export_", dir=output_dir) as temporary:
        staging = Path(temporary)
        for name, content in outputs.items():
            (staging / name).write_text(content, encoding="utf-8", newline="")
        for name in outputs:
            os.replace(staging / name, output_dir / name)
    return [output_dir / name for name in outputs]


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--input", required=True, type=Path, help="Comparison JSON: all_items_diff.json or item_*_diff.json.")
    parser.add_argument("--output-dir", type=Path, help="Default: data/disclosure_output/<company>/<old>_vs_<new>/<input_stem>/.")
    parser.add_argument("--chunks-root", type=Path,
                        help="Extraction root containing <company>/<year>/<year>_chunks.json. Inferred beside comparison/, otherwise data/raw.")
    parser.add_argument("--previous-json", type=Path, help="Explicit previous-year extraction chunks JSON for original paragraphs.")
    parser.add_argument("--current-json", type=Path, help="Explicit current-year extraction chunks JSON for original paragraphs.")
    parser.add_argument("--items", nargs="+", help="Optional Item filter, e.g. --items 1 1A 7 8.")
    parser.add_argument("--company", help="Company label for the TSV; defaults to the input company.")
    parser.add_argument("--industry", help="Industry label; blank if absent from the input.")
    parser.add_argument("--annotator", help="Annotator name; blank if absent from the input.")
    parser.add_argument("--split", help="Dataset split label; blank if absent from the input.")
    parser.add_argument("--match-threshold", type=float, default=DEFAULT_MATCH_THRESHOLD,
                        help="Minimum combined token/character similarity, from 0 to 1. Default: 0.55, as in filing_sentence_annotator. Highest-ranked available pairs are assigned one-to-one.")
    args = parser.parse_args(argv)
    try:
        source = args.input.expanduser()
        document = read_comparison(source)
        stats: dict[str, int] = {}
        records = make_annotations(document, items=args.items, company=args.company,
                                   industry=args.industry, annotator=args.annotator, split=args.split,
                                   match_threshold=args.match_threshold, stats=stats)
        fill_original_paragraphs(
            records, document, source,
            chunks_root=args.chunks_root.expanduser() if args.chunks_root is not None else None,
            previous_json=args.previous_json.expanduser() if args.previous_json is not None else None,
            current_json=args.current_json.expanduser() if args.current_json is not None else None,
        )
        output_dir = args.output_dir.expanduser() if args.output_dir else (
            Path("data/disclosure_output") / slug(str(document.get("company") or args.company or "unknown"))
            / f"{document['old_year']}_vs_{document['new_year']}" / slug(source.stem)
        )
        files = export_annotations(records, output_dir, source)
        paired = sum(bool(record["Previous Disclosure Text"] and record["Current Disclosure Text"]) for record in records)
        old_only = sum(not record["Current Disclosure Text"] for record in records)
        new_only = sum(not record["Previous Disclosure Text"] for record in records)
        print(f"Sentence comparison rows: {len(records)}")
        print(f"Suggested pairs: {paired}; unmatched previous: {old_only}; unmatched current: {new_only}")
        print(f"Additional unchanged sentence pairs removed: {stats['unchanged_sentence_pairs_removed']}")
        print(f"Columns per row: {len(COLUMNS)}")
        for path in files:
            print(f"Output: {path}")
        if records:
            print("Open paste_into_sheets.html, click Copy rows, then paste normally into column A of Google Sheets.")
        else:
            print("No changed disclosures in the selected items; the headered TSV contains only column names.")
        print("Review suggested sentence matches. Unmatched sentences are not automatically New/Removed; annotation fields remain blank.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
