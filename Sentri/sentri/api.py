from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from starlette.middleware.trustedhost import TrustedHostMiddleware
from urllib.parse import urlparse

from sentri import __version__
from sentri.config import Settings
from sentri.gateway import GatewayDisabledError, GatewayRequestError
from sentri.mcp_server import create_mcp_server
from sentri.models import (
    ApprovalRequest,
    AuditResponse,
    GatewayExecutionRequest,
    GatewayExecutionResponse,
    InteractRequest,
    InteractResponse,
    OutcomeReport,
    OutcomeResponse,
    PermitVerificationRequest,
    PermitVerificationResponse,
    StorageSettingsResponse,
    StorageSettingsUpdate,
)
from sentri.service import ExecutionNotFound, OutcomeConflict, SentriService
from sentri.security import PermitError


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = (settings or Settings()).with_runtime_overrides()
    service = SentriService(settings)
    mcp = create_mcp_server(service)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.initialize()
        async with mcp.session_manager.run():
            yield
        await service.close()

    bearer = HTTPBearer(auto_error=False, scheme_name="SentriBearer")
    app = FastAPI(
        title="Sentri Control Room API",
        summary="Governance preflight and telemetry for hosted AI agents",
        description=(
            "Call interact before downstream tool execution. A completed response with "
            "result.authorized=true permits only the exact submitted actions."
        ),
        version=__version__,
        lifespan=lifespan,
        servers=[{"url": settings.public_base_url}],
        dependencies=[Depends(bearer)],
    )
    app.state.service = service
    public_host = urlparse(settings.public_base_url).hostname
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(
            dict.fromkeys(
                [
                    "localhost",
                    "127.0.0.1",
                    "[::1]",
                    "testserver",
                    *([public_host] if public_host else []),
                ]
            )
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allow_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )

    rate_windows: dict[str, deque[float]] = defaultdict(deque)
    rate_lock = asyncio.Lock()

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        now = time.monotonic()
        client_key = request.client.host if request.client else "unknown"
        async with rate_lock:
            window = rate_windows[client_key]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= settings.rate_limit_requests_per_minute:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Sentri request rate limit exceeded."},
                    headers={"Retry-After": "60"},
                )
            window.append(now)

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds the Sentri size limit."},
                    )
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length."})

        exempt = request.method == "OPTIONS" or request.url.path in {
            "/health",
            "/openapi.json",
        }
        if settings.auth_required and not exempt:
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {settings.api_token}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "A valid Sentri bearer token is required."},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.principal = "sentri-service-token"
        else:
            request.state.principal = "local-loopback"

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path == "/execute":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(
        "/interact",
        operation_id="sentriInteract",
        response_model=InteractResponse,
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def interact(payload: InteractRequest, request: Request) -> InteractResponse:
        return await service.interact(payload, principal=request.state.principal)

    @app.post(
        "/approvals/{thread_id}",
        operation_id="reviewSentriExecution",
        response_model=InteractResponse,
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def approve(
        thread_id: str, payload: ApprovalRequest, request: Request
    ) -> InteractResponse:
        try:
            verified = payload.model_copy(update={"reviewer": request.state.principal})
            return await service.approve(thread_id, verified)
        except ExecutionNotFound as exc:
            raise HTTPException(
                status_code=404, detail="No interrupted execution exists for this thread."
            ) from exc

    @app.post(
        "/outcomes",
        operation_id="reportSentriOutcome",
        response_model=OutcomeResponse,
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def report_outcome(
        payload: OutcomeReport, request: Request
    ) -> OutcomeResponse:
        """Attach observed tool results to a previously authorized workflow DAG."""
        try:
            authenticated = payload.model_copy(
                update={
                    "metadata": {
                        **payload.metadata,
                        "authenticated_principal": request.state.principal,
                    }
                }
            )
            return await service.report_outcome(authenticated)
        except ExecutionNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail="The execution and thread pair was not found in graph state.",
            ) from exc
        except OutcomeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/execute",
        operation_id="executeSentriGateway",
        response_model=GatewayExecutionResponse,
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def execute_gateway(
        payload: GatewayExecutionRequest, request: Request
    ) -> GatewayExecutionResponse:
        """Execute authorized model calls so Sentri observes provider usage directly."""
        try:
            return await service.execute_gateway(
                payload, principal=request.state.principal
            )
        except ExecutionNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail="The execution and thread pair was not found in graph state.",
            ) from exc
        except GatewayDisabledError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (GatewayRequestError, OutcomeConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/permits/verify",
        operation_id="verifySentriPermit",
        response_model=PermitVerificationResponse,
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def verify_permit(
        payload: PermitVerificationRequest,
    ) -> PermitVerificationResponse:
        try:
            return await service.verify_permit(payload)
        except PermitError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get(
        "/audit",
        operation_id="getSentriAudit",
        response_model=AuditResponse,
    )
    async def audit(
        execution_id: str | None = Query(
            default=None,
            description="Filter by execution ID; omit to return the latest events.",
        ),
        limit: int = Query(default=500, ge=1, le=5000),
    ) -> AuditResponse:
        return AuditResponse(
            execution_id=execution_id,
            events=await service.storage.query(execution_id, limit),
        )

    @app.get("/audit/verify", include_in_schema=False)
    async def verify_audit(
        execution_id: str | None = None,
        limit: int = Query(default=5_000, ge=1, le=5_000),
    ) -> dict:
        return await service.storage.verify_integrity(execution_id, limit)

    @app.get("/health", operation_id="getSentriHealth")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": app.version,
            "storage_mode": settings.storage_mode,
            "retention_days": settings.retention_days or "Forever",
            "hard_limits": {
                "send_money": "blocked",
                "delete_records": "blocked",
                "share_personal_info": "blocked",
            },
            "execution_gateway": {
                "enabled": settings.gateway_enabled,
                "providers": {
                    "openai": bool(settings.openai_api_key),
                    "gemini": bool(settings.gemini_api_key),
                },
            },
        }

    @app.get("/settings/storage", include_in_schema=False)
    async def get_storage_settings() -> StorageSettingsResponse:
        return StorageSettingsResponse.model_validate(service.storage_settings())

    @app.put("/settings/storage", include_in_schema=False)
    async def update_storage_settings(
        payload: StorageSettingsUpdate,
        request: Request,
    ) -> StorageSettingsResponse:
        return StorageSettingsResponse.model_validate(
            await service.configure_storage(
                payload.storage_mode,
                payload.retention_days,
                actor=request.state.principal,
            )
        )

    @app.get(
        "/dashboard-stream",
        operation_id="streamSentriDashboard",
        include_in_schema=False,
    )
    async def dashboard_stream(request: Request) -> StreamingResponse:
        try:
            queue = service.storage.subscribe()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def events() -> AsyncIterator[str]:
            try:
                yield "event: ready\ndata: {}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=settings.dashboard_heartbeat_seconds
                        )
                        yield f"event: telemetry\ndata: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield "event: heartbeat\ndata: {}\n\n"
            finally:
                service.storage.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    dashboard_path = Path(__file__).parent / "static" / "dashboard.html"

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(dashboard_path)

    app.mount("/mcp", mcp_app, name="sentri-mcp")
    return app


app = create_app()
