#!/usr/bin/env python3
"""Run SEC HTML extraction and export table pairs to a 17-column TSV.

Keep this file beside compare_html_tables.py. Extraction arguments such as
--ticker, --cik, --company, --previous-year, --current-year, --item and
--user-agent are passed directly to that program. Alternatively, use --input
to export an existing result.json without fetching filings again. Omit --table
to include every extracted table in the Item, one table pair per annotation row.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from html import escape

VERSION = '1.3.0'
COLUMNS = [
    'Annotator', 'Company', 'Industry', 'Split', 'Filing Form',
    'Previous Fiscal Year', 'Current Fiscal Year', 'Item',
    'Previous Section / Subsection', 'Previous Table / Chunk ID',
    'Previous Disclosure JSON', 'Current Section / Subsection',
    'Current Table / Chunk ID', 'Current Disclosure JSON',
    'Change Taxonomy', 'Content Taxonomy', 'Materiality',
]
CHANGE_LABELS = ['New', 'Removed', 'Expanded', 'Reduced', 'Modified', 'Reworded', 'Unchanged']
CONTENT_LABELS = [
    'Strategy & Business Model', 'Operations & Capacity', 'Technology & AI',
    'Cybersecurity & Data', 'Supply Chain & Third Parties',
    'Regulation, Legal & Compliance', 'Financial & Capital Resources',
    'Human Capital & Organization', 'Other / Unclassified',
]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key!r}')
        result[key] = value
    return result


def read_result(path):
    result = json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique_object)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object from compare_html_tables.py.')
    for side in ('previous', 'current'):
        if not isinstance(result.get(side), dict) or result[side].get('fiscal_year') is None:
            raise ValueError(f'Input is missing the {side} filing or its fiscal year.')
    return result


def select_table(filing, requested_title, side, *, allow_missing=False):
    normalize = lambda s: ' '.join(s.split()).casefold()
    tables = filing.get('tables')
    if not isinstance(tables, dict):
        raise ValueError(f'{side}: expected a tables object.')
    matches = [name for name in tables if normalize(name) == normalize(requested_title)]
    if not matches and allow_missing:
        return '', ''
    if len(matches) != 1:
        raise ValueError(f'{side}: expected one table matching {requested_title!r}; found {len(matches)}. '
                         f'Available titles: {", ".join(tables)}')
    name = matches[0]
    if not isinstance(tables[name], dict) or not tables[name]:
        raise ValueError(f'{side}: selected table has no data.')
    # The disclosure cell contains only table -> period -> section -> value.
    # Source metadata, page IDs, diagnostics and other tables are not copied.
    content = {name: tables[name]}
    compact = json.dumps(content, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return name, compact


def table_chunk_ids(filing, item):
    """Use pre-filter Item positions; legacy JSON falls back to its table order."""
    result, used = {}, set()
    metadata = filing.get('table_metadata', {})
    for position, name in enumerate(filing['tables'], start=1):
        index = metadata.get(name, {}).get('item_table_index', position)
        if type(index) is not int or index < 1:
            raise ValueError(f'{name!r}: item_table_index must be a positive integer.')
        chunk_id = f"table_{filing['fiscal_year']}_{item}_{index:02d}"
        if chunk_id in used:
            raise ValueError(f'Duplicate table position {index} within one filing.')
        used.add(chunk_id)
        result[name] = chunk_id
    return result


def make_annotation(document, title, *, annotator='', previous_section=None,
                    current_section=None, change_taxonomy='', content_taxonomy='', materiality='',
                    allow_missing=False):
    if change_taxonomy and change_taxonomy not in CHANGE_LABELS:
        raise ValueError('Unknown Change Taxonomy label.')
    if content_taxonomy and content_taxonomy not in CONTENT_LABELS:
        raise ValueError('Unknown Content Taxonomy label.')
    if materiality not in ('', 'Yes', 'No'):
        raise ValueError('Materiality must be Yes, No, or blank for annotation.')
    previous_name, previous_json = select_table(document['previous'], title, 'previous', allow_missing=allow_missing)
    current_name, current_json = select_table(document['current'], title, 'current', allow_missing=allow_missing)
    if not previous_name and not current_name:
        raise ValueError(f'Table {title!r} is absent from both filings.')
    item = re.sub(r'^item\s*', '', str(document.get('item', '')).strip(), flags=re.I).rstrip('.').upper()
    return dict(zip(COLUMNS, [
        annotator, document.get('company', ''), document.get('industry', ''),
        document.get('split', 'Development/Validation'), document.get('filing_form', '10-K'),
        document['previous']['fiscal_year'], document['current']['fiscal_year'], item,
        (previous_name if previous_section is None else previous_section) if previous_name else '',
        table_chunk_ids(document['previous'], item)[previous_name] if previous_name else '', previous_json,
        (current_name if current_section is None else current_section) if current_name else '',
        table_chunk_ids(document['current'], item)[current_name] if current_name else '', current_json,
        change_taxonomy, content_taxonomy, materiality,
    ]))


def make_annotations(document, title=None, **options):
    if title is not None:
        if not title.strip():
            raise ValueError('--table must be a title; omit it to export all tables.')
        return [make_annotation(document, title, **options)]
    # Preserve current filing order, then append previous-only tables. Matching
    # ignores case/spacing only; renamed tables remain separate for human review.
    titles = {}
    for side in ('current', 'previous'):
        tables = document[side].get('tables')
        if not isinstance(tables, dict):
            raise ValueError(f'{side}: expected a tables object.')
        seen = set()
        for name in tables:
            normalized = ' '.join(name.split()).casefold()
            if not normalized or normalized in seen:
                raise ValueError(f'{side}: blank or ambiguous table title {name!r}.')
            seen.add(normalized)
            titles.setdefault(normalized, name)
    if not titles:
        raise ValueError('No tables are available in either filing.')
    return [make_annotation(document, name, allow_missing=True, **options)
            for name in titles.values()]


def safe_cell(value):
    value = '' if value is None else str(value)
    value = value.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
    if value.lstrip().startswith(('=', '+', '-', '@')):
        value = "'" + value
    return value


def quoted_tsv(rows):
    """Spreadsheet text dialect: tab delimiters and fully quoted fields.

    Embedded double quotes are doubled for transport, then restored by a TSV
    reader. In particular JSON commas and quotes belong to ONE field.
    """
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter='\t', quoting=csv.QUOTE_ALL,
                        doublequote=True, lineterminator='\n')
    writer.writerows(rows)
    return stream.getvalue()


def clipboard_page(values, tsv):
    """Compatibility wrapper for callers exporting a single annotation row."""
    return clipboard_rows_page([values], tsv)


def clipboard_rows_page(rows, tsv):
    """A local, offline copy helper. Clipboard writes occur only on user action."""
    table_rows = ''.join('<tr>' + ''.join(
        '<td style="mso-number-format:\\@">' + escape(value) + '</td>' for value in values) + '</tr>'
        for values in rows)
    headers = '<tr>' + ''.join('<th>'+escape(c)+'</th>' for c in COLUMNS) + '</tr>'
    payload = {'html': '<html><body><table><tbody>'+table_rows+'</tbody></table></body></html>',
               'text': tsv}
    # Do not allow filing text to terminate the inert JSON script element.
    encoded = json.dumps(payload, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    values = rows[0]
    summary = escape(f'{values[1]} · {values[5]} → {values[6]} · Item {values[7]} · {len(rows)} annotation row(s)')
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Copy annotation to Google Sheets</title>
<style>
*{box-sizing:border-box}body{font:16px/1.55 system-ui,sans-serif;color:#17304b;background:#f3f6fa;margin:0;padding:36px}
main{max-width:1060px;margin:auto;background:white;border:1px solid #d8e1ec;border-radius:16px;padding:32px}
h1{font-size:27px;line-height:1.25;margin:0 0 12px}p{margin:12px 0}.summary{color:#536880}
.actions{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}button{border:1px solid #245683;border-radius:8px;padding:12px 18px;font:inherit;cursor:pointer;background:#fff;color:#174a78}
#copy-row{background:#174a78;color:white}button:focus-visible{outline:3px solid #dc9d16;outline-offset:3px}
#status{background:#edf4fb;padding:14px;border-radius:8px;min-height:55px}.note{color:#536880;font-size:14px}
details{margin-top:24px}.scroll{overflow:auto;max-height:350px;margin-top:12px;border:1px solid #d8e1ec}
table{border-collapse:collapse;font-size:12px}th,td{border:1px solid #d8e1ec;padding:8px;min-width:130px;max-width:450px;vertical-align:top;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#edf4fb;text-align:left}
</style></head><body><main>
<h1>Copy table annotations</h1><p class="summary">''' + summary + '''</p>
<p>One table pair per row, with 17 columns in every row. Each disclosure JSON stays inside its own table cell. The header is not copied.</p>
<ol><li>Click <strong>Copy all rows</strong>.</li><li>In Google Sheets, single-click column <strong>A</strong> of the first empty annotation row, with enough empty rows below.</li><li>Paste normally with <strong>⌘V</strong> on Mac or <strong>Ctrl+V</strong> on Windows.</li></ol>
<p class="note">Do not double-click into cell-edit mode, use “Split text to columns,” or choose paste-as-plain-text for this copy method.</p>
<div class="actions"><button id="copy-row" type="button">Copy all rows</button><button id="select-row" type="button">Select all rows for manual copy</button><button id="download-tsv" type="button">Download quoted TSV</button></div>
<p id="status" role="status" aria-live="polite">Ready. Annotator and annotation-label cells may intentionally be blank.</p>
<p class="note">If automatic copying is unavailable or blocked, select all rows using the second button, then press ⌘C / Ctrl+C yourself. This page makes no network requests and never reads your clipboard.</p>
<details id="preview"><summary>Preview all annotation rows</summary><div class="scroll"><table id="preview-table"><thead>''' + headers + '''</thead><tbody id="annotation-rows">''' + table_rows + '''</tbody></table></div></details>
<p class="note">Fallback: import the downloaded TSV into a new sheet using Tab as the separator, then copy its cells to your template. JSON contains many commas; do not choose Comma.</p>
</main><script id="clipboard-data" type="application/json">''' + encoded + '''</script>
<script>
const payload = JSON.parse(document.getElementById('clipboard-data').textContent);
const status = document.getElementById('status');
let manualSelection = false;
document.getElementById('copy-row').addEventListener('click', async () => {
  manualSelection = false;
  if (!navigator.clipboard || !navigator.clipboard.write || !window.ClipboardItem) {
    status.textContent = 'Automatic copy is unavailable here. Click Select all rows for manual copy, then press ⌘C / Ctrl+C.';
    return;
  }
  try {
    await navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([payload.html], {type:'text/html'}),
      'text/plain': new Blob([payload.text], {type:'text/plain'})
    })]);
    status.textContent = 'Copied all annotation rows with 17 columns each. Now single-click column A in Google Sheets and paste with ⌘V / Ctrl+V.';
  } catch (error) {
    status.textContent = 'The browser did not allow automatic copying. Click Select all rows for manual copy, then press ⌘C / Ctrl+C yourself.';
  }
});
document.getElementById('select-row').addEventListener('click', () => {
  document.getElementById('preview').open = true;
  const range = document.createRange();
  range.selectNode(document.getElementById('annotation-rows'));
  const selection = window.getSelection();
  selection.removeAllRanges(); selection.addRange(range);
  manualSelection = true;
  status.textContent = 'Rows selected. Press ⌘C / Ctrl+C, then paste normally into column A of your sheet.';
});
document.addEventListener('copy', (event) => {
  const selection = window.getSelection();
  if (!manualSelection || !event.clipboardData || !selection ||
      !selection.containsNode(document.getElementById('annotation-rows'), true)) return;
  event.clipboardData.setData('text/html', payload.html);
  event.clipboardData.setData('text/plain', payload.text);
  event.preventDefault();
  status.textContent = 'Copied all annotation rows with 17 columns each. Paste normally into column A of your sheet.';
});
document.getElementById('download-tsv').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([payload.text], {type:'text/tab-separated-values;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = 'paste_into_sheets.tsv'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});
</script></body></html>
'''


def export_annotation(record, output_dir):
    return export_annotations([record], output_dir)


def export_annotations(records, output_dir):
    if not records:
        raise ValueError('No annotation rows to export.')
    rows = [[safe_cell(record.get(column, '')) for column in COLUMNS] for record in records]
    tsv = quoted_tsv(rows)
    # Validate with a real TSV parser, not a naive string split.
    parsed = list(csv.reader(io.StringIO(tsv), delimiter='\t', strict=True))
    if parsed != rows or any(len(row) != 17 for row in parsed):
        raise ValueError('Every TSV row must contain exactly 17 columns.')
    for row, record in zip(parsed, records):
        for index in (10, 13):
            if row[index] and json.loads(row[index]) != json.loads(record[COLUMNS[index]]):
                raise ValueError('TSV serialization changed a disclosure JSON object.')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    files = [output_dir/'table_annotations.tsv', output_dir/'paste_into_sheets.tsv',
             output_dir/'paste_into_sheets.html']
    files[0].write_text(quoted_tsv([COLUMNS, *rows]), encoding='utf-8')
    files[1].write_text(tsv, encoding='utf-8')
    files[2].write_text(clipboard_rows_page(rows, tsv), encoding='utf-8')
    return files


def run_extractor(arguments):
    # Use the same Python interpreter and ordinary function arguments; no shell
    # command construction or second manual execution is needed.
    try:
        import compare_html_tables
    except ModuleNotFoundError as exc:
        if exc.name == 'compare_html_tables':
            raise ValueError('Keep export_table_annotations.py beside compare_html_tables.py.') from exc
        raise
    return compare_html_tables.main(arguments)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', type=Path, help='Optional existing result.json; skips extraction')
    parser.add_argument('--table', help='Optional exact table title, case-insensitive; omit for all extracted tables in the Item')
    parser.add_argument(
        '--output-dir',
        type=Path,
        default=Path('data/table_output/table_annotation_export'),
        help='Default: data/table_output/table_annotation_export',
    )
    parser.add_argument('--annotator', default='')
    parser.add_argument('--previous-section', default=None, help='Default: selected table source heading')
    parser.add_argument('--current-section', default=None, help='Default: selected table source heading')
    parser.add_argument('--change-taxonomy', choices=CHANGE_LABELS, default='')
    parser.add_argument('--content-taxonomy', choices=CONTENT_LABELS, default='')
    parser.add_argument('--materiality', choices=['Yes', 'No'], default='')
    args, extraction_args = parser.parse_known_args(argv)
    try:
        if args.input is not None and extraction_args:
            raise ValueError('--input skips extraction; remove the filing/API arguments or remove --input.')
        if any(a.split('=', 1)[0] == '--output-format' for a in extraction_args):
            raise ValueError('This exporter automatically selects JSON extraction; omit --output-format.')
        if args.input is None and not extraction_args:
            raise ValueError('Supply filing/API arguments, e.g. --ticker MU --previous-year 2024 '
                             '--current-year 2025 --item 7 --user-agent "Your name and email"; '
                             'or supply --input result.json.')
        out = args.output_dir.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        # A fresh staging directory prevents a failed extractor from reusing a
        # stale result.json or replacing a previous successful TSV export.
        with tempfile.TemporaryDirectory(prefix='table_annotation_run_', dir=out.parent) as folder:
            staging = Path(folder)
            if args.input is None:
                selection = 'the selected table' if args.table is not None else 'all supported tables in the selected Item'
                print(f'Step 1/2: extract {selection} from both filings.', flush=True)
                table_args = ['--table', args.table] if args.table is not None else []
                code = run_extractor(extraction_args + [
                    '--output-format', 'json', '--output-dir', str(staging)] + table_args)
                if code != 0:
                    raise ValueError('HTML extraction failed. No new annotation TSV was exported.')
                source = staging/'result.json'
            else:
                source = args.input.expanduser()
            document = read_result(source)
            if any('item_table_index' not in document[side].get('table_metadata', {}).get(name, {})
                   for side in ('previous', 'current') for name in document[side].get('tables', {})):
                print('Note: older JSON lacks Item table positions. Chunk IDs use the table order in that JSON; '
                      're-extract with compare_html_tables.py v1.3.1+ for stable numbering when filtering.', flush=True)
            records = make_annotations(
                document, args.table, annotator=args.annotator,
                previous_section=args.previous_section, current_section=args.current_section,
                change_taxonomy=args.change_taxonomy, content_taxonomy=args.content_taxonomy,
                materiality=args.materiality,
            )
            print(f'Step 2/2: export {len(records)} table-pair annotation row(s) in your 17-column format.', flush=True)
            files = export_annotations(records, staging)
            if args.input is None:
                files.insert(0, source)
            out.mkdir(parents=True, exist_ok=True)
            for path in files:
                os.replace(path, out/path.name)
        print(f'Output: {out}')
        print('Open paste_into_sheets.html in your browser, click Copy all rows, then paste normally into column A.')
        print('The TSV files are fully quoted; for file import, explicitly choose Tab as the separator.')
        print('Annotator and annotation labels are blank unless supplied as options.')
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
