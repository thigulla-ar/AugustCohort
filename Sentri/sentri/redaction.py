from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any


PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"(?<!\w)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\w)"),
    "card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "ipv4": re.compile(
        r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\."
        r"(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b"
    ),
    "date_of_birth": re.compile(
        r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b"
    ),
    "postal_address": re.compile(
        r"\b\d{1,6}\s+[A-Z0-9.' -]{2,60}\s+"
        r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|BOULEVARD|BLVD|LANE|LN|DRIVE|DR|COURT|CT)\b",
        re.I,
    ),
}

_REDACTION_KEY = secrets.token_bytes(32)
SECRET_KEY_RE = re.compile(
    r"^(?:password|passwd|secret|client[_-]?secret|api[_-]?key|authorization|"
    r"cookie|credential|access[_-]?token|refresh[_-]?token|bearer[_-]?token|id[_-]?token)s?$",
    re.I,
)


def configure_redaction_key(secret: str) -> None:
    global _REDACTION_KEY
    _REDACTION_KEY = hashlib.sha256(("sentri-redaction:" + secret).encode("utf-8")).digest()


def pii_types(text: str) -> list[str]:
    return [name for name, pattern in PII_PATTERNS.items() if pattern.search(text)]


def _digest(value: str) -> str:
    return hmac.new(_REDACTION_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def redact_text(text: str) -> str:
    for name, pattern in PII_PATTERNS.items():
        text = pattern.sub(lambda match: f"[HASHED_{name.upper()}:{_digest(match.group(0))}]", text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            redact_text(str(key)): (
                "[REDACTED_SECRET]"
                if SECRET_KEY_RE.search(str(key))
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
