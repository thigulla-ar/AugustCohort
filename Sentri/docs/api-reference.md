# API reference

The generated contract is [`openapi.json`](../openapi.json). When Sentri is running, interactive FastAPI documentation is available at `/docs`.

## Authentication

Loopback mode is unauthenticated unless explicitly configured otherwise. Non-loopback mode requires:

```http
Authorization: Bearer SENTRI_API_TOKEN
```

`/health` and `/openapi.json` remain unauthenticated. Request-size limits, rate limiting, trusted-host validation, CORS, and response hardening apply at the HTTP boundary.

## REST endpoints

| Method | Path | Purpose | Host-facing OpenAPI |
|---|---|---|---|
| POST | `/interact` | Run governance preflight and issue permits when authorized. | Yes |
| POST | `/approvals/{thread_id}` | Resume an interrupted graph with a review decision. | Yes |
| POST | `/permits/verify` | Validate and optionally consume an exact-action permit. | Yes |
| POST | `/execute` | Run authorized allowlisted provider generation through Sentri. | Yes |
| POST | `/outcomes` | Attach external tool results to the completed workflow DAG. | Yes |
| GET | `/audit` | Query latest or execution-specific telemetry. | Yes |
| GET | `/health` | Return service, storage, policy, and gateway status. | Yes |
| GET | `/audit/verify` | Verify the local HMAC event chain. | No |
| GET/PUT | `/settings/storage` | Read or change local storage and retention. | No |
| GET | `/dashboard-stream` | Stream telemetry as server-sent events. | No |
| GET | `/dashboard` | Serve the local control room. | No |
| any | `/mcp/` | Streamable HTTP MCP transport. | N/A |

## Core request objects

### PlannedAction

| Field | Required | Notes |
|---|---|---|
| `id` | No | Generated when omitted; reuse the returned ID in later calls. |
| `tool` | Yes | Exact downstream tool or API name. |
| `operation` | Yes | Exact operation to perform. |
| `arguments` | No | Bounded JSON object; included in the authorization hash. |
| `rationale` | No | Why the action is needed. |
| `mutates_state` | No | `true` routes ordinary mutations to human review. |
| `data_classification` | No | Labels such as `public`, `internal`, `sensitive`, or `pii`. |

### InteractRequest

Contains `message`, optional `conversation_id`, `host`, `actions`, and bounded `metadata`.

### OutcomeReport

Contains the original `execution_id` and `thread_id`, one or more outcomes, optional final response, optional upstream tool references, reporter identity, and metadata. Every outcome must carry the permit for its action. Raw output may be submitted for hashing but is not persisted.

### GatewayExecutionRequest

Contains `execution_id`, `thread_id`, one or more `{action, permit}` authorizations, and optional bounded upstream tool references. Upstream references allow only tool, operation, status, request ID, and evidence IDs; raw inputs and outputs are forbidden by the schema.

## Response semantics

`/interact` is successful only when both conditions hold:

```text
status == "completed"
result.authorized == true
```

Do not infer authorization from HTTP 200 alone.

Permit verification returns `valid`, `consumed`, claims, and a message. Execute only when validation succeeds and the permit has been consumed immediately before the call.

Outcome and gateway responses return an overall status:

- `succeeded`
- `failed`
- `cancelled`
- `mixed`

## Error behavior

| HTTP status | Typical meaning |
|---|---|
| 400 | Invalid HTTP metadata such as `Content-Length`. |
| 401 | Missing or incorrect bearer token. |
| 403 | Invalid, expired, mismatched, or replayed permit. |
| 404 | Execution/thread or interrupted graph state not found. |
| 409 | Conflicting outcome, invalid gateway batch, or unsupported execution request. |
| 413 | Request body exceeds the configured maximum. |
| 422 | Pydantic request validation failed. |
| 429 | Per-client request rate limit exceeded. |
| 503 | Gateway disabled or dashboard subscriber limit reached. |

## MCP tools

MCP tools mirror the REST lifecycle and add UI/storage helpers:

| MCP tool | REST equivalent or purpose |
|---|---|
| `sentri_interact` | `POST /interact` |
| `sentri_audit` | `GET /audit` |
| `sentri_verify_permit` | `POST /permits/verify` |
| `sentri_execute` | `POST /execute` |
| `sentri_report_outcome` | `POST /outcomes` |
| `sentri_review` | `POST /approvals/{thread_id}` |
| `sentri_dashboard_data` | Structured dashboard snapshot |
| `sentri_configure_storage` | Storage/retention update |
| `sentri_render_control_room` | Return the MCP App UI resource |

For full field constraints, use the generated schema rather than duplicating it in client code.
