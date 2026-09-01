"""Home Assistant integration tool via REST API.

Called from tool executor/orchestrator. Registered as "home_assistant".
No existing file. Input schema: {action: str, entity: str}.
User instruction: do all remaining ones.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_SUPPORTED_ACTIONS = {"turn_on", "turn_off", "toggle", "get_state"}


@ToolRegistry.register("home_assistant")
class HomeAssistantTool(BaseTool):
    """Control Home Assistant entities via the REST API."""

    tool_id = "home_assistant"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="home_assistant",
            description=(
                "Control smart-home devices and sensors via Home Assistant REST API. "
                "Supports turn_on, turn_off, toggle, and get_state actions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "One of: turn_on, turn_off, toggle, get_state",
                        "enum": list(_SUPPORTED_ACTIONS),
                    },
                    "entity": {
                        "type": "string",
                        "description": "Home Assistant entity ID, e.g. light.living_room",
                    },
                },
                "required": ["action", "entity"],
            },
            category="home_automation",
            latency_estimate=0.5,
            timeout_seconds=10.0,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")

    def _token(self) -> str | None:
        return os.environ.get("HA_TOKEN")

    def _headers(self) -> Dict[str, str]:
        token = self._token()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    def execute(self, action: str = "", entity: str = "", **_: Any) -> ToolResult:  # type: ignore[override]
        """Execute a Home Assistant action on *entity*."""
        try:
            import httpx
        except ImportError:
            return ToolResult(
                tool_name=self.tool_id,
                content="httpx not installed. Run: pip install httpx",
                success=False,
            )

        token = self._token()
        if not token:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "HA_TOKEN environment variable is not set. "
                    "Create a long-lived access token in Home Assistant under "
                    "Profile → Security → Long-Lived Access Tokens and set HA_TOKEN."
                ),
                success=False,
            )

        if action not in _SUPPORTED_ACTIONS:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Unsupported action '{action}'. Choose from: {', '.join(sorted(_SUPPORTED_ACTIONS))}",
                success=False,
            )

        base = self._base_url()
        headers = self._headers()

        try:
            if action == "get_state":
                url = f"{base}/api/states/{entity}"
                resp = httpx.get(url, headers=headers, timeout=10.0)
                resp.raise_for_status()
                data: Dict[str, Any] = resp.json()
                state = data.get("state", "unknown")
                attrs = data.get("attributes", {})
                return ToolResult(
                    tool_name=self.tool_id,
                    content=json.dumps({"entity": entity, "state": state, "attributes": attrs}),
                    success=True,
                )
            else:
                # turn_on / turn_off / toggle map to service calls
                service_domain = entity.split(".")[0] if "." in entity else "homeassistant"
                url = f"{base}/api/services/{service_domain}/{action}"
                payload = {"entity_id": entity}
                resp = httpx.post(url, headers=headers, json=payload, timeout=10.0)
                resp.raise_for_status()
                return ToolResult(
                    tool_name=self.tool_id,
                    content=f"OK: {action} executed on {entity}",
                    success=True,
                )
        except Exception as exc:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Home Assistant request failed: {exc}",
                success=False,
            )


__all__ = ["HomeAssistantTool"]
