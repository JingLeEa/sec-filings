"""Shared input, prompt, and validation logic for alignment evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sec_disclosure.agents.alignment_prompts import CHANGE_ANALYSIS_PROMPT
from sec_disclosure.agents.materiality_prompts import MATERIALITY_PROMPT

CHANGE_LABELS = {"Expanded", "Reduced", "Modified", "Reworded", "Unchanged"}
MATERIALITY_FIELDS = {"materiality", "materiality_score", "confidence_score", "reason"}
def schema(stage):
    if stage == "change":
        action = "classify"
        collection = "analyses"
        fields = {
            "match_id": {"type": "string"},
            "llm": {"type": ["string", "null"], "enum": sorted(CHANGE_LABELS) + [None]},
        }
    else:
        action = "classify_materiality"
        collection = "classifications"
        fields = {
            "match_id": {"type": "string"},
            "materiality": {"type": "string", "enum": ["Yes", "No", "Uncertain"]},
            "materiality_score": {"type": "number"},
            "confidence_score": {"type": "number"},
            "reason": {"type": "string"},
            "key_change": {"type": "string"},
        }
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"action": {"type": "string", "enum": [action]},
                       collection: {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": fields, "required": list(fields),
        }}},
        "required": ["action", collection],
    }


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def observed_spend(root):
    return sum(read_json(path)["estimated_usd"]
               for path in root.glob("*/*/openai_eval/token_usage.json"))


def other_active_batches(root, current):
    return [path for path in root.glob("*/*/openai_eval/active_batch.json")
            if path != current]


def projected_cost(requests, input_price, output_price):
    estimated_input = sum(len(json.dumps(request["body"], ensure_ascii=False)) / 3
                          for request in requests)
    maximum_output = sum(request["body"]["max_output_tokens"] for request in requests)
    return (estimated_input * input_price + maximum_output * output_price) / 1_000_000


def load_inputs(alignment_path, disclosures_root):
    document = read_json(alignment_path)
    if document.get("run_complete") is not True or not isinstance(document.get("alignments"), list):
        raise ValueError("Use a completed prepared alignments.json file.")
    ticker = document["company"]
    years = (str(document["previous_year"]), str(document["current_year"]))
    records = {}
    for year in years:
        for name in ("disclosures.json", "review_candidates.json"):
            path = disclosures_root / ticker / year / name
            if not path.is_file():
                if name == "review_candidates.json":
                    continue
                raise ValueError(f"Missing disclosure evidence: {path}")
            source = read_json(path)
            if source.get("company") != ticker or str(source.get("fiscal_year")) != year:
                raise ValueError(f"Wrong company or year in {path}")
            for record in source["disclosures"]:
                key = record["disclosure_id"]
                if key in records:
                    raise ValueError(f"Duplicate disclosure ID: {key}")
                records[key] = record
    seen = set()
    for row in document["alignments"]:
        key = row.get("match_id")
        if not isinstance(key, str) or key in seen:
            raise ValueError("Alignment match IDs must be unique strings.")
        seen.add(key)
        for side, year in (("previous", years[0]), ("current", years[1])):
            for disclosure_id in row[side + "_ids"]:
                record = records.get(disclosure_id)
                if record is None:
                    citation = next((entry for entry in row.get("evidence", [])
                                     if entry.get("disclosure_id") == disclosure_id), None)
                    sentences = citation.get("sentences", []) if citation else []
                    if not sentences or any(not isinstance(item.get("text"), str) or not item["text"]
                                            for item in sentences):
                        raise ValueError(f"Missing disclosure text and cited sentences: {disclosure_id}")
                    content = " ".join(item["text"] for item in sentences)
                    metadata = next((entry for entry in row.get(side + "_disclosures", [])
                                     if entry.get("disclosure_id") == disclosure_id), {})
                    records[disclosure_id] = {"disclosure_id": disclosure_id,
                                              "fiscal_year": year, "item": metadata.get("item"),
                                              "section": metadata.get("section"), "content": content,
                                              "evidence_source": "alignment_citations"}
                    record = records[disclosure_id]
                elif record.get("evidence_source") == "alignment_citations":
                    citation = next((entry for entry in row.get("evidence", [])
                                     if entry.get("disclosure_id") == disclosure_id), None)
                    if citation and citation.get("sentences"):
                        content = " ".join(item["text"] for item in citation["sentences"])
                        if content != record["content"]:
                            raise ValueError(f"Conflicting cited text for {disclosure_id}")
                if str(record["fiscal_year"]) != year:
                    raise ValueError(f"Wrong-year disclosure: {disclosure_id}")
        if set(row.get("materiality_analysis", {})) != MATERIALITY_FIELDS:
            raise ValueError("Prepare evaluation alignments with four materiality fields first.")
    return document, records


def source_identity(document, records):
    rows = [{key: value for key, value in row.items()
             if key not in {"change_analysis", "materiality_analysis"}}
            for row in document["alignments"]]
    return fingerprint({"company": document["company"], "years": [document["previous_year"],
                        document["current_year"]], "rows": rows,
                        "records": {key: {field: record.get(field) for field in
                                          ("fiscal_year", "item", "section", "content", "evidence_source")}
                                    for key, record in records.items()}})


def evidence(row, stage):
    if stage == "change":
        return {key: row[key] for key in
                ("match_id", "previous_ids", "current_ids", "relationship")}

    # Match the production materiality evidence view while excluding prior labels.
    return {"match_id": row["match_id"], "status": row["status"],
            "relationship": row["relationship"],
            "alignment_explanation": row["explanation"],
            "previous_disclosures": row.get("previous_disclosures", []),
            "current_disclosures": row.get("current_disclosures", []),
            "evidence": [{"disclosure_id": citation["disclosure_id"],
                          "sentences": [{"sentence_id": sentence["sentence_id"],
                                         "text": sentence["text"]}
                                        for sentence in citation.get("sentences", [])]}
                         for citation in row.get("evidence", [])],
            **({"unmatched_type": row.get("unmatched_type")}
               if row["status"] == "unmatched" else {})}


def apply_deterministic(document):
    changed = False
    for row in document["alignments"]:
        analysis = row["change_analysis"]
        if row["status"] == "auto_matched":
            if analysis.get("status") != "completed":
                analysis.update(status="completed", llm="Unchanged")
                changed = True
            materiality = row["materiality_analysis"]
            if materiality["materiality"] is None:
                materiality.update(materiality="No", materiality_score=0.0,
                                   confidence_score=1.0,
                                   reason="The disclosures are exact-text matches; no substantive change is identified.")
                changed = True
        elif row["status"] == "unmatched" and analysis.get("status") != "completed":
            before, after = row["previous_ids"], row["current_ids"]
            if bool(before) == bool(after):
                raise ValueError(f"Unmatched row is not one-sided: {row['match_id']}")
            analysis.update(status="completed", llm="Removed" if before else "New")
            changed = True
        elif row["status"] not in {"ai_verified", "auto_matched", "unmatched"}:
            raise ValueError(f"Unsupported alignment status: {row['status']}")
    return changed


def pending_rows(document, stages):
    return [row for row in document["alignments"]
            if ("change" in stages and row["status"] == "ai_verified"
                and row["change_analysis"].get("status") != "completed")
            or ("materiality" in stages and row["status"] != "auto_matched"
                and row["materiality_analysis"]["materiality"] is None)]


def make_requests(rows, records, stages, model, effort, batch_size, document,
                  max_output_tokens=8000):
    requests, specs = [], {}
    for stage in stages:
        selected = [row for row in rows if
                    (stage == "change" and row["status"] == "ai_verified"
                     and row["change_analysis"].get("status") != "completed")
                    or (stage == "materiality" and row["status"] != "auto_matched"
                        and row["materiality_analysis"]["materiality"] is None)]
        groups = []
        for row in selected:
            if stage == "change":
                row_items = {records[key]["item"] for key in row["previous_ids"] + row["current_ids"]}
                if len(row_items) != 1:
                    raise ValueError(f"Change-analysis row spans SEC Items: {row['match_id']}")
                item = next(iter(row_items))
            else:
                item = None
            if not groups or len(groups[-1][1]) >= batch_size or (stage == "change" and groups[-1][0] != item):
                groups.append((item, []))
            groups[-1][1].append(row)
        for number, (item, group) in enumerate(groups):
            custom_id = f"{stage}_{number:04d}"
            if stage == "change":
                members = sorted({key for row in group for key in row["previous_ids"] + row["current_ids"]})
                payload = {"change_analysis": True,
                           "previous_year": document["previous_year"],
                           "current_year": document["current_year"],
                           "item": item,
                           "alignments": [evidence(row, stage) for row in group],
                           "records": [{field: records[key].get(field) for field in
                                        ("disclosure_id", "fiscal_year", "item", "content")}
                                       for key in members]}
            else:
                payload = {"company": document["company"],
                           "previous_year": document["previous_year"],
                           "current_year": document["current_year"],
                           "rows": [evidence(row, stage) for row in group]}
            body = {
                "model": model, "reasoning": {"effort": effort},
                "instructions": CHANGE_ANALYSIS_PROMPT if stage == "change" else MATERIALITY_PROMPT,
                "input": json.dumps(payload, ensure_ascii=False),
                "text": {"format": {"type": "json_schema", "name": f"eval_{stage}",
                                    "strict": True, "schema": schema(stage)}},
                "max_output_tokens": max_output_tokens,
            }
            requests.append({"custom_id": custom_id, "method": "POST",
                             "url": "/v1/responses", "body": body})
            specs[custom_id] = {"stage": stage, "match_ids": [row["match_id"] for row in group]}
    return requests, specs


def response_text(body):
    if body.get("status") != "completed":
        raise ValueError(f"Response status is {body.get('status')}, not completed.")
    chunks = [part["text"] for item in body.get("output", []) if item.get("type") == "message"
              for part in item.get("content", []) if part.get("type") == "output_text"]
    if len(chunks) != 1:
        raise ValueError("Expected one JSON output_text message.")
    return chunks[0]


def validate_results(stage, result, match_ids):
    action, collection = (("classify", "analyses") if stage == "change"
                          else ("classify_materiality", "classifications"))
    if (not isinstance(result, dict) or set(result) != {"action", collection}
            or result["action"] != action or not isinstance(result[collection], list)):
        raise ValueError(f"Response must contain action:{action} and a {collection} array.")
    rows = result[collection]
    if [row.get("match_id") for row in rows if isinstance(row, dict)] != match_ids or len(rows) != len(match_ids):
        raise ValueError("Response IDs do not match the submitted rows in order.")
    for row in rows:
        if stage == "change":
            if set(row) != {"match_id", "llm"} or row["llm"] not in CHANGE_LABELS | {None}:
                raise ValueError("Invalid change-analysis label.")
        else:
            if set(row) != MATERIALITY_FIELDS | {"match_id", "key_change"}:
                raise ValueError("Invalid materiality fields.")
            strength, confidence = row["materiality_score"], row["confidence_score"]
            if any(type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1
                   for score in (strength, confidence)):
                raise ValueError("Materiality scores must be finite numbers from 0 to 1.")
            expected = "Uncertain" if confidence < 0.60 else "Yes" if strength >= 0.50 else "No"
            if (row["materiality"] != expected or not isinstance(row["reason"], str)
                    or not row["reason"].strip() or not isinstance(row["key_change"], str)
                    or not row["key_change"].strip()):
                raise ValueError("Materiality label, scores, or reason is inconsistent.")
    return rows


def collect_responses(raw, specs, document):
    by_id = {row["match_id"]: row for row in document["alignments"]}
    seen, updates, usage, errors = set(), [], {"input_tokens": 0, "output_tokens": 0}, []
    for line in raw.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        custom_id = entry.get("custom_id")
        if custom_id not in specs or custom_id in seen:
            raise ValueError(f"Unknown or duplicate batch custom_id: {custom_id}")
        seen.add(custom_id)
        response = entry.get("response")
        if not isinstance(response, dict) or response.get("status_code") != 200:
            errors.append({"custom_id": custom_id, "error": entry.get("error") or response})
            continue
        body = response["body"]
        tokens = body.get("usage") or {}
        usage["input_tokens"] += tokens.get("input_tokens", 0)
        usage["output_tokens"] += tokens.get("output_tokens", 0)
        try:
            result = validate_results(specs[custom_id]["stage"],
                                      json.loads(response_text(body)), specs[custom_id]["match_ids"])
        except (ValueError, KeyError, TypeError) as error:
            errors.append({"custom_id": custom_id, "error": str(error)})
            continue
        updates.extend((specs[custom_id]["stage"], row) for row in result)
    for missing in specs.keys() - seen:
        errors.append({"custom_id": missing, "error": "Missing batch response"})
    for stage, result in updates:
        target = by_id[result["match_id"]]
        if stage == "change":
            target["change_analysis"].update(status="completed" if result["llm"] else "needs_review",
                                              llm=result["llm"])
        else:
            target["materiality_analysis"] = {key: result[key] for key in
                                               ("materiality", "materiality_score", "confidence_score", "reason")}
    return usage, errors, len(updates)



