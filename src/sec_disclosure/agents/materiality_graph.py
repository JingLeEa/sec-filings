"""LangGraph validation loop for a batch of materiality decisions."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from sec_disclosure.llm.disclosures import digest, write_json

from .materiality_runtime import unique_object


GRAPH_POLICY = "langgraph_materiality_v2"


class MaterialityState(TypedDict):
    identity: str
    step: int
    trace: list[dict]
    action: dict | None
    action_error: str | None
    result: list[dict] | None
    finished: bool


class MaterialityAgentGraph:
    def __init__(self, runtime, job_id, system, payload, validate):
        self.runtime, self.job_id = runtime, job_id
        self.system, self.payload, self.validate = system, payload, validate

    def persist(self, state):
        write_json(self.runtime.output / "traces" / f"{self.job_id}.json", state["trace"])

    def next_turn(self, state):
        if state["finished"]:
            return "finish"
        if state["step"] >= self.runtime.args.max_steps:
            return "exhausted"
        return "model"

    def model(self, state):
        request = {
            **self.payload,
            "remaining_turns_including_this_one": self.runtime.args.max_steps - state["step"],
            "validation_history": state["trace"],
        }
        action, error = None, None
        try:
            response = self.runtime.call(
                self.job_id,
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                self.system,
            )
            if response["finish_reason"] != "stop":
                raise ValueError("Response did not finish normally.")
            action = json.loads(response["text"], object_pairs_hook=unique_object)
            if not isinstance(action, dict):
                raise ValueError("Expected a JSON action object.")
        except (ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        return {"step": state["step"] + 1, "action": action, "action_error": error}

    def route_action(self, state):
        return "invalid" if state["action_error"] is not None else "validate"

    def invalid(self, state):
        state = deepcopy(state)
        state["trace"].append({
            "validation_error": state["action_error"][:500],
            "instruction": "Return the exact classification schema for every supplied match_id.",
        })
        self.persist(state)
        return state

    def validate_action(self, state):
        state = deepcopy(state)
        try:
            state["result"] = self.validate(state["action"])
            state["finished"] = True
            state["trace"].append({"action": state["action"]})
        except (ValueError, KeyError, TypeError) as exc:
            state["trace"].append({
                "validation_error": str(exc)[:500],
                "instruction": (
                    "Correct the strength/confidence scores, derived labels, fields, "
                    "and match_id coverage."
                ),
            })
        self.persist(state)
        return state

    def finish(self, state):
        return {"result": state["result"]}

    def exhausted(self, state):
        raise ValueError(f"{self.job_id} exhausted its validation turns without a valid classification.")

    def build(self, checkpointer):
        builder = StateGraph(MaterialityState)
        for name, node in (
                ("model", self.model),
                ("invalid", self.invalid),
                ("validate", self.validate_action),
                ("finish", self.finish),
                ("exhausted", self.exhausted)):
            builder.add_node(name, node)
        routes = {"model": "model", "finish": "finish", "exhausted": "exhausted"}
        builder.add_conditional_edges(START, self.next_turn, routes)
        builder.add_conditional_edges("model", self.route_action, {"invalid": "invalid", "validate": "validate"})
        builder.add_conditional_edges("invalid", self.next_turn, routes)
        builder.add_conditional_edges("validate", self.next_turn, routes)
        builder.add_edge("finish", END)
        builder.add_edge("exhausted", END)
        return builder.compile(checkpointer=checkpointer, name="materiality")

    def run(self):
        identity = digest(json.dumps({
            "payload": self.payload,
            "system": self.system,
            "max_steps": self.runtime.args.max_steps,
        }, sort_keys=True).encode())
        initial = {
            "identity": identity,
            "step": 0,
            "trace": [],
            "action": None,
            "action_error": None,
            "result": None,
            "finished": False,
        }
        directory = self.runtime.output / "graph"
        directory.mkdir(parents=True, exist_ok=True)
        config = {
            "configurable": {"thread_id": self.job_id, "checkpoint_ns": ""},
            "recursion_limit": self.runtime.args.max_steps * 3 + 5,
        }
        with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
            graph = self.build(saver)
            snapshot = graph.get_state(config)
            if snapshot.values and snapshot.values["identity"] != identity:
                raise ValueError("Materiality checkpoint does not match this job or its source rows.")
            try:
                if snapshot.values:
                    result = graph.invoke(None, config, durability="sync") if snapshot.next else snapshot.values
                else:
                    result = graph.invoke(initial, config, durability="sync")
            finally:
                latest = graph.get_state(config)
                if latest.values:
                    self.persist(latest.values)
            return result["result"]
