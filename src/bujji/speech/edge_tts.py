"""Edge TTS backend — Microsoft neural voices, free, internet-required.

Supports Telugu (te-IN-ShrutiNeural, te-IN-MohanNeural), Hindi, and 300+ voices
across 100+ languages. Install with: pip install edge-tts
"""

from __future__ import annotations

import asyncio
from typing import List

from bujji.core.registry import TTSRegistry
from bujji.speech.tts import TTSBackend, TTSResult

_DEFAULT_VOICE = "te-IN-ShrutiNeural"

# Map common BCP-47 tags to a reasonable default Edge voice
LANGUAGE_DEFAULTS: dict[str, str] = {
    "te": "te-IN-ShrutiNeural",
    "hi": "hi-IN-SwaraNeural",
    "ta": "ta-IN-PallaviNeural",
    "kn": "kn-IN-SapnaNeural",
    "ml": "ml-IN-SobhanaNeural",
    "en": "en-US-JennyNeural",
}


@TTSRegistry.register("edge_tts")
class EdgeTTSBackend(TTSBackend):
    """Microsoft Edge neural TTS — free, no API key, requires internet."""

    backend_id = "edge_tts"

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = _DEFAULT_VOICE,
        speed: float = 1.0,
        output_format: str = "mp3",
    ) -> TTSResult:
        try:
            import edge_tts as _edge
        except ImportError:
            raise RuntimeError(
                "edge-tts not installed. Run: pip install edge-tts"
            )

        # Convert speed (1.0 = normal) to edge-tts rate string (+0%, +20%, -10%, …)
        rate_pct = int((speed - 1.0) * 100)
        rate_str = f"{rate_pct:+d}%"

        async def _synth() -> bytes:
            communicate = _edge.Communicate(text, voice_id, rate=rate_str)
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        audio_bytes = asyncio.run(_synth())

        return TTSResult(
            audio=audio_bytes,
            format="mp3",
            voice_id=voice_id,
            sample_rate=24000,
            metadata={"backend": "edge_tts"},
        )

    def available_voices(self) -> List[str]:
        return list(LANGUAGE_DEFAULTS.values()) + ["te-IN-MohanNeural"]

    def health(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False
