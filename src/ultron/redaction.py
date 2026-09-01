"""Secret redaction before any hosted send. Deny-list is configurable.

Patterns cover env files, API keys, tokens, JWTs and PEM blocks. The
configurable deny-list lives in the OmniRoute secrets dir as extra_patterns.txt
(one regex per line). Never log the matched content — only pattern names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("private_key_assignment", re.compile(
        r"(?i)(api[_-]?key|secret|password|passwd|pwd|token|auth)[_a-z0-9-]*\s*[:=]\s*['\"]?([^\s'\"]{8,})")),
    ("env_file_reference", re.compile(r"(?m)^\.env\b.*$")),
    ("connection_string", re.compile(
        r"\b(?:postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp)://[^\s:@]+:[^\s@]+@[^\s]+")),
    ("generic_hex_secret", re.compile(r"(?i)\b[a-f0-9]{40,64}\b")),
]

MASK = "[REDACTED:{name}]"


@dataclass
class RedactionFinding:
    name: str
    start: int
    end: int


def load_extra_patterns(secrets_dir: Path) -> list[tuple[str, re.Pattern]]:
    path = secrets_dir / "extra_patterns.txt"
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append((f"custom:{line[:24]}", re.compile(line)))
        except re.error:
            continue
    return patterns


class Redactor:
    def __init__(self, secrets_dir: Path | None = None,
                 disabled: set[str] | None = None):
        self.patterns = list(DEFAULT_PATTERNS)
        if secrets_dir is not None:
            self.patterns.extend(load_extra_patterns(secrets_dir))
        self.disabled = disabled or set()

    def scan(self, text: str) -> list[RedactionFinding]:
        findings = []
        for name, pattern in self.patterns:
            if name in self.disabled:
                continue
            for match in pattern.finditer(text):
                findings.append(RedactionFinding(name, match.start(), match.end()))
        return sorted(findings, key=lambda f: f.start)

    def redact(self, text: str) -> tuple[str, list[str]]:
        """Return (redacted text, pattern names hit — never the content)."""
        names: list[str] = []
        out = text

        def _replace(match: re.Match, name: str) -> str:
            return MASK.format(name=name)

        for name, pattern in self.patterns:
            if name in self.disabled:
                continue
            hits = len(pattern.findall(out))
            if hits:
                names.extend([name] * hits)
                out = pattern.sub(lambda m, n=name: _replace(m, n), out)
        return out, names


def dry_run(text: str, secrets_dir: Path | None = None) -> dict:
    """Preview exactly what would leave the process on a hosted send."""
    redactor = Redactor(secrets_dir)
    redacted, names = redactor.redact(text)
    return {
        "findings": [{"name": name, "count": names.count(name)} for name in dict.fromkeys(names)],
        "total": len(names),
        "redacted_text": redacted if names else None,
        "would_send_verbatim": not names,
    }
