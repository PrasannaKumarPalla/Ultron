"""Runtime voice preference — set by the switch_voice tool, read by VoicePipeline.

Persisted to ~/.bujji/voice_state.json so the choice survives restarts.
Overrides win over config.toml [tts] / [tts.languages.*] settings.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional

_STATE_PATH = Path.home() / ".bujji" / "voice_state.json"
_lock = threading.Lock()

# Curated presets: (language, gender) -> ordered candidates of (backend, voice_id).
# First healthy backend wins at synthesis time.
VOICE_PRESETS: Dict[tuple, list] = {
    ("en", "female"): [("kokoro", "af_heart"), ("kokoro", "af_bella"),
                       ("edge_tts", "en-US-AriaNeural")],
    ("en", "male"): [("kokoro", "am_adam"), ("kokoro", "am_michael"),
                     ("edge_tts", "en-US-GuyNeural")],
    ("te", "female"): [("edge_tts", "te-IN-ShrutiNeural"), ("mms_tts", "tel")],
    ("te", "male"): [("edge_tts", "te-IN-MohanNeural")],
    ("hi", "female"): [("edge_tts", "hi-IN-SwaraNeural"), ("mms_tts", "hin")],
    ("ta", "female"): [("edge_tts", "ta-IN-PallaviNeural"), ("mms_tts", "tam")],
    ("kn", "female"): [("edge_tts", "kn-IN-SapnaNeural"), ("mms_tts", "kan")],
}


def _load() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def set_voice(language: str, backend: str, voice_id: str) -> None:
    """Set the active voice for a language ("*" = all/default)."""
    with _lock:
        state = _load()
        state.setdefault("languages", {})[language or "*"] = {
            "backend": backend,
            "voice_id": voice_id,
        }
        _save(state)


def clear(language: Optional[str] = None) -> None:
    """Clear override for one language, or all if None."""
    with _lock:
        state = _load()
        if language:
            state.get("languages", {}).pop(language, None)
        else:
            state.pop("languages", None)
        _save(state)


def get_for_language(lang: str) -> Optional[dict]:
    """Return {"backend", "voice_id"} override for lang, falling back to "*"."""
    langs = _load().get("languages", {})
    return langs.get(lang) or langs.get("*")


__all__ = ["VOICE_PRESETS", "set_voice", "clear", "get_for_language"]
