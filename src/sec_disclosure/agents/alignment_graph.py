"""LangGraph action routing with durable, per-job SQLite checkpoints.

Graph state contains only JSON data. API credentials, source indexes, and validator
functions stay in the runner, outside checkpoints. The existing request ledger
protects the API side effect when a model node is replayed after interruption.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.metadata import version
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from sec_disclosure.llm.disclosures import digest, write_json


GRAPH_POLICY = "langgraph_alignment_v1"


def orchestration_manifest():
    return {"engine": "langgraph", "policy": GRAPH_POLICY,
            "versions": {name: version(name) for name in
                         ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core")}}


class AgentState(TypedDict):
    identity: str
    initialized: bool
    step: int
    visible: list[str]
    trace: list[dict]
    history: list[dict]
    action: dict | None
    action_error: str | None
    repair: dict | None
    result: list[dict] | None
    finished: bool
    reason: str | None
    offline: bool


class AlignmentAgentGraph:
    def __init__(self, runtime, role, job_id, system, payload, data, visible,
                 *, validate=None, repair=None, seed=None, source_request=None, offline=False):
        self.runtime, self.role, self.job_id = runtime, role, job_id
        self.system, self.payload, self.data = system, payload, data
        self.visible = set(visible)
        self.validate, self.repair = validate, repair
        self.seed, self.source_request, self.offline = seed, source_request, offline

    def restore(self, state):
        if self.repair is not None:
            self.repair.restore(state["repair"])

    def persist(self, state):
        write_json(self.runtime.output / "traces" / f"{self.job_id}.json", state["trace"])
        if self.repair is not None:
            self.restore(state)
            write_json(self.runtime.output / "validation_errors" / f"{self.job_id}.json",
                       {"job_id": self.job_id, **self.repair.audit()})

    def prepare(self, state):
        state = deepcopy(state)
        self.restore(state)
        if not state["initialized"] and self.repair is not None and self.seed is not None:
            self.repair.receive(self.seed, set(state["visible"]))
            state["repair"] = self.repair.snapshot()
            state["trace"].append({"action": self.seed, "source_request": self.source_request, "cached_seed": True})
        state.update(initialized=True, offline=self.offline)
        self.persist(state)
        return state

    def next_turn(self, state):
        self.restore(state)
        if (state["finished"] or state["offline"] or state["step"] >= self.runtime.args.max_steps
                or self.repair is not None and not self.repair.pending):
            return "finish"
        return "model"

    def model(self, state):
        from .alignment_runtime import unique_object

        self.restore(state)
        payload = self.repair.correction_payload(self.payload) if self.repair is not None and self.repair.started else self.payload
        request = {**payload, "remaining_turns_including_this_one": self.runtime.args.max_steps - state["step"],
                   "history": state["history"] if self.repair is not None else state["trace"]}
        action, error = None, None
        try:
            response = self.runtime.call(self.role, self.job_id,
                json.dumps(request, ensure_ascii=False, separators=(",", ":")), self.system)
            if response["finish_reason"] != "stop":
                raise ValueError("Response did not finish normally.")
            action = json.loads(response["text"], object_pairs_hook=unique_object)
            if not isinstance(action, dict):
                raise ValueError("Expected a JSON action object.")
        except (ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        # Budget/API interruptions propagate without advancing this turn. On
        # restart, the ledger returns a completed response or requires retry permission.
        return {"step": state["step"] + 1, "action": action, "action_error": error}

    def route_action(self, state):
        if state["action_error"] is not None:
            return "invalid_action"
        if state["action"].get("action") in {"search", "context"}:
            return "tools"
        return "validate"

    def record_error(self, state, message):
        instruction = ("Correct the action/schema or evidence references." if self.repair is not None else
                       "Correct the action/schema/evidence references, or finish with needs_review.")
        entry = {"validation_error": message[:500], "instruction": instruction}
        state["trace"].append(entry)
        if self.repair is not None:
            state["history"].append(entry)
            self.repair.events.append({"event_id": len(self.repair.events) + 1, "proposal_index": None,
                "affected_required_ids": sorted(self.repair.pending), "outcome": "rejected",
                "errors": [{"error_type": "invalid_action", "field": "$", "invalid_value": None, "message": message}]})
            state["repair"] = self.repair.snapshot()
        self.persist(state)
        return state

    def invalid_action(self, state):
        state = deepcopy(state)
        self.restore(state)
        return self.record_error(state, state["action_error"])

    def tools(self, state):
        state = deepcopy(state)
        self.restore(state)
        visible = set(state["visible"])
        try:
            entry = {"action": state["action"],
                     "observation": self.data.tool(state["action"], visible, item=self.payload["item"])}
        except (ValueError, KeyError, TypeError) as exc:
            # Keep exactly the evidence the local tool exposed, including any
            # valid earlier queries in a partially invalid multi-query action.
            state["visible"] = sorted(visible)
            return self.record_error(state, str(exc))
        state["visible"] = sorted(visible)
        state["trace"].append(entry)
        if self.repair is not None:
            state["history"].append(entry)
        self.persist(state)
        return state

    def validate_action(self, state):
        state = deepcopy(state)
        self.restore(state)
        try:
            if self.repair is None:
                state["result"] = self.validate(state["action"], set(state["visible"]))
                state["finished"] = True
                entry = {"action": state["action"]}
            else:
                self.repair.receive(state["action"], set(state["visible"]))
                state["repair"] = self.repair.snapshot()
                entry = {"action": state["action"], "pending_disclosure_ids": sorted(self.repair.pending)}
                state["history"] = []
        except (ValueError, KeyError, TypeError) as exc:
            return self.record_error(state, str(exc))
        state["trace"].append(entry)
        self.persist(state)
        return state

    def finish(self, state):
        self.restore(state)
        reason = None
        if self.repair is not None:
            if self.repair.pending:
                reason = ("Offline repair preserved valid proposals; model corrections are pending." if state["offline"] else
                          "Automatic repair turn limit reached; remaining disclosures need review.")
            result = self.repair.results(reason)
        else:
            result = state["result"]
            if not state["finished"]:
                reason = "Agent step limit reached without a valid final decision."
        return {"result": result, "reason": reason}

    def build(self, checkpointer):
        builder = StateGraph(AgentState)
        for name, node in (("prepare", self.prepare), ("model", self.model), ("tools", self.tools),
                           ("validate", self.validate_action), ("invalid_action", self.invalid_action), ("finish", self.finish)):
            builder.add_node(name, node)
        builder.add_edge(START, "prepare")
        routes = {"model": "model", "finish": "finish"}
        builder.add_conditional_edges("prepare", self.next_turn, routes)
        builder.add_conditional_edges("model", self.route_action,
                                      {name: name for name in ("tools", "validate", "invalid_action")})
        for name in ("tools", "validate", "invalid_action"):
            builder.add_conditional_edges(name, self.next_turn, routes)
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=checkpointer, name=f"alignment_{self.role}")

    def run(self):
        scope = self.visible | (self.repair.required if self.repair is not None else set())
        if self.data.item_for(scope) != self.payload["item"]:
            raise ValueError("Agent evidence must stay within the comparison job's SEC Item.")
        identity = digest(json.dumps({"role": self.role, "payload": self.payload, "system": self.system,
            "visible": sorted(self.visible), "required": sorted(self.repair.required) if self.repair is not None else [],
            "seed": self.seed, "max_steps": self.runtime.args.max_steps}, sort_keys=True).encode())
        initial = {"identity": identity, "initialized": False, "step": 0, "visible": sorted(self.visible),
                   "trace": [], "history": [], "action": None, "action_error": None,
                   "repair": self.repair.snapshot() if self.repair is not None else None,
                   "result": None, "finished": False, "reason": None, "offline": self.offline}
        directory = self.runtime.output / "graph"
        directory.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": self.job_id, "checkpoint_ns": ""},
                  "recursion_limit": self.runtime.args.max_steps * 3 + 10}
        with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
            graph = self.build(saver)
            snapshot = graph.get_state(config)
            if snapshot.values and snapshot.values["identity"] != identity:
                raise ValueError("Graph checkpoint does not match this job; use a new output directory or revalidate the cache.")
            try:
                if snapshot.values and self.offline:
                    # An offline inspection must not execute a pending live
                    # model node or discard a response awaiting validation.
                    self.restore(snapshot.values)
                    reason = "Offline repair preserved valid proposals; model corrections are pending." if self.repair.pending else None
                    return self.repair.results(reason), snapshot.values["trace"], reason
                if snapshot.values and snapshot.values["offline"] and not self.offline:
                    # An offline import is complete locally, but its unresolved
                    # proposals can start correction turns without reapplying its seed.
                    result = graph.invoke({**snapshot.values, "offline": False}, config, durability="sync")
                elif snapshot.values:
                    result = graph.invoke(None, config, durability="sync") if snapshot.next else snapshot.values
                else:
                    result = graph.invoke(initial, config, durability="sync")
            finally:
                latest = graph.get_state(config)
                if latest.values:
                    self.persist(latest.values)
            self.visible = set(result["visible"])
            return result["result"], result["trace"], result["reason"]
