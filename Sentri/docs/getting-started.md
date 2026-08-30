# Getting started

## Prerequisites

- Python 3.11 or newer
- PowerShell examples below, or an equivalent shell
- A local browser for the fallback dashboard
- For hosted chat integration: a reachable HTTPS origin; hosted services cannot call your machine's `localhost` directly

Provider API keys are not required for governance preflight, audit, storage, or the dashboard. They are required only when using the optional controlled execution gateway.

## Install

From the repository root:

```powershell
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

The `.env` file is intentionally ignored by Git. Do not put real secrets in `.env.example`.

## Start in local mode

The default configuration uses SQLite and stores telemetry under `~/Documents/Sentri`.

```powershell
python -m sentri.main
```

Open:

- Dashboard: `http://localhost:8000/dashboard`
- Interactive API documentation: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`
- MCP transport: `http://localhost:8000/mcp/`

Loopback development is authentication-free by default. A non-loopback public URL automatically requires a bearer token and signing secret.

## Submit a first governed action

This example proposes a read-only action. Sentri evaluates and records it but does not call `project.get` itself.

```powershell
$request = @{
  message = "Read project alpha"
  host = "local"
  actions = @(
    @{
      tool = "project.get"
      operation = "read"
      arguments = @{ project_id = "alpha" }
      rationale = "Retrieve the project requested by the user"
      mutates_state = $false
      data_classification = @("internal")
    }
  )
} | ConvertTo-Json -Depth 8

$decision = Invoke-RestMethod `
  -Uri http://localhost:8000/interact `
  -Method Post `
  -ContentType application/json `
  -Body $request

$decision | ConvertTo-Json -Depth 10
```

A safe read normally returns:

- `status: completed`
- `result.authorized: true`
- an `execution_id` and `thread_id`
- one signed permit per exact action in `result.permits`

The host must consume the corresponding permit immediately before executing the downstream action. The submitted action must produce the same canonical JSON representation; changing its tool, operation, arguments, or relevant fields invalidates authorization.

## Understand decision statuses

| Status | Meaning | Host behavior |
|---|---|---|
| `completed` | Preflight finished. Inspect `result.authorized`. | Execute only exact authorized actions and enforce permits. |
| `approval_required` | LangGraph is paused at the safety gate. | Do not execute. Open the control room for review. |
| `blocked` | The action was rejected or a hard-limit review completed. | Do not execute or retry a disguised equivalent. |
| `error` | Sentri could not complete governance. | Fail closed and investigate. |

Ordinary state-changing actions may be approved. Hard-limit actions still pause for acknowledgement, but approval cannot authorize them.

## Verify a permit

Extract the exact action and permit from the decision and call `POST /permits/verify` with `consume: true`. A consumed permit cannot be replayed.

```powershell
$verification = @{
  permit = $decision.result.permits[0].permit
  action = @{
    id = $decision.result.action_ids[0]
    tool = "project.get"
    operation = "read"
    arguments = @{ project_id = "alpha" }
    rationale = "Retrieve the project requested by the user"
    mutates_state = $false
    data_classification = @("internal")
  }
  consume = $true
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri http://localhost:8000/permits/verify `
  -Method Post `
  -ContentType application/json `
  -Body $verification
```

After an externally executed action, call `POST /outcomes` so all five worker views receive terminal evidence. If the action is an allowlisted provider generation, use `POST /execute` instead; Sentri consumes the permit, executes the provider request, captures usage, and records outcomes itself.

## View telemetry

Refresh the dashboard after the request. You should see one preflight event in each tab:

- Work Queue: `call_tree`
- Change Log: `proposed_changes`
- Rogue: `policy_evaluation`
- Builder: `planned_execution`
- Clerk: `cost_metrics`

After `/outcomes` or `/execute`, each tab also receives a correlated terminal event.

## Validate the installation

```powershell
pytest -q --basetemp .sentri-test-docs -p no:cacheprovider
python scripts/export_openapi.py
python scripts/check_public_repo.py
```

## Common first-run issues

### The dashboard is empty

Opening the dashboard does not import existing ChatGPT or Gemini history. Submit `/interact` or call `sentri_interact` through a connected MCP client.

### A hosted chat cannot reach Sentri

`localhost:8000` is visible only on your computer. Use a controlled HTTPS tunnel for development or deploy Sentri behind TLS. Set `SENTRI_PUBLIC_BASE_URL` to that HTTPS origin and configure the required secrets.

### Tokens remain zero

Preflight does not call a model provider. Accurate provider usage appears when a supported request is executed through `/execute` or `sentri_execute`. External clients may report usage in outcome diagnostics, but that measurement remains caller-supplied.

### Configuration changes do not take effect

Environment values are read at process startup. Stop the server with `Ctrl+C`, restart it, and refresh the dashboard. Storage settings changed in the dashboard are persisted separately in `~/Documents/Sentri/settings.json` and override only storage mode and retention.
