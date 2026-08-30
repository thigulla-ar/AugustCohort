# Operations

## Configuration precedence

1. Code defaults
2. `.env` or process environment
3. `~/Documents/Sentri/settings.json` for dashboard-managed `storage_mode` and `retention_days` only

Environment changes require a restart. Dashboard storage changes apply immediately and persist across restarts.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SENTRI_STORAGE_MODE` | `sqlite` | `ephemeral`, `sqlite`, or `jsonl`. |
| `SENTRI_RETENTION_DAYS` | `30` | Positive days or `Forever`. |
| `SENTRI_DATA_DIR` | `~/Documents/Sentri` | Local telemetry/settings directory. |
| `SENTRI_PUBLIC_BASE_URL` | `http://localhost:8000` | Canonical host origin. |
| `SENTRI_ALLOW_ORIGINS` | loopback origin | JSON list of allowed CORS origins. |
| `SENTRI_AUTH_REQUIRED` | automatic | Defaults to required for non-loopback URLs. |
| `SENTRI_API_TOKEN` | unset | Caller bearer token; minimum 32 characters when required. |
| `SENTRI_SIGNING_SECRET` | local generated key | Permit and audit-chain secret; minimum 32 characters in public mode. |
| `SENTRI_PERMIT_TTL_SECONDS` | `300` | Signed permit lifetime. |
| `SENTRI_MAX_REQUEST_BYTES` | `1000000` | HTTP request body ceiling. |
| `SENTRI_RATE_LIMIT_REQUESTS_PER_MINUTE` | `120` | Per-client in-process rate limit. |
| `SENTRI_MAX_DASHBOARD_SUBSCRIBERS` | `25` | Concurrent SSE subscribers. |
| `SENTRI_GATEWAY_ENABLED` | `false` | Enable controlled model-provider execution. |
| `SENTRI_GATEWAY_TIMEOUT_SECONDS` | `60` | Provider timeout, maximum 600 seconds. |
| `SENTRI_GATEWAY_MAX_OUTPUT_CHARS` | `100000` | Provider output character ceiling. |
| `SENTRI_OPENAI_API_KEY` | unset | Server-side OpenAI credential. |
| `SENTRI_GEMINI_API_KEY` | unset | Server-side Gemini credential. |
| `SENTRI_GATEWAY_ALLOWED_OPENAI_MODELS` | `[]` | Exact OpenAI model allowlist. |
| `SENTRI_GATEWAY_ALLOWED_GEMINI_MODELS` | `[]` | Exact Gemini model allowlist. |
| `SENTRI_GATEWAY_PRICING` | `{}` | Operator-maintained per-million token rates. |

Never reuse the API token as the signing secret.

## Storage modes

| Mode | Location | Durability | Query behavior |
|---|---|---|---|
| Ephemeral | Process RAM | Lost on restart | In-memory filtered list |
| SQLite | `~/Documents/Sentri/sentri.db` | Persistent | Indexed by execution, worker, and time |
| JSONL | `~/Documents/Sentri/logs/YYYY-MM-DD_sentri.jsonl` | Persistent append-only daily files | Sequential scan across daily files |

Changing mode does not migrate or delete data in the previous backend. The dashboard immediately begins reading and writing the newly active backend.

## Retention

Retention runs once at startup and then every 24 hours by default.

- SQLite removes records older than the cutoff.
- Ephemeral removes old in-memory events.
- JSONL removes complete daily files older than the cutoff day.
- `Forever` disables automatic expiration.

This maintenance deletion applies only to Sentri's own expired telemetry and is not available to agents.

## Dashboard metrics

| Metric | Definition |
|---|---|
| Events | Number of telemetry events in the current snapshot. |
| Alerts | Count of Rogue alerts in the snapshot. |
| Tokens | Sum of Clerk token records, including nested terminal gateway usage. |
| Cost USD | Sum of configured Clerk cost estimates. |
| Executions | Distinct executions with Work Queue `call_tree` events. |
| Completion % | Executions with Builder terminal outcomes divided by executions seen. |
| Action p95 | 95th-percentile reported/observed downstream action latency. |

Tokens can legitimately be zero. Preflight alone does not call a provider, and externally executed tools report tokens only when the caller supplies usage diagnostics. The strongest measurement source is `sentri_gateway`.

## Audit integrity

Verify the active store:

```powershell
Invoke-RestMethod http://localhost:8000/audit/verify
```

Or scope verification:

```powershell
Invoke-RestMethod "http://localhost:8000/audit/verify?execution_id=EXECUTION_ID"
```

Investigate any integrity mismatch before relying on the affected chain. Legacy events without hashes are reported as verification errors.

## Health and monitoring

`GET /health` exposes version, active storage mode, retention, hard-limit state, and gateway/provider configuration. It does not expose secrets or verify that provider credentials are accepted upstream.

Recommended operational checks:

- health response and process uptime
- HTTP 401/403/409/429/503 rates
- storage growth and disk capacity
- retention success
- integrity verification
- approval backlog
- gateway latency, failure rate, retries, and cost
- SSE subscriber saturation

## Backup and recovery

- Ephemeral mode has no recovery path.
- For SQLite, stop Sentri or use a SQLite-aware backup process so the database and WAL state are consistent.
- JSONL files can be copied by day, but protect them as sensitive audit data.
- Back up the signing secret independently and securely; changing it prevents validation of prior signatures with the new key.
- The current in-memory LangGraph checkpointer does not preserve pending approvals across restart.

## Production hardening

- Terminate TLS at a trusted reverse proxy.
- Put OAuth/OIDC or another identity-aware gateway in front of Sentri for individual principals.
- Replace the shared bearer principal and in-memory rate limiter for multi-user deployments.
- Replace `MemorySaver` with a durable LangGraph checkpointer.
- Use a managed secret store and rotate credentials under an incident-tested procedure.
- Restrict egress to approved provider origins.
- Forward audit evidence to an independently controlled sink if local tamper evidence is insufficient.
- Run one writer process per SQLite/JSONL data directory unless concurrency is engineered and tested.
- Never expose the local dashboard without the same authentication boundary as the API.

## Release validation

```powershell
pytest -q --basetemp .sentri-test-release -p no:cacheprovider
python scripts/export_openapi.py
python scripts/check_public_repo.py
git diff --check
```

Review `git status --short --ignored` before publishing. `.env`, signing keys, databases, JSONL files, and runtime settings must remain untracked.
