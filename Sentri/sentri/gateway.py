from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sentri.config import Settings
from sentri.models import CostMetrics, PlannedAction
from sentri.redaction import SECRET_KEY_RE, pii_types


OPENAI_TOOL = "openai.responses"
OPENAI_OPERATION = "create"
GEMINI_TOOL = "gemini.generate_content"
GEMINI_OPERATION = "generate"
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


class GatewayError(RuntimeError):
    pass


class GatewayDisabledError(GatewayError):
    pass


class GatewayRequestError(GatewayError):
    pass


class ProviderCallError(GatewayError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        http_status: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.http_status = http_status
        self.provider_request_id = provider_request_id


class OpenAIArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=256)
    input: str = Field(min_length=1, max_length=100_000)
    instructions: str | None = Field(default=None, max_length=20_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=100_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)


class GeminiArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=256)
    contents: str = Field(min_length=1, max_length=100_000)
    system_instruction: str | None = Field(default=None, max_length=20_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=100_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)


@dataclass(frozen=True)
class ProviderResult:
    output: str
    provider: str
    model: str
    model_version: str | None
    provider_request_id: str | None
    http_status: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0


class ProviderAdapter(Protocol):
    provider: str

    def validate(self, action: PlannedAction) -> None: ...

    async def execute(self, action: PlannedAction) -> ProviderResult: ...


def _bounded_token(value: Any) -> int:
    try:
        return min(max(int(value or 0), 0), 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0


def _reject_secret_fields(value: Any, path: str = "arguments") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                raise GatewayRequestError(
                    f"Secret-bearing field {path}.{key} is forbidden; configure keys server-side."
                )
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def _reject_secret_values(value: Any, settings: Settings) -> None:
    serialized = json.dumps(value, default=str, ensure_ascii=False)
    configured = [
        secret.get_secret_value()
        for secret in (settings.openai_api_key, settings.gemini_api_key)
        if secret and secret.get_secret_value()
    ]
    if any(secret in serialized for secret in configured) or any(
        pattern.search(serialized) for pattern in SECRET_VALUE_PATTERNS
    ):
        raise GatewayRequestError(
            "A secret-like value was found in provider arguments; configure secrets server-side."
        )


def _contains_secret_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def _safe_request_id(response: httpx.Response) -> str | None:
    value = response.headers.get("x-request-id") or response.headers.get(
        "x-goog-request-id"
    )
    return value[:512] if value else None


def _safe_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:maximum] or None


class OpenAIResponsesAdapter:
    provider = "openai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    def _arguments(self, action: PlannedAction) -> OpenAIArguments:
        if action.tool.casefold() != OPENAI_TOOL or action.operation.casefold() != OPENAI_OPERATION:
            raise GatewayRequestError(
                f"Unsupported OpenAI action; use {OPENAI_TOOL}.{OPENAI_OPERATION}."
            )
        if action.mutates_state:
            raise GatewayRequestError("Model-generation gateway actions cannot mutate state.")
        _reject_secret_fields(action.arguments)
        _reject_secret_values(action.arguments, self.settings)
        try:
            arguments = OpenAIArguments.model_validate(action.arguments)
        except ValidationError as exc:
            raise GatewayRequestError("OpenAI gateway arguments are invalid.") from exc
        if arguments.model not in self.settings.gateway_allowed_openai_models:
            raise GatewayRequestError(
                f"OpenAI model {arguments.model!r} is not in the gateway allowlist."
            )
        return arguments

    def validate(self, action: PlannedAction) -> None:
        self._arguments(action)
        if not self.settings.openai_api_key:
            raise GatewayRequestError("The OpenAI gateway key is not configured.")

    async def execute(self, action: PlannedAction) -> ProviderResult:
        arguments = self._arguments(action)
        if not self.settings.openai_api_key:
            raise GatewayRequestError("The OpenAI gateway key is not configured.")
        payload = arguments.model_dump(exclude_none=True)
        payload["store"] = False
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": (
                        f"Bearer {self.settings.openai_api_key.get_secret_value()}"
                    ),
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ProviderCallError("OpenAI request timed out.", provider=self.provider) from exc
        except httpx.HTTPError as exc:
            raise ProviderCallError("OpenAI transport failed.", provider=self.provider) from exc
        request_id = _safe_request_id(response)
        if response.status_code >= 400:
            raise ProviderCallError(
                f"OpenAI returned HTTP {response.status_code}.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=request_id,
            )
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderCallError(
                "OpenAI returned an invalid JSON response.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=request_id,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderCallError(
                "OpenAI returned an unexpected response shape.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=request_id,
            )
        if data.get("status") not in {None, "completed"}:
            raise ProviderCallError(
                f"OpenAI response status was {data.get('status')!r}.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=_safe_text(data.get("id") or request_id, 512),
            )
        output_parts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    output_parts.append(content["text"])
        output = "\n".join(output_parts)
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        prompt_tokens = _bounded_token(usage.get("input_tokens"))
        completion_tokens = _bounded_token(usage.get("output_tokens"))
        total_tokens = _bounded_token(usage.get("total_tokens"))
        return ProviderResult(
            output=output,
            provider=self.provider,
            model=arguments.model,
            model_version=_safe_text(data.get("model") or arguments.model, 256),
            provider_request_id=_safe_text(data.get("id") or request_id, 512),
            http_status=response.status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens or prompt_tokens + completion_tokens,
            cached_tokens=_bounded_token(details.get("cached_tokens")),
        )


class GeminiGenerateContentAdapter:
    provider = "gemini"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    def _arguments(self, action: PlannedAction) -> GeminiArguments:
        if action.tool.casefold() != GEMINI_TOOL or action.operation.casefold() != GEMINI_OPERATION:
            raise GatewayRequestError(
                f"Unsupported Gemini action; use {GEMINI_TOOL}.{GEMINI_OPERATION}."
            )
        if action.mutates_state:
            raise GatewayRequestError("Model-generation gateway actions cannot mutate state.")
        _reject_secret_fields(action.arguments)
        _reject_secret_values(action.arguments, self.settings)
        try:
            arguments = GeminiArguments.model_validate(action.arguments)
        except ValidationError as exc:
            raise GatewayRequestError("Gemini gateway arguments are invalid.") from exc
        if not MODEL_NAME_RE.fullmatch(arguments.model):
            raise GatewayRequestError("Gemini model name contains forbidden characters.")
        if arguments.model not in self.settings.gateway_allowed_gemini_models:
            raise GatewayRequestError(
                f"Gemini model {arguments.model!r} is not in the gateway allowlist."
            )
        return arguments

    def validate(self, action: PlannedAction) -> None:
        self._arguments(action)
        if not self.settings.gemini_api_key:
            raise GatewayRequestError("The Gemini gateway key is not configured.")

    async def execute(self, action: PlannedAction) -> ProviderResult:
        arguments = self._arguments(action)
        if not self.settings.gemini_api_key:
            raise GatewayRequestError("The Gemini gateway key is not configured.")
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": arguments.contents}]}]
        }
        if arguments.system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": arguments.system_instruction}]
            }
        generation_config = {
            "maxOutputTokens": arguments.max_output_tokens,
            "temperature": arguments.temperature,
            "topP": arguments.top_p,
        }
        compact_config = {key: value for key, value in generation_config.items() if value is not None}
        if compact_config:
            payload["generationConfig"] = compact_config
        model = quote(arguments.model, safe="-_.")
        try:
            response = await self.client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={
                    "x-goog-api-key": self.settings.gemini_api_key.get_secret_value(),
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ProviderCallError("Gemini request timed out.", provider=self.provider) from exc
        except httpx.HTTPError as exc:
            raise ProviderCallError("Gemini transport failed.", provider=self.provider) from exc
        request_id = _safe_request_id(response)
        if response.status_code >= 400:
            raise ProviderCallError(
                f"Gemini returned HTTP {response.status_code}.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=request_id,
            )
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderCallError(
                "Gemini returned an invalid JSON response.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=request_id,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderCallError(
                "Gemini returned an unexpected response shape.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=request_id,
            )
        prompt_feedback = data.get("promptFeedback") or {}
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            raise ProviderCallError(
                "Gemini blocked the prompt.",
                provider=self.provider,
                http_status=response.status_code,
                provider_request_id=_safe_text(data.get("responseId") or request_id, 512),
            )
        output_parts: list[str] = []
        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    output_parts.append(part["text"])
        usage = data.get("usageMetadata") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = _bounded_token(usage.get("promptTokenCount"))
        completion_tokens = _bounded_token(usage.get("candidatesTokenCount"))
        total_tokens = _bounded_token(usage.get("totalTokenCount"))
        return ProviderResult(
            output="\n".join(output_parts),
            provider=self.provider,
            model=arguments.model,
            model_version=_safe_text(data.get("modelVersion") or arguments.model, 256),
            provider_request_id=_safe_text(data.get("responseId") or request_id, 512),
            http_status=response.status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens or prompt_tokens + completion_tokens,
            cached_tokens=_bounded_token(usage.get("cachedContentTokenCount")),
        )


class ExecutionGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        adapters: dict[str, ProviderAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.gateway_timeout_seconds),
            follow_redirects=False,
        )
        self.adapters: dict[str, ProviderAdapter] = adapters or {
            OPENAI_TOOL: OpenAIResponsesAdapter(settings, self.client),
            GEMINI_TOOL: GeminiGenerateContentAdapter(settings, self.client),
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def adapter_for(self, action: PlannedAction) -> ProviderAdapter:
        adapter = self.adapters.get(action.tool.casefold())
        if not adapter:
            raise GatewayRequestError(
                f"Tool {action.tool!r} is not available through the Sentri execution gateway."
            )
        adapter.validate(action)
        return adapter

    async def execute(self, action: PlannedAction) -> ProviderResult:
        result = await self.adapter_for(action).execute(action)
        if len(result.output) > self.settings.gateway_max_output_chars:
            raise ProviderCallError(
                "Provider output exceeded the configured gateway limit.",
                provider=result.provider,
                http_status=result.http_status,
                provider_request_id=result.provider_request_id,
            )
        found_pii = pii_types(result.output)
        if found_pii:
            raise ProviderCallError(
                "Provider output contained personal information and was suppressed.",
                provider=result.provider,
                http_status=result.http_status,
                provider_request_id=result.provider_request_id,
            )
        if _contains_secret_value(result.output):
            raise ProviderCallError(
                "Provider output contained a secret-like value and was suppressed.",
                provider=result.provider,
                http_status=result.http_status,
                provider_request_id=result.provider_request_id,
            )
        return result

    def cost_metrics(self, result: ProviderResult, latency_ms: float) -> CostMetrics:
        entry = self.settings.gateway_pricing.get(
            f"{result.provider}:{result.model}", {}
        )
        input_rate = float(entry.get("input_per_million", 0))
        output_rate = float(entry.get("output_per_million", 0))
        cost = (
            result.prompt_tokens * input_rate
            + result.completion_tokens * output_rate
        ) / 1_000_000
        source = str(entry.get("source") or "sentri_gateway_unpriced")
        return CostMetrics(
            latency_ms=latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            estimated_cost_usd=cost,
            pricing_source=source,
        )
