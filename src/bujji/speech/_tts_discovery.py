"""Auto-discover available text-to-speech backends.

Priority when backend="auto": kokoro (local) → edge_tts → openai → cartesia
When config.tts.backend is set explicitly, that backend is tried first.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bujji.core.config import BujjiConfig
    from bujji.speech.tts import TTSBackend


def get_tts_backend(config: "BujjiConfig") -> Optional["TTSBackend"]:
    """Return the configured (or first available) TTS backend."""
    # Trigger registration of all backends
    import bujji.speech  # noqa: F401

    from bujji.core.registry import TTSRegistry

    tts_cfg = getattr(config, "tts", None)
    preferred = getattr(tts_cfg, "backend", "auto") if tts_cfg else "auto"

    def _try(key: str, **kwargs) -> Optional["TTSBackend"]:
        if not TTSRegistry.contains(key):
            return None
        try:
            backend = TTSRegistry.get(key)(**kwargs)
            if backend.health():
                return backend
        except Exception:
            pass
        return None

    # Explicit backend requested — try it first, fall through on failure
    if preferred not in ("", "auto"):
        backend = _try(preferred)
        if backend:
            return backend

    # Prefer the zero-download offline Windows voice.
    backend = _try("windows_sapi")
    if backend:
        return backend

    backend = _try("edge_tts")
    if backend:
        return backend

    backend = _try("kokoro")
    if backend:
        return backend

    if TTSRegistry.contains("openai_tts") and os.environ.get("OPENAI_API_KEY"):
        backend = _try("openai_tts", api_key=os.environ["OPENAI_API_KEY"])
        if backend:
            return backend

    if TTSRegistry.contains("cartesia") and os.environ.get("CARTESIA_API_KEY"):
        backend = _try("cartesia", api_key=os.environ["CARTESIA_API_KEY"])
        if backend:
            return backend

    return None
