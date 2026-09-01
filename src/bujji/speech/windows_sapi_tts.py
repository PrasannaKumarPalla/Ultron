"""Offline Windows text-to-speech using the built-in SAPI voices."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List

from bujji.core.registry import TTSRegistry
from bujji.speech.tts import TTSBackend, TTSResult


@TTSRegistry.register("windows_sapi")
class WindowsSapiTTSBackend(TTSBackend):
    """Generate WAV audio without network access or downloaded ML models."""

    backend_id = "windows_sapi"

    @staticmethod
    def _voice_and_tokens():
        if os.name != "nt":
            raise RuntimeError("Windows SAPI is only available on Windows")
        import win32com.client

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        return voice, list(voice.GetVoices())

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            voice, tokens = self._voice_and_tokens()
            if voice_id:
                wanted = voice_id.casefold()
                for token in tokens:
                    if wanted in (
                        token.Id.casefold(),
                        token.GetDescription().casefold(),
                    ):
                        voice.Voice = token
                        break
            voice.Rate = max(-10, min(10, round((speed - 1.0) * 5)))
            handle, raw_path = tempfile.mkstemp(suffix=".wav")
            os.close(handle)
            path = Path(raw_path)
            try:
                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Open(str(path), 3, False)
                voice.AudioOutputStream = stream
                voice.Speak(text)
                stream.Close()
                audio = path.read_bytes()
            finally:
                path.unlink(missing_ok=True)
            selected_voice = voice.Voice.GetDescription()
            del stream, tokens, voice
        finally:
            pythoncom.CoUninitialize()
        return TTSResult(
            audio=audio,
            format="wav",
            voice_id=selected_voice,
            sample_rate=22050,
            metadata={"backend": self.backend_id, "offline": True},
        )

    def available_voices(self) -> List[str]:
        import pythoncom

        pythoncom.CoInitialize()
        try:
            voice, tokens = self._voice_and_tokens()
            names = [token.GetDescription() for token in tokens]
            del tokens, voice
            return names
        except Exception:
            return []
        finally:
            pythoncom.CoUninitialize()

    def health(self) -> bool:
        import pythoncom

        pythoncom.CoInitialize()
        try:
            voice, tokens = self._voice_and_tokens()
            ready = bool(tokens)
            del tokens, voice
            return ready
        except Exception:
            return False
        finally:
            pythoncom.CoUninitialize()
