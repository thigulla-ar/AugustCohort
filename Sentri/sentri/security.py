from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sentri.models import PlannedAction, TelemetryEvent


class PermitError(ValueError):
    """Raised when a signed action permit is invalid or expired."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def action_hash(action: PlannedAction | dict[str, Any]) -> str:
    validated = (
        action if isinstance(action, PlannedAction) else PlannedAction.model_validate(action)
    )
    digest = hashlib.sha256(canonical_json(validated.model_dump(mode="json"))).hexdigest()
    return f"sha256:{digest}"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise PermitError("Permit encoding is invalid.") from exc


@dataclass(frozen=True)
class VerifiedPermit:
    execution_id: str
    thread_id: str
    action_id: str
    action_hash: str
    nonce: str
    issued_at: int
    expires_at: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "thread_id": self.thread_id,
            "action_id": self.action_id,
            "action_hash": self.action_hash,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


class PermitSigner:
    def __init__(self, secret: str, ttl_seconds: int = 300) -> None:
        if len(secret) < 32:
            raise ValueError("The Sentri signing secret must contain at least 32 characters.")
        self._key = hashlib.sha256(("sentri-permit:" + secret).encode("utf-8")).digest()
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        execution_id: str,
        thread_id: str,
        action: PlannedAction | dict[str, Any],
    ) -> dict[str, Any]:
        validated = (
            action
            if isinstance(action, PlannedAction)
            else PlannedAction.model_validate(action)
        )
        issued_at = int(time.time())
        payload = {
            "v": 1,
            "execution_id": execution_id,
            "thread_id": thread_id,
            "action_id": validated.id,
            "action_hash": action_hash(validated),
            "nonce": secrets.token_urlsafe(18),
            "iat": issued_at,
            "exp": issued_at + self.ttl_seconds,
        }
        encoded = _b64encode(canonical_json(payload))
        signature = _b64encode(hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest())
        return {
            "action_id": validated.id,
            "action_hash": payload["action_hash"],
            "expires_at": payload["exp"],
            "permit": f"{encoded}.{signature}",
        }

    def verify(
        self,
        permit: str,
        action: PlannedAction | dict[str, Any],
        *,
        execution_id: str | None = None,
        thread_id: str | None = None,
        now: int | None = None,
        allow_expired: bool = False,
    ) -> VerifiedPermit:
        try:
            encoded, supplied_signature = permit.split(".", 1)
        except ValueError as exc:
            raise PermitError("Permit format is invalid.") from exc
        expected_signature = _b64encode(
            hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise PermitError("Permit signature is invalid.")
        try:
            payload = json.loads(_b64decode(encoded))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermitError("Permit payload is invalid.") from exc
        required = {
            "v",
            "execution_id",
            "thread_id",
            "action_id",
            "action_hash",
            "nonce",
            "iat",
            "exp",
        }
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise PermitError("Permit claims are incomplete.")
        if payload["v"] != 1:
            raise PermitError("Permit version is unsupported.")
        current = int(time.time()) if now is None else now
        if not isinstance(payload["exp"], int) or (
            not allow_expired and current > payload["exp"]
        ):
            raise PermitError("Permit has expired.")
        if not isinstance(payload["iat"], int) or payload["iat"] > current + 30:
            raise PermitError("Permit issue time is invalid.")
        validated = (
            action
            if isinstance(action, PlannedAction)
            else PlannedAction.model_validate(action)
        )
        if payload["action_id"] != validated.id:
            raise PermitError("Permit action ID does not match the proposed action.")
        expected_hash = action_hash(validated)
        if not hmac.compare_digest(str(payload["action_hash"]), expected_hash):
            raise PermitError("Permit does not authorize these exact action arguments.")
        if execution_id is not None and payload["execution_id"] != execution_id:
            raise PermitError("Permit execution ID does not match.")
        if thread_id is not None and payload["thread_id"] != thread_id:
            raise PermitError("Permit thread ID does not match.")
        return VerifiedPermit(
            execution_id=str(payload["execution_id"]),
            thread_id=str(payload["thread_id"]),
            action_id=str(payload["action_id"]),
            action_hash=str(payload["action_hash"]),
            nonce=str(payload["nonce"]),
            issued_at=payload["iat"],
            expires_at=payload["exp"],
        )


def sign_telemetry_event(event: TelemetryEvent, secret: str) -> str:
    value = event.model_dump(mode="json", exclude={"integrity_hash"})
    key = hashlib.sha256(("sentri-audit:" + secret).encode("utf-8")).digest()
    return f"hmac-sha256:{hmac.new(key, canonical_json(value), hashlib.sha256).hexdigest()}"


def is_loopback_url(value: str) -> bool:
    hostname = (urlparse(value).hostname or "").casefold()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def load_or_create_local_secret(data_dir: Path) -> str:
    """Persist a development-only key so local audit chains survive restarts."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".sentri-signing.key"
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if len(secret) < 32:
            raise ValueError(f"Local Sentri signing key is invalid: {path}")
        return secret
    generated = secrets.token_urlsafe(48)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(generated + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return generated
    except FileExistsError:
        secret = path.read_text(encoding="utf-8").strip()
        if len(secret) < 32:
            raise ValueError(f"Local Sentri signing key is invalid: {path}")
        return secret
