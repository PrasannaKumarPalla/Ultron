"""Plugin tool registry with JSON-schema tools and grammar-ready output.

Tools are plain functions decorated with @tool(...). Discovery imports every
module in a directory (built-ins ship with the app; operators can point
ULTRON_TOOLS_DIR at their own). Schemas compile to strict Ollama `format`
objects — that IS our constrained-decoding grammar source (ADR-0007).
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable

    def strict_format(self) -> dict:
        """Normalize a schema into a strict object grammar for `format=`."""
        grammar = {
            "type": "object",
            "properties": dict(self.schema.get("properties", {})),
            "required": list(self.schema.get("required", [])),
            "additionalProperties": False,
        }
        return grammar


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]

    def schemas(self) -> list[dict]:
        return [{"name": spec.name, "description": spec.description,
                 "schema": spec.schema, "format": spec.strict_format()}
                for spec in self.specs()]

    def discover(self, directory: Path) -> int:
        """Import every *.py in a directory and register decorated tools here."""
        directory = Path(directory)
        if not directory.is_dir():
            return 0
        before = len(self._tools)
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module_name = f"ultron_tools_{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                continue
            for value in vars(module).values():
                tool_spec = getattr(value, "tool_spec", None)
                if tool_spec is not None and tool_spec.name not in self._tools:
                    self.register(tool_spec)
        return len(self._tools) - before


REGISTRY = ToolRegistry()


def tool(name: str, description: str, schema: dict) -> Callable:
    def decorate(fn: Callable) -> Callable:
        REGISTRY.register(ToolSpec(name=name, description=description,
                                   schema=schema, handler=fn))
        fn.tool_spec = REGISTRY.get(name)
        return fn

    return decorate
