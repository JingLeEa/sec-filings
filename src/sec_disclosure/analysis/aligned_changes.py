"""Add lexical and embedding analysis to finalized, LLM-classified alignments."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from sec_disclosure.agents.alignment_data import AlignmentData
from sec_disclosure.agents.change_analysis import CHANGE_TAXONOMY, effective_change_analysis
from sec_disclosure.review_overrides import manual_change_label
from sec_disclosure.indexing.semantic_classification import classify_alignment_row
from sec_disclosure.indexing.semantic_retrieval import SemanticIndex
from sec_disclosure.llm.client import LLMError
from sec_disclosure.llm.config import load_config
from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.llm.embeddings import make_embedder

from .lexical_analysis import compute_lexical_metrics


def enrich_alignment(output: Path, data: AlignmentData, *, embedder, document=None) -> dict:
    path = output / "alignments.json"
    original = path.read_bytes() if document is None else None
    document = json.loads(original) if document is None else deepcopy(document)
    identity = (document.get("company"), document.get("previous_year"), document.get("current_year"))
    if identity != (data.ticker, *data.years) or not document.get("run_complete") or document.get("error"):
        raise ValueError("Finish alignment for the requested company and years before change analysis.")
    rows = document["alignments"]
    for row in rows:
        if row["status"] not in {"auto_matched", "ai_verified", "unmatched"} and not manual_change_label(row):
            raise ValueError("Only finalized alignment rows can receive change analysis.")
        members = row["previous_ids"] + row["current_ids"]
        if not members or any(key not in data.records for key in members):
            raise ValueError("Alignment members are missing from the selected disclosure extraction.")
        data.item_for(members)
        for side, year in (("previous", data.years[0]), ("current", data.years[1])):
            if any(data.records[key]["fiscal_year"] != year for key in row[side + "_ids"]):
                raise ValueError("Alignment evidence belongs to the wrong comparison year.")
        analysis = effective_change_analysis(row)
        if analysis["status"] not in {"completed", "needs_review"}:
            raise ValueError("Run LLM change analysis before lexical/semantic enrichment.")

    # Embed only members of finalized two-sided groups. One-sided changes are
    # determined by the completed alignment, rather than inferred from retrieval.
    keys = {key for row in rows if row["previous_ids"] and row["current_ids"]
            for key in row["previous_ids"] + row["current_ids"]}
    records = {key: data.records[key] for key in sorted(keys)}
    index = SemanticIndex(records, embedder=embedder)
    # Include every aligned counterpart, even when it is outside a top-k list.
    candidates = index.candidates(*data.years, top_k=max(1, len(records)))
    for row in rows:
        previous = "\n\n".join(data.records[key]["content"] for key in row["previous_ids"])
        current = "\n\n".join(data.records[key]["content"] for key in row["current_ids"])
        lexical = compute_lexical_metrics(previous, current)
        if not row["previous_ids"] or not row["current_ids"]:
            semantic = {"label": "New" if row["current_ids"] else "Removed",
                        "score": None, "normalised_score": None, "matched_id": None,
                        "rationale": "One-sided change established by the finalized alignment."}
        else:
            semantic = classify_alignment_row(row, candidates, records)
            if semantic["label"] == "Added":
                semantic.update(label="Modified", rationale=(
                    "Low semantic similarity within a finalized matched group; "
                    "the accepted alignment establishes that a counterpart exists."))
        semantic["method"] = "embedding_cosine" if previous and current else "alignment_presence"
        analysis = effective_change_analysis(row)
        analysis.update(lexical=lexical, semantic=semantic)
        # The validated LLM label is the final taxonomy; independent lexical and
        # semantic measurements remain available for inspection and disagreement.
        analysis["final_taxonomy"] = manual_change_label(row) or (analysis["llm"] if analysis["llm"] in CHANGE_TAXONOMY else None)
        row["change_analysis"] = analysis

    if original is not None and path.read_bytes() != original:
        raise ValueError("Alignment changed during analysis; no enriched output was written.")
    write_json(path, document)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--disclosures-dir", type=Path)
    parser.add_argument("--alignment-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--embedding-model", default="bge-m3")
    parser.add_argument("--input-source", choices=("files", "postgres"), default="postgres")
    args = parser.parse_args(argv)
    try:
        data = AlignmentData(args.data_dir, args.ticker.lower(), (args.previous_year, args.current_year),
                             disclosures_dir=args.disclosures_dir, input_source=args.input_source,
                             env_file=args.env_file)
        document = None
        if args.input_source == 'postgres':
            from sec_disclosure.storage.pipeline_snapshots import alignment_snapshot, alignment_output
            snapshot = alignment_snapshot(args.ticker.lower(), args.previous_year, args.current_year, env_file=args.env_file)
            document = alignment_output(snapshot, args.alignment_dir)
            manifest = snapshot['documents']['manifest.json']['payload']
        else:
            manifest = json.loads((args.alignment_dir / "manifest.json").read_text())
        canonical = lambda hashes: {str(Path(key).resolve()): value for key, value in hashes.items()}
        if canonical(manifest["input_hashes"]) != canonical(data.hashes):
            raise ValueError("Saved alignment evidence is stale; finish alignment with the current inputs first.")
        embedder = make_embedder(config=load_config(args.env_file), model=args.embedding_model,
                                 cache_dir=args.data_dir / "embeddings", progress=True)
        result = enrich_alignment(args.alignment_dir, data, embedder=embedder, document=document)
        print(f"Lexical and semantic analysis saved for {len(result['alignments'])} finalized rows.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, LLMError) as error:
        print(f"Change enrichment stopped: {error}", file=sys.stderr)
        return 1
