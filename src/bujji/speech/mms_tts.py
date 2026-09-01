"""MMS TTS backend — Meta's Massively Multilingual Speech, fully local/offline.

Female-leaning single-speaker VITS models per language (~150 MB each,
downloaded once from Hugging Face then cached). Telugu quality is good and it
removes the internet dependency of edge_tts. Requires torch + transformers.

voice_id here is an MMS language code: "tel" (Telugu), "hin", "tam", "kan",
"mal", "eng". BCP-47 tags ("te", "hi", …) are accepted and mapped.
"""

from __future__ import annotations

import io
import logging
import threading
from typing import List

from bujji.core.registry import TTSRegistry
from bujji.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

_LANG_TO_MMS = {
    "te": "tel", "tel": "tel",
    "hi": "hin", "hin": "hin",
    "ta": "tam", "tam": "tam",
    "kn": "kan", "kan": "kan",
    "ml": "mal", "mal": "mal",
    "en": "eng", "eng": "eng",
}

_DEFAULT_VOICE = "tel"


@TTSRegistry.register("mms_tts")
class MMSTTSBackend(TTSBackend):
    """Local multilingual TTS via facebook/mms-tts-* VITS models."""

    backend_id = "mms_tts"

    def __init__(self) -> None:
        self._models: dict = {}
        self._lock = threading.Lock()

    def _get_model(self, lang: str):
        with self._lock:
            if lang in self._models:
                return self._models[lang]
            from transformers import VitsModel, AutoTokenizer

            name = f"facebook/mms-tts-{lang}"
            logger.info("Loading MMS TTS model %s (first use downloads ~150 MB)", name)
            model = VitsModel.from_pretrained(name)
            tokenizer = AutoTokenizer.from_pretrained(name)
            self._models[lang] = (model, tokenizer)
            return self._models[lang]

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = _DEFAULT_VOICE,
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        import torch

        lang = _LANG_TO_MMS.get(voice_id.lower().strip(), "tel")
        model, tokenizer = self._get_model(lang)

        # VITS speaking rate: >1.0 = faster
        if hasattr(model, "speaking_rate") and speed and speed != 1.0:
            model.speaking_rate = speed

        inputs = tokenizer(text, return_tensors="pt")
        with torch.no_grad():
            output = model(**inputs).waveform

        waveform = output.squeeze().cpu().numpy()
        sample_rate = int(model.config.sampling_rate)

        buf = io.BytesIO()
        try:
            import soundfile as sf

            sf.write(buf, waveform, sample_rate, format="WAV", subtype="PCM_16")
        except ImportError:
            # Minimal WAV writer fallback
            import struct
            import wave

            pcm = (waveform * 32767).clip(-32768, 32767).astype("int16")
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))

        return TTSResult(
            audio=buf.getvalue(),
            format="wav",
            voice_id=lang,
            sample_rate=sample_rate,
            metadata={"backend": "mms_tts", "model": f"facebook/mms-tts-{lang}"},
        )

    def available_voices(self) -> List[str]:
        return sorted(set(_LANG_TO_MMS.values()))

    def health(self) -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
            return True
        except ImportError:
            return False
