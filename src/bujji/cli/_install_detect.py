"""Detect how the assistant build was installed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class InstallInfo:
    """How the assistant build was installed."""

    kind: str
    upgrade_command: str
    repo_root: Optional[Path] = None


def detect_install() -> InstallInfo:
    try:
        import bujji

        pkg_file = Path(bujji.__file__).resolve()
    except Exception:
        return InstallInfo(
            kind="unknown",
            upgrade_command="git pull && uv sync --extra desktop",
        )

    parts = [p.lower() for p in pkg_file.parts]

    if "uv" in parts and "tools" in parts:
        return InstallInfo(
            kind="uv-tool",
            upgrade_command="uv tool upgrade bujji",
        )

    candidate = pkg_file.parent
    for _ in range(8):
        if (candidate / ".git").exists() and (candidate / "pyproject.toml").exists():
            return InstallInfo(
                kind="editable-git",
                upgrade_command=f"cd {candidate} && git pull && uv sync --extra desktop",
                repo_root=candidate,
            )
        if candidate.parent == candidate:
            break
        candidate = candidate.parent

    if "site-packages" in parts:
        return InstallInfo(
            kind="pypi",
            upgrade_command="pip install --upgrade bujji",
        )

    return InstallInfo(
        kind="unknown",
        upgrade_command="git pull && uv sync --extra desktop",
    )
