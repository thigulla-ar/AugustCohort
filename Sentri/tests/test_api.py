from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentri.api import create_app
from sentri.config import Settings


def test_safe_interaction_completes(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/interact",
            json={
                "message": "Read the project status",
                "actions": [
                    {
                        "tool": "project.get",
                        "operation": "read",
                        "arguments": {"project_id": "alpha"},
                    }
                ],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["result"]["authorized"] is True
        audit = client.get("/audit", params={"execution_id": body["execution_id"]})
        assert len(audit.json()["events"]) == 5
        recent = client.get("/audit", params={"limit": 5})
        assert recent.status_code == 200
        assert len(recent.json()["events"]) == 5


def test_dashboard_storage_settings_are_selectable_and_persisted(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(app) as client:
        initial = client.get("/settings/storage")
        assert initial.status_code == 200
        assert initial.json()["storage_mode"] == "sqlite"
        dashboard = client.get("/dashboard")
        assert "Storage and retention" in dashboard.text
        assert 'id="storage-mode"' in dashboard.text

        updated = client.put(
            "/settings/storage",
            json={"storage_mode": "jsonl", "retention_days": 7},
        )
        assert updated.status_code == 200
        assert updated.json()["storage_mode"] == "jsonl"
        assert updated.json()["retention_days"] == 7
        assert (tmp_path / "settings.json").exists()
        assert (tmp_path / "logs").is_dir()

    reloaded = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(reloaded) as client:
        saved = client.get("/settings/storage").json()
        assert saved["storage_mode"] == "jsonl"
        assert saved["retention_days"] == 7


def test_dashboard_can_select_ephemeral_forever(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(app) as client:
        updated = client.put(
            "/settings/storage",
            json={"storage_mode": "ephemeral", "retention_days": None},
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["storage_mode"] == "ephemeral"
        assert body["retention_days"] == "Forever"
        assert body["active_path"] == "RAM only"


def test_hard_limit_interrupts_then_stays_blocked(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/interact",
            json={
                "message": "Pay the invoice",
                "actions": [
                    {
                        "tool": "payments",
                        "operation": "send money",
                        "arguments": {"amount": 10},
                        "mutates_state": True,
                    }
                ],
            },
        )
        body = response.json()
        assert body["status"] == "approval_required"
        assert body["alerts"][0]["hard_limit"] is True
        reviewed = client.post(
            f"/approvals/{body['thread_id']}",
            json={"approved": True, "reviewer": "operator", "reason": "requested"},
        )
        assert reviewed.json()["status"] == "blocked"
        assert reviewed.json()["result"]["authorized"] is False


def test_mutation_can_resume_after_human_approval(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="ephemeral", data_dir=tmp_path))
    with TestClient(app) as client:
        pending = client.post(
            "/interact",
            json={
                "message": "Add a project note",
                "actions": [
                    {
                        "tool": "project.note",
                        "operation": "create",
                        "arguments": {"text": "Ready"},
                        "mutates_state": True,
                    }
                ],
            },
        ).json()
        assert pending["status"] == "approval_required"
        reviewed = client.post(
            f"/approvals/{pending['thread_id']}",
            json={"approved": True, "reviewer": "operator", "reason": "expected"},
        ).json()
        assert reviewed["status"] == "completed"
        assert reviewed["result"]["authorized"] is True


def test_mcp_streamable_http_initializes(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="ephemeral", data_dir=tmp_path))
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "sentri-test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "Sentri"
        tools = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert tools.status_code == 200
        names = {tool["name"] for tool in tools.json()["result"]["tools"]}
        assert "sentri_report_outcome" in names
        assert "sentri_verify_permit" in names
        assert "sentri_execute" in names
        assert "sentri_render_control_room" in names
        assert "sentri_dashboard_data" in names
        assert "sentri_configure_storage" in names
        render_tool = next(
            tool
            for tool in tools.json()["result"]["tools"]
            if tool["name"] == "sentri_render_control_room"
        )
        assert render_tool["_meta"]["ui"]["resourceUri"].startswith("ui://sentri/")

        resources = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        ).json()["result"]["resources"]
        assert resources[0]["mimeType"] == "text/html;profile=mcp-app"

        resource = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": resources[0]["uri"]},
            },
        ).json()["result"]["contents"][0]
        assert "Sentri Control Room" in resource["text"]

        rendered = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "sentri_render_control_room",
                    "arguments": {"limit": 10},
                },
            },
        ).json()["result"]
        assert rendered["structuredContent"]["summary"]["events"] == 0


def test_builder_records_completed_workflow_outcome(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(app) as client:
        authorized = client.post(
            "/interact",
            json={
                "message": "Read project alpha",
                "actions": [
                    {
                        "tool": "project.get",
                        "operation": "read",
                        "arguments": {"project_id": "alpha"},
                    }
                ],
            },
        ).json()
        action_id = authorized["result"]["action_ids"][0]
        permit = authorized["result"]["permits"][0]["permit"]

        response = client.post(
            "/outcomes",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "reported_by": "local",
                "outcomes": [
                    {
                        "action_id": action_id,
                        "status": "succeeded",
                        "output": {"owner_email": "person@example.com"},
                        "latency_ms": 42.5,
                        "metadata": {
                            "reviewer": "reviewer@example.com",
                            "api_key": "must-never-be-stored",
                        },
                        "diagnostics": {
                            "provider": "example-provider",
                            "model": "example-model",
                            "tool_version": "2.1",
                            "provider_request_id": "request-123",
                            "retry_count": 1,
                            "http_status": 200,
                            "evidence": [{
                                "source_id": "project-alpha",
                                "title": "Project record",
                                "uri": "https://example.test/project/alpha?token=secret#section",
                            }],
                            "calculation_steps": ["Loaded and validated the record"],
                        },
                        "permit": permit,
                    }
                ],
                "final_response": {"message": "Project loaded"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        result_node = next(
            node
            for node in body["completed_dag"]["nodes"]
            if node["type"] == "tool_result"
        )
        assert result_node["action_id"] == action_id
        assert result_node["output_hash"].startswith("sha256:")
        assert "person@example.com" not in response.text
        assert "reviewer@example.com" not in response.text
        assert {"from": action_id, "to": f"result:{action_id}"} in body[
            "completed_dag"
        ]["edges"]

        events = client.get(
            "/audit", params={"execution_id": authorized["execution_id"]}
        ).json()["events"]
        assert len(events) == 10
        terminal = [event for event in events if event["payload"].get("phase") == "terminal"]
        assert {event["worker"] for event in terminal} == {
            "work_queue", "change_log", "rogue", "builder", "clerk"
        }
        for event in events:
            audit_context = event["payload"]["audit"]
            assert audit_context["schema_version"] == "1.2"
            assert audit_context["security"]["permit_required_before_execution"] is True
        recorded = next(event for event in events if event["kind"] == "execution_outcome")
        assert recorded["worker"] == "builder"
        assert recorded["payload"]["status"] == "succeeded"
        recorded_text = str(recorded)
        assert "?token=secret" not in recorded_text
        assert "must-never-be-stored" not in recorded_text
        result = next(
            node for node in recorded["payload"]["dag"]["nodes"]
            if node["type"] == "tool_result"
        )
        evidence = result["diagnostics"]["evidence"][0]
        assert evidence["uri"] == "https://example.test/project/alpha"
        assert evidence["uri_hash"].startswith("sha256:")

        snapshot = client.get("/audit", params={"limit": 500})
        assert snapshot.status_code == 200

        duplicate = client.post(
            "/outcomes",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "outcomes": [
                    {
                        "action_id": action_id,
                        "status": "succeeded",
                        "permit": permit,
                    }
                ],
            },
        )
        assert duplicate.status_code == 409


def test_outcome_must_match_exact_authorized_action_set(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="ephemeral", data_dir=tmp_path))
    with TestClient(app) as client:
        authorized = client.post(
            "/interact",
            json={
                "message": "Read a project",
                "actions": [
                    {
                        "tool": "project.get",
                        "operation": "read",
                        "arguments": {},
                    }
                ],
            },
        ).json()
        response = client.post(
            "/outcomes",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "outcomes": [
                    {
                        "action_id": "not-authorized",
                        "status": "succeeded",
                        "permit": authorized["result"]["permits"][0]["permit"],
                    }
                ],
            },
        )
        assert response.status_code == 409
        assert "does not match authorization" in response.json()["detail"]


def test_signed_permit_binds_exact_action_and_is_single_use(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="ephemeral", data_dir=tmp_path))
    with TestClient(app) as client:
        authorized = client.post(
            "/interact",
            json={
                "message": "Read project alpha",
                "actions": [
                    {
                        "tool": "project.get",
                        "operation": "read",
                        "arguments": {"project_id": "alpha"},
                    }
                ],
            },
        ).json()
        action_id = authorized["result"]["action_ids"][0]
        permit = authorized["result"]["permits"][0]["permit"]
        action = {
            "id": action_id,
            "tool": "project.get",
            "operation": "read",
            "arguments": {"project_id": "alpha"},
        }
        verified = client.post(
            "/permits/verify", json={"permit": permit, "action": action}
        )
        assert verified.status_code == 200
        assert verified.json()["consumed"] is True

        replay = client.post(
            "/permits/verify", json={"permit": permit, "action": action}
        )
        assert replay.status_code == 403

        modified = dict(action)
        modified["arguments"] = {"project_id": "different"}
        rejected = client.post(
            "/permits/verify", json={"permit": permit, "action": modified}
        )
        assert rejected.status_code == 403


def test_public_deployment_requires_bearer_authentication(tmp_path: Path) -> None:
    token = "t" * 48
    app = create_app(
        Settings(
            storage_mode="ephemeral",
            data_dir=tmp_path,
            public_base_url="https://sentri.example",
            allow_origins=["https://chatgpt.com"],
            api_token=token,
            signing_secret="s" * 48,
        )
    )
    with TestClient(app, base_url="https://sentri.example") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/audit").status_code == 401
        assert client.get(
            "/audit", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200


def test_public_deployment_fails_closed_without_secrets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SENTRI_API_TOKEN"):
        Settings(
            storage_mode="ephemeral",
            data_dir=tmp_path,
            public_base_url="https://sentri.example",
        )


def test_outcome_rejects_pii_in_final_response(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="ephemeral", data_dir=tmp_path))
    with TestClient(app) as client:
        authorized = client.post(
            "/interact",
            json={
                "message": "Read project",
                "actions": [{"tool": "project.get", "operation": "read"}],
            },
        ).json()
        action_id = authorized["result"]["action_ids"][0]
        response = client.post(
            "/outcomes",
            json={
                "execution_id": authorized["execution_id"],
                "thread_id": authorized["thread_id"],
                "outcomes": [
                    {
                        "action_id": action_id,
                        "status": "succeeded",
                        "permit": authorized["result"]["permits"][0]["permit"],
                    }
                ],
                "final_response": {"email": "person@example.com"},
            },
        )
        assert response.status_code == 409
        assert "personal information" in response.json()["detail"]


def test_audit_events_have_valid_integrity_chain(tmp_path: Path) -> None:
    app = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(app) as client:
        client.post(
            "/interact",
            json={"message": "Read status", "actions": []},
        )
        result = client.get("/audit/verify").json()
        assert result["valid"] is True
        assert result["checked_events"] == 5

    restarted = create_app(Settings(storage_mode="sqlite", data_dir=tmp_path))
    with TestClient(restarted) as client:
        result = client.get("/audit/verify").json()
        assert result["valid"] is True
        assert result["checked_events"] == 5
