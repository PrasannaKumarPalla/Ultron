"""Inference Engine primitive â€” LLM runtime management."""

from __future__ import annotations

import importlib

# Import engine modules to trigger @EngineRegistry.register() decorators
import bujji.engine.ollama  # noqa: F401
import bujji.engine.openai_compat_engines  # noqa: F401
from bujji.engine._base import (
    EngineConnectionError,
    InferenceEngine,
    messages_to_dicts,
)
from bujji.engine._discovery import discover_engines, discover_models, get_engine

# Optional engines â€” only register if their SDK deps are present
for _optional in ("cloud", "litellm", "gemma_cpp"):
    try:
        importlib.import_module(f".{_optional}", __name__)
    except ImportError:
        pass

__all__ = [
    "EngineConnectionError",
    "InferenceEngine",
    "discover_engines",
    "discover_models",
    "get_engine",
    "messages_to_dicts",
]
