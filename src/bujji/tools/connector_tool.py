"""Adapter that wraps a connector's ToolSpec into a BaseTool the agent can call."""

from __future__ import annotations

import json
from typing import Any

from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec


class ConnectorTool(BaseTool):
    """Thin wrapper that dispatches a tool call to the owning connector instance.

    The connector must expose a method whose name matches the tool's ``name``
    field. Arguments passed by the agent are forwarded as keyword arguments.
    """

    is_local = True

    def __init__(self, connector: Any, spec: ToolSpec) -> None:
        self._connector = connector
        self._spec = spec

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def execute(self, **params: Any) -> ToolResult:
        method_name = self._spec.name  # e.g. "obsidian_search_notes"
        # Try exact name first, then strip the connector_id prefix
        method = getattr(self._connector, method_name, None)
        if method is None:
            conn_id = getattr(self._connector, "connector_id", "")
            short = method_name[len(conn_id) + 1:] if conn_id and method_name.startswith(conn_id + "_") else None
            if short:
                method = getattr(self._connector, short, None)
        if method is None:
            return ToolResult(
                tool_name=self._spec.name,
                result=None,
                error=f"Connector has no method for tool '{self._spec.name}'",
                success=False,
            )
        try:
            result = method(**params)
            # Serialize lists/dicts to JSON string for agent consumption
            if isinstance(result, (list, dict)):
                output = json.dumps(result, ensure_ascii=False, indent=2)
            else:
                output = str(result)
            return ToolResult(tool_name=self._spec.name, result=output, success=True)
        except Exception as exc:
            return ToolResult(
                tool_name=self._spec.name,
                result=None,
                error=str(exc),
                success=False,
            )
