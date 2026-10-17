"""LangGraph coordinator for bounded parallel, same-Item alignment jobs.

Completed jobs remain portable JSON checkpoints. Each active agent has its own
durable LangGraph thread, so restarting this coordinator skips completed jobs
and resumes an interrupted agent at its pending node.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from sec_disclosure.llm.client import LLMError
from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.llm.concurrency import run_jobs
from .alignment_prompts import MATCHING_PROMPT, VERIFICATION_PROMPT
from .alignment_repair import VerificationRepair
from .alignment_runtime import RunLimit


class WorkflowState(TypedDict):
    matching_index: int
    verification_index: int
    matches: list[dict]
    groups: list[dict]
    verification_plan: list[dict]
    complete: bool


class AlignmentWorkflow:
    def __init__(self, args, data, runtime, candidates, automatic, source=None):
        # Validators and reporting remain shared with the CLI and offline repair.
        from . import disclosure_alignment

        self.core, self.args, self.data = disclosure_alignment, args, data
        self.runtime, self.output = runtime, runtime.output
        self.candidates, self.automatic, self.source = candidates, automatic, source
        self.exact_ids = {key for row in automatic for key in row["previous_ids"] + row["current_ids"]}
        by_item = defaultdict(list)
        for key in sorted(candidates):
            by_item[data.records[key]["item"]].append(key)
        self.jobs = [] if source else [chunk for values in by_item.values()
                                      for chunk in self.core.batches(values, args.batch_size)]

    def run_matching_job(self, index):
        anchors = self.jobs[index]
        job_id = f"matching_{index + 1:03d}"
        checkpoint = self.output / "jobs" / f"{job_id}.json"
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
        else:
            visible = set(anchors) | {hit["disclosure_id"] for anchor in anchors for hit in self.candidates[anchor]}
            payload = {"previous_year": self.data.years[0], "current_year": self.data.years[1], "anchors": anchors,
                       "item": self.data.item_for(anchors),
                       "candidate_rankings": {anchor: self.candidates[anchor] for anchor in anchors},
                       "records": [self.data.view(key) for key in sorted(visible)]}
            result, trace, reason = self.runtime.agent("matching", job_id, MATCHING_PROMPT, payload, self.data, visible,
                lambda action, shown: self.core.validate_matches(action, shown, anchors, self.data))
            saved = {"result": result, "error": reason, "anchors": anchors}
            write_json(checkpoint, saved)
        additions = saved["result"] or [{"current_id": key, "previous_ids": [], "rationale": saved["error"],
                                         "matching_failed": True} for key in anchors]
        return additions

    def matching_job(self, state):
        matches = state["matches"] + self.run_matching_job(state["matching_index"])
        write_json(self.output / "matching_proposals.json", matches)
        return {"matches": matches, "matching_index": state["matching_index"] + 1}

    def matching_phase(self, state):
        results = {}

        def consume(index, additions):
            results[index] = additions
            write_json(self.output / "matching_proposals.json",
                       state["matches"] + [row for key in sorted(results) for row in results[key]])

        run_jobs(range(state["matching_index"], len(self.jobs)), self.run_matching_job, consume, self.args.workers)
        return {"matches": state["matches"] + [row for key in sorted(results) for row in results[key]],
                "matching_index": len(self.jobs)}

    def group_proposals(self, state):
        groups = self.source.groups if self.source else self.core.connected_groups(self.data, state["matches"], self.exact_ids)
        plan = self.source.jobs if self.source else [
            {"job_id": f"verification_{number:03d}", "groups": batch}
            for number, batch in enumerate(self.core.verification_jobs(groups, self.data), 1)]
        write_json(self.output / "proposed_groups.json", groups)
        return {"groups": groups, "verification_plan": plan}

    def run_verification_job(self, planned, matches):
        job_id, batch = planned["job_id"], planned["groups"]
        checkpoint = self.output / "jobs" / f"{job_id}.json"
        saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
        if saved is None or saved.get("repair_pending") and not self.args.offline_repair:
            members = {key for group in batch for key in group["previous_ids"] + group["current_ids"]}
            required = members - self.exact_ids
            item = self.data.item_for(members)
            visible = set(members)
            absence_checks = []
            for group in batch:
                if not group["previous_ids"] or not group["current_ids"]:
                    key = (group["previous_ids"] + group["current_ids"])[0]
                    record = self.data.records[key]
                    other_year = self.data.years[1] if record["fiscal_year"] == self.data.years[0] else self.data.years[0]
                    search = self.data.search(other_year, record["summary"], item=item)
                    absence_checks.append({"anchor_id": key, **search})
                    visible.update(hit["disclosure_id"] for hit in search["hits"])
            payload = {"previous_year": self.data.years[0], "current_year": self.data.years[1], "proposed_groups": batch,
                       "item": item, "required_ids": sorted(required),
                       "records": [self.data.view(key, full=True) for key in sorted(members)],
                       "matching_rationales": [m for m in matches if m["current_id"] in required],
                       "automatic_absence_searches": absence_checks}
            if self.automatic:
                payload["established_exact_links"] = [{"previous_ids": row["previous_ids"], "current_ids": row["current_ids"]}
                    for row in self.automatic if self.data.item_for(row["previous_ids"]) == item]
            if self.source:
                payload, visible = planned["payload"], set(planned["visible"])
            repair = VerificationRepair(self.data, required,
                lambda action, shown, needed, records: self.core.validate_residual_alignments(
                    action, shown, needed, records, self.exact_ids), self.core.review_row)
            try:
                result, trace, reason = self.runtime.verification(job_id, VERIFICATION_PROMPT, payload, self.data, visible, repair,
                    seed=planned.get("seed"), source_request=planned.get("source_request"), offline=self.args.offline_repair)
            except (RunLimit, OSError, sqlite3.Error, ValueError, RuntimeError, LLMError):
                write_json(checkpoint, {"result": repair.results("Automatic repair paused; corrections are pending."),
                           "error": "Automatic repair paused.", "groups": batch, "repair_pending": True})
                raise
            saved = {"result": result, "error": reason, "groups": batch,
                     "repair_pending": bool(repair.pending) and self.args.offline_repair}
            write_json(checkpoint, saved)
        return saved

    def save_progress(self, state):
        with self.runtime.condition:
            self.core.save_report(self.output, self.data, self.candidates, state["matches"], state["groups"],
                                  self.core.checkpoint_decisions(self.output), self.runtime, complete=False)

    def verification_job(self, state):
        self.run_verification_job(state["verification_plan"][state["verification_index"]], state["matches"])
        self.save_progress(state)
        return {"verification_index": state["verification_index"] + 1}

    def verification_phase(self, state):
        run_jobs(state["verification_plan"][state["verification_index"]:],
                 lambda planned: self.run_verification_job(planned, state["matches"]),
                 lambda planned, saved: self.save_progress(state), self.args.workers)
        return {"verification_index": len(state["verification_plan"])}

    def route_matching(self, state):
        return ("matching_phase" if self.args.workers > 1 else "matching_job") if state["matching_index"] < len(self.jobs) else "group_proposals"

    def route_verification(self, state):
        return ("verification_phase" if self.args.workers > 1 else "verification_job") if state["verification_index"] < len(state["verification_plan"]) else "finish"

    def finish(self, state):
        return {"complete": not any(json.loads(p.read_text()).get("repair_pending")
                                     for p in (self.output / "jobs").glob("verification_*.json"))}

    def build(self):
        builder = StateGraph(WorkflowState)
        for name in ("matching_job", "matching_phase", "group_proposals", "verification_job", "verification_phase", "finish"):
            builder.add_node(name, getattr(self, name))
        matching_routes = {name: name for name in ("matching_job", "matching_phase", "group_proposals")}
        verification_routes = {name: name for name in ("verification_job", "verification_phase", "finish")}
        builder.add_conditional_edges(START, self.route_matching, matching_routes)
        builder.add_conditional_edges("matching_job", self.route_matching, matching_routes)
        builder.add_conditional_edges("matching_phase", self.route_matching, matching_routes)
        builder.add_conditional_edges("group_proposals", self.route_verification, verification_routes)
        builder.add_conditional_edges("verification_job", self.route_verification, verification_routes)
        builder.add_conditional_edges("verification_phase", self.route_verification, verification_routes)
        builder.add_edge("finish", END)
        return builder.compile(name="disclosure_alignment")

    def run(self):
        return self.build().invoke({"matching_index": 0, "verification_index": 0,
            "matches": self.source.matches if self.source else [], "groups": [], "verification_plan": [], "complete": False},
            {"recursion_limit": 2 * len(self.data.records) + 10})
