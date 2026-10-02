"""Validate saved disclosure evidence and expose local, read-only agent tools."""

from __future__ import annotations

import json
from pathlib import Path

from sec_disclosure.indexing.disclosure_retrieval import DisclosureIndex
from sec_disclosure.llm.disclosures import apply_source_unit_policy, digest, normalized


class AlignmentData:
    def __init__(self, data_dir: Path, ticker: str, years: tuple[str, str], *, disclosures_dir: Path | None = None):
        self.ticker, self.years = ticker, years
        self.records, self.paragraphs, self.audit, self.hashes = {}, {}, {}, {}
        self.extraction_usage = {}
        for year in years:
            root = (disclosures_dir or data_dir / "disclosures") / ticker / year
            raw = data_dir / "raw" / ticker / year
            chunks = self.read(raw / f"{year}_chunks.json")
            sentences = self.read(raw / f"{year}_chunk_sentences.json")
            parents = {p["id"]: p for p in chunks}
            units = {s["id"]: s for s in sentences}
            if len(parents) != len(chunks) or len(units) != len(sentences):
                raise ValueError("Duplicate raw evidence IDs.")
            self.paragraphs[year] = chunks
            usage = self.read(root / "token_usage.json")
            if not usage.get("run_complete"):
                raise ValueError(f"Finish disclosure extraction for {ticker} {year} before alignment.")
            self.extraction_usage[year] = usage["reported_tokens"]
            claimed = set()
            for filename, status in (("disclosures.json", "source_validated"),
                                     ("review_candidates.json", "needs_review")):
                document = self.read(root / filename)
                self.check_metadata(document, year)
                # The saved grouping must refer to these exact raw files.
                for name, expected in document["input_hashes"].items():
                    current = raw / Path(name).name
                    if not current.is_file() or digest(current.read_bytes()) != expected:
                        raise ValueError(f"Stale extraction evidence for {year}: {current.name}.")
                for disclosure in document["disclosures"]:
                    self.check_metadata(disclosure, year)
                    key = disclosure["disclosure_id"]
                    if key in self.records:
                        raise ValueError(f"Duplicate disclosure ID: {key}.")
                    selected = {}
                    for source in disclosure["sources"]:
                        parent = parents.get(source["paragraph_id"])
                        if not parent or parent["item"] != disclosure["item"]:
                            raise ValueError(f"Invalid parent/Item for {key}.")
                        if source["original_paragraph"] != parent["text"]:
                            raise ValueError(f"Original paragraph changed for {key}.")
                        for sentence in source["sentences"]:
                            sid = sentence["sentence_id"]
                            unit = units.get(sid)
                            if (not unit or unit["chunk_id"] != parent["id"] or
                                    unit["text"] != sentence["text"] or sid in claimed):
                                raise ValueError(f"Invalid/reused sentence evidence: {sid}.")
                            selected[sid] = sentence["text"]
                            claimed.add(sid)
                    if not selected or disclosure["content"] != "\n\n".join(s["selected_text"] for s in disclosure["sources"]):
                        raise ValueError(f"Missing or inconsistent content for {key}.")
                    # Check selected text against the precise sentence selection.
                    for source in disclosure["sources"]:
                        if "".join(source["selected_text"].split()) != "".join(" ".join(s["text"] for s in source["sentences"]).split()):
                            raise ValueError(f"Selected content differs from sentence evidence: {key}.")
                    verification = apply_source_unit_policy(disclosure["verification"])
                    effective_status = verification["status"] if verification.get("ignored_review_reasons") else status
                    self.records[key] = {**disclosure, "verification": verification,
                                         "extraction_status": effective_status, "sentence_map": selected}
            for filename, field in (("excluded_sources.json", "excluded"), ("unassigned_sources.json", "sources")):
                document = self.read(root / filename)
                self.check_metadata(document, year)
                for row in document[field]:
                    if not row.get("text", "").strip():
                        continue
                    unit = units.get(row["sentence_id"])
                    if not unit or unit["text"] != row["text"]:
                        raise ValueError("Audit evidence differs from raw source.")
                    key = row["paragraph_id"] + "_AUDIT"
                    record = self.audit.setdefault(key, {
                        "disclosure_id": key, "fiscal_year": year, "item": row["item"],
                        "section": parents[row["paragraph_id"]].get("item_title", ""),
                        "summary": "", "content": "", "sentences": [], "reasons": [],
                        "paragraph_id": row["paragraph_id"], "kind": "ungrouped_or_excluded_evidence"})
                    record["content"] += row["text"] + "\n"
                    record["sentences"].append({"sentence_id": row["sentence_id"], "text": row["text"]})
                    record["reasons"].append(row.get("reason", ""))
        self.index, self.audit_index = DisclosureIndex(self.records), DisclosureIndex(self.audit)

    def read(self, path: Path):
        value = path.read_bytes()
        self.hashes[str(path)] = digest(value)
        return json.loads(value)

    def check_metadata(self, record, year):
        if record.get("company") != self.ticker or str(record.get("fiscal_year")) != year:
            raise ValueError("Disclosure input includes the wrong company/year.")

    def item_for(self, keys) -> str:
        items = {self.records[key]["item"] for key in keys}
        if len(items) != 1:
            raise ValueError("A comparison job must contain disclosures from exactly one SEC Item.")
        return next(iter(items))

    def view(self, key: str, *, full: bool = False) -> dict:
        d = self.records[key]
        result = {field: d[field] for field in ("disclosure_id", "fiscal_year", "item", "section", "taxonomy", "summary", "extraction_status")}
        sentences, size = [], 0
        for sid, text in d["sentence_map"].items():
            if not full and sentences and size + len(text) > 1300:
                break
            sentences.append({"sentence_id": sid, "text": text})
            size += len(text)
        result.update(sentences=sentences, evidence_complete=len(sentences) == len(d["sentence_map"]),
                      extraction_review_reasons=d["verification"].get("review_reasons", []))
        return result

    def context(self, key: str) -> dict:
        result = self.view(key, full=True)
        d = self.records[key]
        selected = {s["paragraph_id"] for s in d["sources"]}
        paragraphs = self.paragraphs[d["fiscal_year"]]
        positions = [i for i, p in enumerate(paragraphs) if p["id"] in selected]
        neighbors = sorted({j for i in positions for j in (i - 1, i, i + 1) if 0 <= j < len(paragraphs)})
        result["paragraph_context_only"] = [
            {"paragraph_id": paragraphs[i]["id"], "text": paragraphs[i]["text"]}
            for i in neighbors if paragraphs[i]["item"] == d["item"]]
        return result

    def search(self, year: str, query: str, offset: int = 0, *, item: str) -> dict:
        hits = self.index.search(query, year, item=item, limit=8, offset=offset)
        audit = self.audit_index.search(query, year, item=item, limit=4)
        return {"year": year, "item": item, "query": query, "offset": offset, "scope": "same_item_including_review_and_audit",
                "hits": [{**h, "record": self.view(h["disclosure_id"])} for h in hits],
                "audit_hits": [{**h, "record": self.audit[h["disclosure_id"]]} for h in audit if h["text_score"] > 0]}

    def tool(self, action: dict, visible: set[str], *, item: str) -> dict:
        if action["action"] == "search":
            queries = action.get("queries")
            if not isinstance(queries, list) or not 1 <= len(queries) <= 3:
                raise ValueError("search requires 1-3 queries.")
            results = []
            for query in queries:
                if not isinstance(query, dict) or query.get("year") not in self.years:
                    raise ValueError("Search year must be one of the comparison years.")
                if query.get("item", item) != item:
                    raise ValueError("Search cannot change the comparison job's SEC Item.")
                text, offset = query.get("query"), query.get("offset", 0)
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000 or type(offset) is not int or not 0 <= offset <= len(self.records):
                    raise ValueError("Invalid search query/offset.")
                result = self.search(query["year"], text, offset, item=item)
                visible.update(h["disclosure_id"] for h in result["hits"])
                results.append(result)
            return {"search_results": results}
        ids = action.get("disclosure_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(not isinstance(key, str) or key not in visible for key in ids):
            raise ValueError("context requires 1-4 visible disclosure IDs.")
        if any(self.records[key]["item"] != item for key in ids):
            raise ValueError("Context cannot access a different SEC Item.")
        return {"context_results": [self.context(key) for key in ids]}
