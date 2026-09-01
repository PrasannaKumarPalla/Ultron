from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RoleSpec:
    name: str
    system_prompt: str
    tools: tuple[str, ...] = ()
    model: str | None = None
    desk_position: dict = field(default_factory=dict)


class RoleRegistry:
    """YAML-backed role definitions, hot-reloaded whenever the file changes on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime: float | None = None
        self._roles: dict[str, RoleSpec] = {}

    def _load(self) -> None:
        if not self.path.exists():
            self._roles = {}
            self._mtime = None
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._roles = {
            role_id: RoleSpec(
                name=str(spec.get("name", role_id)),
                system_prompt=str(spec.get("system_prompt", "")),
                tools=tuple(spec.get("tools") or ()),
                model=spec.get("model") or None,
                desk_position=dict(spec.get("desk_position") or {}),
            )
            for role_id, spec in raw.items()
            if isinstance(spec, dict)
        }
        self._mtime = mtime

    def reload(self) -> None:
        self._mtime = None
        self._load()

    def get(self, role_id: str) -> RoleSpec | None:
        self._load()
        return self._roles.get(role_id)

    def all(self) -> dict[str, RoleSpec]:
        self._load()
        return dict(self._roles)

    def describe(self) -> list[dict]:
        return [
            {"id": role_id, "name": spec.name, "system_prompt": spec.system_prompt,
             "tools": list(spec.tools), "model": spec.model,
             "desk_position": spec.desk_position}
            for role_id, spec in sorted(self.all().items())
        ]