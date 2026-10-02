"""Incremental verifier validation and read-only import of previous alignment runs."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

from sec_disclosure.llm.disclosures import digest
from .alignment_exact import EXACT_POLICY, exact_matches


class ProposalError(ValueError):
    def __init__(self, message, field, value, code="invalid_proposal"):
        super().__init__(message)
        self.detail = {"error_type": code, "field": field, "invalid_value": value, "message": message}


def normalize_proposal(raw):
    if not isinstance(raw, dict):
        raise ProposalError("Alignment entry must be an object.", "$", raw, "invalid_structure")
    row, repairs = copy.deepcopy(raw), []
    if "needs_review" not in row:
        row["needs_review"] = True
        repairs.append("needs_review")
    reason = row.get("review_reason")
    if row.get("needs_review") is True and (reason is None or isinstance(reason, str) and not reason.strip()):
        row["review_reason"] = "Original proposal omitted required review metadata; preserved for review."
        repairs.append("review_reason")
    groups = {}
    if isinstance(row.get("evidence"), list):
        for index, citation in enumerate(row["evidence"]):
            if not isinstance(citation, dict) or not isinstance(citation.get("disclosure_id"), str) or not isinstance(citation.get("sentence_ids"), list):
                raise ProposalError("Malformed evidence entry.", f"evidence[{index}]", citation, "invalid_evidence_structure")
            selected = groups.setdefault(citation["disclosure_id"], [])
            for sid in citation["sentence_ids"]:
                if not isinstance(sid, str):
                    raise ProposalError("Sentence IDs must be strings.", f"evidence[{index}].sentence_ids", sid, "invalid_sentence_id")
                if sid not in selected:
                    selected.append(sid)
        row["evidence"] = [{"disclosure_id": key, "sentence_ids": values} for key, values in groups.items()]
    for side in ("previous_ids", "current_ids"):
        if not isinstance(row.get(side), list) or any(not isinstance(key, str) for key in row[side]):
            raise ProposalError("Alignment sides must be lists of disclosure IDs.", side, row.get(side), "invalid_id_list")
    return row, repairs


def explain_error(error, row, visible, data):
    """Locate the first failing check on this proposal, never copy batch errors."""
    if isinstance(error, ProposalError):
        return error.detail
    message = str(error)
    field, value, code = "$", row, "invalid_proposal"
    if isinstance(row, dict):
        for side, year in (("previous_ids", data.years[0]), ("current_ids", data.years[1])):
            if message.startswith(side):
                values = row.get(side, [])
                field, value, code = side, values, "invalid_disclosure_ids"
                if isinstance(values, list):
                    value = [{"disclosure_id": key, "reason":
                              "unknown_id" if key not in data.records else
                              "wrong_year" if data.records[key]["fiscal_year"] != year else
                              "not_visible" if key not in visible else "duplicate_id"}
                             for key in values if key not in visible or key not in data.records or
                             data.records[key]["fiscal_year"] != year or values.count(key) > 1]
                break
        else:
            if "review reason" in message.lower():
                field, value, code = "review_reason", row.get("review_reason"), "invalid_review_metadata"
            elif "needs_review" in message:
                field, value, code = "needs_review", row.get("needs_review"), "invalid_review_metadata"
            elif "explanation" in message:
                field, value, code = "explanation", row.get("explanation"), "missing_explanation"
            elif "sentence_ids" in message or "Evidence" in message or "sentence" in message:
                field, value, code = "evidence", row.get("evidence"), "invalid_citations"
                members = set(row.get("previous_ids", []) + row.get("current_ids", []))
                evidence = row.get("evidence")
                if isinstance(evidence, list):
                    cited = {e.get("disclosure_id") for e in evidence if isinstance(e, dict)}
                    if "EVERY" in message:
                        value = {"missing_disclosure_ids": sorted(members - cited)}
                    elif "identify each" in message:
                        value = {"citations_outside_alignment": sorted(cited - members)}
                    elif "sentence_ids" in message:
                        for i, e in enumerate(evidence):
                            key, sids = e["disclosure_id"], e["sentence_ids"]
                            bad = [sid for sid in sids if sid not in data.records[key]["sentence_map"]]
                            if bad or not sids:
                                field, value = f"evidence[{i}].sentence_ids", bad or sids
                                break
    return {"error_type": code, "field": field, "invalid_value": value, "message": message}


class VerificationRepair:
    def __init__(self, data, required, validate, fallback):
        self.data, self.required = data, set(required)
        self.validate, self.fallback = validate, fallback
        self.accepted, self.events, self.latest_errors = [], [], []
        self.started = False

    @property
    def covered(self):
        return {key for row in self.accepted for key in row["previous_ids"] + row["current_ids"]}

    @property
    def pending(self):
        return self.required - self.covered

    def receive(self, action, visible):
        if not isinstance(action, dict) or action.get("action") != "finalize" or not isinstance(action.get("alignments"), list):
            raise ProposalError("Verifier must return action:finalize with alignments.", "action/alignments", action, "invalid_action")
        self.started = True
        self.latest_errors = []
        mentioned = set()
        signatures = {(tuple(r["previous_ids"]), tuple(r["current_ids"])) for r in self.accepted}
        for index, raw in enumerate(action["alignments"]):
            members = {k for side in ("previous_ids", "current_ids")
                       for k in (raw.get(side, []) if isinstance(raw, dict) and isinstance(raw.get(side), list) else [])
                       if isinstance(k, str)}
            mentioned.update(members)
            event = {"event_id": len(self.events) + 1, "proposal_index": index,
                     "affected_required_ids": sorted(members & self.required), "original_proposal": raw}
            row = raw
            try:
                row, repairs = normalize_proposal(raw)
                signature = (tuple(row["previous_ids"]), tuple(row["current_ids"]))
                if signature in signatures:
                    event.update(outcome="preserved_existing_proposal", errors=[])
                else:
                    valid = self.validate({"action": "finalize", "alignments": [row]}, visible, members & self.required, self.data)[0]
                    if repairs:
                        valid["status"] = "needs_review"
                        valid["review_reasons"].append("missing_review_metadata")
                        valid["metadata_repairs"] = repairs
                    self.accepted.append(valid)
                    signatures.add(signature)
                    event.update(outcome="metadata_repaired" if repairs else "accepted", errors=[], metadata_fields_filled=repairs)
            except (ValueError, KeyError, TypeError) as exc:
                event.update(outcome="rejected", errors=[explain_error(exc, row, visible, self.data)])
                self.latest_errors.append(event)
            self.events.append(event)
        for key in sorted(self.pending - mentioned):
            event = {"event_id": len(self.events) + 1, "proposal_index": None, "affected_required_ids": [key],
                     "outcome": "omitted", "errors": [{"error_type": "omitted_disclosure", "field": "alignments",
                     "invalid_value": key, "message": "Required disclosure was omitted from the final proposals."}]}
            self.events.append(event)
            self.latest_errors.append(event)

    def correction_payload(self, original):
        item_scope = self.data.item_for(self.required)
        related = set(self.pending)
        for event in self.latest_errors:
            raw = event.get("original_proposal")
            if isinstance(raw, dict):
                for side in ("previous_ids", "current_ids"):
                    if isinstance(raw.get(side), list):
                        related.update(k for k in raw[side] if isinstance(k, str) and k in self.data.records
                                       and self.data.records[k]["item"] == item_scope)
        errors = []
        for event in self.latest_errors:
            if set(event["affected_required_ids"]) & self.pending or not event["affected_required_ids"]:
                item = copy.deepcopy(event)
                if isinstance(item.get("original_proposal"), dict):
                    item["original_proposal"].pop("change_type", None)
                errors.append(item)
        return {"previous_year": self.data.years[0], "current_year": self.data.years[1],
                "item": item_scope,
                "repair_mode": True, "required_ids": sorted(self.pending),
                "proposed_groups": [g for g in original["proposed_groups"] if set(g["previous_ids"] + g["current_ids"]) & self.pending],
                "preserved_proposals": [{"previous_ids": r["previous_ids"], "current_ids": r["current_ids"]} for r in self.accepted],
                "validation_errors": errors, "records": [self.data.view(key, full=True) for key in sorted(related)],
                "automatic_absence_searches": [s for s in original.get("automatic_absence_searches", []) if s["anchor_id"] in self.pending]}

    def results(self, reason="Automatic verification repair has not completed."):
        result = copy.deepcopy(self.accepted)
        for key in sorted(self.pending):
            before = [key] if self.data.records[key]["fiscal_year"] == self.data.years[0] else []
            row = self.fallback({"previous_ids": before, "current_ids": [] if before else [key]}, reason)
            relevant = [e for e in self.latest_errors if key in e["affected_required_ids"]]
            row["review_reasons"] = ["invalid_or_omitted_verifier_proposal"] if self.started else ["agent_did_not_finish"]
            row["validation_errors"] = [{"event_id": e["event_id"], "errors": e["errors"]} for e in relevant]
            result.append(row)
        return result

    def audit(self):
        events = copy.deepcopy(self.events)
        for event in events:
            if event["outcome"] in ("rejected", "omitted"):
                affected = set(event["affected_required_ids"])
                event["resolution"] = "covered_by_valid_proposal" if affected and affected <= self.covered else "still_unresolved" if affected else "invalid_extra_proposal"
        return {"pending_disclosure_ids": sorted(self.pending), "events": events}

    def snapshot(self):
        """JSON-compatible state; source data and validation functions stay local."""
        return copy.deepcopy({key: getattr(self, key) for key in
                              ("accepted", "events", "latest_errors", "started")})

    def restore(self, snapshot):
        for key in ("accepted", "events", "latest_errors", "started"):
            setattr(self, key, copy.deepcopy(snapshot[key]))


class RepairSource:
    """Import exact cached responses; historical token usage stays in its own ledger."""
    def __init__(self, folder, data):
        self.folder, self.hashes = Path(folder), {}
        manifest = self.read(self.folder / "manifest.json")
        if manifest.get("comparison_scope") != "same_item":
            raise ValueError("Repair source predates same-Item-only comparison. Start a fresh run with a new --output-dir instead of importing legacy cross-Item jobs.")
        canonical = lambda values: {str(Path(k).resolve()): v for k, v in values.items()}
        if canonical(manifest["input_hashes"]) != canonical(data.hashes):
            raise ValueError("Repair source inputs do not match the current disclosures.")
        self.exact_policy = manifest.get("exact_matching_policy")
        self.automatic = []
        if self.exact_policy:
            if self.exact_policy != EXACT_POLICY:
                raise ValueError("Repair source uses a different exact matching policy.")
            automatic = self.read(self.folder / "automatic_matches.json")
            if automatic != {"policy": EXACT_POLICY, "alignments": exact_matches(data)}:
                raise ValueError("Repair source exact matches differ from validated source text.")
            self.automatic = automatic["alignments"]
        exact_ids = {key for row in self.automatic for key in row["previous_ids"] + row["current_ids"]}
        self.candidates = self.read(self.folder / "candidate_matches.json")
        self.matches = self.read(self.folder / "matching_proposals.json")
        self.groups = self.read(self.folder / "proposed_groups.json")
        self.usage = self.read(self.folder / "token_usage.json")
        requests = []
        for path in sorted((self.folder / "requests").glob("*.json")):
            record = self.read(path)
            if record["agent"] == "verification" and record["status"] == "completed":
                requests.append((path, record))
        self.jobs = []
        for path in sorted((self.folder / "jobs").glob("verification_*.json")):
            job = self.read(path)
            trace = self.read(self.folder / "traces" / path.name)
            seed = trace[-1].get("action") if trace else None
            if not isinstance(seed, dict) or seed.get("action") != "finalize":
                seed = None
            choices = []
            for request_path, record in requests:
                if record["job_id"] != path.stem:
                    continue
                payload = json.loads(record["prompt"])
                if seed is not None:
                    try:
                        if json.loads(record["result"]["text"]) != seed or payload["history"] != trace[:-1]:
                            continue
                    except ValueError:
                        continue
                choices.append((record.get("started_at", ""), request_path, payload))
            if not choices:
                raise ValueError(f"No exact saved verifier request found for {path.stem}.")
            _, request_path, payload = sorted(choices)[-1]
            visible = set(payload["required_ids"]) | {row["disclosure_id"] for row in payload["records"]}
            for search in payload.get("automatic_absence_searches", []):
                visible.update(h["disclosure_id"] for h in search["hits"])
            for entry in trace:
                for search in entry.get("observation", {}).get("search_results", []):
                    visible.update(h["disclosure_id"] for h in search["hits"])
            required = {k for g in job["groups"] for k in g["previous_ids"] + g["current_ids"]} - exact_ids
            if required != set(payload["required_ids"]) or not visible <= data.records.keys():
                raise ValueError("Invalid saved verifier scope.")
            if data.item_for(visible | required) != payload.get("item"):
                raise ValueError("Saved verifier evidence crosses its SEC Item scope.")
            payload.pop("history", None)
            payload.pop("remaining_turns_including_this_one", None)
            self.jobs.append({"job_id": path.stem, "groups": job["groups"], "payload": payload,
                              "visible": visible, "seed": seed, "source_request": str(request_path)})
        required_ids = [k for job in self.jobs for k in job["payload"]["required_ids"]]
        if set(required_ids) | exact_ids != data.records.keys() or any(n != 1 for n in Counter(required_ids).values()):
            raise ValueError("Repair source verification jobs do not cover disclosures exactly once.")

    def read(self, path):
        content = path.read_bytes()
        self.hashes[str(path.resolve())] = digest(content)
        return json.loads(content)
