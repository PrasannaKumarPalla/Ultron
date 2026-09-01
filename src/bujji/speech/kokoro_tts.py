"""Kokoro TTS backend â€” fully open-source, runs locally.

Requires the kokoro package: pip install kokoro
Falls back gracefully if not installed.
"""

from __future__ import annotations

import io
from typing import List

from bujji.core.registry import TTSRegistry
from bujji.speech.tts import TTSBackend, TTSResult


@TTSRegistry.register("kokoro")
class KokoroTTSBackend(TTSBackend):
    """Kokoro TTS â€” local open-source voice synthesis."""

    backend_id = "kokoro"

    def __init__(self, *, model_path: str = "", device: str = "auto") -> None:
        self._model_path = model_path
        self._device = device
        self._pipeline = None
        self._pipelines: dict = {}

    def _ensure_pipeline(self, lang_code: str = "a") -> None:
        if lang_code in self._pipelines:
            self._pipeline = self._pipelines[lang_code]
            return
        try:
            from kokoro import KPipeline

            pipe = KPipeline(lang_code=lang_code)
            self._pipelines[lang_code] = pipe
            self._pipeline = pipe
        except ImportError:
            raise RuntimeError(
                "kokoro package not installed. Install with: pip install kokoro"
            )

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "af_heart",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        # Auto-detect language code: voices starting with 'b' are British English
        lang_code = "b" if voice_id.startswith("b") else "a"
        self._ensure_pipeline(lang_code)
        import numpy as np
        import soundfile as sf

        samples = []
        for _, _, audio in self._pipeline(text, voice=voice_id, speed=speed):
            samples.append(audio)

        if not samples:
            return TTSResult(audio=b"", format=output_format, voice_id=voice_id)

        combined = np.concatenate(samples)
        buf = io.BytesIO()
        sf.write(buf, combined, 24000, format=output_format.upper())
        buf.seek(0)

        return TTSResult(
            audio=buf.read(),
            format=output_format,
            voice_id=voice_id,
            sample_rate=24000,
            duration_seconds=len(combined) / 24000,
            metadata={"backend": "kokoro"},
        )

    def available_voices(self) -> List[str]:
        return ["af_heart", "af_bella", "am_adam", "am_michael"]

    def health(self) -> bool:
        try:
            self._ensure_pipeline()
            return True
        except RuntimeError:
            return False
