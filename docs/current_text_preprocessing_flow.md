# Current Text Preprocessing Flow

This document records the current preprocessing behavior in:

- `src/sec_disclosure/extraction/sec_10k_extractor.py`
- `src/sec_disclosure/comparison/lexical_diff.py`

It describes what happens from SEC filing input through cleaned chunk output, sentence-level extraction output, and lexical comparison output.

## 1. High-Level Flow

```text
SEC 10-K source
  -> download or read HTML
  -> parse visible HTML blocks
  -> detect Items
  -> optionally follow incorporated annual/financial report references
  -> merge continued narrative blocks
  -> resolve inline footnotes
  -> detect section/subsection headers
  -> assign paragraph/chunk IDs
  -> create sentence-level records under each chunk
  -> write raw extraction outputs
  -> lexical comparison reads chunk JSON files
  -> lexical comparison removes unchanged sentences
  -> write comparison outputs
```

The lexical comparison stage starts only after extraction has written `<year>_chunks.json`.

## 2. Input Sources

The extractor accepts two input modes.

SEC API mode:

```bash
python3 scripts/extract_filings.py --ticker NVDA --year 2024
```

Direct source mode:

```bash
python3 scripts/extract_filings.py "https://www.sec.gov/Archives/edgar/data/.../.../nvda-20240128.htm" --year 2024
```

In SEC API mode, the code:

1. Resolves ticker to CIK using `https://www.sec.gov/files/company_tickers.json`.
2. Reads SEC submissions JSON from `https://data.sec.gov/submissions/CIK{cik}.json`.
3. Looks for an original `10-K` whose `reportDate` starts with the requested fiscal year.
4. If the target filing is not in the recent submissions list, loads historical submission files under `filings.files`.
5. Stops once a matching original 10-K is found.
6. Builds the filing URL from CIK, accession number, and primary document.
7. Downloads the filing HTML into memory.

The extractor no longer saves the downloaded raw HTML by default. Outputs go under:

```text
data/raw/<company>/<year>/
```

## 3. Default and Supported Items

The default extracted Items are:

```text
1
1A
7
8
```

Item 15 is supported, but it is no longer included by default.

Supported Items are:

```text
1
1A
7
8
15
```

Their default titles are:

```text
1  -> Business
1A -> Risk Factors
7  -> Management's Discussion and Analysis
8  -> Financial Statements and Supplementary Data
15 -> Exhibits and Financial Statement Schedules
```

Item 15 can enter the extraction in two ways:

1. Explicitly request it:

```bash
python3 scripts/extract_filings.py --ticker JPM --year 2025 --items 1 1A 7 8 15
```

2. Let the extractor auto-include it when Item 7 or Item 8 looks like a pointer to Item 15, financial statements, notes, or glossary material.

## 4. Conditional Item 15 Auto-Inclusion

The extractor first extracts the requested Items. With the default CLI settings, that means Items `1`, `1A`, `7`, and `8`.

After those Items are extracted, it checks the extracted Item 7 and Item 8 text.

Item 15 is auto-included if either Item 7 or Item 8 meets one of these conservative conditions:

1. The text explicitly references `Item 15` or `Part IV, Item 15`, and also mentions a financial-statement target such as:

```text
consolidated financial statements
financial statements
notes thereto
notes to consolidated financial statements
accounting pronouncements
```

2. The text is short, currently at most `1800` characters, and contains pointer wording such as:

```text
appears on pages
included on pages
set forth
information required by this item
read in conjunction
included in this annual report
```

and also points to financial statements, notes, a glossary, or page references.

Examples that should trigger Item 15:

```text
The information required by this Item is set forth in our Consolidated Financial Statements and Notes thereto included in this Annual Report on Form 10-K.
```

```text
Management's discussion and analysis ... appears on pages 46-160.
```

```text
Refer to Note 1 of the Notes to the Consolidated Financial Statements in Part IV, Item 15 ...
```

This is intentionally not fuzzy matching. It is a narrow rule for short pointer sections and explicit Item 15 references.

## 5. Item Boundary Detection

The current Item end boundaries are:

```text
Item 1  ends at Item 1A, 1B, 1C, or 2
Item 1A ends at Item 1B, 1C, or 2
Item 7  ends at Item 7A or 8
Item 8  ends at Item 9, 9A, 9B, or 9C
Item 15 ends at Item 16 or Signatures
```

The block-based path finds Item headings by applying `parse_item_heading` to each block.

Supported Item labels include:

```text
1, 1A, 1B, 1C, 2, 3, 4, 5, 6, 7, 7A, 8, 9, 9A, 9B, 9C, 15, 16
```

The parser accepts optional leading `Part I`, `Part II`, etc.

It accepts punctuation after the Item number:

```text
.
-
:
)
```

A line is rejected as an Item heading if:

- It is longer than 160 characters.
- It has more than 3 periods.
- It contains words such as `page`, `see`, `included`, `contained`, `above`, or `below`.

For each requested or auto-included Item:

1. Find all candidate starts.
2. Find the nearest later heading that is one of the Item's end Items.
3. For Item 15, also allow `Signatures` as an end marker.
4. If Item 15 has no explicit end marker, allow it to run to the end of the document.
5. Build all candidate ranges.
6. Choose the candidate with the greatest total block-text length.

Important implication:

```text
If an Item heading appears more than once, the extractor picks the longest plausible section.
```

## 6. HTML Block Extraction

The main path uses HTML block extraction rather than simple plain-text extraction.

The extractor parses filing HTML into `FilingBlock` objects. Each block contains:

```text
index
tag
text
style
bold
mixed_bold
italic
mixed_italic
font_size
segments
```

The block extractor keeps these block tags:

```text
div
p
h1
h2
h3
h4
h5
h6
```

The block extractor drops these tags entirely:

```text
script
style
noscript
table
svg
head
```

It also drops nodes with hidden styles:

```text
display:none
visibility:hidden
```

Inside block extraction:

- `<br>` becomes a newline during parsing, but block cleaning usually collapses internal line breaks.
- `<li>` is converted to a newline plus `- `.
- `&nbsp;` becomes a normal space.
- HTML entities are unescaped.
- Typography is normalized:

```text
curly apostrophes -> '
curly quotes      -> "
en dash           -> -
em dash           -> -
```

The block text cleaner also:

- Replaces `•` with `• `.
- Collapses spaces, tabs, carriage returns, form feeds, and vertical tabs.
- Removes spaces around line breaks.
- Collapses one or more internal newlines into a single space in normal block text.
- Collapses repeated whitespace.
- Strips leading/trailing whitespace.

Important implication:

```text
Most extracted HTML blocks become one cleaned line of text, even if the source HTML had internal line breaks.
```

## 7. Superscript Footnote Handling

The extractor detects numeric superscript footnote markers when:

- The marker text is 1 to 3 digits.
- The style looks like a superscript, such as small font plus negative top position or `vertical-align:super`.

During parsing, a superscript marker is temporarily represented as:

```text
[[FNREF:1]]
```

After section extraction, `resolve_section_footnotes` tries to replace references with the corresponding footnote definition.

A footnote definition can look like:

```text
1 Employee headcount includes subsidiaries.
```

If a matching definition is found, the original prose becomes:

```text
85,100 [footnote 1: Employee headcount includes subsidiaries.] people
```

If no matching definition is found, the marker becomes:

```text
[footnote 1]
```

Footnote definition blocks are not kept as separate narrative chunks when they are used to resolve earlier references.

## 8. Tables

The narrative extractor removes normal HTML tables from the main block stream.

Tables are still parsed by a separate structure extractor when needed for:

- Layout headings.
- Table of contents detection.
- Cross-reference index detection.
- Item 15 TOC-guided section labels.
- Referenced annual report outlines.
- Note headers stored as small layout tables.

### 8.1 Layout Tables

Some filings place Item headings inside one-row layout tables.

The extractor checks whether table events contain one-row Item labels that are missing from the normal block stream. If yes, it rebuilds the block stream from structure events and preserves:

- Non-table text events.
- One-row table Item headings.
- Small supported layout-table headings.

It does not retain full financial tables for narrative extraction.

### 8.2 Note Headers in Tables

Some filings store financial-statement note headers as a one-row, two-cell table:

```text
Note 3: | Operating Segments
```

For supported note-heading patterns, the extractor can treat this as a header such as:

```text
Note 3: Operating Segments
```

This is intentionally narrow so numeric data tables are not accidentally converted into section headers.

### 8.3 Table Captions

`is_table_caption` detects labels such as:

```text
Table 1: Something
Table 2.1 - Something
Table IV: Something
```

It only treats them as captions if they do not end with a sentence terminal. This avoids dropping normal sentences like:

```text
Table 2 presents ...
```

### 8.4 Period Comparison Labels

`is_period_comparison_label` detects standalone labels like:

```text
2024 vs. 2023
FY 2024 compared to FY 2023
Q1 2024 versus Q1 2023
```

These are treated as section context rather than normal narrative chunks.

Standalone period-comparison labels become subheaders even when they are plain text. The important restriction is that the whole block must be only the standalone period-comparison label.

Example:

```text
2024 compared with 2023
```

can become a child heading under a parent such as:

```text
Consolidated Results of Operations > 2024 compared with 2023
```

But a full sentence such as:

```text
2024 compared with 2023 revenue increased due to higher demand.
```

does not match the standalone-label rule and remains narrative text.

Period labels inside table events are still treated as table context and are not promoted to narrative subheaders.

## 9. Dropped Lines and Page Noise

`should_drop_line` removes page and filing noise.

A line is dropped if it is:

- A repeated filing/page header.
- A standalone page number with 1 to 4 digits.
- A horizontal rule made of repeated `-`, en dash, em dash, `_`, or spaces.
- `table of contents`.
- `index`.
- `part` followed by roman numerals.

Repeated filing/page header detection removes:

- Full labels like `59 | 2025 10-K`.
- Full labels like `2025 Form 10-K | 59`.
- `(Continued)` or `Continued`.
- `Notes to consolidated financial statements`.
- All-caps company/subsidiary headers matching patterns like:

```text
JPMORGAN CHASE & CO. AND SUBSIDIARIES
... Corporation and subsidiaries
... Company and subsidiaries
```

Important implication:

```text
This repeated-header removal is deliberately narrow. It tries to avoid deleting normal narrative references to Form 10-Ks.
```

## 10. Subheader Detection

The extractor tracks boldness using:

- `<b>`
- `<strong>`
- CSS `font-weight:bold`
- CSS `font-weight:700`, `800`, or `900`

It also tracks italic styling using:

- `<i>`
- `<em>`
- CSS `font-style:italic`
- CSS `font-style:oblique`

It tracks plain/non-emphasized text inside the same block. A block can therefore be:

```text
bold=True
mixed_bold=False
```

or:

```text
bold=True
mixed_bold=True
```

`mixed_bold=True` means part of the block looked bold and part looked plain.

Similarly:

```text
italic=True
mixed_italic=False
```

means the whole text run looked italic, while:

```text
italic=True
mixed_italic=True
```

means only part of the block looked italic.

Subheaders are detected by `is_subheader_block`.

A block is not treated as a subheader if:

- It is a table caption.
- It has tag `toc_text`.
- It has tag `merged_text`.
- It is empty.
- It is an Item heading.
- It is a dropped line.
- It starts with a bullet marker.
- Its text length is greater than 140 characters.
- It has more than 14 words.
- It ends with `.`, `!`, or `?`.

A block is treated as a subheader if it passes the exclusions above and one of the following is true:

- It is a TOC-derived header with tag `toc_header`.
- It has font size at least 10 and looks like a title.
- It is bold.
- It is a short standalone italic label.
- It is an HTML heading tag `h1` to `h6`.
- It looks like a title.

`looks_like_title` means at least 65% of scored alphabetic words begin with uppercase, are all uppercase, or contain product-style uppercase/digit patterns such as `x86` or `xPU`.

A short standalone italic label is a conservative rule for filings such as JPMorgan where subsection labels are italic but not bold. The current rule requires:

- the whole block is italic, not mixed italic;
- it has 2 to 8 alphabetic/alphanumeric words;
- the first alphabetic word starts with uppercase, or the text otherwise looks like a title;
- the usual subheader exclusions still pass, such as no bullet marker and no sentence-ending punctuation.

Example:

```text
Human capital > Global workforce
```

where `Global workforce` is italic in the filing.

Connector words are ignored for title scoring, including:

```text
a, an, and, as, at, by, for, from, in, into, of, on, or, the, to, with
```

There is extra colon handling:

- A colon-ending block outside `h1` to `h6` may still be treated as narrative if it is mixed bold and does not look like a title.
- A colon-ending block is treated as narrative if it ends with patterns such as:

```text
reflecting:
driven by:
due to:
because of:
as follows:
the following:
includes:
consists of:
comprised of:
```

Important implication:

```text
The `item_title` for each chunk is the current detected section path, not necessarily the SEC Item default title.
```

## 11. Header Hierarchy and Section Paths

Subheaders are stored as a section path.

The JSON record has:

```text
item_title
section_path
```

`item_title` is the joined path:

```text
Parent Header > Child Header > Sub Child Header
```

`section_path` stores the same hierarchy as a list.

Header hierarchy uses font size when available:

1. If a new header has a larger font size than the current header, it replaces higher-level context.
2. If a new header has a smaller font size, it is treated as a child.
3. If font sizes are the same, visual style is checked before all-caps priority.

For same-size headings, stronger styles close weaker styles or same-style siblings, while weaker styles can remain under stronger parent headings.

Current style strength is:

```text
toc/html heading > bold > bold_italic > italic > title
```

This allows JPM-style same-size headings such as:

```text
Human capital
Global workforce
```

to become:

```text
Human capital > Global workforce
```

when `Human capital` is bold and `Global workforce` is italic.

It also handles Citi-style headings where a bold parent is followed by a bold+italic child:

```text
2025 Results Summary
Citigroup
```

can become:

```text
2025 Results Summary > Citigroup
```

when `2025 Results Summary` is bold and `Citigroup` is bold+italic at the same font size.

If font size and style do not decide the relationship, all-caps priority can close the previous same-level header.

Same-size, same-style headings are treated as siblings before all-caps priority is considered. This prevents a short all-caps heading from being nested under a longer all-caps heading only because of the all-caps priority heuristic.

For example:

```text
SEGMENT REVENUES AND INCOME (LOSS)
REVENUES(1)
INCOME
```

becomes:

```text
SEGMENT REVENUES AND INCOME (LOSS) > REVENUES(1)
SEGMENT REVENUES AND INCOME (LOSS) > INCOME
```

rather than nesting `INCOME` under `REVENUES(1)`, assuming `REVENUES(1)` and `INCOME` have the same font size and style.

All-caps priority is only used for:

- Multi-word all-caps headings.
- Longer all-caps words.

Short all-caps acronyms such as `CCG`, `DCAI`, or `MD&A` are not automatically treated as higher-priority all-caps headings.

When font size is unavailable, the extractor also tracks the style kind of each header:

```text
toc
html_heading
bold
bold_italic
italic
title
```

This prevents the hierarchy from depending only on text casing. A weaker/different style can be treated as a child of the current parent, while same-style headings become siblings. For example:

```text
Human capital
Global workforce
Rewarding and supporting employees
```

can become:

```text
Human capital > Global workforce
Human capital > Rewarding and supporting employees
```

where `Human capital` is bold and the two child labels are italic.

If a stronger style appears after an italic child, the extractor climbs back out of the italic subsection instead of nesting the stronger header underneath it.

Some generic child titles keep parent context when font size is unavailable, including:

```text
Overview
Market Trends
Competition
Customers
Products
Services
Financial Performance
Operating Performance
```

Example:

```text
DCAI
Overview
```

can become:

```text
DCAI > Overview
```

Connector words such as `and`, `of`, `for`, and `with` do not automatically disqualify a standalone line from being a header. If the line otherwise looks title-like, it can still become a same-level header.

Example:

```text
Management's Discussion and Analysis
Operating Segment Results
Consolidated Results of Operations
Liquidity and Capital Resources
Critical Accounting Estimates
```

can become:

```text
Management's Discussion and Analysis
Operating Segment Results
Consolidated Results of Operations
Liquidity and Capital Resources
Critical Accounting Estimates
```

## 12. Item 15 TOC-Guided Extraction

Item 15 has special handling because some companies include annual report content inside or after Item 15.

This special handling is only used if Item 15 is explicitly requested or auto-included.

The extractor:

1. Finds the Item 15 range.
2. Looks for a block whose text is exactly `Table of Contents`.
3. Looks for the first table after that TOC label.
4. Extracts TOC entries from that table.
5. Checks whether the TOC looks supported.

The TOC must have at least 4 entries and include at least one narrative marker such as:

```text
management s discussion and analysis
executive overview
firmwide risk management
critical accounting estimates used by the firm
```

If supported:

- TOC entries become `toc_header` blocks.
- Body text matching a TOC entry becomes a header.
- Other body text becomes `toc_text`.
- `Table of Contents`, dropped lines, and Item headings are skipped.
- Table cells that match TOC entries can also become headers.
- TOC indentation is used to infer hierarchy when available.
- TOC indentation uses effective indentation:

```text
effective indent = max(padding-left, margin-left, 0) + text-indent
```

- If all effective indents are missing or collapse to one level, all TOC entries are treated as same-level.
- If more than one distinct effective indent exists, each distinct indent becomes a hierarchy level.
- Distinct effective indents are grouped only when they are within `0.1pt` of each other.
- Hanging indents can cancel out: for example, `padding-left:4.5pt` plus `text-indent:-4.5pt` has an effective indent of zero and remains same-level.

Important caveat:

```text
The current code does not require a large minimum indent gap for TOC hierarchy.
Small nonzero effective indent differences can create parent-child levels.
```

Header matching normalizes:

- Typography.
- Case.
- `&` to `and`.
- Non-alphanumeric characters to spaces.
- Parentheses are removed for TOC match keys.

Important implication:

```text
For JPM-style Item 15, `item_title` can be driven by TOC section titles rather than bold inline phrases.
```

Indented TOC children are represented in `item_title` and `section_path`:

```text
Management's discussion and analysis > Executive Overview
```

## 13. Cross-Reference Index Fallback

Some filings do not organize the full annual-report content under standard SEC Item headings.

For those filings, the extractor can fall back to cross-reference index or multi-column TOC logic.

This is used for filings where:

- Item numbers appear in index/TOC tables.
- The real narrative sections are titled elsewhere in the document.
- The Item number is in a separate table column from the title.

The fallback:

1. Parses structure events from the full HTML.
2. Reads Item references from tables.
3. Finds referenced pages and referenced titles.
4. Builds candidate report section titles for each Item.
5. Locates matching report headings.
6. Extracts blocks under those matched report headings.
7. Removes broad overlaps, such as Item 1 accidentally absorbing Item 7, Item 8, or Item 15.

For Item 7, pages assigned to Item 8 or Item 15 are excluded from the Item 7 fallback search.

For Item 1, pages assigned to other requested Items are excluded.

### Intel-style Item 7 root selection

Some non-traditional filings, including Intel's 2025 filing, list only selected
Item 7 subsections and page ranges in the cross-reference index. The actual HTML
still places additional content immediately under the Item 7 parent, for example:

```text
Management's Discussion and Analysis
  Overview
  Significant Events and Trends Impacting Results
  Operating Segment Results
  Consolidated Results of Operations
  Liquidity and Capital Resources
```

The extractor therefore treats the cross-reference title
`Management's Discussion and Analysis` as a root range. It keeps the
unpaginated introductory headings and their text, rather than selecting only
the page-linked subsection titles.

The root range stops before known non-Item-7 sections such as `Properties`,
`Quantitative and Qualitative Disclosures About Market Risk`, and the financial
statement section. Any additional subsection explicitly listed for Item 7 in the
cross-reference index, such as `Critical Accounting Estimates`, is selected
separately so it is retained even when it appears after another Item in the
HTML layout.

For this path, the output hierarchy can therefore look like:

```text
Item 7 - Management's Discussion and Analysis > Overview
Item 7 - Operating Segment Results
Item 7 - Management's Discussion and Analysis > Critical Accounting Estimates
```

Important implication:

```text
The cross-reference index identifies the candidate content and page range, but
the final hierarchy follows the source HTML heading styles. A heading with the
same effective style as the Item 7 root is a sibling; a smaller or weaker
heading becomes its child.
```

### Cross-reference page-range filtering

When the cross-reference index includes printed page ranges, the extractor also
uses those ranges as a content filter. This is especially useful when the HTML
contains several report sections with similar headings or when the filing does
not use standard SEC Item headings.

The page-range flow is:

1. Read the page numbers from the cross-reference index, including ranges such
   as `Pages 18-29`.
2. Scan the HTML structure events for footer-style printed-page markers such as
   `MD&A | 18`, `MD&A | 19`, or `Image | MD&A | 18`.
3. Assign the events before each footer marker to that marker's page. A footer
   is treated as appearing after the content on its printed page.
4. Keep only events whose inferred page is included in the Item's referenced
   page set.
5. Apply the report heading hierarchy and known Item boundaries afterward.

The page range is therefore a candidate filter, not the only extraction rule.
It does not replace heading hierarchy, root-section selection, or overlap
exclusions. For example, Item 7 can still stop before Properties or Item 7A
even when the page ranges overlap because the section boundary logic is applied
after page filtering.

Footer markers are removed from the extracted text. A bare number is not treated
as a page marker because it could be a table value, year, or other narrative
number. If the filing does not expose usable footer-style markers in its HTML,
the extractor keeps the existing heading-based fallback rather than silently
dropping content.

This means page-based extraction is best understood as:

```text
cross-reference page range
        +
HTML footer page markers
        +
heading hierarchy and Item boundaries
        -> selected narrative blocks
```

## 14. Referenced Annual or Financial Reports

Some short Items say the content is incorporated by reference from another annual or financial report.

The extractor tries to follow those references unless `--no-follow-references` is used.

It recognizes a referenced report only when:

- The Item text is not too long.
- It contains an incorporation/reference phrase.
- It contains `information in response to this item`.
- It mentions an annual or financial report.
- It quotes named sections.

It does not follow incidental cross-references in normal narrative text.

If a referenced report is detected:

1. Locate the SEC filing index.
2. Prefer `EX-13` or `EX-13.*`.
3. Otherwise look for a unique exhibit whose description mentions annual or financial report.
4. Require the referenced report to be HTML.
5. Parse the report structure.
6. Use the report's own TOC or explicit `h1` to `h6` heading hierarchy.
7. Select the quoted referenced section paths.

If there are overlapping referenced sections and `--include-reference-overlaps` is not used:

- A subsection assigned to a more specific requested Item is excluded from the broader Item.

For referenced reports:

- Known report headings are removed from narrative unless they are needed as subheaders.
- Table captions and period comparison labels reset the active parent header instead of becoming narrative.
- One-cell, one-row prose layout tables may become subheaders.
- Full numeric tables are excluded.
- Repeated running headers are skipped.

The source of a chunk is updated to the referenced report source if the chunk came from the referenced report.

The output also writes `<year>_sources.json`, containing:

```text
primary_source
referenced_items
```

## 15. Block Merging Before Chunk Records

After section blocks are found, the extractor merges continued blocks with `merge_continued_blocks`.

Two adjacent blocks are not merged if either block is:

- A table caption.
- A period comparison label.
- A subheader.

If the current block starts with a bullet marker, it is merged into the previous pending block.

Current bullet markers are detected by:

```text
•
‣
▪
▫
◦
●
○
* followed by whitespace
- followed by whitespace
```

If the current block does not start with a bullet, the extractor merges it with the previous block when the previous block does not end with a sentence terminal.

There is also a narrow wrapped-reference exception. If the previous block ends
with an unfinished connector such as `and`, `of`, `to`, `above`, or `below`,
and the next block begins with a quote or opening parenthesis, the blocks are
merged even if the next block is styled like a heading. This handles references
that are split across HTML blocks, for example:

```text
... see the discussion above and
"Managing Global Risk-Other Risks-Country Risk" below
```

The exception is intentionally limited so a quoted heading after a complete
sentence remains a separate heading.

There is one extra bullet-list safeguard:

- If the previous block starts with a bullet and the current block does not start with a bullet, the current block is not merged merely because the bullet lacks a full stop.
- It is merged only when the current block still has bullet-continuation layout, such as `padding-left`, `margin-left`, or `text-indent`.

This keeps rendered bullet continuations together while preventing a short bullet label from swallowing the normal paragraph that follows the list.

The same idea is applied at the HTML segment level:

- If a non-bullet segment follows a bullet segment, it is only appended to the bullet when it is marked as a bullet continuation.
- A new normal segment after a bullet starts a new sentence unit when it looks like a new context.
- If the previous bullet has not ended with sentence punctuation and the next segment starts with a lowercase letter, the next segment is treated as a continuation of that same bullet.
- If non-bullet text appears later inside the same HTML segment after a bullet sentence, it can become a separate sentence unit instead of being forced into the bullet.

The last case matters for Citi-style text such as:

```text
• Banamex-related notable item: expenses included ... (Banamex) Excluding the Russia-related notable item ..., net income was ...
```

This can become:

```text
• Banamex-related notable item: expenses included ... (Banamex)
Excluding the Russia-related notable item ..., net income was ...
```

In code, this distinction is tracked in `sentence_units_from_block`:

```text
segment.continues_previous_bullet -> layout-confirmed continuation
segment_units                    -> already emitted a sentence from the same HTML segment
```

Sentence terminals are:

```text
.
!
?
```

with optional trailing quote or closing bracket characters:

```text
"
'
)
]
```

When blocks are joined:

- If the current block starts with a bullet, it is joined with a newline.
- If the previous block ends with `-`, the current block is appended directly with no space.
- Otherwise, the blocks are joined with one space.

The merged block receives:

```text
tag="merged_text"
index=<previous block index>
segments=<previous segments + current segments>
```

Important implications:

```text
Extraction intentionally groups a lead-in paragraph and its following bullet blocks into one parent chunk record.
```

```text
Bullet points do not get separate paragraph/chunk IDs, but they can get separate sentence-level IDs in the sentence output.
```

## 16. Chunk Record Creation

The main block-based path creates records with `build_records_from_section_blocks`.

For each Item:

1. Start with an empty section path.
2. Set `item_chunk_index` to 1.
3. Iterate through the Item's blocks in order.
4. Skip table captions.
5. If a block is a subheader, update the section path and do not create a chunk record for that header block.
6. Otherwise, create one chunk record for the full block text.
7. Increment `item_chunk_index`.

The current block-based path does not split long narrative blocks by `max_chars`.

`max_chars` remains relevant for fallback plain-text extraction and for some downstream tools, but the block-based record path preserves the HTML/block-derived paragraph structure.

Each block-based JSON record contains:

```json
{
  "id": "nvda_2024_1A_P001",
  "company": "nvda",
  "year": "2024",
  "item": "1A",
  "item_default_title": "Risk Factors",
  "item_title": "Risk Factors > Some Subheader",
  "section_path": ["Risk Factors", "Some Subheader"],
  "item_chunk_index": 1,
  "source_block_index": 123,
  "text": "...",
  "source": "..."
}
```

The chunk ID format is:

```text
<company>_<year>_<item>_P<item_chunk_index padded to 3 digits>
```

Examples:

```text
nvda_2024_1_P001
nvda_2024_1A_P001
nvda_2024_7_P001
nvda_2024_8_P001
nvda_2024_15_P001
```

Important implication:

```text
Chunk numbering resets within each Item.
```

## 17. Sentence-Level Extraction Output

The extractor now creates sentence-level records during extraction.

These are written to:

```text
<year>_chunk_sentences.json
<year>_chunk_sentences.txt
```

Sentence IDs are derived from the parent chunk ID:

```text
<company>_<year>_<item>_P<chunk index>_S<sentence index>
```

Examples:

```text
nvda_2024_1A_P001_S001
nvda_2024_1A_P001_S002
intc_2025_7_P004_S003
```

Sentence records contain:

```json
{
  "id": "intc_2025_1_P040_S002",
  "chunk_id": "intc_2025_1_P040",
  "company": "intc",
  "year": "2025",
  "item": "1",
  "item_default_title": "Business",
  "item_title": "Our Business > Products > Key Products",
  "section_path": ["Our Business", "Products", "Key Products"],
  "item_chunk_index": 40,
  "sentence_index": 2,
  "bullet_level": 1,
  "bullet_indent_pt": 36.0,
  "source_block_index": 123,
  "text": "...",
  "source": "..."
}
```

`sentence_index` starts at 1 within each parent chunk.

Important implication:

```text
Sentence IDs are stable relative to the current parent chunk. If upstream chunk grouping changes, sentence IDs can still change.
```

## 18. Extraction Sentence Splitting

Extraction sentence splitting is handled by `split_extraction_sentence_units`.

Before punctuation splitting, the extractor:

1. Normalizes typography.
2. Inserts a line break before symbol bullets that appear mid-text.
3. Protects common abbreviations by temporarily replacing periods.

Protected abbreviations include:

```text
Co.
Corp.
Dr.
Inc.
Jr.
Ltd.
Mr.
Mrs.
Ms.
No.
Prof.
Sr.
U.S.
U.K.
e.g.
i.e.
```

The extractor also protects generic uppercase dotted initialisms that have at least two letters, such as:

```text
J.P.
P.C.
U.S.A.
```

This prevents `J.P. Morgan` from being split into `J.P.` and `Morgan ...`.

It splits non-bullet lines using:

```regex
(?<=[.!?])\s+(?=["'(\[]?[A-Z0-9])
```

That means it splits after `.`, `!`, or `?` when the next sentence starts with:

- Optional `"`, `'`, `(`, or `[`.
- Then uppercase A-Z or a digit.

After splitting, abbreviation periods are restored.

If a rendered line break splits one sentence into two lines, the extractor can join the second line back to the previous sentence when:

- the previous sentence does not end with `.`, `!`, or `?`;
- the previous sentence is not a bullet; and
- the new line starts with a lowercase letter.

Example:

```text
... heightened standards guidelines should
be rescinded.
```

becomes:

```text
... heightened standards guidelines should be rescinded.
```

## 19. Bullet Sentence Handling

At extraction time, bullet points can become sentence-level units.

A line is treated as a bullet sentence if it starts with:

```text
•
‣
▪
▫
◦
●
○
* followed by whitespace
- followed by whitespace
```

If a bullet line has no punctuation-based sentence boundary, the whole bullet remains one sentence unit.

If a non-bullet continuation line follows a bullet unit, the continuation is appended to the previous bullet sentence.

This is useful for cases where a bullet is broken across rendered HTML divs.

There is a narrow exception for embedded narrative after a short bullet label. This was added for Citi-style text where a short bullet label is immediately followed by a new explanatory lead-in.

The split is applied when:

- the text starts with a bullet marker;
- the text before the new lead-in is short, currently at most 10 alphanumeric words; and
- the next lead-in starts with uppercase `The`.

For example, if one line looks like:

```text
• Non-Markets net interest income The following are details ...
```

the extractor splits it into:

```text
• Non-Markets net interest income
The following are details ...
```

This prevents a short bullet label from absorbing a new narrative lead-in. In contrast, normal bullet continuations are merged when they continue the previous bullet context, such as a same-segment continuation, a layout-marked continuation block, or a lowercase segment after an unfinished bullet.

For example, Intel has a rendered split like:

```text
▪we and
the DOC entered into an amendment ...
```

Because `the DOC...` starts with lowercase and the bullet has not ended with punctuation, it remains one bullet sentence unit:

```text
▪we and the DOC entered into an amendment ...
```

## 20. Bullet Level Handling

Sentence records can store:

```text
bullet_level
bullet_indent_pt
```

There are two ways to infer bullet level.

First, if a bullet segment has CSS indentation such as:

```text
padding-left:36pt
padding-left:72pt
padding-left:108pt
```

the extractor tracks indentation dynamically:

- First observed indent becomes level 1.
- A larger indent than the latest level increments the level.
- A smaller indent pops back to an earlier level.
- The same indent reuses the existing level.

This is not hard-coded to only `36pt`. The level is based on relative indentation changes.

Second, if CSS indentation is unavailable, leading spaces/tabs before the bullet are used as a fallback:

```text
level = max(1, leading_whitespace_count // 2 + 1)
```

If no bullet is detected:

```text
bullet_level = null
bullet_indent_pt = null
```

Important implication:

```text
Bullet hierarchy is best-effort and depends on how much useful indentation information the HTML provides.
```

## 21. Section Text Output

The TXT section files are built from section blocks after extraction.

For each block:

- If it is a subheader, blank lines are added around it.
- Otherwise, the block text is followed by a blank line.

Possible files include:

```text
<year>_item_1.txt
<year>_item_1a.txt
<year>_item_7.txt
<year>_item_8.txt
<year>_item_15.txt
```

Only Items present in the extraction output get section text files.

Important implication:

```text
If Item 15 is neither explicitly requested nor auto-included, `<year>_item_15.txt` is not written.
```

The section text files are mainly for inspection. The main structured files are JSON.

## 22. Extraction Output Files

The extractor writes:

```text
<year>_chunks.json
<year>_chunks.txt
<year>_chunk_sentences.json
<year>_chunk_sentences.txt
<year>_item_1.txt
<year>_item_1a.txt
<year>_item_7.txt
<year>_item_8.txt
<year>_item_15.txt          # only if Item 15 is extracted
<year>_sources.json
```

`<year>_chunks.txt` is formatted as:

```text
[nvda_2024_1A_P001] Item 1A - Some Subheader
chunk text
```

`<year>_chunk_sentences.txt` is formatted as:

```text
[nvda_2024_1A_P001_S001] Parent nvda_2024_1A_P001 - Item 1A - Some Subheader
sentence text
```

`<year>_chunks.json` is the main structured input to comparison and annotation tools today.

`<year>_chunk_sentences.json` is available for sentence-level workflows, but the current lexical comparison still reads the chunk JSON and performs its own temporary sentence splitting.

## 23. Fallback Plain-Text Path

If block extraction fails to produce section blocks, the extractor falls back to plain-text extraction.

This path uses `FilingTextExtractor`.

It keeps more block-like tags, including:

```text
address
article
aside
blockquote
br
center
dd
div
dl
dt
figcaption
footer
form
h1-h6
header
hr
li
main
nav
ol
p
pre
section
tr
ul
```

It drops:

```text
script
style
noscript
table
svg
head
```

In this fallback path:

- `<li>` becomes `- `.
- Block tags add newlines.
- Tables are removed.
- Typography is normalized.
- Repeated whitespace and repeated blank lines are cleaned.
- Item headings are found with a multiline regex over the cleaned text.
- Sections are selected with the same Item boundary rules.
- Conditional Item 15 auto-inclusion can still run based on extracted Item 7/8 text.
- `cleanup_section_text` removes dropped lines.

Fallback records are created with `build_records`, which uses `split_into_chunks`.

`split_into_chunks`:

1. Splits section text into paragraphs using blank lines.
2. Normalizes each paragraph.
3. Removes empty paragraphs.
4. Merges small paragraphs into a buffer until the buffer reaches `min_chars`.
5. Splits long paragraphs by `max_chars`.

Default `min_chars` is:

```text
120
```

Fallback records do not include `source_block_index`.

Fallback records set:

```text
item_title = item_default_title
section_path = [item_default_title]
```

## 24. Fallback Long Paragraph Splitting

Long paragraph splitting is currently only used by the fallback plain-text path.

Long text is split by:

```regex
(?<=[.!?])\s+(?=[A-Z0-9])
```

That means it splits after `.`, `!`, or `?` when the next sentence starts with uppercase A-Z or a digit.

The splitter then:

1. Adds sentences to a current chunk until adding another sentence would exceed `max_chars`.
2. Starts a new chunk when needed.
3. If a resulting chunk is still longer than `max_chars`, uses `textwrap.wrap`.
4. `textwrap.wrap` uses:

```text
break_long_words=False
break_on_hyphens=False
```

Important limitation:

```text
This fallback splitter does not protect abbreviations such as U.S. or Inc.
```

## 25. Lexical Comparison Input

Lexical comparison reads two extraction chunk JSON files:

```bash
python3 scripts/compare_item_changes.py data/raw/nvda/2023/2023_chunks.json data/raw/nvda/2024/2024_chunks.json
```

It currently reads:

```text
<year>_chunks.json
```

It does not currently use:

```text
<year>_chunk_sentences.json
```

This means sentence-level extraction output exists, but lexical comparison still performs its own temporary sentence splitting from each chunk's `text`.

## 26. Comparison Grouping

The comparison step groups records by:

```text
item
item_title
```

More precisely, it normalizes:

- `item` by removing a leading `item`, spaces, underscores, or hyphens, then uppercasing.
- `item_title` by normalizing typography, collapsing whitespace, and stripping.

The output order follows the sequence in the extracted data, not alphabetical order.

Title pairing cases:

- `exact`: same Item and same normalized title exists in both years.
- `manual`: user supplied a `--title-map`.
- `old_title_only`: title exists only in old year.
- `new_title_only`: title exists only in new year.

Manual title maps use:

```bash
--title-map "ITEM::OLD_TITLE::NEW_TITLE"
```

The code validates that:

- The old title exists in the old records.
- The new title exists in the new records.
- The target new title is not already matched by another manual mapping.

Important implication:

```text
There is currently no fuzzy title matching by default.
```

## 27. Comparison Sentence Splitting

Comparison sentence splitting happens in `split_sentences`.

Before splitting, comparison text normalization:

- Normalizes typography:

```text
non-breaking space -> normal space
curly apostrophes  -> '
curly quotes       -> "
en dash            -> -
em dash            -> -
```

- Collapses all whitespace to single spaces.
- Strips leading/trailing whitespace.

The comparison splitter protects these abbreviations from period splitting:

```text
Co.
Corp.
Dr.
Inc.
Jr.
Ltd.
Mr.
Mrs.
Ms.
No.
Prof.
Sr.
U.S.
U.K.
e.g.
i.e.
```

It also protects generic uppercase dotted initialisms that have at least two letters, such as:

```text
J.P.
P.C.
U.S.A.
```

It temporarily replaces periods in those abbreviations with a placeholder.

Then it splits using:

```regex
(?<=[.!?])\s+(?=["'(\[]?[A-Z0-9])
```

Before comparison splitting, `•` bullets are converted into separate lines:

```regex
\s*•\s* -> \n• 
```

Important current difference:

```text
Extraction sentence output handles more bullet metadata than lexical comparison does.
```

Comparison currently gives special line handling to `•` bullets, but not all bullet forms such as `- ` or `▪`.

## 28. Removing Unchanged Sentences

Within each paired `item` and `item_title` group:

1. Old-year chunks become old sentence occurrences.
2. New-year chunks become new sentence occurrences.
3. Each occurrence has a normalized sentence string.
4. The code counts normalized sentence occurrences using `Counter`.
5. The intersection of old and new counters is treated as unchanged.
6. Unchanged sentences are removed from both years.
7. Duplicates are counted separately.

Example:

```text
If a sentence appears twice in 2023 and once in 2024:
one occurrence is removed as unchanged,
one 2023 occurrence remains as 2023-only.
```

The current method is exact normalized sentence matching only.

It does not do fuzzy matching or semantic matching.

For exact comparison, each sentence is normalized by:

- Applying typography normalization.
- Collapsing whitespace.
- Stripping.
- Applying `.casefold()`.

Exact comparison ignores:

- Case differences.
- Repeated whitespace.
- Curly quote vs straight quote differences.
- Curly apostrophe vs straight apostrophe differences.
- En dash vs hyphen differences.
- Em dash vs hyphen differences.
- Non-breaking spaces.

It does not ignore:

- Added or removed commas.
- Added or removed apostrophes unless they are curly vs straight variants.
- Added or removed words.
- Reordered words.
- Semantic equivalence.
- Similar but not exact rewrites.

## 29. Comparison Output

Comparison writes files under:

```text
data/comparison/<company>/<old_year>_vs_<new_year>/
```

It writes:

```text
all_items_diff.json
all_items_diff.txt
item_1_diff.json
item_1_diff.txt
item_1a_diff.json
item_1a_diff.txt
item_7_diff.json
item_7_diff.txt
item_8_diff.json
item_8_diff.txt
item_15_diff.json        # only if Item 15 is present in comparison output
item_15_diff.txt         # only if Item 15 is present in comparison output
```

Item files are only written for Items present in the comparison output.

The text report displays:

```text
Item <item> - <item_default_title>
Totals: <old_year>-only ..., <new_year>-only ..., unchanged removed ...

## <item_title>
Only in <old_year>
[<source_id>] sentence

Only in <new_year>
[<source_id>] sentence
```

The JSON output keeps sentence occurrence metadata, including:

```text
source_id
source_block_index
item_chunk_index
sentence_index
sentence
```

The internal `normalized` field is removed before output.

## 30. Current ID Semantics

Current extraction has two ID levels.

Chunk ID:

```text
<company>_<year>_<item>_P<chunk index>
```

Example:

```text
nvda_2024_1A_P001
```

Sentence ID:

```text
<company>_<year>_<item>_P<chunk index>_S<sentence index>
```

Example:

```text
nvda_2024_1A_P001_S001
```

The chunk ID means:

```text
Company nvda
Fiscal year 2024
Item 1A
Chunk/paragraph-like unit 001 within Item 1A
```

The sentence ID means:

```text
Sentence-like unit 001 within parent chunk nvda_2024_1A_P001
```

The IDs do not currently mean:

```text
paragraph on page 001
paragraph within source page
HTML div number
original SEC page coordinate
```

## 31. Current Gap for Future Database and UI Work

The extraction output is now suitable for both paragraph/chunk storage and sentence-level storage.

For a future database, useful entities would likely be:

```text
filings
sections/items
chunks
sentences
comparison_runs
sentence_diffs
llm_summaries
annotations
```

The current lexical comparison still uses chunk JSON as its input and creates sentence occurrences temporarily.

If the UI needs clickable sentence-level before/after views, the next likely improvement is:

```text
Use `<year>_chunk_sentences.json` as the comparison input, or store both chunks and sentences in PostgreSQL before comparison.
```

## 32. What This Document Does Not Change

This document does not change code or outputs.

It records the current behavior after recent updates:

- Item 15 is not default.
- Item 15 can be auto-included based on Item 7/8 pointer language.
- Chunk IDs include company ticker.
- Sentence-level extraction outputs exist.
- Bullet sentence records can include bullet level and indentation.
- Block-based extraction preserves long block/chunk structure instead of splitting by `max_chars`.
- Lexical comparison still performs its own sentence splitting from chunk JSON.
