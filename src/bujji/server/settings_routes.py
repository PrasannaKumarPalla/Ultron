"""FastAPI settings router — config, branding, and memory-facts endpoints.

Registered in server/app.py via include_router.
No existing file. Endpoints: GET/PATCH /api/settings, GET/PATCH /api/settings/branding,
GET/DELETE /api/memory/facts.
User instruction: do all remaining ones.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

import tomllib
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

_CONFIG_OVERRIDE_PATH = Path("~/.bujji/config_override.toml").expanduser()

# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------

_ALLOWED_SETTINGS = {"models", "voice_id", "wake_word", "tts_speed"}


def _config_path() -> Path:
    from bujji.core.config import DEFAULT_CONFIG_DIR

    return Path(os.environ.get("BUJJI_CONFIG", DEFAULT_CONFIG_DIR / "config.toml"))


def _save_tts_settings(updates: Dict[str, Any]) -> None:
    """Persist UI voice settings in the same config consumed by the TTS API."""
    import tomlkit

    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = (
        tomlkit.parse(path.read_text(encoding="utf-8"))
        if path.exists()
        else tomlkit.document()
    )
    if "tts" not in doc:
        doc.add("tts", tomlkit.table())
    if "voice_id" in updates:
        doc["tts"]["voice_id"] = updates["voice_id"]
    if "tts_speed" in updates:
        doc["tts"]["speed"] = updates["tts_speed"]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _load_override() -> Dict[str, Any]:
    if not _CONFIG_OVERRIDE_PATH.exists():
        return {}
    try:
        with open(_CONFIG_OVERRIDE_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        logger.warning("Failed to load config override: %s", exc)
        return {}


def _to_toml_value(v: Any) -> str:
    """Serialize a Python value to a TOML-compatible string fragment."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(v, list):
        items = ", ".join(_to_toml_value(i) for i in v)
        return f"[{items}]"
    # Fallback: JSON-encode
    return json.dumps(v)


def _save_override(data: Dict[str, Any]) -> None:
    _CONFIG_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = [f"{k} = {_to_toml_value(v)}" for k, v in data.items()]
        _CONFIG_OVERRIDE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save config override: %s", exc)
        raise


def _get_live_config() -> Dict[str, Any]:
    """Return the live config, merging base config with any overrides."""
    try:
        from bujji.core.config import get_config  # type: ignore

        cfg = get_config()
        base: Dict[str, Any] = {
            "models": getattr(cfg, "models", []),
            "voice_id": getattr(getattr(cfg, "tts", None), "voice_id", ""),
            "wake_word": getattr(cfg, "wake_word", "hey bujji"),
            "tts_speed": getattr(getattr(cfg, "tts", None), "speed", 1.0),
        }
    except Exception:
        base = {
            "models": [],
            "voice_id": "",
            "wake_word": "hey bujji",
            "tts_speed": 1.0,
        }

    override = _load_override()
    base.update({k: v for k, v in override.items() if k in _ALLOWED_SETTINGS})
    return base


# ------------------------------------------------------------------
# Pydantic models
# ------------------------------------------------------------------


class SettingsPatch(BaseModel):
    models: Any = None
    voice_id: str | None = None
    wake_word: str | None = None
    tts_speed: float | None = None


class BrandingPatch(BaseModel):
    product_name: str | None = None
    tagline: str | None = None
    wake_word: str | None = None
    display_name: str | None = None


# ------------------------------------------------------------------
# Settings endpoints
# ------------------------------------------------------------------


@router.get("/api/settings")
async def get_settings() -> JSONResponse:
    """Return current config subset (models, voice_id, wake_word, tts_speed)."""
    return JSONResponse(_get_live_config())


@router.patch("/api/settings")
async def patch_settings(body: SettingsPatch) -> JSONResponse:
    """Update config fields. Writes to ~/.bujji/config_override.toml."""
    override = _load_override()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields provided")
    override.update(updates)
    try:
        _save_tts_settings(updates)
        _save_override(override)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {exc}")
    return JSONResponse(_get_live_config())


@router.get("/api/settings/voices")
async def get_voices() -> JSONResponse:
    """List voices available to the active offline Windows TTS backend."""
    try:
        from bujji.speech.windows_sapi_tts import WindowsSapiTTSBackend

        voices = WindowsSapiTTSBackend().available_voices()
        return JSONResponse({"voices": [{"id": name, "name": name} for name in voices]})
    except Exception as exc:
        logger.warning("Failed to enumerate voices: %s", exc)
        return JSONResponse({"voices": []})


# ------------------------------------------------------------------
# Branding endpoints
# ------------------------------------------------------------------


@router.get("/api/settings/branding")
async def get_branding_endpoint() -> JSONResponse:
    """Return current branding configuration."""
    try:
        from bujji.brand import get_branding

        b = get_branding()
        return JSONResponse(
            {
                "product_name": b.product_name,
                "tagline": b.tagline,
                "wake_word": b.wake_word,
                "display_name": b.display_name,
                "command_name": b.command_name,
            }
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not load branding: {exc}")


@router.patch("/api/settings/branding")
async def patch_branding_endpoint(body: BrandingPatch) -> JSONResponse:
    """Update branding fields and persist them."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No valid branding fields provided")
    try:
        from bujji.brand import get_branding, save_user_branding  # type: ignore

        save_user_branding(updates)
        b = get_branding()
        return JSONResponse(
            {
                "status": "ok",
                "branding": {
                    "product_name": b.product_name,
                    "tagline": b.tagline,
                    "wake_word": b.wake_word,
                    "display_name": b.display_name,
                    "command_name": b.command_name,
                },
            }
        )
    except ImportError:
        raise HTTPException(
            status_code=501, detail="save_user_branding not available in this build"
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update branding: {exc}")


# ------------------------------------------------------------------
# Memory facts endpoints
# ------------------------------------------------------------------


@router.get("/api/memory/facts")
async def list_facts() -> JSONResponse:
    """Return all stored memory facts."""
    try:
        from bujji.memory.store import create_fact_store

        store = create_fact_store()
        facts = store.list()
        return JSONResponse(
            [
                {"text": f.text, "source": f.source, "created_at": f.created_at}
                for f in facts
            ]
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load facts: {exc}")


@router.delete("/api/memory/facts")
async def clear_facts() -> JSONResponse:
    """Delete all stored memory facts."""
    try:
        from bujji.memory.store import create_fact_store

        store = create_fact_store()
        store.clear()
        return JSONResponse({"status": "ok", "message": "All facts cleared"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to clear facts: {exc}")


__all__ = ["router"]
