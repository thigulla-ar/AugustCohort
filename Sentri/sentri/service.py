from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sentri.config import Settings, StorageMode
from sentri.graph import SentriGraph, initial_state
from sentri.gateway import (
    ExecutionGateway,
    GatewayDisabledError,
    GatewayRequestError,
    ProviderCallError,
)
from sentri.models import (
    ActionOutcome,
    ApprovalRequest,
    CostMetrics,
    ExecutionDiagnostics,
    GatewayActionResult,
    GatewayExecutionRequest,
    GatewayExecutionResponse,
    InteractRequest,
    InteractResponse,
    OutcomeReport,
    OutcomeResponse,
    PermitVerificationRequest,
    PermitVerificationResponse,
    PlannedAction,
    RiskAlert,
    TelemetryEvent,
    WorkerName,
)
from sentri.redaction import configure_redaction_key, pii_types
from sentri.security import PermitError, PermitSigner, load_or_create_local_secret
from sentri.storage import SentriStorageEngine
from sentri.telemetry import build_outcome_events


class ExecutionNotFound(LookupError):
    pass


class OutcomeConflict(RuntimeError):
    pass


class SentriService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not self.settings.signing_secret:
            self.settings.signing_secret = load_or_create_local_secret(
                self.settings.data_dir
            )
        configure_redaction_key(self.settings.signing_secret)
        self.permits = PermitSigner(
            self.settings.signing_secret, self.settings.permit_ttl_seconds
        )
        self.storage = SentriStorageEngine(settings)
        self.graph = SentriGraph(self.storage)
        self.gateway = ExecutionGateway(settings)
        self._outcome_lock = asyncio.Lock()
        self._permit_lock = asyncio.Lock()
        self._consumed_permits: set[str] = set()
        self._gateway_inflight: set[str] = set()

    async def initialize(self) -> None:
        await self.storage.initialize()

    async def close(self) -> None:
        await self.gateway.close()
        await self.storage.close()

    def storage_settings(
        self, *, purged_items: int = 0, message: str = "Storage settings loaded."
    ) -> dict[str, Any]:
        return {
            "storage_mode": self.settings.storage_mode,
            "retention_days": self.settings.retention_days or "Forever",
            "data_dir": str(self.settings.data_dir),
            "active_path": self.storage.active_path(),
            "purged_items": purged_items,
            "message": message,
        }

    async def configure_storage(
        self,
        storage_mode: StorageMode,
        retention_days: int | None,
        actor: str = "local-operator",
    ) -> dict[str, Any]:
        changed_mode = storage_mode != self.settings.storage_mode
        audit_execution = f"admin:{uuid4()}"
        await self.storage.record(
            TelemetryEvent(
                execution_id=audit_execution,
                worker=WorkerName.CHANGE_LOG,
                kind="storage_reconfiguration_requested",
                payload={
                    "actor": actor,
                    "from_mode": self.settings.storage_mode,
                    "to_mode": storage_mode,
                    "from_retention_days": self.settings.retention_days,
                    "to_retention_days": retention_days,
                },
            )
        )
        purged = await self.storage.reconfigure(storage_mode, retention_days)
        await asyncio.to_thread(self.settings.persist_runtime_settings)
        await self.storage.record(
            TelemetryEvent(
                execution_id=audit_execution,
                worker=WorkerName.CHANGE_LOG,
                kind="storage_reconfiguration_applied",
                payload={
                    "actor": actor,
                    "storage_mode": storage_mode,
                    "retention_days": retention_days,
                    "purged_items": purged,
                },
            )
        )
        mode_note = (
            " Active storage changed; existing data in the previous backend was retained."
            if changed_mode
            else ""
        )
        return self.storage_settings(
            purged_items=purged,
            message=f"Storage settings saved.{mode_note}",
        )

    async def dashboard_snapshot(self, limit: int = 500) -> dict[str, Any]:
        events = await self.storage.query(limit=min(max(limit, 1), 5_000))
        alerts = 0
        tokens = 0
        cost = 0.0
        executions: set[str] = set()
        completed: set[str] = set()
        capabilities: set[str] = set()
        hosts: set[str] = set()
        action_latencies: list[float] = []
        for event in events:
            if event.kind == "call_tree":
                executions.add(event.execution_id)
                adoption = event.payload.get("adoption") or {}
                capabilities.update(adoption.get("capabilities") or [])
                host = (event.payload.get("audit") or {}).get("origin", {}).get("host")
                if host:
                    hosts.add(str(host))
            if event.kind == "execution_outcome":
                completed.add(event.execution_id)
            if event.worker.value == "rogue":
                alerts += len(event.payload.get("alerts") or [])
            if event.worker.value == "clerk":
                usage = event.payload.get("usage")
                usage_payload = usage if isinstance(usage, dict) else event.payload
                tokens += int(usage_payload.get("total_tokens") or 0)
                cost += float(usage_payload.get("estimated_cost_usd") or 0)
                if event.kind == "performance_outcome":
                    for item in (event.payload.get("latency") or {}).get("per_action", []):
                        value = item.get("latency_ms")
                        if isinstance(value, (int, float)):
                            action_latencies.append(float(value))
        ordered_latencies = sorted(action_latencies)
        p95 = (
            ordered_latencies[int((len(ordered_latencies) - 1) * 0.95)]
            if ordered_latencies
            else None
        )
        return {
            "events": [event.model_dump(mode="json") for event in events],
            "summary": {
                "events": len(events),
                "alerts": alerts,
                "tokens": tokens,
                "cost_usd": round(cost, 8),
                "adoption": {
                    "executions": len(executions),
                    "completed_executions": len(completed),
                    "completion_rate": round(len(completed) / len(executions), 4)
                    if executions
                    else 0,
                    "hosts": sorted(hosts),
                    "capabilities": sorted(capabilities),
                },
                "performance": {
                    "observed_actions": len(action_latencies),
                    "average_action_latency_ms": round(
                        sum(action_latencies) / len(action_latencies), 3
                    )
                    if action_latencies
                    else None,
                    "p95_action_latency_ms": p95,
                },
            },
            "storage": self.storage_settings(),
        }

    async def interact(
        self, request: InteractRequest, principal: str = "local-loopback"
    ) -> InteractResponse:
        execution_id = str(uuid4())
        # A conversation may contain many turns. Each interrupt needs an isolated
        # checkpoint namespace so telemetry and risk state cannot bleed across turns.
        thread_id = str(uuid4())
        payload = request.model_dump(mode="json")
        payload["metadata"]["authenticated_principal"] = principal
        payload["metadata"]["monotonic_started_at"] = time.monotonic()
        await self.graph.run(
            initial_state(execution_id, thread_id, payload), thread_id
        )
        snapshot = await self.graph.snapshot(thread_id)
        return self._response(snapshot.values, bool(snapshot.next))

    async def approve(
        self, thread_id: str, approval: ApprovalRequest
    ) -> InteractResponse:
        try:
            snapshot = await self.graph.snapshot(thread_id)
        except Exception as exc:
            raise ExecutionNotFound(thread_id) from exc
        if not snapshot.values or not snapshot.next:
            raise ExecutionNotFound(thread_id)
        decision = approval.model_dump(mode="json")
        await self.graph.resume(thread_id, decision)
        finished = await self.graph.snapshot(thread_id)
        return self._response(finished.values, bool(finished.next))

    async def report_outcome(
        self, report: OutcomeReport, *, internal_gateway: bool = False
    ) -> OutcomeResponse:
        try:
            snapshot = await self.graph.snapshot(report.thread_id)
        except Exception as exc:
            raise ExecutionNotFound(report.execution_id) from exc
        state = snapshot.values
        if not state or state.get("execution_id") != report.execution_id:
            raise ExecutionNotFound(report.execution_id)
        if snapshot.next:
            raise OutcomeConflict("The execution is still awaiting human review.")

        final_result = state.get("final_result") or {}
        if state.get("status") != "completed" or not final_result.get("authorized"):
            raise OutcomeConflict("Only an authorized execution can report outcomes.")

        authorized_ids = set(final_result.get("action_ids", []))
        reported_ids = {item.action_id for item in report.outcomes}
        if reported_ids != authorized_ids:
            missing = sorted(authorized_ids - reported_ids)
            unknown = sorted(reported_ids - authorized_ids)
            details = []
            if missing:
                details.append(f"missing action IDs: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown action IDs: {', '.join(unknown)}")
            raise OutcomeConflict("Outcome set does not match authorization; " + "; ".join(details))

        planned = {
            item["id"]: PlannedAction.model_validate(item)
            for item in state.get("planned_actions", [])
        }
        for outcome in report.outcomes:
            try:
                self.permits.verify(
                    outcome.permit,
                    planned[outcome.action_id],
                    execution_id=report.execution_id,
                    thread_id=report.thread_id,
                    allow_expired=True,
                )
            except (KeyError, PermitError) as exc:
                raise OutcomeConflict(
                    f"Outcome permit is invalid for action {outcome.action_id}."
                ) from exc

        final_pii = pii_types(
            json.dumps(report.final_response, default=str, ensure_ascii=False)
        )
        if final_pii:
            raise OutcomeConflict(
                "The reported final response contains unhashed personal information "
                f"({', '.join(final_pii)}). Do not transmit it."
            )

        async with self._outcome_lock:
            if report.execution_id in self._gateway_inflight and not internal_gateway:
                raise OutcomeConflict(
                    "Sentri's execution gateway is currently executing this workflow."
                )
            existing = await self.storage.query(report.execution_id, limit=5_000)
            if any(event.kind == "execution_outcome" for event in existing):
                raise OutcomeConflict("An outcome is already recorded for this execution.")
            terminal_events = build_outcome_events(state, report)
            await self.storage.record_many(terminal_events)
            event = next(
                item
                for item in terminal_events
                if item.worker == WorkerName.BUILDER
            )

        payload = event.payload
        return OutcomeResponse(
            execution_id=report.execution_id,
            thread_id=report.thread_id,
            status=payload["status"],
            event_id=event.event_id,
            completed_dag=payload["dag"],
            message="Downstream workflow outcome recorded by the Builder agent.",
        )

    async def execute_gateway(
        self,
        request: GatewayExecutionRequest,
        principal: str = "local-loopback",
    ) -> GatewayExecutionResponse:
        """Execute only preflight-authorized provider calls and attest observed usage."""
        if not self.settings.gateway_enabled:
            raise GatewayDisabledError("The Sentri execution gateway is disabled.")
        try:
            snapshot = await self.graph.snapshot(request.thread_id)
        except Exception as exc:
            raise ExecutionNotFound(request.execution_id) from exc
        state = snapshot.values
        if not state or state.get("execution_id") != request.execution_id:
            raise ExecutionNotFound(request.execution_id)
        if snapshot.next:
            raise OutcomeConflict("The execution is still awaiting human review.")
        final_result = state.get("final_result") or {}
        if state.get("status") != "completed" or not final_result.get("authorized"):
            raise OutcomeConflict("Only an authorized execution can enter the gateway.")

        planned = {
            item["id"]: PlannedAction.model_validate(item)
            for item in state.get("planned_actions", [])
        }
        submitted = {item.action.id: item for item in request.authorizations}
        authorized_ids = set(final_result.get("action_ids", []))
        if set(submitted) != authorized_ids:
            raise OutcomeConflict(
                "Gateway action set must exactly match the authorized action set."
            )

        # Validate every action and permit before any provider call or permit consumption.
        for action_id, authorization in submitted.items():
            expected = planned.get(action_id)
            if expected is None or authorization.action != expected:
                raise GatewayRequestError(
                    f"Action {action_id!r} does not exactly match the preflight plan."
                )
            self.gateway.adapter_for(authorization.action)
            try:
                self.permits.verify(
                    authorization.permit,
                    expected,
                    execution_id=request.execution_id,
                    thread_id=request.thread_id,
                )
            except PermitError as exc:
                raise GatewayRequestError(
                    f"Permit validation failed for action {action_id!r}."
                ) from exc

        async with self._outcome_lock:
            existing = await self.storage.query(request.execution_id, limit=5_000)
            if any(event.kind == "execution_outcome" for event in existing):
                raise OutcomeConflict("An outcome is already recorded for this execution.")
            if request.execution_id in self._gateway_inflight:
                raise OutcomeConflict("This execution is already running in the gateway.")
            self._gateway_inflight.add(request.execution_id)

        outcomes: list[ActionOutcome] = []
        results: list[GatewayActionResult] = []
        ordered_ids = [item.action.id for item in request.authorizations]
        abort_remaining = False
        try:
            for action_id in ordered_ids:
                authorization = submitted[action_id]
                if abort_remaining:
                    outcomes.append(
                        ActionOutcome(
                            action_id=action_id,
                            status="cancelled",
                            error="Cancelled because an earlier gateway action failed.",
                            permit=authorization.permit,
                            diagnostics=ExecutionDiagnostics(
                                state_transitions=["authorized", "cancelled_before_execution"]
                            ),
                        )
                    )
                    results.append(
                        GatewayActionResult(
                            action_id=action_id,
                            status="cancelled",
                            error="Cancelled because an earlier gateway action failed.",
                        )
                    )
                    continue

                started_at = datetime.now(timezone.utc)
                monotonic_started = time.monotonic()
                try:
                    await self.verify_permit(
                        PermitVerificationRequest(
                            permit=authorization.permit,
                            action=authorization.action,
                            consume=True,
                        )
                    )
                    provider_result = await self.gateway.execute(authorization.action)
                    latency_ms = max((time.monotonic() - monotonic_started) * 1_000, 0)
                    usage = self.gateway.cost_metrics(provider_result, latency_ms)
                    diagnostics = ExecutionDiagnostics(
                        provider=provider_result.provider,
                        model=provider_result.model,
                        model_version=provider_result.model_version,
                        provider_request_id=provider_result.provider_request_id,
                        http_status=provider_result.http_status,
                        cache_hit=provider_result.cached_tokens > 0,
                        state_transitions=[
                            "authorized",
                            "permit_consumed",
                            "provider_request_sent",
                            "provider_response_observed",
                        ],
                        usage=usage,
                    )
                    outcomes.append(
                        ActionOutcome(
                            action_id=action_id,
                            status="succeeded",
                            output=provider_result.output,
                            started_at=started_at,
                            latency_ms=latency_ms,
                            permit=authorization.permit,
                            diagnostics=diagnostics,
                        )
                    )
                    results.append(
                        GatewayActionResult(
                            action_id=action_id,
                            status="succeeded",
                            output=provider_result.output,
                            usage=usage,
                            provider=provider_result.provider,
                            model=provider_result.model,
                            provider_request_id=provider_result.provider_request_id,
                        )
                    )
                except (ProviderCallError, PermitError) as exc:
                    latency_ms = max((time.monotonic() - monotonic_started) * 1_000, 0)
                    provider = exc.provider if isinstance(exc, ProviderCallError) else None
                    request_id = (
                        exc.provider_request_id
                        if isinstance(exc, ProviderCallError)
                        else None
                    )
                    http_status = exc.http_status if isinstance(exc, ProviderCallError) else None
                    safe_error = (
                        str(exc)
                        if isinstance(exc, ProviderCallError)
                        else "Permit validation failed before provider execution."
                    )
                    outcomes.append(
                        ActionOutcome(
                            action_id=action_id,
                            status="failed",
                            error=safe_error,
                            started_at=started_at,
                            latency_ms=latency_ms,
                            permit=authorization.permit,
                            diagnostics=ExecutionDiagnostics(
                                provider=provider,
                                provider_request_id=request_id,
                                http_status=http_status,
                                state_transitions=["authorized", "gateway_failed"],
                            ),
                        )
                    )
                    results.append(
                        GatewayActionResult(
                            action_id=action_id,
                            status="failed",
                            error=safe_error,
                            provider=provider,
                            provider_request_id=request_id,
                        )
                    )
                    abort_remaining = True

            report = OutcomeReport(
                execution_id=request.execution_id,
                thread_id=request.thread_id,
                outcomes=outcomes,
                reported_by="sentri_gateway",
                upstream_tools=request.upstream_tools,
                metadata={
                    "authenticated_principal": principal,
                    "execution_source": "sentri_gateway",
                },
            )
            recorded = await self.report_outcome(report, internal_gateway=True)
            statuses = [item.status for item in results]
            status = statuses[0] if len(set(statuses)) == 1 else "mixed"
            return GatewayExecutionResponse(
                execution_id=request.execution_id,
                thread_id=request.thread_id,
                status=status,
                results=results,
                outcome_event_id=recorded.event_id,
                message=(
                    "Provider calls executed by Sentri; observed usage was recorded by the Clerk agent."
                ),
            )
        finally:
            async with self._outcome_lock:
                self._gateway_inflight.discard(request.execution_id)

    async def verify_permit(
        self, request: PermitVerificationRequest
    ) -> PermitVerificationResponse:
        try:
            verified = self.permits.verify(request.permit, request.action)
            snapshot = await self.graph.snapshot(verified.thread_id)
            state = snapshot.values
            if (
                not state
                or state.get("execution_id") != verified.execution_id
                or state.get("status") != "completed"
                or not (state.get("final_result") or {}).get("authorized")
            ):
                raise PermitError("The related execution is not currently authorized.")
            planned = {
                item["id"]: PlannedAction.model_validate(item)
                for item in state.get("planned_actions", [])
            }
            if verified.action_id not in planned:
                raise PermitError("The action is not present in the authorized execution.")
            self.permits.verify(
                request.permit,
                planned[verified.action_id],
                execution_id=verified.execution_id,
                thread_id=verified.thread_id,
            )
            consumed = False
            if request.consume:
                async with self._permit_lock:
                    prior_events = await self.storage.query(
                        verified.execution_id, limit=5_000
                    )
                    persisted_replay = any(
                        event.kind == "permit_consumed"
                        and event.payload.get("nonce") == verified.nonce
                        for event in prior_events
                    )
                    if verified.nonce in self._consumed_permits or persisted_replay:
                        raise PermitError("Permit has already been consumed.")
                    self._consumed_permits.add(verified.nonce)
                    await self.storage.record(
                        TelemetryEvent(
                            execution_id=verified.execution_id,
                            worker=WorkerName.WORK_QUEUE,
                            kind="permit_consumed",
                            payload={
                                "action_id": verified.action_id,
                                "action_hash": verified.action_hash,
                                "tool": planned[verified.action_id].tool,
                                "operation": planned[verified.action_id].operation,
                                "execution_attestation": "permit_consumed",
                                "nonce": verified.nonce,
                                "expires_at": verified.expires_at,
                            },
                        )
                    )
                    consumed = True
            return PermitVerificationResponse(
                valid=True,
                consumed=consumed,
                claims=verified.as_dict(),
                message=(
                    "Permit consumed; execute only the exact verified action."
                    if consumed
                    else "Permit is valid and has not been consumed."
                ),
            )
        except PermitError:
            raise

    def _response(
        self, values: dict[str, Any], interrupted: bool
    ) -> InteractResponse:
        if not values:
            raise ExecutionNotFound("Graph state is unavailable")
        alerts = [
            RiskAlert.model_validate(item) for item in values.get("risk_alerts", [])
        ]
        hard = any(alert.hard_limit for alert in alerts)
        if interrupted:
            status = "approval_required"
            result = None
            message = (
                "A hard safety limit was triggered. Human acknowledgement is required, "
                "but the forbidden action cannot be authorized."
                if hard
                else "Human approval is required in the Sentri Control Room."
            )
            approval_url = (
                f"{self.settings.public_base_url}/dashboard"
                f"?thread_id={values['thread_id']}"
            )
        else:
            status = "blocked" if values.get("status") == "blocked" else "completed"
            result = values.get("final_result")
            message = result.get("reason") if isinstance(result, dict) else None
            approval_url = None
        if status == "completed" and isinstance(result, dict) and result.get("authorized"):
            result = dict(result)
            result["permits"] = [
                self.permits.issue(
                    execution_id=values["execution_id"],
                    thread_id=values["thread_id"],
                    action=item,
                )
                for item in values.get("planned_actions", [])
            ]
        return InteractResponse(
            execution_id=values["execution_id"],
            thread_id=values["thread_id"],
            status=status,
            result=result,
            alerts=alerts,
            cost=CostMetrics.model_validate(values.get("cost_metrics") or {}),
            approval_url=approval_url,
            message=message,
        )
