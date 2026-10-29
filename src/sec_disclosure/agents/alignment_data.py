"""Validate saved disclosure evidence and expose local, read-only agent tools."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from sec_disclosure.indexing.disclosure_retrieval import DisclosureIndex
from sec_disclosure.llm.disclosures import apply_source_unit_policy, digest, normalized


class AlignmentData:
    def __init__(self, data_dir: Path, ticker: str, years: tuple[str, str], *, disclosures_dir: Path | None = None,
                 input_source="files", env_file=None):
        self.ticker, self.years = ticker, years
        self.input_source = input_source
        self.records, self.paragraphs, self.audit, self.hashes = {}, {}, {}, {}
        self.extraction_usage = {}
        self._database_documents = {}
        if input_source == 'postgres':
            from sec_disclosure.storage.pipeline_snapshots import read_snapshots
            scopes = [(kind, year, year) for year in years for kind in ('raw', 'disclosures')]
            snapshots = read_snapshots(ticker, scopes, env_file=env_file)
            for year in years:
                for kind, folder in (('raw', data_dir / 'raw'), ('disclosures', disclosures_dir or data_dir / 'disclosures')):
                    for name, entry in snapshots[(kind, year, year)]['documents'].items():
                        self._database_documents[str(folder / ticker / year / name)] = entry
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
                    entry = self._database_documents.get(str(current))
                    actual = entry['sha256'] if entry is not None else (
                        digest(current.read_bytes()) if input_source == 'files' and current.is_file() else None)
                    if actual != expected:
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
        if self.input_source == 'postgres':
            from sec_disclosure.storage.pipeline_snapshots import PipelineInputError
            entry = self._database_documents.get(str(path))
            if entry is None:
                raise PipelineInputError(f'Missing PostgreSQL alignment input: {path.name}')
            self.hashes[entry['source_path']] = entry['sha256']
            return deepcopy(entry['payload'])
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

    def audit_key(self, key: str) -> str | None:
        """Resolve only exact saved audit IDs or their exact original paragraph ID."""
        if key in self.audit:
            return key
        candidate = key + "_AUDIT"
        if candidate in self.audit and self.audit[candidate].get("paragraph_id") == key:
            return candidate
        return None

    def audit_context(self, key: str) -> dict:
        """Expose unselected source context without creating a disclosure or a citation."""
        audit = deepcopy(self.audit[key])
        paragraph_id, year, item = audit["paragraph_id"], audit["fiscal_year"], audit["item"]
        audit.update(
            context_only=True, valid_alignment_member=False,
            guidance=("This is ungrouped/excluded source context, not an extracted disclosure. "
                      "Do not put this audit ID, paragraph ID or its unselected sentence IDs in an alignment. "
                      "Use only the eligible selected disclosures and their exact sentence IDs. "
                      "If the counterpart exists only in audit context, explain the extraction gap and flag needs_review."),
        )
        paragraphs = self.paragraphs[year]
        positions = [i for i, paragraph in enumerate(paragraphs) if paragraph["id"] == paragraph_id]
        neighbors = sorted({j for i in positions for j in (i - 1, i, i + 1) if 0 <= j < len(paragraphs)})
        audit["paragraph_context_only"] = [
            {"paragraph_id": paragraphs[i]["id"], "text": paragraphs[i]["text"]}
            for i in neighbors if paragraphs[i]["item"] == item]
        audit["eligible_disclosures"] = [
            self.view(disclosure_id, full=True) for disclosure_id, disclosure in sorted(self.records.items())
            if disclosure["fiscal_year"] == year and disclosure["item"] == item
            and any(source["paragraph_id"] == paragraph_id for source in disclosure["sources"])]
        return audit

    def search(self, year: str, query: str, offset: int = 0, *, item: str) -> dict:
        hits = self.index.search(query, year, item=item, limit=8, offset=offset)
        audit = self.audit_index.search(query, year, item=item, limit=4)
        return {"year": year, "item": item, "query": query, "offset": offset, "scope": "same_item_including_review_and_audit",
                "hits": [{**h, "record": self.view(h["disclosure_id"])} for h in hits],
                "audit_hits": [{**h, "record": {**self.audit[h["disclosure_id"]],
                                                "context_only": True, "valid_alignment_member": False}}
                               for h in audit if h["text_score"] > 0]}

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
        if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(not isinstance(key, str) for key in ids):
            raise ValueError("context requires 1-4 visible disclosure IDs or exact saved audit IDs; use a JSON list of IDs.")
        selected = []
        for key in ids:
            audit_key = self.audit_key(key)
            record = self.records.get(key) if key in self.records else self.audit.get(audit_key)
            if record is None:
                allowed = sorted(k for k in visible if k in self.records and self.records[k]["item"] == item)
                raise ValueError(
                    f"Unknown context ID {key!r}. Use an exact disclosure ID from the supplied records/search hits "
                    f"or an exact saved audit ID ending in _AUDIT. Raw paragraph IDs are accepted only when they "
                    f"identify a saved audit record. Visible disclosure IDs: {json.dumps(allowed)}")
            if record["item"] != item:
                raise ValueError("Context cannot access a different SEC Item.")
            if key in self.records and key not in visible:
                raise ValueError(f"Disclosure {key!r} is not visible. Search its topic in the same SEC Item first, "
                                 "then use an exact disclosure ID from the returned hits.")
            selected.append((key, audit_key))
        results = []
        for key, audit_key in selected:
            if key in self.records:
                results.append(self.context(key))
            else:
                result = self.audit_context(audit_key)
                # Only actual selected disclosures become eligible alignment members.
                visible.update(disclosure["disclosure_id"] for disclosure in result["eligible_disclosures"])
                results.append(result)
        return {"context_results": results}
