"""LLM provider selection for the Bujji Core server.

Bujji Control Core is embedded inside Ultron; Ultron owns a single
OmniRoute runtime (sidecar + router). This module resolves the Local vs
OmniRoute switch against that shared runtime when it exists (so one toggle on
the Ultron dashboard governs both surfaces), and falls back to Bujji-local
persistence when it runs standalone.
"""

from __future__ import annotations

import json
from typing import Any

from bujji.core.paths import get_config_dir

MODES = ("local", "hosted", "auto")

_MODE_FILE = get_config_dir() / "llm-provider-mode.json"


class BadProviderMode(ValueError):
    """Raised when an invalid provider mode string is supplied."""


def validate_mode(mode: str) -> str:
    """Normalize a request mode; raise :class:`BadProviderMode` if invalid."""
    normalized = (mode or "").strip().lower()
    if normalized not in MODES:
        raise BadProviderMode(f"provider mode must be one of: {', '.join(MODES)}")
    return normalized


def _shared_runtime():
    """Return the process-wide OmniRoute runtime, or None outside Ultron."""
    try:
        from ultron.config import get_settings  # type: ignore
        from ultron.omniroute_runtime import get_runtime  # type: ignore

        return get_runtime(get_settings())
    except Exception:
        return None


def _persist_local_mode(mode: str) -> None:
    try:
        _MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODE_FILE.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except OSError:
        # Best-effort persistence only; the in-memory choice still applies.
        pass


def _local_mode() -> str:
    try:
        if _MODE_FILE.exists():
            return json.loads(_MODE_FILE.read_text(encoding="utf-8")).get("mode", "auto")
    except (json.JSONDecodeError, OSError):
        pass
    return "auto"


async def provider_payload(apply_mode: str | None = None) -> dict[str, Any]:
    """Return (and optionally apply) the current provider selection.

    With ``apply_mode`` set to ``local`` / ``hosted`` / ``auto``, the selection
    is persisted and activated before the snapshot is returned. Resolution
    delegates to the shared Ultron OmniRoute runtime so the Bujji Core and the
    Ultron dashboard always agree on one mode.
    """
    if apply_mode is not None:
        apply_mode = validate_mode(apply_mode)

    rt = _shared_runtime()
    if rt is not None:
        if apply_mode is not None:
            rt.set_router_mode(apply_mode)
        mode = rt.router.default_mode
        try:
            healthy = await rt.sidecar.healthy()
            sidecar: dict[str, Any] = dict(rt.sidecar.status())
        except Exception:
            healthy = False
            sidecar = {}
        payload: dict[str, Any] = {
            "integrated": True,
            "source": "shared (Ultron OmniRoute runtime)",
            "mode": mode,
            "providers": ["local", "omniroute"],
            "local": {"engine": "ollama"},
            "omniroute": {"healthy": healthy, **sidecar},
        }
        if healthy:
            try:
                payload["omniroute"]["free_tiers"] = await rt.sidecar.free_tiers()
            except Exception:
                pass
        return payload

    # Standalone Bujji: no shared runtime, so it is local-only by construction,
    # but we keep the mode persisted so the switch normalizes consistently.
    if apply_mode is not None:
        _persist_local_mode(apply_mode)
        mode = apply_mode
    else:
        mode = _local_mode()
    return {
        "integrated": False,
        "source": "local",
        "mode": mode,
        "providers": ["local"],
        "local": {"engine": "ollama"},
        "omniroute": {
            "healthy": False,
            "note": "OmniRoute routing is provided by Ultron; outside Ultron the Core runs local-only.",
        },
    }