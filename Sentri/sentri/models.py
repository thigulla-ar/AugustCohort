from __future__ import annotations

import operator
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sentri.config import StorageMode


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerName(StrEnum):
    WORK_QUEUE = "work_queue"
    CHANGE_LOG = "change_log"
    ROGUE = "rogue"
    BUILDER = "builder"
    CLERK = "clerk"


class PlannedAction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    tool: str = Field(
        min_length=1, max_length=256, description="Exact downstream tool or API name"
    )
    operation: str = Field(
        min_length=1,
        max_length=256,
        description="Operation the tool is expected to perform",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="Requested by the host conversation", max_length=2_000)
    mutates_state: bool = False
    data_classification: list[str] = Field(
        default_factory=list, max_length=32,
        description="Data labels such as public, internal, sensitive, or pii",
    )

    @model_validator(mode="after")
    def limit_arguments(self) -> "PlannedAction":
        encoded = json.dumps(self.arguments, default=str, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 128_000:
            raise ValueError("action arguments exceed 128 KB")
        return self


class InteractRequest(BaseModel):
    message: str = Field(
        min_length=1, max_length=20_000, description="User request from the host chat"
    )
    conversation_id: str | None = Field(default=None, max_length=256)
    host: Literal["chatgpt", "gemini", "local"] = "local"
    actions: list[PlannedAction] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def limit_metadata(self) -> "InteractRequest":
        encoded = json.dumps(self.metadata, default=str, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 128_000:
            raise ValueError("request metadata exceeds 128 KB")
        return self


class RiskAlert(BaseModel):
    code: str
    severity: Literal["low", "medium", "high", "critical"]
    message: str
    action_id: str | None = None
    hard_limit: bool = False
    requires_human: bool = False
    detected_at: datetime = Field(default_factory=utc_now)


class CostMetrics(BaseModel):
    latency_ms: float = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0, ge=0)
    pricing_source: str = "caller_supplied"


class EvidenceReference(BaseModel):
    """Privacy-conscious provenance supplied by the executing host."""

    source_id: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=1_000)
    uri: str | None = Field(
        default=None,
        max_length=4_096,
        description="Source URI. Query strings and fragments are removed before storage.",
    )
    retrieved_at: datetime | None = None
    published_at: datetime | None = None
    content_hash: str | None = Field(default=None, max_length=256)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)


class ExecutionDiagnostics(BaseModel):
    """Bounded, caller-reported diagnostics; secrets and raw payloads do not belong here."""

    provider: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=256)
    tool_version: str | None = Field(default=None, max_length=256)
    provider_request_id: str | None = Field(default=None, max_length=512)
    attempt: int = Field(default=1, ge=1, le=100)
    retry_count: int = Field(default=0, ge=0, le=100)
    timeout_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    http_status: int | None = Field(default=None, ge=100, le=599)
    cache_hit: bool | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=200)
    calculation_steps: list[str] = Field(default_factory=list, max_length=100)
    state_transitions: list[str] = Field(default_factory=list, max_length=100)
    usage: CostMetrics | None = None


class TelemetryEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    worker: WorkerName
    kind: str
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int | None = Field(default=None, ge=1)
    previous_hash: str | None = None
    integrity_hash: str | None = None


class AuditResponse(BaseModel):
    execution_id: str | None = None
    events: list[TelemetryEvent]


class StorageSettingsUpdate(BaseModel):
    storage_mode: StorageMode
    retention_days: int | None = Field(
        default=30,
        ge=1,
        description="Days to retain telemetry, or null for Forever.",
    )


class StorageSettingsResponse(BaseModel):
    storage_mode: StorageMode
    retention_days: int | Literal["Forever"]
    data_dir: str
    active_path: str
    purged_items: int = 0
    message: str


class InteractResponse(BaseModel):
    execution_id: str
    thread_id: str
    status: Literal[
        "completed", "approval_required", "blocked", "error"
    ]
    result: dict[str, Any] | None = None
    alerts: list[RiskAlert] = Field(default_factory=list)
    cost: CostMetrics = Field(default_factory=CostMetrics)
    approval_url: str | None = None
    message: str | None = None


class ApprovalRequest(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2_000)


class PermitVerificationRequest(BaseModel):
    permit: str = Field(min_length=32, max_length=8_192)
    action: PlannedAction
    consume: bool = Field(
        default=True,
        description="Consume the single-use permit before executing the action.",
    )


class PermitVerificationResponse(BaseModel):
    valid: bool
    consumed: bool
    claims: dict[str, Any]
    message: str


class GatewayAuthorization(BaseModel):
    action: PlannedAction
    permit: str = Field(min_length=32, max_length=8_192)


class UpstreamToolReference(BaseModel):
    """Caller-observed tool lineage without retaining raw arguments or output."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=256)
    operation: str = Field(min_length=1, max_length=256)
    status: Literal["succeeded", "failed", "cancelled", "unknown"] = "unknown"
    provider_request_id: str | None = Field(default=None, max_length=512)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class GatewayExecutionRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    authorizations: list[GatewayAuthorization] = Field(min_length=1, max_length=50)
    upstream_tools: list[UpstreamToolReference] = Field(
        default_factory=list,
        max_length=100,
        description=(
            "Optional caller-observed tools that produced context for this gateway call. "
            "Raw arguments and outputs are intentionally excluded."
        ),
    )

    @model_validator(mode="after")
    def unique_gateway_actions(self) -> "GatewayExecutionRequest":
        action_ids = [item.action.id for item in self.authorizations]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("gateway authorizations must contain unique action IDs")
        return self


class ActionOutcome(BaseModel):
    action_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "cancelled"]
    output: Any | None = Field(
        default=None,
        description="Tool output used to calculate a hash; raw output is not persisted.",
    )
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime = Field(default_factory=utc_now)
    latency_ms: float | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    diagnostics: ExecutionDiagnostics = Field(default_factory=ExecutionDiagnostics)
    permit: str = Field(
        min_length=32,
        max_length=8_192,
        description="Signed Sentri permit returned for this exact action.",
    )

    @model_validator(mode="after")
    def validate_timestamps(self) -> "ActionOutcome":
        if self.started_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        return self


class OutcomeReport(BaseModel):
    execution_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    outcomes: list[ActionOutcome] = Field(min_length=1)
    final_response: Any | None = Field(
        default=None,
        description="Host response used to calculate a hash; raw content is not persisted.",
    )
    reported_by: Literal[
        "chatgpt", "gemini", "local", "external", "sentri_gateway"
    ] = "external"
    upstream_tools: list[UpstreamToolReference] = Field(
        default_factory=list, max_length=100
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_action_ids(self) -> "OutcomeReport":
        action_ids = [outcome.action_id for outcome in self.outcomes]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("outcomes must contain unique action IDs")
        encoded = json.dumps(
            {
                "outcomes": [item.model_dump(mode="json") for item in self.outcomes],
                "final_response": self.final_response,
                "upstream_tools": [
                    item.model_dump(mode="json") for item in self.upstream_tools
                ],
                "metadata": self.metadata,
            },
            default=str,
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > 512_000:
            raise ValueError("outcome report exceeds 512 KB")
        return self


class OutcomeResponse(BaseModel):
    execution_id: str
    thread_id: str
    status: Literal["succeeded", "failed", "cancelled", "mixed"]
    event_id: str
    completed_dag: dict[str, Any]
    message: str


class GatewayActionResult(BaseModel):
    action_id: str
    status: Literal["succeeded", "failed", "cancelled"]
    output: Any | None = None
    error: str | None = None
    usage: CostMetrics = Field(default_factory=CostMetrics)
    provider: str | None = None
    model: str | None = None
    provider_request_id: str | None = None


class GatewayExecutionResponse(BaseModel):
    execution_id: str
    thread_id: str
    status: Literal["succeeded", "failed", "cancelled", "mixed"]
    results: list[GatewayActionResult]
    outcome_event_id: str
    message: str


class SentriState(TypedDict, total=False):
    execution_id: str
    thread_id: str
    request: dict[str, Any]
    messages: Annotated[list[dict[str, Any]], operator.add]
    planned_actions: list[dict[str, Any]]
    telemetry_events: Annotated[list[dict[str, Any]], operator.add]
    risk_alerts: Annotated[list[dict[str, Any]], operator.add]
    cost_metrics: dict[str, Any]
    change_set: list[dict[str, Any]]
    execution_dag: dict[str, Any]
    started_at: str
    approval: dict[str, Any] | None
    final_result: dict[str, Any] | None
    status: str
