# Incident investigation

## Investigation goals

Sentri's five views answer different questions about one correlated execution:

1. What did the host intend to do?
2. What exact tools and arguments were authorized?
3. Which policy controls ran, and what did they decide?
4. What was actually reported or directly observed?
5. What changed, how long did it take, and what did it cost?
6. Can the final outcome be reconstructed without retaining raw sensitive content?

## Start with an execution ID

Obtain the `execution_id` from the host response, dashboard, or Work Queue event.

```powershell
$audit = Invoke-RestMethod `
  "http://localhost:8000/audit?execution_id=EXECUTION_ID&limit=500"

$audit.events | Format-Table timestamp, worker, kind, sequence
```

If the service is authenticated, add the bearer header.

## Verify integrity first

```powershell
Invoke-RestMethod `
  "http://localhost:8000/audit/verify?execution_id=EXECUTION_ID"
```

If `valid` is false, preserve the store and signing-key context before further mutation. An integrity failure can indicate manual edits, partial restoration, mixed signing keys, or legacy unsigned events.

## Read the execution in order

### 1. Work Queue: scope and lineage

Inspect:

- caller and host
- action IDs, tools, operations, rationale, and canonical hashes
- requested capabilities
- upstream tool references and evidence IDs
- terminal provider/model/request IDs
- `caller_reported` versus `sentri_gateway_observed` attestation

Use this view to establish what the host asked Sentri to authorize and whether later results cover every authorized action.

### 2. Rogue: policy and circuit breaker

Inspect:

- alert codes and severity
- `hard_limit` and `requires_human`
- the three mandatory control results
- permit validation and bypass indicators
- post-execution final-response PII scan

For a hard-limit alert, confirm that no permit authorized the forbidden action. A reviewer acknowledgement is not an override.

### 3. Change Log: impact

Inspect:

- proposed targets and mutation type
- observed change evidence
- before/after hashes
- rollback status
- evidence gaps

External change evidence is caller-reported unless Sentri itself performed the supported gateway action. Missing evidence should be recorded as uncertainty, not treated as proof that nothing changed.

### 4. Clerk: performance and cost

Inspect:

- total and per-action latency
- missing latency measurements
- attempts and retries
- timeouts and HTTP status
- cache hits
- prompt, completion, and total tokens
- pricing source and estimated cost

`source: sentri_gateway` means Sentri read usage from the provider response. `caller_supplied` means the host supplied the measurement. Zero usage with `caller_supplied` usually means the host did not report tokens.

### 5. Builder: final reconstruction

Read the DAG from `host` to `router`, proposed actions, optional upstream tools, tool results, and `final_response`.

Important fields include:

- overall status and reported action IDs
- action and output hashes
- start/finish time and latency
- error and bounded diagnostics
- final response hash
- reconstruction node/edge counts and action coverage

The Builder hash proves correlation with submitted content; it does not let an investigator recover raw content that Sentri intentionally did not retain.

## Common playbooks

### Hard-limit block

1. Find the Rogue alert code.
2. Identify the matching action ID in Work Queue.
3. Confirm Builder never contains an authorized result path for that action.
4. Check for permit verification or outcome attempts after the block.
5. Treat disguised retries as a possible host-integration bypass.
6. Revise the requested workflow rather than approving the forbidden action.

### Failed provider execution

1. Confirm Work Queue attestation is `sentri_gateway_observed`.
2. Inspect Clerk HTTP status, timeout, retry count, and latency.
3. Match the provider request ID for upstream support.
4. Check allowlist, gateway-enabled state, timeout, and output bounds.
5. Confirm remaining batch actions were aborted after failure.
6. Verify that no raw provider output or key reached telemetry.

### Token or cost anomaly

1. Separate `sentri_gateway` measurements from `caller_supplied` values.
2. Confirm provider/model and request ID.
3. Compare prompt versus completion tokens and cache status.
4. Verify the operator pricing entry for `provider:model`.
5. Check retries so repeated calls are not mistaken for one expensive call.
6. Remember that changing pricing affects future estimates, not historical payloads.

### High Action p95

1. Open Clerk `latency.per_action` records.
2. Separate missing measurements from slow actions.
3. Group by provider, model, tool version, timeout, retry, and cache status.
4. Use Work Queue request IDs to correlate upstream logs.
5. Check whether approval wait time is being confused with tool execution time.

### Low completion percentage

1. Count Work Queue `call_tree` executions.
2. Find executions without Builder `execution_outcome` events.
3. Determine whether they are awaiting review, blocked, abandoned before outcome reporting, or lost during restart.
4. Inspect the host integration for missing `/outcomes` calls.
5. Check whether pending in-memory approvals were lost when the backend restarted.

## Evidence quality labels

| Label | Meaning |
|---|---|
| `sentri_gateway_observed` | Sentri directly made the supported provider request and read its response metadata. |
| `permit_consumed` | Sentri verified and consumed authorization immediately before the claimed execution boundary. |
| `caller_reported` | The host supplied the external result or upstream lineage. |

Do not collapse these labels into one confidence level.

## Export an investigation bundle

Export only the execution needed for the case:

```powershell
$headers = @{}
# For authenticated deployments:
# $headers.Authorization = "Bearer YOUR_SENTRI_API_TOKEN"

Invoke-RestMethod `
  -Headers $headers `
  -Uri "http://localhost:8000/audit?execution_id=EXECUTION_ID&limit=5000" |
  ConvertTo-Json -Depth 30 |
  Set-Content -Encoding utf8 sentri-investigation.json
```

Treat the export as sensitive even though Sentri redacts known secret and PII patterns. Review it before sharing, preserve its hash, and use a protected case-management channel.

## Known evidence limitations

- Sentri does not record chat turns that were never sent to it.
- External tool results and diagnostics can be inaccurate when caller-reported.
- Raw prompts, outputs, and final responses are intentionally not retained.
- Conversation IDs are hashed.
- Evidence URI query strings and fragments are removed.
- In-memory interrupted state is lost on restart.
- Local HMAC integrity is tamper-evident, not independently immutable.
