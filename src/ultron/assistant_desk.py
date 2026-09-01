"""The Assistant desk — Ultron's always-on conversational surface.

Absorbed from the former standalone assistant project (see docs/architecture/bujji-absorption.md)
and ships as the ``bujji`` package. This module wires its wake-word detection,
hardware-aware model selection, and ask/mission routing into Ultron's
Role/Model registries. Local-only: every code path talks to Ollama on loopback.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model_router import route_general_chat_model

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)

_FALLBACK_WAKE_VARIANTS = ("assistant", "bujji", "buji", "budgie", "boujie")

_MISSION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:start|launch|begin|kick\s+off|run)\s+(?:an?\s+|the\s+)?mission"
    r"(?:\s+(?:called|titled|named|for))?\s+(?P<title>\S.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DeskDecision:
    action: str
    title: str | None = None
    query: str | None = None


def normalize_transcript(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z' ]", " ", text.lower())).strip()


def _wake_variants(wake_word: str) -> tuple[str, ...]:
    collapsed = wake_word.replace(" ", "")
    return tuple({wake_word, collapsed, *_FALLBACK_WAKE_VARIANTS})


def heard_wake_word(transcript: str, wake_word: str = "assistant") -> bool:
    """Fuzzy wake-word gate. Union of the absorbed detector, the current
    wake word, and the legacy ``bujji`` brand so nothing stops responding."""
    try:
        from bujji.speech.wake_word import _contains_wake_word

        if _contains_wake_word(transcript):
            return True
    except Exception:
        logger.debug("bujji wake-word detector unavailable; using fallback matcher")
    lower = normalize_transcript(transcript)
    words = set(lower.split())
    for variant in _wake_variants(wake_word.lower()):
        if variant in lower or variant in words:
            return True
    return False


def strip_wake_word(transcript: str, wake_word: str = "assistant") -> str | None:
    """Return the utterance with the wake phrase removed, or None if not addressed."""
    if not heard_wake_word(transcript, wake_word):
        return None
    remainder = transcript
    lower = normalize_transcript(transcript)
    candidates = sorted(set(_wake_variants(wake_word.lower())) | set(_FALLBACK_WAKE_VARIANTS),
                        key=len, reverse=True)
    cuts = []
    for variant in candidates:
        idx = lower.find(variant)
        if idx >= 0:
            cuts.append((idx, len(variant)))
    if cuts:
        idx, length = min(cuts)
        remainder = remainder[idx + length:]
    return remainder.strip(" ,.:-!") or ""


def classify_utterance(utterance: str) -> DeskDecision:
    match = _MISSION_RE.match(utterance[:2000])
    if match:
        title = match.group("title").strip()
        if title:
            return DeskDecision(action="mission", title=title)
    return DeskDecision(action="answer", query=utterance)


def detect_vram_gb() -> float | None:
    """Best-effort local VRAM detection. Never touches the network."""
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            done = subprocess.run(
                [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            mibs = [float(line.split(",")[0]) for line in done.stdout.splitlines()
                    if line.strip() and line.split(",")[0].strip().isdigit()]
            if mibs:
                return max(mibs) / 1024.0
        except Exception:
            pass
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController | Measure-Object -Property AdapterRAM -Sum).Sum"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        value = done.stdout.strip()
        if value.isdigit():
            return int(value) / (1024 ** 3)
    except Exception:
        pass
    return None


def pick_hardware_model(installed: set[str], vram_gb: float | None, fallback: str) -> tuple[str, str]:
    """Hardware-aware pick from the absorbed model catalog; Ultron router as fallback."""
    try:
        from bujji.intelligence.model_catalog import BUILTIN_MODELS
    except Exception:
        logger.debug("bujji model catalog unavailable; falling back to keyword router")
        return route_general_chat_model(set(installed), fallback)
    budget = vram_gb if vram_gb is not None else float("inf")
    candidates = [
        spec for spec in BUILTIN_MODELS
        if spec.model_id in installed
        and not spec.requires_api_key
        and "ollama" in spec.supported_engines
        and (spec.min_vram_gb or 0.0) <= budget
    ]
    if not candidates:
        return route_general_chat_model(set(installed), fallback)
    best = max(candidates, key=lambda spec: spec.parameter_count_b)
    if vram_gb is None:
        reason = f"Largest installed catalog model without a VRAM floor ({best.model_id})"
    else:
        reason = (f"Largest installed catalog model within {vram_gb:.0f} GB VRAM "
                  f"(needs {best.min_vram_gb} GB)")
    return best.model_id, reason


class AssistantDesk:
    """Always-on desk on the Ops floor: listens, answers, triggers missions."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._bridge = None

    def _get_bridge(self):
        if self._bridge is None:
            from .bujji_bridge import get_bujji_bridge

            self._bridge = get_bujji_bridge(self._settings)
        return self._bridge

    async def bridge_status(self) -> dict:
        return await self._get_bridge().status()

    def pick_model(self, installed: set[str], vram_gb: float | None = None) -> tuple[str, str]:
        budget = vram_gb if vram_gb is not None else self._settings.assistant_vram_gb
        if budget is None:
            budget = detect_vram_gb()
        return pick_hardware_model(installed, budget, self._settings.default_model)

    async def handle_transcript(self, transcript: str, installed: set[str] | None = None) -> dict:
        stripped = strip_wake_word(transcript, self._settings.assistant_wake_word)
        if stripped is None:
            return {"triggered": False, "reason": "wake word not heard"}
        decision = classify_utterance(stripped)
        if decision.action == "mission":
            models = installed if installed is not None else await self.installed_models()
            model, reason = self.pick_model(models)
            return {
                "triggered": True,
                "action": "mission",
                "title": decision.title,
                "model": model,
                "model_reason": reason,
            }
        if not decision.query:
            return {"triggered": True, "action": "ignored", "reason": "wake word only"}
        result = await self._get_bridge().ask_full(decision.query)
        return {
            "triggered": True,
            "action": "answer",
            "content": result.get("content", ""),
            "model": result.get("model"),
        }

    async def installed_models(self) -> set[str]:
        status = await self.bridge_status()
        return set(status.get("models") or [])
