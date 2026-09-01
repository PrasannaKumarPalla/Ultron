from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIRS = {".git", ".venv", "node_modules", "__pycache__"}

SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("generic_api_key_or_token", re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*['\"]([A-Za-z0-9+/_-]{16,})['\"]"
    )),
    ("private_key_header", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]+")),
    ("github_token", re.compile(r"gh[po]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
]


def _redact(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def _iter_workspace_files(workspace: Path):
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(workspace).parts):
            continue
        yield path


def scan_secrets(workspace: Path) -> list[dict]:
    findings: list[dict] = []
    for path in _iter_workspace_files(workspace):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relative = path.relative_to(workspace).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule_name, pattern in SECRET_RULES:
                match = pattern.search(line)
                if not match:
                    continue
                secret_value = match.group(0)
                findings.append({
                    "file": relative,
                    "line": line_number,
                    "rule": rule_name,
                    "match_preview": _redact(secret_value),
                })
    return findings


@dataclass
class DependencyScanResult:
    tool: str | None
    available: bool
    findings: list[dict] = field(default_factory=list)


def _run_pip_audit(workspace: Path) -> DependencyScanResult:
    try:
        completed = subprocess.run(
            ["pip-audit", "--format", "json", "-r", str(workspace / "requirements.txt")],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(completed.stdout or "[]")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return DependencyScanResult(tool="pip-audit", available=True, findings=[])
    findings: list[dict] = []
    for entry in data if isinstance(data, list) else data.get("dependencies", []):
        package = entry.get("name", "")
        version = entry.get("version", "")
        for vuln in entry.get("vulns", []):
            findings.append({
                "package": package,
                "installed_version": version,
                "vulnerability_id": vuln.get("id", ""),
                "severity": vuln.get("severity") or "unknown",
            })
    return DependencyScanResult(tool="pip-audit", available=True, findings=findings)


def _run_npm_audit(workspace: Path) -> DependencyScanResult:
    try:
        completed = subprocess.run(
            ["npm", "audit", "--json"],
            cwd=workspace, capture_output=True, text=True, timeout=120,
        )
        data = json.loads(completed.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return DependencyScanResult(tool="npm-audit", available=True, findings=[])
    findings: list[dict] = []
    for package, vuln in data.get("vulnerabilities", {}).items():
        findings.append({
            "package": package,
            "installed_version": vuln.get("range", "unknown"),
            "vulnerability_id": ",".join(v.get("url", "") for v in vuln.get("via", []) if isinstance(v, dict)) or "unknown",
            "severity": vuln.get("severity", "unknown"),
        })
    return DependencyScanResult(tool="npm-audit", available=True, findings=findings)


def scan_dependencies(workspace: Path) -> DependencyScanResult:
    # pip-audit is a core Ultron dependency (see pyproject.toml), so it's always
    # available for Python-target projects — shutil.which is just a robustness
    # check for unusual install situations, not the realistic gate it used to be.
    # npm audit stays best-effort: Node/npm is never a hard dependency of Ultron.
    has_python_deps = (workspace / "requirements.txt").exists() or (workspace / "pyproject.toml").exists()
    has_node_deps = (workspace / "package.json").exists()

    if has_python_deps and (workspace / "requirements.txt").exists() and shutil.which("pip-audit"):
        return _run_pip_audit(workspace)
    if has_node_deps and shutil.which("npm"):
        return _run_npm_audit(workspace)

    tool = "pip-audit" if has_python_deps else ("npm-audit" if has_node_deps else None)
    return DependencyScanResult(tool=tool, available=False, findings=[])
