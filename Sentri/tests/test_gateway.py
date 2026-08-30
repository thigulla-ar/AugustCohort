from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sentri.api import create_app
from sentri.config import Settings
from sentri.gateway import (
    GEMINI_TOOL,
    OPENAI_TOOL,
    GeminiGenerateContentAdapter,
    OpenAIResponsesAdapter,
    ProviderResult,
)
from sentri.models import PlannedAction


class FakeOpenAIAdapter:
    provider = "openai"

    def validate(self, action: PlannedAction) -> None:
        assert action.tool == OPENAI_TOOL

    async def execute(self, action: PlannedAction) -> ProviderResult:
        return ProviderResult(
            output="gateway answer",
            provider="openai",
            model=action.arguments["model"],
            model_version="test-model-2026-01-01",
            provider_request_id="provider-request-123",
            http_status=200,
            prompt_tokens=120,
            completion_tokens=30,
            total_tokens=150,
            cached_tokens=20,
        )


def gateway_settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_mode="ephemeral",
        data_dir=tmp_path,
        gateway_enabled=True,
        openai_api_key="server-only-test-key-value",
        gateway_allowed_openai_models=["test-model"],
        gateway_pricing={
            "openai:test-model": {
                "input_per_million": 2,
                "output_per_million": 4,
                "source": "test-price-table",
            }
        },
    )


def test_gateway_records_provider_usage_without_persisting_output(tmp_path: Path) -> None:
    app = create_app(gateway_settings(tmp_path))
    app.state.service.gateway.adapters[OPENAI_TOOL] = FakeOpenAIAdapter()
    action = {
        "tool": OPENAI_TOOL,
        "operation": "create",
        "arguments": {"model": "test-model", "input": "Summarize this text."},
    }

    with TestClient(app) as client:
        authorized = client.post(
            "/interact", json={"message": "Generate a summary", "actions": [action]}
        ).json()
        action["id"] = authorized["result"]["action_ids"][0]
        permit = authorized["result"]["permits"][0]["permit"]
        response = client.post(
            "/execute",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "authorizations": [{"action": action, "permit": permit}],
                "upstream_tools": [
                    {
                        "tool": "web.search",
                        "operation": "research",
                        "status": "succeeded",
                        "provider_request_id": "search-request-123",
                        "evidence_ids": ["public-source-1"],
                    }
                ],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["results"][0]["output"] == "gateway answer"
        usage = body["results"][0]["usage"]
        assert usage["prompt_tokens"] == 120
        assert usage["completion_tokens"] == 30
        assert usage["total_tokens"] == 150
        assert usage["estimated_cost_usd"] == 0.00036

        events = client.get(
            "/audit", params={"execution_id": authorized["execution_id"]}
        ).json()["events"]
        assert len(events) == 11  # five preflight, permit receipt, five terminal
        stored = str(events)
        assert "gateway answer" not in stored
        assert "server-only-test-key-value" not in stored
        clerk = next(event for event in events if event["kind"] == "performance_outcome")
        assert clerk["payload"]["usage"]["total_tokens"] == 150
        assert clerk["payload"]["usage"]["source"] == "sentri_gateway"
        rogue = next(event for event in events if event["kind"] == "post_execution_safety")
        assert rogue["payload"]["execution_attestation"] == "sentri_gateway_observed"
        builder = next(event for event in events if event["kind"] == "execution_outcome")
        assert builder["payload"]["reconstruction"]["caller_reported_outcome"] is False
        assert builder["payload"]["reconstruction"]["upstream_tool_count"] == 1
        upstream_node = next(
            node
            for node in builder["payload"]["dag"]["nodes"]
            if node["type"] == "upstream_tool"
        )
        assert upstream_node["tool"] == "web.search"
        assert upstream_node["execution_attestation"] == "caller_reported"
        work_queue = next(
            event for event in events if event["kind"] == "execution_completed"
        )
        completed_action = work_queue["payload"]["actions"][0]
        assert completed_action["tool"] == "openai.responses"
        assert completed_action["operation"] == "create"
        assert completed_action["provider"] == "openai"
        assert completed_action["model"] == "test-model"
        assert completed_action["execution_attestation"] == "sentri_gateway_observed"
        assert work_queue["payload"]["upstream_tools"][0]["tool"] == "web.search"
        assert (
            work_queue["payload"]["upstream_tools"][0]["execution_attestation"]
            == "caller_reported"
        )
        permit_event = next(event for event in events if event["kind"] == "permit_consumed")
        assert permit_event["payload"]["tool"] == "openai.responses"
        assert permit_event["payload"]["operation"] == "create"

        replay = client.post(
            "/execute",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "authorizations": [{"action": action, "permit": permit}],
            },
        )
        assert replay.status_code == 409


async def test_openai_adapter_reads_usage_and_keeps_key_out_of_body(tmp_path: Path) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            headers={"x-request-id": "http-request-id"},
            json={
                "id": "resp_123",
                "status": "completed",
                "model": "test-model",
                "output": [
                    {"content": [{"type": "output_text", "text": "provider text"}]}
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                    "input_tokens_details": {"cached_tokens": 3},
                },
            },
        )

    settings = gateway_settings(tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        adapter = OpenAIResponsesAdapter(settings, client)
        action = PlannedAction(
            tool=OPENAI_TOOL,
            operation="create",
            arguments={"model": "test-model", "input": "hello"},
        )
        result = await adapter.execute(action)
    finally:
        await client.aclose()

    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert seen["authorization"] == "Bearer server-only-test-key-value"
    assert "server-only-test-key-value" not in seen["body"]
    assert '"store":false' in seen["body"]
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 4
    assert result.total_tokens == 14
    assert result.cached_tokens == 3


async def test_gemini_adapter_reads_usage_and_keeps_key_out_of_body(tmp_path: Path) -> None:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "responseId": "gemini-response-123",
                "modelVersion": "gemini-test-001",
                "candidates": [{"content": {"parts": [{"text": "gemini text"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 13,
                    "cachedContentTokenCount": 2,
                },
            },
        )

    settings = Settings(
        storage_mode="ephemeral",
        data_dir=tmp_path,
        gateway_enabled=True,
        gemini_api_key="gemini-server-key-value",
        gateway_allowed_gemini_models=["gemini-test"],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        adapter = GeminiGenerateContentAdapter(settings, client)
        result = await adapter.execute(
            PlannedAction(
                tool=GEMINI_TOOL,
                operation="generate",
                arguments={"model": "gemini-test", "contents": "hello"},
            )
        )
    finally:
        await client.aclose()

    assert seen["url"].endswith("/v1beta/models/gemini-test:generateContent")
    assert seen["key"] == "gemini-server-key-value"
    assert "gemini-server-key-value" not in seen["body"]
    assert result.prompt_tokens == 8
    assert result.completion_tokens == 5
    assert result.total_tokens == 13
    assert result.cached_tokens == 2


def test_gateway_rejects_secret_fields_before_consuming_permit(tmp_path: Path) -> None:
    app = create_app(gateway_settings(tmp_path))
    action = {
        "tool": OPENAI_TOOL,
        "operation": "create",
        "arguments": {
            "model": "test-model",
            "input": "hello",
            "api_key": "must-not-leave-sentri",
        },
    }
    with TestClient(app) as client:
        authorized = client.post(
            "/interact", json={"message": "Generate", "actions": [action]}
        ).json()
        action["id"] = authorized["result"]["action_ids"][0]
        response = client.post(
            "/execute",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "authorizations": [
                    {
                        "action": action,
                        "permit": authorized["result"]["permits"][0]["permit"],
                    }
                ],
            },
        )
        assert response.status_code == 409
        events = client.get(
            "/audit", params={"execution_id": authorized["execution_id"]}
        ).json()["events"]
        assert not any(event["kind"] == "permit_consumed" for event in events)
        assert "must-not-leave-sentri" not in str(events)
