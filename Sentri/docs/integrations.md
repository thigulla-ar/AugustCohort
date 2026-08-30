# Host integrations

## Integration contract

Every host integration must enforce the same lifecycle:

1. Convert the intended downstream calls into exact `PlannedAction` objects.
2. Call `/interact` or `sentri_interact` before execution.
3. Stop when the status is `approval_required`, `blocked`, or `error`.
4. For `completed`, also require `result.authorized` to be `true`.
5. Immediately before each external tool call, consume its exact permit.
6. Execute only the verified action, without changing arguments.
7. Report every outcome through `/outcomes` or `sentri_report_outcome`.
8. Prefer `/execute` or `sentri_execute` for supported provider generation so Sentri observes usage directly.

Sentri cannot passively observe all conversation turns. The host must call it. A custom command such as `Sentri Track` works only when host instructions or application code map that phrase to the Sentri tools.

## Connection options

| Option | Best for | Endpoint or artifact | Embedded UI |
|---|---|---|---|
| REST | Custom applications and automation | FastAPI endpoints | Use `/dashboard` separately |
| OpenAPI/GPT Action | Classic Custom GPT action workflows | `openapi.json` | No MCP App UI |
| MCP | ChatGPT plugins/apps and MCP-compatible clients | `/mcp/` | Yes, when the host supports MCP Apps UI |

## ChatGPT through MCP

Sentri exposes a Streamable HTTP MCP server at:

```text
https://YOUR_PUBLIC_SENTRI_HOST/mcp/
```

It also registers `ui://sentri/control-room-v1.html`. The `sentri_render_control_room` tool returns that resource so a compatible host can render the five-tab control room inside the conversation. The official OpenAI plugin quickstart describes this model as an MCP server with an optional iframe-rendered web component: [MCP server and UI quickstart](https://developers.openai.com/plugins/build/app-quickstart).

Connection steps vary by ChatGPT product and workspace policy. Use OpenAI's current [connect and test guidance](https://developers.openai.com/plugins/deploy/connect-chatgpt), then provide the public `/mcp/` URL and the Sentri bearer credential when required.

Recommended host instructions:

```text
Before any downstream tool execution, call sentri_interact with the exact
proposed actions. Continue only when status is completed and authorized is
true. Consume each permit immediately before an external action. Prefer
sentri_execute for supported OpenAI or Gemini generation. Report all external
outcomes. Never bypass blocked or approval-required decisions. When review or
inspection is requested, call sentri_render_control_room.
```

The most important MCP tools are:

- `sentri_interact`
- `sentri_verify_permit`
- `sentri_execute`
- `sentri_report_outcome`
- `sentri_review`
- `sentri_audit`
- `sentri_render_control_room`
- `sentri_dashboard_data`
- `sentri_configure_storage`

## ChatGPT through a GPT Action

1. Deploy Sentri or expose it through a controlled HTTPS tunnel.
2. Set `SENTRI_PUBLIC_BASE_URL` to the public origin.
3. Generate independent API and signing secrets.
4. Replace `YOUR_PUBLIC_SENTRI_HOST` in a deployment copy of `openapi.json`.
5. Import that JSON as the GPT Action schema.
6. Configure API-key authentication as a Bearer credential using `SENTRI_API_TOKEN`.
7. Add the lifecycle instructions shown above, using REST operation IDs instead of MCP names.
8. Test a read-only request before testing state-changing review.

Keep the placeholder in the public source repository. Do not commit a deployed schema containing a temporary tunnel address if that address is private or short-lived.

A GPT can invoke only the operations exposed in `openapi.json`. Dashboard SSE and local settings routes are deliberately excluded from the host-facing schema.

## Gemini and other hosts

For a tool-capable Gemini application, map the schemas from `openapi.json` into the host's function declarations, or connect `/mcp/` when the chosen Gemini client supports remote MCP. Google's current Gemini API describes function tools as code that lets the model interact with systems outside the model's knowledge: [Gemini function calling](https://ai.google.dev/api/generate-content#FunctionDeclaration).

The governance lifecycle remains identical. The UI resource renders only when that particular client implements MCP Apps UI; otherwise the MCP tools still return structured data.

## Public deployment configuration

Generate two independent secrets:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set them only in the server environment:

```powershell
$env:SENTRI_PUBLIC_BASE_URL = "https://sentri.example.com"
$env:SENTRI_API_TOKEN = "first-generated-secret"
$env:SENTRI_SIGNING_SECRET = "second-generated-secret"
$env:SENTRI_ALLOW_ORIGINS = '["https://chatgpt.com"]'
python -m sentri.main
```

The API token authenticates callers. The signing secret signs permits and telemetry. Neither belongs in a prompt, action argument, OpenAPI file, dashboard payload, or source repository.

## Controlled provider execution

Enable the gateway only when the server should make provider calls:

```powershell
$env:SENTRI_GATEWAY_ENABLED = "true"
$env:SENTRI_OPENAI_API_KEY = "server-side-provider-key"
$env:SENTRI_GATEWAY_ALLOWED_OPENAI_MODELS = '["YOUR_ALLOWED_MODEL"]'
$env:SENTRI_GATEWAY_PRICING = '{"openai:YOUR_ALLOWED_MODEL":{"input_per_million":0,"output_per_million":0,"source":"operator_configured"}}'
```

Use current provider pricing maintained by the operator. A zero or absent rate preserves token counts but produces zero estimated cost.

## Troubleshooting connections

- **No telemetry:** verify the host actually called `sentri_interact`; ordinary chat text is not automatically forwarded.
- **Connection to localhost fails:** hosted runtimes cannot reach your loopback interface.
- **401 response:** configure the same bearer token in the host and Sentri environment.
- **Trusted host or CORS rejection:** make `SENTRI_PUBLIC_BASE_URL` and `SENTRI_ALLOW_ORIGINS` match the deployment.
- **MCP UI does not render:** confirm the client supports MCP Apps UI; use the structured tools or `/dashboard` fallback otherwise.
- **Approval disappears after restart:** the current LangGraph checkpointer is in memory.
