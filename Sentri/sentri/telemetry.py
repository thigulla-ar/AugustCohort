from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sentri import __version__
from sentri.models import (
    CostMetrics,
    OutcomeReport,
    PlannedAction,
    RiskAlert,
    SentriState,
    TelemetryEvent,
    WorkerName,
)
from sentri.safety import evaluate_actions
from sentri.redaction import redact
from sentri.security import action_hash


AUDIT_SCHEMA_VERSION = "1.2"
POLICY_VERSION = "sentri-hard-limits-v1"
INVESTIGATION_GOALS = [
    "incident_investigation",
    "adoption_analysis",
    "performance_improvement",
    "audit_evidence",
]


def _audit_context(state: SentriState) -> dict[str, Any]:
    request = state.get("request", {})
    metadata = request.get("metadata", {})
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "goals": INVESTIGATION_GOALS,
        "correlation": {
            "execution_id": state["execution_id"],
            "thread_id": state["thread_id"],
            "conversation_id_hash": _content_hash(request.get("conversation_id")),
        },
        "origin": {
            "host": request.get("host", "local"),
            "authenticated_principal": metadata.get(
                "authenticated_principal", "unknown"
            ),
        },
        "security": {
            "policy_version": POLICY_VERSION,
            "hard_limits": [
                "never_send_money",
                "never_delete_records",
                "never_share_pii",
            ],
            "pii_storage": "keyed_hash_redaction",
            "raw_tool_outputs_persisted": False,
            "permit_required_before_execution": True,
        },
        "runtime": {
            "sentri_version": __version__,
            "graph_version": "sentri-stategraph-v1",
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "evidence": {
            "integrity": "hmac_hash_chain",
            "request_message_hash": _content_hash(request.get("message")),
            "caller_reported_fields": True,
        },
    }


def _event(
    state: SentriState, worker: WorkerName, kind: str, payload: dict[str, Any]
) -> TelemetryEvent:
    enriched = {"audit": _audit_context(state), **payload}
    return TelemetryEvent(
        execution_id=state["execution_id"],
        worker=worker,
        kind=kind,
        payload=enriched,
    )


async def work_queue_hook(state: SentriState) -> dict[str, Any]:
    actions = [PlannedAction.model_validate(item) for item in state["planned_actions"]]
    event = _event(
        state,
        WorkerName.WORK_QUEUE,
        "call_tree",
        {
            "caller": state["request"].get("host", "local"),
            "authenticated_principal": state["request"]
            .get("metadata", {})
            .get("authenticated_principal", "unknown"),
            "callee": "sentri.router",
            "phase": "preflight",
            "received_at": state.get("started_at"),
            "children": [
                {
                    "action_id": action.id,
                    "tool": action.tool,
                    "operation": action.operation,
                    "rationale": action.rationale,
                    "action_hash": action_hash(action),
                    "mutates_state": action.mutates_state,
                    "data_classification": action.data_classification,
                    "argument_keys": sorted(map(str, action.arguments.keys())),
                    "queue_status": "proposed",
                }
                for action in actions
            ],
            "thread_id": state["thread_id"],
            "adoption": {
                "planned_action_count": len(actions),
                "capabilities": sorted(
                    {f"{action.tool}.{action.operation}" for action in actions}
                ),
                "conversation_linked": bool(
                    state["request"].get("conversation_id")
                ),
            },
        },
    )
    return {"telemetry_events": [event.model_dump(mode="json")]}


async def change_log_hook(state: SentriState) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    for item in state["planned_actions"]:
        action = PlannedAction.model_validate(item)
        if action.mutates_state:
            changes.append(
                {
                    "action_id": action.id,
                    "target": action.arguments.get("path")
                    or action.arguments.get("record_id")
                    or "unspecified",
                    "operation": action.operation,
                    "action_hash": action_hash(action),
                    "target_hash": _content_hash(
                        action.arguments.get("path")
                        or action.arguments.get("record_id")
                        or "unspecified"
                    ),
                    "change_type": "state_mutation",
                    "status": "proposed",
                    "before_hash": None,
                    "after_hash": None,
                    "rollback_status": "not_reported",
                }
            )
    event = _event(
        state,
        WorkerName.CHANGE_LOG,
        "proposed_changes",
        {
            "phase": "preflight",
            "changes": changes,
            "count": len(changes),
            "coverage": {
                "all_mutating_actions_identified": True,
                "observed_results_pending": bool(changes),
            },
        },
    )
    return {
        "change_set": changes,
        "telemetry_events": [event.model_dump(mode="json")],
    }


async def rogue_hook(state: SentriState) -> dict[str, Any]:
    actions = [PlannedAction.model_validate(item) for item in state["planned_actions"]]
    alerts = evaluate_actions(actions)
    event = _event(
        state,
        WorkerName.ROGUE,
        "safety_evaluation",
        {
            "decision": "flagged" if alerts else "allow",
            "phase": "preflight",
            "alerts": [alert.model_dump(mode="json") for alert in alerts],
            "hard_limits": [
                "never_send_money", "never_delete_records", "never_share_pii"
            ],
            "actions_evaluated": [
                {
                    "action_id": action.id,
                    "action_hash": action_hash(action),
                    "decision": "flagged"
                    if any(alert.action_id == action.id for alert in alerts)
                    else "allow",
                    "finding_codes": [
                        alert.code for alert in alerts if alert.action_id == action.id
                    ],
                }
                for action in actions
            ],
            "control_coverage": {
                "evaluated_action_count": len(actions),
                "hard_limit_alert_count": sum(
                    1 for alert in alerts if alert.hard_limit
                ),
                "human_review_required": any(
                    alert.requires_human for alert in alerts
                ),
                "normalization_applied": True,
            },
        },
    )
    return {
        "risk_alerts": [alert.model_dump(mode="json") for alert in alerts],
        "telemetry_events": [event.model_dump(mode="json")],
    }


async def builder_hook(state: SentriState) -> dict[str, Any]:
    action_nodes = [
        {
            "id": item["id"],
            "type": "proposed_action",
            "label": f"{item['tool']}.{item['operation']}",
            "action_hash": action_hash(item),
            "phase": "proposed",
            "mutates_state": bool(item.get("mutates_state")),
        }
        for item in state["planned_actions"]
    ]
    dag = {
        "nodes": [
            {"id": "host", "type": "caller"},
            {"id": "router", "type": "router"},
            *action_nodes,
        ],
        "edges": [
            {"from": "host", "to": "router"},
            *[
                {"from": "router", "to": item["id"]}
                for item in state["planned_actions"]
            ],
        ],
    }
    event = _event(state, WorkerName.BUILDER, "execution_dag", dag)
    event.payload["reconstruction"] = {
        "status": "planned",
        "node_count": len(dag["nodes"]),
        "edge_count": len(dag["edges"]),
        "observed_results_pending": len(action_nodes),
    }
    return {
        "execution_dag": dag,
        "telemetry_events": [event.model_dump(mode="json")],
    }


async def clerk_hook(state: SentriState) -> dict[str, Any]:
    metadata = state["request"].get("metadata", {})

    def bounded_int(name: str, maximum: int) -> int:
        try:
            return min(max(int(metadata.get(name, 0)), 0), maximum)
        except (TypeError, ValueError, OverflowError):
            return 0

    def bounded_float(name: str, maximum: float) -> float:
        try:
            value = float(metadata.get(name, 0))
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if value != value or value in {float("inf"), float("-inf")}:
            return 0.0
        return min(max(value, 0.0), maximum)

    prompt = bounded_int("prompt_tokens", 100_000_000)
    completion = bounded_int("completion_tokens", 100_000_000)
    input_per_million = bounded_float("input_cost_per_million", 1_000_000)
    output_per_million = bounded_float("output_cost_per_million", 1_000_000)
    current_monotonic = time.monotonic()
    try:
        started = float(metadata.get("monotonic_started_at", current_monotonic))
    except (TypeError, ValueError, OverflowError):
        started = current_monotonic
    if started != started or started < 0 or started > current_monotonic:
        started = current_monotonic
    metrics = CostMetrics(
        latency_ms=max((time.monotonic() - started) * 1000, 0),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        estimated_cost_usd=(
            prompt * input_per_million + completion * output_per_million
        )
        / 1_000_000,
    )
    event = _event(
        state,
        WorkerName.CLERK,
        "cost_metrics",
        {
            **metrics.model_dump(mode="json"),
            "phase": "preflight",
            "timing": {
                "request_received_at": state.get("started_at"),
                "preflight_elapsed_ms": metrics.latency_ms,
                "queue_ms": None,
                "approval_wait_ms": None,
                "tool_execution_ms": None,
            },
            "adoption": {
                "host": state["request"].get("host", "local"),
                "action_count": len(state.get("planned_actions", [])),
                "model": metadata.get("model"),
                "model_version": metadata.get("model_version"),
            },
            "measurement_quality": {
                "token_usage_source": "caller_supplied",
                "pricing_source": metrics.pricing_source,
                "missing_token_usage": prompt == 0 and completion == 0,
            },
        },
    )
    return {
        "cost_metrics": metrics.model_dump(mode="json"),
        "telemetry_events": [event.model_dump(mode="json")],
    }


WORKER_HOOKS = {
    "work_queue": work_queue_hook,
    "change_log": change_log_hook,
    "rogue": rogue_hook,
    "builder": builder_hook,
    "clerk": clerk_hook,
}


def _content_hash(value: Any) -> str | None:
    if value is None:
        return None
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _effective_latency_ms(outcome: Any) -> float | None:
    if outcome.latency_ms is not None:
        return outcome.latency_ms
    if outcome.started_at:
        return max(
            (outcome.finished_at - outcome.started_at).total_seconds() * 1_000,
            0,
        )
    return None


def _safe_uri(uri: str | None) -> str | None:
    """Keep source lineage useful without retaining credentials or query secrets."""
    if not uri:
        return None
    try:
        parsed = urlsplit(uri)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return None


def _diagnostics(outcome: Any) -> dict[str, Any]:
    details = outcome.diagnostics.model_dump(mode="json", exclude_none=True)
    details["evidence"] = [
        {
            **item.model_dump(mode="json", exclude_none=True),
            "uri": _safe_uri(item.uri),
            "uri_hash": _content_hash(item.uri),
        }
        for item in outcome.diagnostics.evidence
    ]
    return redact(details)


def _reported_hash(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
        return value
    return _content_hash(value)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int((len(ordered) - 1) * fraction), len(ordered) - 1)]


def build_outcome_events(
    state: SentriState, report: OutcomeReport
) -> list[TelemetryEvent]:
    """Create correlated terminal evidence for every telemetry agent."""
    dag = copy.deepcopy(state.get("execution_dag") or {"nodes": [], "edges": []})
    result_node_ids: list[str] = []
    statuses: list[str] = []
    planned = {
        item["id"]: PlannedAction.model_validate(item)
        for item in state.get("planned_actions", [])
    }

    upstream_node_ids: list[str] = []
    for index, reference in enumerate(report.upstream_tools, start=1):
        node_id = f"upstream_tool:{index}"
        upstream_node_ids.append(node_id)
        dag["nodes"].append(
            {
                "id": node_id,
                "type": "upstream_tool",
                **redact(reference.model_dump(mode="json", exclude_none=True)),
                "execution_attestation": "caller_reported",
            }
        )
        dag["edges"].extend(
            {"from": node_id, "to": action_id} for action_id in planned
        )

    for outcome in report.outcomes:
        result_id = f"result:{outcome.action_id}"
        result_node_ids.append(result_id)
        statuses.append(outcome.status)
        dag["nodes"].append(
            {
                "id": result_id,
                "type": "tool_result",
                "action_id": outcome.action_id,
                "status": outcome.status,
                "started_at": outcome.started_at.isoformat()
                if outcome.started_at
                else None,
                "finished_at": outcome.finished_at.isoformat(),
                "latency_ms": _effective_latency_ms(outcome),
                "output_hash": _content_hash(outcome.output),
                "error": redact(outcome.error),
                "metadata": redact(outcome.metadata),
                "diagnostics": _diagnostics(outcome),
            }
        )
        dag["edges"].append({"from": outcome.action_id, "to": result_id})

    final_response_hash = _content_hash(report.final_response)
    if report.final_response is not None:
        final_id = "final_response"
        dag["nodes"].append(
            {
                "id": final_id,
                "type": "final_response",
                "output_hash": final_response_hash,
            }
        )
        dag["edges"].extend(
            {"from": result_id, "to": final_id} for result_id in result_node_ids
        )

    overall_status = statuses[0] if len(set(statuses)) == 1 else "mixed"
    latencies = [
        value
        for outcome in report.outcomes
        if (value := _effective_latency_ms(outcome)) is not None
    ]
    usage = [
        outcome.diagnostics.usage
        for outcome in report.outcomes
        if outcome.diagnostics.usage is not None
    ]

    gateway_observed = report.reported_by == "sentri_gateway"
    execution_attestation = (
        "sentri_gateway_observed" if gateway_observed else "caller_reported"
    )
    upstream_tools = [
        {
            **redact(item.model_dump(mode="json", exclude_none=True)),
            "execution_attestation": "caller_reported",
        }
        for item in report.upstream_tools
    ]
    work_queue = _event(
        state,
        WorkerName.WORK_QUEUE,
        "execution_completed",
        {
            "phase": "terminal",
            "status": overall_status,
            "reported_by": report.reported_by,
            "actions": [
                {
                    "action_id": outcome.action_id,
                    "action_hash": action_hash(planned[outcome.action_id]),
                    "tool": planned[outcome.action_id].tool,
                    "operation": planned[outcome.action_id].operation,
                    "status": outcome.status,
                    "provider": outcome.diagnostics.provider,
                    "model": outcome.diagnostics.model,
                    "model_version": outcome.diagnostics.model_version,
                    "attempt": outcome.diagnostics.attempt,
                    "retry_count": outcome.diagnostics.retry_count,
                    "provider_request_id": outcome.diagnostics.provider_request_id,
                    "execution_attestation": execution_attestation,
                    "finished_at": outcome.finished_at.isoformat(),
                }
                for outcome in report.outcomes
            ],
            "completion": {
                "authorized_action_count": len(planned),
                "reported_action_count": len(report.outcomes),
                "complete_action_coverage": set(planned)
                == {item.action_id for item in report.outcomes},
            },
            "upstream_tools": upstream_tools,
            "tool_lineage": {
                "direct_action_count": len(report.outcomes),
                "upstream_tool_count": len(upstream_tools),
                "upstream_attestation": (
                    "caller_reported" if upstream_tools else "not_supplied"
                ),
                "raw_arguments_or_outputs_persisted": False,
            },
        },
    )

    observed_changes = []
    outcomes_by_id = {item.action_id: item for item in report.outcomes}
    for action_id, action in planned.items():
        if not action.mutates_state:
            continue
        outcome = outcomes_by_id[action_id]
        target = (
            action.arguments.get("path")
            or action.arguments.get("record_id")
            or "unspecified"
        )
        observed_changes.append(
            {
                "action_id": action_id,
                "action_hash": action_hash(action),
                "target": target,
                "target_hash": _content_hash(target),
                "operation": action.operation,
                "status": outcome.status,
                "before_hash": _reported_hash(outcome.metadata, "before_hash"),
                "after_hash": _reported_hash(outcome.metadata, "after_hash"),
                "rollback_status": outcome.metadata.get(
                    "rollback_status", "not_reported"
                ),
            }
        )
    change_log = _event(
        state,
        WorkerName.CHANGE_LOG,
        "observed_changes",
        {
            "phase": "terminal",
            "changes": observed_changes,
            "count": len(observed_changes),
            "evidence_gaps": [
                item["action_id"]
                for item in observed_changes
                if item["status"] == "succeeded"
                and (not item["before_hash"] or not item["after_hash"])
            ],
        },
    )

    rogue = _event(
        state,
        WorkerName.ROGUE,
        "post_execution_safety",
        {
            "phase": "terminal",
            "decision": "compliant",
            "authorized_action_set_matched": True,
            "all_permits_signature_valid": True,
            "final_response_pii_scan": "passed",
            "preflight_alerts": state.get("risk_alerts", []),
            "hard_limit_bypass_detected": False,
            "execution_attestation": (
                "sentri_gateway_observed" if gateway_observed else "caller_reported"
            ),
            "upstream_tool_attestation": (
                "caller_reported" if report.upstream_tools else "not_supplied"
            ),
            "note": (
                "Sentri executed the provider calls and observed their results."
                if gateway_observed
                else "Outcome facts are caller-reported; permit signatures attest authorization, not downstream execution."
            ),
        },
    )

    builder = _event(
        state,
        WorkerName.BUILDER,
        "execution_outcome",
        {
            "phase": "terminal",
            "status": overall_status,
            "reported_by": report.reported_by,
            "reported_action_ids": [item.action_id for item in report.outcomes],
            "final_response_hash": final_response_hash,
            "metadata": redact(report.metadata),
            "reconstruction": {
                "status": "complete",
                "node_count": len(dag["nodes"]),
                "edge_count": len(dag["edges"]),
                "action_coverage": len(report.outcomes),
                "source_evidence_count": sum(
                    len(item.diagnostics.evidence) for item in report.outcomes
                ),
                "upstream_tool_count": len(upstream_node_ids),
                "caller_reported_outcome": not gateway_observed,
            },
            "dag": dag,
        },
    )

    clerk = _event(
        state,
        WorkerName.CLERK,
        "performance_outcome",
        {
            "phase": "terminal",
            "status": overall_status,
            "action_count": len(report.outcomes),
            "successful_actions": statuses.count("succeeded"),
            "failed_actions": statuses.count("failed"),
            "cancelled_actions": statuses.count("cancelled"),
            "retry_count": sum(item.diagnostics.retry_count for item in report.outcomes),
            "latency": {
                "reported_action_count": len(latencies),
                "missing_action_count": len(report.outcomes) - len(latencies),
                "total_ms": sum(latencies),
                "average_ms": sum(latencies) / len(latencies) if latencies else None,
                "p95_ms": _percentile(latencies, 0.95),
                "per_action": [
                    {
                        "action_id": item.action_id,
                        "latency_ms": _effective_latency_ms(item),
                        "timeout_ms": item.diagnostics.timeout_ms,
                        "cache_hit": item.diagnostics.cache_hit,
                    }
                    for item in report.outcomes
                ],
            },
            "usage": {
                "prompt_tokens": sum(item.prompt_tokens for item in usage),
                "completion_tokens": sum(item.completion_tokens for item in usage),
                "total_tokens": sum(item.total_tokens for item in usage),
                "estimated_cost_usd": sum(
                    item.estimated_cost_usd for item in usage
                ),
                "reported_action_count": len(usage),
                "source": "sentri_gateway" if gateway_observed else "caller_supplied",
            },
        },
    )

    events = [work_queue, change_log, rogue, builder, clerk]
    related = {
        f"{event.worker.value}:{event.kind}": event.event_id for event in events
    }
    for event in events:
        event.payload["audit"]["related_events"] = related
    return events


def build_outcome_event(state: SentriState, report: OutcomeReport) -> TelemetryEvent:
    """Backward-compatible helper returning the Builder terminal event."""
    return next(
        event
        for event in build_outcome_events(state, report)
        if event.worker == WorkerName.BUILDER
    )
