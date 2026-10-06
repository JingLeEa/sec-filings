"""Build HTML and Markdown reports for a saved taxonomy evaluation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import re
from typing import Any


STATUS_REASONS = {
    "pair_mismatch": "Located source sentences do not form the annotated pair in a supported alignment.",
    "missing_prediction": "No benchmark source sentence was located in the saved citations for this available comparison.",
    "unmapped_evidence": "Only part of the required source evidence was located; selected citations cannot establish the correspondence.",
    "ambiguous_sentence": "Repeated source text could not be resolved to a unique sentence occurrence.",
    "ambiguous_prediction": "More than one alignment group represents the annotated case.",
    "needs_review": "The uniquely mapped alignment has not been finalized.",
    "missing_taxonomy": "The mapped alignment has no final_taxonomy value.",
    "invalid_taxonomy": "The final_taxonomy value is not a supported single label.",
    "mixed_gold_group": "Uniquely mapped annotation rows in the same group have different gold taxonomies.",
}


def summarize_alignment_inventory(predictions: dict, rows: list[dict], finalized_statuses: set[str]) -> list[dict]:
    """Count disclosure links separately from benchmark sentence cases.

    Cartesian expansion counts group membership combinations. Original links,
    when retained in a consolidated group, determine the supported pair count.
    Review groups are recorded separately and do not enter the finalized counts.
    """
    periods: dict[tuple, list] = defaultdict(list)
    for prediction in predictions.values():
        periods[prediction.key[:3]].append(prediction)
    scored_keys = {
        (row["company"], row["comparison_previous_year"], row["comparison_current_year"], row["match_id"])
        for row in rows if row["status"] == "scored"
    }
    inventory = []
    for period, members in sorted(periods.items()):
        finalized = [prediction for prediction in members if prediction.row.get("status") in finalized_statuses]
        expanded, supported, one_sided = set(), set(), set()
        pair_entries = one_sided_entries = 0
        relationships: dict[str, Counter] = defaultdict(Counter)
        for prediction in finalized:
            row = prediction.row
            before, after = set(row["previous_ids"]), set(row["current_ids"])
            combinations = {(previous, current) for previous in before for current in after}
            expanded.update(combinations)
            pair_entries += len(combinations)
            sources = (row.get("grouping") or {}).get("source_alignments", [])
            if sources:
                for source in sources:
                    supported.update((previous, current)
                                     for previous in before & set(source.get("previous_ids", []))
                                     for current in after & set(source.get("current_ids", [])))
            else:
                supported.update(combinations)
            singles = {(previous, None) for previous in before} if not after else set()
            if not before:
                singles.update((None, current) for current in after)
            one_sided.update(singles)
            one_sided_entries += len(singles)
            relationship = relationships[str(row.get("relationship", "unspecified"))]
            relationship.update(groups=1, pair_entries=len(combinations), one_sided_entries=len(singles))
        inventory.append(dict(
            company=period[0], previous_year=period[1], current_year=period[2],
            files=sorted({str(prediction.path) for prediction in members}),
            finalized_groups=len(finalized), review_groups=len(members) - len(finalized),
            pair_entries=pair_entries, unique_expanded_pairs=len(expanded),
            duplicate_pair_entries=pair_entries - len(expanded), unique_supported_pairs=len(supported),
            unsupported_expanded_pairs=len(expanded - supported),
            one_sided_entries=one_sided_entries, unique_one_sided_cases=len(one_sided),
            scored_alignment_groups=sum(prediction.key in scored_keys for prediction in finalized),
            scored_benchmark_rows=sum(row["status"] == "scored" and (
                row["company"], row["comparison_previous_year"], row["comparison_current_year"]
            ) == period for row in rows),
            relationships=[dict(relationship=name, **counts) for name, counts in sorted(relationships.items())],
        ))
    return inventory


def _count(value: int) -> str:
    return f"{value:,}"


def _percent(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}%}"


def _score(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _coverage_formula(scored: int, total: int, value: float | None) -> str:
    if not total:
        return "Unavailable: 0 annotation rows in this scope"
    return f"{_count(scored)} ÷ {_count(total)} × 100 = {_percent(value)}"


def _table(headers: list[str], rows: list[list[Any]], caption: str) -> str:
    heading = "".join(f'<th scope="col">{escape(header)}</th>' for header in headers)
    body = "".join("<tr>" + "".join(
        f'<th scope="row">{escape(str(value))}</th>' if index == 0 else f"<td>{escape(str(value))}</td>"
        for index, value in enumerate(row)
    ) + "</tr>" for row in rows)
    return (f'<div class="table-wrap"><table><caption>{escape(caption)}</caption>'
            f"<thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>")


def render_report(summary: dict[str, Any]) -> str:
    """Render current summary values without hard-coded company or run counts."""
    overall, available = summary["overall"], summary["available_comparisons"]
    labels = summary["labels"]
    inventory = summary["alignment_inventory"]
    companies = sorted({entry["company"].upper() for entry in inventory})
    company = ", ".join(companies)
    unavailable = overall["benchmark_rows"] - available["benchmark_rows"]
    matched = summary["coverage_accounting"]["uniquely_mapped_rows"]
    scored_groups = sum(entry["scored_alignment_groups"] for entry in inventory)
    scored_two_sided = summary["coverage_accounting"]["scored_two_sided_rows"]
    scored_one_sided = summary["coverage_accounting"]["scored_one_sided_rows"]
    title = f"{company} change taxonomy evaluation"
    body = [f"""<header>
      <div class="eyebrow">SEC disclosure benchmark · Evaluation report</div>
      <h1>{escape(title)}</h1>
      <p class="subtitle">Sentence annotation coverage and conditional taxonomy scores</p>
      <div class="tags"><span>Target: final_taxonomy</span><span>Unit: annotation CSV row</span>
      <span>Items: {escape(', '.join(summary['by_item']))}</span></div>
    </header>
    <section aria-labelledby="results"><h2 id="results">Results at a glance</h2>
      <div class="cards">
        <article><div class="card-label">Coverage of available comparisons</div>
          <div class="card-value">{_percent(available['coverage'])}</div>
          <p>{_count(available['scored_rows'])} scored / {_count(available['benchmark_rows'])} annotation rows</p></article>
        <article><div class="card-label">Conditional taxonomy accuracy</div>
          <div class="card-value">{_percent(overall['accuracy'], 2)}</div>
          <p>{_count(overall['correct_rows'])} correct / {_count(overall['scored_rows'])} scored rows</p></article>
        <article><div class="card-label">Macro-F1 · six classes</div>
          <div class="card-value">{_score(overall['macro_f1'])}</div>
          <p>Weighted-F1: {_score(overall['weighted_f1'])}</p></article>
      </div>
      <p>This run scored <strong>{_count(overall['scored_rows'])} benchmark annotation rows</strong>,
      supplied by <strong>{_count(scored_groups)} finalized alignment groups</strong>.
      Full benchmark coverage is <strong>{_percent(overall['coverage'])}</strong>
      ({_count(overall['scored_rows'])} / {_count(overall['benchmark_rows'])});
      {_count(unavailable)} annotation rows have no matching saved filing comparison.</p>
      <div class="notice"><strong>Interpretation.</strong> Scores describe the eligible subset.
      Saved evidence contains selected sentence citations, and groups with mixed mapped gold labels
      are excluded. These scores do not establish performance across the entire benchmark.</div>
    </section>"""]

    body.append('<section aria-labelledby="coverage"><h2 id="coverage">Where the coverage numbers come from</h2>')
    body.append("<p>The denominator comes from the <strong>annotation CSV</strong>. Each selected CSV row counts once, "
                "including one-sided New and Removed cases. Alignment groups and disclosure links use separate units.</p>")
    body.append(_table(["Count", "Rows", "Definition"], [
        ["All selected benchmark rows", _count(overall["benchmark_rows"]), "CSV rows for the selected company and Items."],
        ["Benchmark rows with available comparisons", _count(available["benchmark_rows"]), "CSV rows whose SEC filing pair resolves to a saved comparison."],
        ["Rows uniquely mapped to an alignment", _count(matched), "CSV source occurrences map to exactly one supported alignment group, before taxonomy eligibility checks."],
        ["Rows eligible for taxonomy scoring", _count(overall["scored_rows"]), "Unique supported correspondence, finalized single prediction, and uniform mapped gold label."],
        ["Skipped within available comparisons", _count(available["excluded_rows"]), "Available-comparison CSV rows that fail mapping or taxonomy eligibility checks."],
        ["Benchmark rows without a saved comparison", _count(unavailable), "CSV rows whose actual filing comparison is unavailable."],
    ], "Coverage accounting in benchmark annotation rows"))
    body.append(f"""<div class="formula-grid">
      <div><strong>Coverage of available comparisons</strong><code>{_coverage_formula(overall['scored_rows'], available['benchmark_rows'], available['coverage'])}</code></div>
      <div><strong>Full benchmark coverage</strong><code>{_coverage_formula(overall['scored_rows'], overall['benchmark_rows'], overall['coverage'])}</code></div>
    </div>
    <p>The {_count(overall['scored_rows'])} scored rows include {_count(scored_two_sided)} two-sided sentence cases
    and {_count(scored_one_sided)} one-sided cases. Multiple sentence annotations can map to the same disclosure link.</p>
    <details><summary>Example: one disclosure link can cover three annotation rows</summary>
    <pre>Saved alignment: Disclosure D1 → Disclosure D2     1 disclosure link

Annotation row 1: Sentence A1 → Sentence B1
Annotation row 2: Sentence A2 → Sentence B2
Annotation row 3: Sentence A3 → Sentence B3           3 annotation rows</pre>
    <p>Each annotation row is scored once if it passes the eligibility rules.</p></details></section>""")

    body.append('<section aria-labelledby="filings"><h2 id="filings">Benchmark and filing comparison scope</h2>')
    body.append("<p>SEC accession IDs identify the actual filings. They take precedence over the CSV year columns, "
                "which can differ from fiscal-year result folders. Original annotation years remain unchanged.</p>")
    comparison_rows = []
    for entry in summary["benchmark_comparisons"]:
        comparison = (f"{entry['comparison_previous_year']}–{entry['comparison_current_year']}"
                      if entry["comparison_previous_year"] else "No matching saved comparison")
        comparison_rows.append([
            f"{entry['previous_year']}–{entry['current_year']}", _count(entry["benchmark_rows"]), comparison,
            entry["period_matching"].replace("_", " "),
        ])
    body.append(_table(["Year columns in annotation CSV", "CSV rows", "Resolved fiscal-year result", "Matching method"],
                       comparison_rows, "Annotation year columns and resolved result folders"))
    result_rows = []
    for period, metrics in summary["by_result_period"].items():
        result_rows.append([period, _count(metrics["benchmark_rows"]), _count(metrics["scored_rows"]),
                            _percent(metrics["coverage"]), _percent(metrics["accuracy"], 2), _score(metrics["macro_f1"])])
    body.append(_table(["Result fiscal years", "CSV rows for these filings", "Scored CSV rows", "Coverage", "Accuracy", "Macro-F1"],
                       result_rows, "Evaluation by fiscal-year result comparison"))
    body.append('<p class="small">A dash means no defined score or denominator. A saved result with zero corresponding '
                'benchmark rows receives no taxonomy score.</p></section>')

    body.append('<section aria-labelledby="inventory"><h2 id="inventory">Alignment result inventory</h2>')
    body.append("<p>These counts come from finalized alignment records, using disclosure IDs. "
                "They describe the saved output structure and are independent of the CSV-row coverage denominator.</p>")
    inventory_rows = [[
        f"{entry['previous_year']}–{entry['current_year']}", _count(entry["finalized_groups"]),
        _count(entry["pair_entries"]), _count(entry["unique_supported_pairs"]),
        _count(entry["unique_one_sided_cases"]), _count(entry["scored_alignment_groups"]),
    ] for entry in inventory]
    inventory_rows.append(["Total", *[_count(sum(entry[field] for entry in inventory)) for field in (
        "finalized_groups", "pair_entries", "unique_supported_pairs", "unique_one_sided_cases", "scored_alignment_groups",
    )]])
    body.append(_table(["Result fiscal years", "Alignment groups", "Expanded pair entries", "Unique supported pairs", "One-sided cases", "Groups supplying scored rows"],
                       inventory_rows, "Disclosure groups and pair counts from the alignment results"))
    body.append("<p><strong>Calculation:</strong> for each two-sided group, expand "
                "<code>len(previous_ids) × len(current_ids)</code>. A 1×3 group produces three pair entries; "
                "a 2×3 group produces six. Deduplicate identical previous/current disclosure-ID pairs within each "
                "filing comparison, and retain original-link support when a consolidated group provides it. "
                "One-sided New/Removed cases are counted separately.</p>")
    for entry in inventory:
        notes = []
        if entry["duplicate_pair_entries"]:
            duplicate_count = entry["duplicate_pair_entries"]
            noun = "entry was" if duplicate_count == 1 else "entries were"
            notes.append(f"{_count(duplicate_count)} duplicate pair {noun} removed")
        if entry["unsupported_expanded_pairs"]:
            notes.append(f"{_count(entry['unsupported_expanded_pairs'])} expanded combinations lacked original-link support")
        if entry["review_groups"]:
            notes.append(f"{_count(entry['review_groups'])} review groups are excluded from finalized inventory counts")
        if notes:
            body.append(f"<p class=\"small\">{escape(entry['previous_year'])}–{escape(entry['current_year'])}: "
                        + escape("; ".join(notes)) + ".</p>")
    relationship_rows = [[f"{entry['previous_year']}–{entry['current_year']}", counts["relationship"],
                          _count(counts["groups"]), _count(counts["pair_entries"]), _count(counts["one_sided_entries"])]
                         for entry in inventory for counts in entry["relationships"]]
    body.append('<details><summary>Relationship breakdown: one-to-one, one-to-many, many-to-one and many-to-many</summary>')
    body.append(_table(["Result", "Relationship", "Groups", "Pair entries", "One-sided entries"], relationship_rows,
                       "Finalized inventory by alignment relationship"))
    body.append('</details></section>')

    body.append('<section aria-labelledby="classes"><h2 id="classes">Taxonomy performance by class</h2>')
    body.append("<p>Precision, recall and F1 use only scored annotation rows. Class coverage below uses "
                "the annotation rows belonging to available comparisons. Support is the number of scored gold rows.</p>")
    class_rows = []
    for label in labels:
        values = available["per_class"][label]
        class_rows.append([label, _count(values["benchmark_count"]), _count(values["support"]),
                           _percent(values["coverage"]), _score(values["precision"]), _score(values["recall"]), _score(values["f1"])])
    body.append(_table(["Gold taxonomy", "Available CSV rows", "Scored support", "Class coverage", "Precision", "Recall", "F1"],
                       class_rows, "Class metrics and coverage for available filing comparisons"))
    sparse = [(label, available["per_class"][label]["support"]) for label in labels
              if available["per_class"][label]["support"] < 5]
    if sparse:
        description = "; ".join(f"{label}: {support}" for label, support in sparse)
        body.append(f'<div class="notice"><strong>Small class samples.</strong> Scored support below five rows: '
                    f'{escape(description)}. These class scores provide limited evidence of general performance.</div>')
    body.append("<p class=\"small\">Macro-F1 is the unweighted mean of F1 over all six classes; unsupported classes "
                "contribute zero when any rows are scored. Weighted-F1 weights by scored gold support. "
                "Scores are unavailable when there are no scored rows.</p></section>")

    body.append('<section aria-labelledby="confusion"><h2 id="confusion">Confusion matrix</h2>')
    body.append("<p>Rows are gold labels; columns are predictions from <code>change_analysis.final_taxonomy</code>. "
                "Each scored annotation row contributes one count.</p>")
    matrix = overall["confusion_matrix"]
    confusion_rows = [[label, *[_count(matrix[label][predicted]) for predicted in labels],
                       _count(sum(matrix[label].values()))] for label in labels]
    confusion_rows.append(["Total predicted", *[_count(sum(matrix[label][predicted] for label in labels))
                                               for predicted in labels], _count(overall["scored_rows"])])
    body.append(_table(["Gold / Predicted", *labels, "Gold support"], confusion_rows, "Confusion matrix on scored benchmark rows"))
    errors = sorted(((matrix[gold][predicted], gold, predicted) for gold in labels for predicted in labels
                     if gold != predicted and matrix[gold][predicted]), reverse=True)
    if errors:
        description = "; ".join(f"{gold} → {predicted}: {count}" for count, gold, predicted in errors[:3])
        body.append(f"<p><strong>Most frequent errors:</strong> {escape(description)}. "
                    f"There are {_count(overall['scored_rows'] - overall['correct_rows'])} incorrect scored rows.</p>")
    body.append('</section>')

    body.append('<section aria-labelledby="exclusions"><h2 id="exclusions">Why annotation rows were skipped</h2>')
    body.append("<p>Exclusions affect coverage. They do not enter taxonomy accuracy, precision, recall or F1.</p>")
    exclusion_rows = []
    if unavailable:
        exclusion_rows.append(["Unavailable filing comparison", _count(unavailable), "0",
                               "The benchmark's actual SEC filing pair has no matching saved result."])
    for status, reason in STATUS_REASONS.items():
        amount = available["status_counts"][status]
        if amount:
            exclusion_rows.append([status, _count(amount), _count(amount), reason])
    exclusion_rows.append(["Total skipped", _count(overall["excluded_rows"]), _count(available["excluded_rows"]), ""])
    body.append(_table(["Reason", "All benchmark rows", "Within available comparisons", "Explanation"], exclusion_rows,
                       "Exclusion counts with unavailable comparisons separated from mapping failures"))
    body.append('<p class="small">An absent selected citation does not prove an alignment error. '
                'The row audit distinguishes a proven pair mismatch from unverifiable correspondence.</p></section>')

    item_rows = []
    for item, metrics in summary["available_by_item"].items():
        item_rows.append([item, _count(summary["by_item"][item]["benchmark_rows"]), _count(metrics["benchmark_rows"]),
                          _count(metrics["scored_rows"]), _percent(metrics["coverage"]),
                          _percent(metrics["accuracy"], 2), _score(metrics["macro_f1"])])
    body.append('<section aria-labelledby="items"><h2 id="items">Results by SEC Item</h2>')
    body.append(_table(["Item", "All CSV rows", "Available CSV rows", "Scored rows", "Available coverage", "Accuracy", "Macro-F1"],
                       item_rows, "Item-level scores with available-comparison coverage"))
    body.append('</section>')

    body.append('<section aria-labelledby="method"><h2 id="method">Method and limitations</h2><ol>')
    body.append("<li>Resolve the actual previous/current filing pair using SEC accession IDs in the supplied URLs. "
                "Use year columns only when filing identities are unavailable; known conflicting filings are excluded.</li>"
                "<li>Locate source sentences within the resolved comparison, SEC Item and side. Compatible IDs require "
                "matching text; renumbered IDs can use a unique formatting-normalized exact text match.</li>"
                "<li>Require a unique common alignment with supported source links. For New/Removed annotations, "
                "require a corresponding one-sided alignment.</li>"
                "<li>Read only change_analysis.final_taxonomy from a finalized alignment. A group's label is "
                "projected onto uniquely mapped benchmark rows only when those rows share one gold taxonomy.</li>"
                "<li>Compare that label with each eligible CSV row's Change Taxonomy. Count every eligible row once.</li></ol>"
                "<p>Formatting normalization preserves words, case, amounts and dates; no fuzzy or substring matching "
                "selects predictions. Unchanged maps to Reworded under the supplied benchmark convention. "
                "Lexical, semantic, LLM, content taxonomy and materiality values do not supply taxonomy predictions.</p>"
                "<div class=\"notice\"><strong>Limits of this evaluation.</strong> Group predictions and sentence annotations "
                "have different granularity. Excluding mixed gold groups selects a restricted subset. "
                "A group with uniform mapped labels may still contain uncited or unannotated sentences with other labels. "
                "Several scored sentence rows can reuse the same group prediction, so scored rows are not independent "
                "group-level samples. Broader evaluation requires gold and predicted labels at a common granularity.</div></section>")

    body.append('<section aria-labelledby="audit"><h2 id="audit">Inputs and audit files</h2>')
    body.append('<p>This report uses the same run as the machine-readable outputs below. Input SHA-256 hashes identify '
                'the evaluated files. The evaluator reads input files without changing them.</p>')
    body.append(_table(["Input file", "SHA-256"], [[entry["path"], entry["sha256"]] for entry in summary["inputs"]],
                       "Evaluated input files and hashes"))
    body.append('<div class="downloads"><a href="summary.json">Full JSON summary</a>'
                '<a href="row_results.csv">Annotation row audit CSV</a>'
                '<a href="per_class_metrics.csv">Class metrics CSV</a>'
                '<a href="confusion_matrix.csv">Confusion matrix CSV</a></div>')
    filing_rows = [[f"{entry['previous_year']}–{entry['current_year']}", entry["previous"] or "Unavailable",
                    entry["current"] or "Unavailable"] for entry in summary["source_comparisons"]]
    body.append('<details><summary>SEC filing accession IDs used for comparison matching</summary>')
    body.append(_table(["Result fiscal years", "Previous accession", "Current accession"], filing_rows,
                       "SEC accession IDs recovered from result citations"))
    body.append('</details></section><footer>Evaluation target: change taxonomy · '
                'Primary unit: benchmark annotation row · Coverage and taxonomy scores use separate denominators</footer>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>
:root {{ color-scheme: light; --ink:#183046; --muted:#586d7f; --line:#d9e3ea; --accent:#126c70; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:#edf2f5; font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:36px auto; padding:0 32px 36px; background:#fff; border:1px solid var(--line); border-radius:12px; }}
header {{ padding:38px 0 30px; border-bottom:3px solid var(--accent); }}
.eyebrow {{ color:var(--accent); font-size:12px; text-transform:uppercase; letter-spacing:1.5px; font-weight:700; }}
h1 {{ font-size:34px; line-height:1.2; letter-spacing:-.6px; margin:12px 0; }}
.subtitle {{ color:var(--muted); font-size:17px; margin:0 0 18px; }}
.tags,.downloads {{ display:flex; flex-wrap:wrap; gap:8px; }}
.tags span {{ padding:4px 10px; border-radius:5px; background:#eaf3f3; color:#255458; font-size:12px; }}
section {{ padding:27px 0 4px; }}
h2 {{ font-size:22px; line-height:1.3; margin:0 0 15px; letter-spacing:-.2px; }}
p {{ margin:12px 0; }}
.cards {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
.cards article {{ padding:20px; border:1px solid #cddfe0; background:#f5faf9; border-radius:8px; }}
.card-label {{ color:#385e63; font-size:12px; font-weight:650; line-height:1.4; }}
.card-value {{ font-size:39px; font-weight:700; line-height:1.25; margin:10px 0; letter-spacing:-1px; }}
.cards p {{ font-size:12px; color:var(--muted); margin:0; }}
.notice {{ background:#fff8e8; border-left:3px solid #c79832; padding:13px 17px; margin:18px 0 8px; font-size:14px; }}
.table-wrap {{ overflow-x:auto; margin:17px 0; border:1px solid var(--line); border-radius:6px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; line-height:1.45; }}
caption {{ text-align:left; padding:11px 13px; background:#f5f8fa; color:var(--muted); font-size:12px; }}
th,td {{ text-align:left; padding:11px 13px; vertical-align:top; border-top:1px solid var(--line); overflow-wrap:anywhere; }}
thead th {{ background:#eaf0f4; font-size:12px; font-weight:700; }}
tbody th {{ font-weight:600; }}
tbody tr:nth-child(even) {{ background:#f9fbfc; }}
.formula-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:20px 0; }}
.formula-grid>div {{ border:1px solid var(--line); border-radius:6px; padding:15px; }}
.formula-grid strong {{ display:block; font-size:13px; margin-bottom:9px; }}
code,pre {{ font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace; background:#f0f4f7; border-radius:3px; }}
code {{ padding:3px 5px; overflow-wrap:anywhere; }}
.formula-grid code {{ display:block; padding:9px; font-size:14px; }}
pre {{ padding:16px; overflow-x:auto; }}
.small {{ font-size:12px; color:var(--muted); }}
details {{ margin:16px 0; border:1px solid var(--line); border-radius:6px; padding:12px 15px; }}
summary {{ cursor:pointer; font-size:13px; font-weight:600; }}
ol {{ padding-left:22px; }} li {{ margin:9px 0; }}
a {{ color:var(--accent); text-underline-offset:3px; }}
.downloads a {{ font-size:13px; padding:7px 11px; border:1px solid #b8d2d3; border-radius:5px; text-decoration:none; }}
footer {{ border-top:1px solid var(--line); margin-top:28px; padding-top:17px; font-size:11px; color:var(--muted); }}
@media(max-width:750px) {{ main {{ margin:0; padding:0 18px 24px; border-radius:0; }} h1 {{ font-size:27px; }}
  .cards,.formula-grid {{ grid-template-columns:1fr; }} .card-value {{ font-size:34px; }} th,td {{ padding:9px; }} }}
@media print {{ body {{ background:#fff; font-size:10pt; }} main {{ max-width:none; border:0; margin:0; padding:0; }}
  header {{ padding:0 0 18px; }} h1 {{ font-size:24pt; }} h2 {{ font-size:15pt; break-after:avoid; }}
  section {{ padding-top:20px; }} .cards,.formula-grid,.notice,tr {{ break-inside:avoid; }}
  .table-wrap {{ overflow:visible; }} table {{ font-size:8pt; }} th,td {{ padding:6px; }}
  thead {{ display:table-header-group; }} details {{ display:none; }} a {{ color:inherit; }}
  .cards {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} .formula-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
  .card-value {{ font-size:27pt; }} @page {{ size:A4; margin:14mm; }} }}
</style></head><body><main>{''.join(body)}</main></body></html>
"""


@dataclass
class _ReportElement:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_ReportElement | str] = field(default_factory=list)


class _ReportParser(HTMLParser):
    """Read the generated report so both output formats share the same content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _ReportElement("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _ReportElement(tag, dict(attrs))
        self.stack[-1].children.append(element)
        if tag not in {"meta", "link", "br", "hr", "img", "input", "source", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        if len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise ValueError(f"Report contains an unexpected closing tag: {tag}")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _elements(element: _ReportElement, tag: str) -> list[_ReportElement]:
    return [match for child in element.children if isinstance(child, _ReportElement)
            for match in ([child] if child.tag == tag else _elements(child, tag))]


def _plain_text(element: _ReportElement) -> str:
    return "".join(child if isinstance(child, str) else _plain_text(child) for child in element.children)


def _markdown_text(value: str) -> str:
    # Preserve literal input paths and text instead of interpreting them as markup.
    value = escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]#!|])", r"\\\1", value)


def _markdown_inline(element: _ReportElement | str) -> str:
    if isinstance(element, str):
        return _markdown_text(element)
    if element.tag == "code":
        text = _plain_text(element)
        fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
        padding = " " if text.startswith("`") or text.endswith("`") else ""
        text = text.replace("|", "\\|")
        return f"{fence}{padding}{text}{padding}{fence}"
    content = "".join(_markdown_inline(child) for child in element.children)
    if element.tag == "strong":
        return f"**{content}**"
    if element.tag == "em":
        return f"*{content}*"
    if element.tag == "a":
        return f"[{content}]({element.attrs['href']})"
    return content


def _inline_line(element: _ReportElement | str) -> str:
    return re.sub(r"\s+", " ", _markdown_inline(element)).strip()


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ])


def _markdown_block(element: _ReportElement | str) -> str:
    if isinstance(element, str):
        return _inline_line(element)
    tag = element.tag
    if re.fullmatch(r"h[1-6]", tag):
        return f"{'#' * int(tag[1])} {_inline_line(element)}"
    if tag in {"p", "strong", "em", "code", "a", "span"}:
        return _inline_line(element)
    if tag == "pre":
        text = _plain_text(element).strip("\n")
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
        return f"{fence}text\n{text}\n{fence}"
    if tag == "table":
        rows = [[_inline_line(cell) for cell in row.children
                 if isinstance(cell, _ReportElement) and cell.tag in {"th", "td"}]
                for row in _elements(element, "tr")]
        return _markdown_table(rows[0], rows[1:])
    if tag == "ol":
        return "\n".join(f"{index}. {_inline_line(child)}" for index, child in enumerate(
            [child for child in element.children if isinstance(child, _ReportElement) and child.tag == "li"], 1))
    if tag == "summary":
        return f"**{_inline_line(element)}**"
    if tag == "footer":
        return f"---\n\n{_inline_line(element)}"
    classes = set((element.attrs.get("class") or "").split())
    if "cards" in classes:
        rows = []
        for article in _elements(element, "article"):
            values = {child.attrs.get("class"): _inline_line(child) for child in article.children
                      if isinstance(child, _ReportElement) and child.tag == "div"}
            basis = " ".join(_inline_line(child) for child in _elements(article, "p"))
            rows.append([values["card-label"], values["card-value"], basis])
        return _markdown_table(["Metric", "Value", "Basis"], rows)
    if "notice" in classes:
        return f"> {_inline_line(element)}"
    if "tags" in classes:
        return " · ".join(_inline_line(child) for child in _elements(element, "span"))
    if "downloads" in classes:
        return "\n".join(f"- {_inline_line(child)}" for child in _elements(element, "a"))
    children = element.children
    if tag == "header":
        children = sorted(children, key=lambda child: not (isinstance(child, _ReportElement) and child.tag == "h1"))
    return "\n\n".join(block for child in children if (block := _markdown_block(child)))


def render_markdown_report(summary: dict[str, Any]) -> str:
    """Generate Markdown with all report content, including expanded details."""
    parser = _ReportParser()
    parser.feed(render_report(summary))
    parser.close()
    if len(parser.stack) != 1:
        raise ValueError("Report contains unclosed HTML elements")
    main = _elements(parser.root, "main")
    if len(main) != 1:
        raise ValueError("Report must contain exactly one main element")
    return _markdown_block(main[0]).rstrip() + "\n"
