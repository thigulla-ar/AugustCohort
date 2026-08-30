# Security policy

## Supported versions

Security fixes are applied to the latest release on the default branch. Sentri
is pre-1.0 software, so operators should upgrade to the newest available
version rather than relying on an older release line.

## Reporting a vulnerability

Do not disclose a suspected vulnerability, credential, personal information,
or exploit details in a public issue.

Use the repository's private **Security > Advisories > Report a vulnerability**
workflow. If private vulnerability reporting has not yet been enabled, contact
the repository owner privately and ask them to enable it before sharing
technical details.

Include the affected version, deployment mode, reproduction steps, expected and
observed behavior, and impact. Redact access tokens, signing secrets, permits,
personal information, local paths, database contents, and telemetry payloads.

## Operator responsibilities

- Never commit `.env`, `.sentri-signing.key`, databases, JSONL telemetry, or
  runtime `settings.json` files.
- Use independent high-entropy API-token and signing-secret values. Store model
  provider keys in environment variables or a secret manager, never in action
  arguments, chat messages, telemetry, or source control.
- Keep `YOUR_PUBLIC_SENTRI_HOST` in the public schema; inject a deployment URL
  through a release/deployment process when appropriate.
- Put non-loopback deployments behind TLS and OAuth/OIDC or an equivalent
  identity-aware gateway.
- Run `python scripts/check_public_repo.py` before publishing and enable the
  hosting provider's secret scanning and push protection.

Sentri authorization permits prove that an exact action passed the configured
policy. Calls made through `POST /execute` are directly observed by the
Sentri-controlled gateway; external downstream calls remain caller-reported.
The gateway is limited to fixed provider origins, explicit model allowlists,
non-mutating text generation, bounded responses, and server-side credentials.
It suppresses output when personal information or secret-like material is found.
