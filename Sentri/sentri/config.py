from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


StorageMode = Literal["ephemeral", "sqlite", "jsonl"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SENTRI_", extra="ignore"
    )

    storage_mode: StorageMode = "sqlite"
    retention_days: int | None = 30
    data_dir: Path = Path.home() / "Documents" / "Sentri"
    public_base_url: str = "http://localhost:8000"
    allow_origins: list[str] = ["http://localhost:8000"]
    retention_interval_seconds: int = 86_400
    dashboard_heartbeat_seconds: int = 15
    auth_required: bool | None = None
    api_token: str | None = None
    signing_secret: str | None = None
    permit_ttl_seconds: int = 300
    max_request_bytes: int = 1_000_000
    rate_limit_requests_per_minute: int = 120
    max_dashboard_subscribers: int = 25
    gateway_enabled: bool = False
    gateway_timeout_seconds: float = 60.0
    gateway_max_output_chars: int = 100_000
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    gateway_allowed_openai_models: list[str] = Field(default_factory=list)
    gateway_allowed_gemini_models: list[str] = Field(default_factory=list)
    gateway_pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("data_dir", mode="before")
    @classmethod
    def expand_data_dir(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            return Path(value).expanduser()
        return value

    @field_validator("retention_days", mode="before")
    @classmethod
    def parse_forever(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() == "forever":
            return None
        return value

    @field_validator("retention_days")
    @classmethod
    def positive_retention(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("retention_days must be at least 1 or 'Forever'")
        return value

    @field_validator(
        "permit_ttl_seconds",
        "max_request_bytes",
        "rate_limit_requests_per_minute",
        "max_dashboard_subscribers",
        "gateway_max_output_chars",
    )
    @classmethod
    def positive_security_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("security limits must be positive")
        return value

    @field_validator("gateway_timeout_seconds")
    @classmethod
    def positive_gateway_timeout(cls, value: float) -> float:
        if value <= 0 or value > 600:
            raise ValueError("gateway_timeout_seconds must be between 0 and 600")
        return value

    @field_validator("openai_api_key", "gemini_api_key", mode="before")
    @classmethod
    def blank_provider_keys_are_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_public_security(self) -> "Settings":
        from sentri.security import is_loopback_url

        public = not is_loopback_url(self.public_base_url)
        required = self.auth_required
        if required is None:
            required = public
        if public and required is False:
            raise ValueError("Authentication cannot be disabled for a non-loopback URL.")
        self.auth_required = required
        if required:
            if not self.api_token or len(self.api_token) < 32:
                raise ValueError(
                    "A non-loopback Sentri deployment requires SENTRI_API_TOKEN "
                    "with at least 32 characters."
                )
            if not self.signing_secret or len(self.signing_secret) < 32:
                raise ValueError(
                    "A non-loopback Sentri deployment requires SENTRI_SIGNING_SECRET "
                    "with at least 32 characters."
                )
            if "*" in self.allow_origins:
                raise ValueError("Authenticated Sentri deployments cannot allow CORS origin '*'.")
        if self.gateway_enabled:
            if not self.openai_api_key and not self.gemini_api_key:
                raise ValueError(
                    "The execution gateway requires SENTRI_OPENAI_API_KEY or "
                    "SENTRI_GEMINI_API_KEY."
                )
            if self.openai_api_key and not self.gateway_allowed_openai_models:
                raise ValueError(
                    "SENTRI_GATEWAY_ALLOWED_OPENAI_MODELS must explicitly allow models."
                )
            if self.gemini_api_key and not self.gateway_allowed_gemini_models:
                raise ValueError(
                    "SENTRI_GATEWAY_ALLOWED_GEMINI_MODELS must explicitly allow models."
                )
        for key, entry in self.gateway_pricing.items():
            if not isinstance(entry, dict):
                raise ValueError(f"Gateway pricing entry {key!r} must be an object.")
            for field in ("input_per_million", "output_per_million"):
                try:
                    amount = float(entry.get(field, 0))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"Gateway pricing {key!r}.{field} is invalid.") from exc
                if not math.isfinite(amount) or amount < 0 or amount > 1_000_000:
                    raise ValueError(f"Gateway pricing {key!r}.{field} is out of range.")
        return self

    @property
    def runtime_settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def with_runtime_overrides(self) -> "Settings":
        """Load dashboard-managed storage settings, if they exist."""
        path = self.runtime_settings_path
        if not path.exists():
            return self
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self
        allowed = {
            key: payload[key]
            for key in ("storage_mode", "retention_days")
            if key in payload
        }
        return Settings.model_validate({**self.model_dump(), **allowed})

    def persist_runtime_settings(self) -> None:
        """Atomically persist settings managed by the local dashboard."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.runtime_settings_path
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "storage_mode": self.storage_mode,
                    "retention_days": self.retention_days,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
