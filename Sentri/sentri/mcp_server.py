from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from sentri.models import (
    ApprovalRequest,
    GatewayExecutionRequest,
    InteractRequest,
    OutcomeReport,
    PermitVerificationRequest,
)
from sentri.service import SentriService


CONTROL_ROOM_URI = "ui://sentri/control-room-v1.html"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"


def create_mcp_server(service: SentriService) -> FastMCP:
    public_host = urlparse(service.settings.public_base_url).netloc
    allowed_hosts = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        *([public_host] if public_host else []),
    ]
    allowed_origins = list(
        dict.fromkeys(
            [*service.settings.allow_origins, service.settings.public_base_url]
        )
    )
    mcp = FastMCP(
        "Sentri",
        instructions=(
            "Call sentri_interact before executing proposed tools. Execute actions only "
            "when status is completed and result.authorized is true. Immediately before each "
            "tool call, consume its signed permit with sentri_verify_permit and execute only "
            "the exact verified action. For allowlisted OpenAI or Gemini generation, prefer "
            "sentri_execute so Sentri executes the call, observes provider usage, consumes "
            "permits, and reports outcomes itself. After externally executing every "
            "authorized action, call sentri_report_outcome. Never bypass a block. When the "
            "user asks to inspect Sentri, or when review is required, call "
            "sentri_render_control_room to show its control room inside the conversation."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
    )

    control_room_path = Path(__file__).parent / "static" / "mcp_app.html"

    @mcp.resource(
        CONTROL_ROOM_URI,
        name="sentri-control-room",
        title="Sentri Control Room",
        description="Interactive five-agent governance and telemetry control room.",
        mime_type=MCP_APP_MIME_TYPE,
        meta={"ui": {"prefersBorder": True}},
    )
    async def sentri_control_room_resource() -> str:
        return control_room_path.read_text(encoding="utf-8")

    @mcp.tool(
        name="sentri_interact",
        description="Preflight proposed actions through Sentri governance and auditing.",
    )
    async def sentri_interact(
        message: str,
        actions: list[dict] | None = None,
        conversation_id: str | None = None,
        host: str = "chatgpt",
        metadata: dict | None = None,
    ) -> dict:
        request = InteractRequest.model_validate(
            {
                "message": message,
                "actions": actions or [],
                "conversation_id": conversation_id,
                "host": host,
                "metadata": {
                    **(metadata or {}),
                    "authenticated_principal": "authenticated-mcp-client",
                },
            }
        )
        return (
            await service.interact(request, principal="authenticated-mcp-client")
        ).model_dump(mode="json")

    @mcp.tool(
        name="sentri_audit",
        description="Read locally stored Sentri telemetry for an execution.",
    )
    async def sentri_audit(execution_id: str, limit: int = 500) -> dict:
        events = await service.storage.query(execution_id, limit)
        return {
            "execution_id": execution_id,
            "events": [event.model_dump(mode="json") for event in events],
        }

    @mcp.tool(
        name="sentri_dashboard_data",
        title="Refresh Sentri control room data",
        description=(
            "Return the latest five-agent telemetry and storage settings without "
            "opening another UI instance. Used by the Sentri MCP App."
        ),
        structured_output=True,
    )
    async def sentri_dashboard_data(limit: int = 500) -> dict[str, Any]:
        return await service.dashboard_snapshot(limit)

    @mcp.tool(
        name="sentri_configure_storage",
        title="Configure Sentri storage",
        description=(
            "Set local telemetry storage to ephemeral, sqlite, or jsonl and choose "
            "a positive retention duration; null retention means Forever."
        ),
        structured_output=True,
    )
    async def sentri_configure_storage(
        storage_mode: str, retention_days: int | None = 30
    ) -> dict[str, Any]:
        if storage_mode not in {"ephemeral", "sqlite", "jsonl"}:
            raise ValueError("storage_mode must be ephemeral, sqlite, or jsonl")
        if retention_days is not None and retention_days < 1:
            raise ValueError("retention_days must be positive or null")
        return await service.configure_storage(  # type: ignore[arg-type]
            storage_mode, retention_days, actor="authenticated-mcp-client"
        )

    @mcp.tool(
        name="sentri_verify_permit",
        description=(
            "Validate and consume a single-use signed Sentri permit immediately before "
            "executing the exact proposed action. Never execute when validation fails."
        ),
    )
    async def sentri_verify_permit(
        permit: str, action: dict, consume: bool = True
    ) -> dict:
        request = PermitVerificationRequest.model_validate(
            {"permit": permit, "action": action, "consume": consume}
        )
        return (await service.verify_permit(request)).model_dump(mode="json")

    @mcp.tool(
        name="sentri_execute",
        description=(
            "Execute an authorized, allowlisted OpenAI Responses or Gemini GenerateContent "
            "call through Sentri. Sentri consumes permits, reads provider-reported token "
            "usage, calculates configured cost, and records all five terminal audit events. "
            "Include upstream_tools to disclose caller-observed research lineage without "
            "sending raw tool arguments or outputs."
        ),
    )
    async def sentri_execute(
        execution_id: str,
        thread_id: str,
        authorizations: list[dict],
        upstream_tools: list[dict] | None = None,
    ) -> dict:
        request = GatewayExecutionRequest.model_validate(
            {
                "execution_id": execution_id,
                "thread_id": thread_id,
                "authorizations": authorizations,
                "upstream_tools": upstream_tools or [],
            }
        )
        return (
            await service.execute_gateway(
                request, principal="authenticated-mcp-client"
            )
        ).model_dump(mode="json")

    @mcp.tool(
        name="sentri_render_control_room",
        title="Open Sentri Control Room",
        description=(
            "Render Sentri's interactive governance dashboard inside the host chat. "
            "Use this when the user asks to view Sentri or when an execution needs review."
        ),
        meta={
            "ui": {"resourceUri": CONTROL_ROOM_URI},
            "openai/outputTemplate": CONTROL_ROOM_URI,
            "openai/toolInvocation/invoking": "Opening Sentri Control Room…",
            "openai/toolInvocation/invoked": "Sentri Control Room ready.",
        },
        structured_output=True,
    )
    async def sentri_render_control_room(
        thread_id: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        snapshot = await service.dashboard_snapshot(limit)
        snapshot["review_thread_id"] = thread_id
        return snapshot

    @mcp.tool(
        name="sentri_review",
        description="Approve or reject an interrupted, approvable Sentri execution.",
    )
    async def sentri_review(
        thread_id: str, approved: bool, reviewer: str, reason: str
    ) -> dict:
        decision = ApprovalRequest(
            approved=approved,
            reviewer="authenticated-mcp-client",
            reason=f"{reason} (display name supplied: {reviewer})",
        )
        return (await service.approve(thread_id, decision)).model_dump(mode="json")

    @mcp.tool(
        name="sentri_report_outcome",
        description=(
            "Record the observed success, failure, or cancellation of every action in a "
            "previously authorized Sentri execution. Each outcome must include its signed "
            "permit."
        ),
    )
    async def sentri_report_outcome(
        execution_id: str,
        thread_id: str,
        outcomes: list[dict],
        final_response: Any | None = None,
        reported_by: str = "external",
        upstream_tools: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        report = OutcomeReport.model_validate(
            {
                "execution_id": execution_id,
                "thread_id": thread_id,
                "outcomes": outcomes,
                "final_response": final_response,
                "reported_by": reported_by,
                "upstream_tools": upstream_tools or [],
                "metadata": {
                    **(metadata or {}),
                    "authenticated_principal": "authenticated-mcp-client",
                },
            }
        )
        return (await service.report_outcome(report)).model_dump(mode="json")

    return mcp
