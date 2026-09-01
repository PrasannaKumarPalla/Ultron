"""switch_voice tool — change BUJJI's speaking voice at runtime.

Handles "switch to Telugu voice", "use the other girl voice", "male voice",
"reset voice". Writes to bujji.speech.voice_state, which VoicePipeline
consults on every turn, so the change is immediate and persistent.
"""

from __future__ import annotations

import json
from typing import Any

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec

_LANG_ALIASES = {
    "english": "en", "en": "en",
    "telugu": "te", "te": "te", "tenglish": "te",
    "hindi": "hi", "hi": "hi",
    "tamil": "ta", "ta": "ta",
    "kannada": "kn", "kn": "kn",
    "all": "*", "*": "*", "": "*",
}


@ToolRegistry.register("switch_voice")
class SwitchVoiceTool(BaseTool):
    """Switch, list, or reset the assistant's TTS voice."""

    tool_id = "switch_voice"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="switch_voice",
            description=(
                "Change the assistant's speaking voice. Use when the user asks to "
                "switch voice, language of the voice, or gender (e.g. 'Telugu voice', "
                "'female voice', 'switch to Bella'). Actions: set (language+gender or "
                "explicit voice_id), list (show available voices), reset (back to defaults)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "list", "reset"],
                        "description": "set a voice, list voices, or reset to config defaults",
                    },
                    "language": {
                        "type": "string",
                        "description": "english, telugu, hindi, tamil, kannada, or 'all'",
                    },
                    "gender": {
                        "type": "string",
                        "enum": ["female", "male"],
                        "description": "voice gender (default female)",
                    },
                    "voice_id": {
                        "type": "string",
                        "description": "explicit voice id, e.g. af_bella or te-IN-ShrutiNeural",
                    },
                },
                "required": ["action"],
            },
            category="voice",
            latency_estimate=0.05,
            timeout_seconds=5.0,
        )

    def execute(  # type: ignore[override]
        self,
        action: str = "set",
        language: str = "",
        gender: str = "female",
        voice_id: str = "",
        **_: Any,
    ) -> ToolResult:
        from bujji.speech import voice_state

        if action == "list":
            listing = {
                f"{lang}/{gen}": [f"{b}:{v}" for b, v in cands]
                for (lang, gen), cands in voice_state.VOICE_PRESETS.items()
            }
            return ToolResult(
                tool_name=self.tool_id,
                content=json.dumps(listing, indent=2),
                success=True,
            )

        if action == "reset":
            raw = language.lower().strip()
            lang = _LANG_ALIASES.get(raw)
            if lang is None and raw:
                return ToolResult(
                    tool_name=self.tool_id,
                    content=f"Unknown language '{language}'. Use english/telugu/hindi/tamil/kannada or 'all'.",
                    success=False,
                )
            voice_state.clear(None if lang in (None, "*") else lang)
            return ToolResult(
                tool_name=self.tool_id,
                content="Voice reset to config defaults.",
                success=True,
            )

        # action == "set"
        lang = _LANG_ALIASES.get(language.lower().strip(), "")
        if voice_id:
            # Explicit voice id: infer backend from known preset entries, else guess.
            backend = ""
            for cands in voice_state.VOICE_PRESETS.values():
                for b, v in cands:
                    if v.lower() == voice_id.lower():
                        backend, voice_id = b, v
                        break
            if not backend:
                backend = "edge_tts" if "-" in voice_id else "kokoro"
            target_lang = lang or "*"
            voice_state.set_voice(target_lang, backend, voice_id)
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Voice for '{target_lang}' switched to {backend}:{voice_id}.",
                success=True,
            )

        if not lang:
            return ToolResult(
                tool_name=self.tool_id,
                content="Specify a language (english/telugu/hindi/tamil/kannada/all) or a voice_id.",
                success=False,
            )

        gen = gender.lower().strip() or "female"
        preset_lang = "en" if lang == "*" else lang
        candidates = voice_state.VOICE_PRESETS.get((preset_lang, gen))
        if not candidates:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"No {gen} voice preset for '{preset_lang}'. Use action=list to see options.",
                success=False,
            )
        backend, vid = candidates[0]
        voice_state.set_voice(lang, backend, vid)
        return ToolResult(
            tool_name=self.tool_id,
            content=f"Voice for '{lang}' switched to {backend}:{vid} ({gen}).",
            success=True,
        )


__all__ = ["SwitchVoiceTool"]
