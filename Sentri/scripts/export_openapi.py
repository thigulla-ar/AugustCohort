"""Export the host-facing schema from the FastAPI application."""

import json
from pathlib import Path

from sentri.api import app


schema = app.openapi()
schema["servers"] = [
    {
        "url": "https://YOUR_PUBLIC_SENTRI_HOST",
        "description": "Replace with the HTTPS URL that reaches the local Sentri service",
    }
]
schema["info"]["description"] = (
    "Sentri governance preflight for ChatGPT GPT Actions and Gemini API tools. "
    "Call sentriInteract before any proposed action and proceed only when status is "
    "completed and result.authorized is true. Immediately before each downstream tool "
    "call, consume its signed permit with verifySentriPermit and execute only the exact "
    "verified action. Then call reportSentriOutcome with every observed action result and "
    "its permit. Authenticate with the configured Sentri bearer token. Never retry or "
    "bypass blocked actions."
)

target = Path(__file__).resolve().parents[1] / "openapi.json"
target.write_text(
    json.dumps(schema, indent=2) + "\n",
    encoding="utf-8",
    newline="\n",
)
print(target)
