"""Fail if files headed for Git contain common private-repository hazards."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_NAMES = {
    ".env",
    ".sentri-signing.key",
    "settings.json",
}
SENSITIVE_SUFFIXES = {".db", ".db-shm", ".db-wal", ".jsonl"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "OpenAI-style secret": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "JWT-like credential": re.compile(
        r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    "personal Windows path": re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+"),
    "temporary public tunnel": re.compile(
        r"(?i)https://[^\s/]*(?:ngrok|trycloudflare|loca\.lt|tunnel)[^\s]*"
    ),
    "populated Sentri secret": re.compile(
        r"(?m)^SENTRI_(?:API_TOKEN|SIGNING_SECRET|OPENAI_API_KEY|GEMINI_API_KEY)"
        r"[ \t]*=[ \t]*[^\s#][^\r\n]*$"
    ),
}


def candidate_files() -> list[Path]:
    command = [
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [path for path in ROOT.rglob("*") if path.is_file()]
    return [ROOT / line for line in result.stdout.splitlines() if line]


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        if path.name in SENSITIVE_NAMES or any(
            path.name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES
        ):
            findings.append((relative, 0, "sensitive runtime filename"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append((relative, line, label))

    if findings:
        print("Public-repository check failed:")
        for path, line, label in sorted(set(findings)):
            location = f"{path}:{line}" if line else path
            print(f"- {location}: {label}")
        print("No matched secret values were printed. Remove or ignore these items.")
        return 1

    print(f"Public-repository check passed ({len(candidate_files())} candidate files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
