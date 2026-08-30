# Sentri documentation

Sentri is a local-first governance and observability layer for agent actions initiated by ChatGPT, Gemini, or another tool-capable host. It evaluates proposed actions before execution, issues exact-action permits, records five correlated telemetry views, and supports human review without persisting raw model or tool output.

## Choose your path

- **First-time operator:** start with [Getting started](getting-started.md), then read [Operations](operations.md).
- **Chat platform integrator:** read [Host integrations](integrations.md) and [API reference](api-reference.md).
- **Security or platform architect:** read [Architecture](architecture.md), [`SECURITY.md`](../SECURITY.md), and [Operations](operations.md).
- **Incident investigator:** use the [Incident investigation guide](incident-investigation.md).

## Documentation map

| Guide | Purpose |
|---|---|
| [Getting started](getting-started.md) | Install, configure, run, and submit the first governed action. |
| [Architecture](architecture.md) | Understand the LangGraph, five workers, trust boundaries, permits, and state. |
| [Host integrations](integrations.md) | Connect REST/OpenAPI, ChatGPT MCP UI, GPT Actions, or a Gemini client. |
| [API reference](api-reference.md) | Learn the REST endpoints, MCP tools, statuses, and request lifecycle. |
| [Operations](operations.md) | Manage storage, retention, secrets, metrics, integrity, and production deployment. |
| [Incident investigation](incident-investigation.md) | Reconstruct failures, policy blocks, cost anomalies, and performance issues. |
| [Security policy](../SECURITY.md) | Report vulnerabilities and follow secure deployment practices. |

## Important behavior

Sentri is not a passive chat recorder. A conversation appears in Sentri only when the host explicitly calls a Sentri REST endpoint or MCP tool. A phrase such as `Sentri Track` or `Sentri Execute` has no special power unless the host integration is configured to translate it into those calls.

Sentri also does not execute arbitrary downstream tools. It either:

1. authorizes an exact proposed action and returns a signed permit for the host to enforce; or
2. executes a narrowly supported, allowlisted OpenAI or Gemini text-generation action through its controlled gateway.

The three hard limits—sending money, deleting records, and sharing unhashed personal information—cannot be overridden by a reviewer.

## Demo

The edited control-room walkthrough is available as [`Sentri-Control-Room-Demo.mp4`](../Sentri-Control-Room-Demo.mp4).
