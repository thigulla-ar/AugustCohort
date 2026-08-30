from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from sentri.models import PlannedAction, RiskAlert, SentriState, TelemetryEvent
from sentri.storage import SentriStorageEngine
from sentri.telemetry import WORKER_HOOKS


WorkerHook = Callable[[SentriState], Awaitable[dict[str, Any]]]


def router_node(state: SentriState) -> dict[str, Any]:
    request = state["request"]
    actions = [
        PlannedAction.model_validate(item).model_dump(mode="json")
        for item in request.get("actions", [])
    ]
    return {
        "planned_actions": actions,
        "messages": [
            {
                "role": "assistant",
                "content": f"Sentri planned {len(actions)} proposed action(s).",
            }
        ],
        "status": "evaluating",
    }


def safety_gate_node(state: SentriState) -> dict[str, Any]:
    alerts = [RiskAlert.model_validate(item) for item in state.get("risk_alerts", [])]
    requiring_human = [alert for alert in alerts if alert.requires_human]
    if not requiring_human:
        return {"status": "authorized"}

    hard = [alert for alert in requiring_human if alert.hard_limit]
    decision = interrupt(
        {
            "type": "sentri_approval_required",
            "execution_id": state["execution_id"],
            "thread_id": state["thread_id"],
            "approvable": not bool(hard),
            "hard_limit": bool(hard),
            "alerts": [alert.model_dump(mode="json") for alert in requiring_human],
            "instruction": (
                "Acknowledge and revise the request; hard limits cannot be overridden."
                if hard
                else "Approve or reject in the Sentri Control Room."
            ),
        }
    )

    if hard:
        return {
            "status": "blocked",
            "approval": decision if isinstance(decision, dict) else None,
            "final_result": {
                "authorized": False,
                "reason": "A non-overridable Sentri hard limit was triggered.",
            },
        }
    approved = isinstance(decision, dict) and bool(decision.get("approved"))
    return {
        "status": "authorized" if approved else "blocked",
        "approval": decision if isinstance(decision, dict) else None,
        "final_result": None
        if approved
        else {"authorized": False, "reason": "Human reviewer rejected the action."},
    }


def route_after_safety(state: SentriState) -> Literal["authorize", "finish"]:
    return "authorize" if state.get("status") == "authorized" else "finish"


def authorize_node(state: SentriState) -> dict[str, Any]:
    """Issue a preflight decision; downstream tools are intentionally not run here."""
    return {
        "status": "completed",
        "final_result": {
            "authorized": True,
            "decision": "allow",
            "action_ids": [item["id"] for item in state["planned_actions"]],
            "contract": "The host may now execute only the exact proposed actions.",
        },
    }


def finish_node(state: SentriState) -> dict[str, Any]:
    return {"status": state.get("status", "blocked")}


class SentriGraph:
    def __init__(self, storage: SentriStorageEngine) -> None:
        self.storage = storage
        self.checkpointer = MemorySaver()
        self.graph = self._build()

    def _stored_hook(self, hook: WorkerHook) -> WorkerHook:
        async def wrapped(state: SentriState) -> dict[str, Any]:
            update = await hook(state)
            events = [
                TelemetryEvent.model_validate(item)
                for item in update.get("telemetry_events", [])
            ]
            if events:
                await self.storage.record_many(events)
            return update

        return wrapped

    def _build(self):
        builder = StateGraph(SentriState)
        builder.add_node("router", router_node)
        for name, hook in WORKER_HOOKS.items():
            builder.add_node(name, self._stored_hook(hook))
        builder.add_node("safety_gate", safety_gate_node)
        builder.add_node("authorize", authorize_node)
        builder.add_node("finish", finish_node)

        builder.add_edge(START, "router")
        for name in WORKER_HOOKS:
            builder.add_edge("router", name)
        builder.add_edge(list(WORKER_HOOKS), "safety_gate")
        builder.add_conditional_edges(
            "safety_gate",
            route_after_safety,
            {"authorize": "authorize", "finish": "finish"},
        )
        builder.add_edge("authorize", END)
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=self.checkpointer)

    async def run(self, state: SentriState, thread_id: str) -> dict[str, Any]:
        return await self.graph.ainvoke(
            state, config={"configurable": {"thread_id": thread_id}}
        )

    async def resume(
        self, thread_id: str, decision: dict[str, Any]
    ) -> dict[str, Any]:
        from langgraph.types import Command

        return await self.graph.ainvoke(
            Command(resume=decision),
            config={"configurable": {"thread_id": thread_id}},
        )

    async def snapshot(self, thread_id: str):
        return await self.graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )

    async def is_interrupted(self, thread_id: str) -> bool:
        snapshot = await self.snapshot(thread_id)
        return bool(snapshot.next) and "safety_gate" in snapshot.next


def initial_state(
    execution_id: str, thread_id: str, request: dict[str, Any]
) -> SentriState:
    return {
        "execution_id": execution_id,
        "thread_id": thread_id,
        "request": request,
        "messages": [{"role": "user", "content": request["message"]}],
        "planned_actions": [],
        "telemetry_events": [],
        "risk_alerts": [],
        "cost_metrics": {},
        "change_set": [],
        "execution_dag": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "approval": None,
        "final_result": None,
        "status": "received",
    }
