"""VoicePipeline — orchestrates wake → STT → agent → TTS → play."""

from __future__ import annotations

import io
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class VoicePipeline:
    """Ties wake word, STT, agent, and TTS into a continuous loop.

    On wake detection: records audio via VAD → transcribes → runs agent
    → synthesizes TTS → plays back. Supports barge-in (interrupt playback).
    """

    def __init__(
        self,
        agent,
        stt,
        tts,
        wake_detector,
        *,
        voice_id: str = "bm_george",
        tts_speed: float = 1.0,
        conversation_mode: bool = True,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._agent = agent
        self._stt = stt
        self._tts = tts
        self._wake = wake_detector
        self._voice_id = voice_id
        self._tts_speed = tts_speed
        self._conversation_mode = conversation_mode
        self._on_event = on_event or (lambda e: None)
        self._active = False
        self._muted = False
        self._playing = False
        self._play_stop = threading.Event()
        self._turn_lock = threading.Lock()

    def _emit(self, event: dict) -> None:
        try:
            self._on_event(event)
        except Exception:
            pass

    def start(self) -> None:
        self._active = True
        self._wake.add_callback(self._on_wake)
        self._wake.start()
        logger.info("VoicePipeline started — listening for wake word")
        self._emit({"type": "ready", "product_name": _brand_name()})

    def stop(self) -> None:
        self._active = False
        self._wake.remove_callback(self._on_wake)
        self._wake.stop()

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        """Mute/unmute wake-word listening. Also interrupts current playback on mute."""
        self._muted = bool(muted)
        if self._muted:
            self.interrupt()
        self._emit({"type": "mute_changed", "muted": self._muted})

    def _on_wake(self, *, suppress_ack: bool = False) -> None:
        if not self._active or self._muted:
            return
        if self._playing:
            self.interrupt()
        threading.Thread(
            target=self._handle_turn,
            kwargs={"suppress_ack": suppress_ack},
            daemon=True,
            name="bujji-turn",
        ).start()

    def _handle_turn(self, *, suppress_ack: bool = False) -> None:
        if not self._turn_lock.acquire(blocking=False):
            return  # drop overlapping wake if already mid-turn
        try:
            # ONE mic owner at a time: the wake loop's sd.rec and the turn's
            # VAD recorder corrupt each other's buffers if they overlap.
            try:
                self._wake.stop(wait=True)
            except Exception:
                pass
            self._run_turn(suppress_initial_ack=suppress_ack)
        finally:
            self._turn_lock.release()
            if self._active and not self._muted:
                try:
                    self._wake.start()
                except Exception:
                    logger.exception("Could not restart wake detector")

    # Follow-up utterances that end a continuous conversation (EN + Tenglish/Telugu).
    _STOP_WORDS = {
        "stop", "bye", "goodbye", "cancel", "nothing", "that's all", "thats all",
        "thank you", "thanks", "chalu", "aagu", "aagipo", "inka chalu", "vellipo",
    }

    def _run_turn(self, *, suppress_initial_ack: bool = False) -> None:
        """One wake-word activation: a single turn, or a whole conversation
        when conversation_mode is on (keeps listening until silence/stop word)."""
        follow_up = False
        while self._active:
            spoke = self._run_single_turn(
                follow_up=follow_up,
                play_ack=not follow_up and not suppress_initial_ack,
            )
            if not spoke or not self._conversation_mode:
                return
            follow_up = True

    @staticmethod
    def _play_ack(rising: bool = True) -> None:
        """Short Alexa-style chime: rising = wake acknowledged, falling = done."""
        try:
            import numpy as np
            import sounddevice as sd

            sr = 44100
            freqs = (620, 880) if rising else (880, 620)
            parts = []
            for f in freqs:
                t = np.linspace(0, 0.09, int(sr * 0.09), False)
                tone = 0.25 * np.sin(2 * np.pi * f * t)
                fade = np.linspace(1, 0, len(tone)) if not rising else np.ones(len(tone))
                parts.append((tone * fade).astype("float32"))
            data = np.concatenate(parts).reshape(-1, 1)
            with sd.OutputStream(samplerate=sr, channels=1, dtype="float32") as out:
                out.write(data)
        except Exception:
            pass

    def _run_single_turn(
        self, follow_up: bool = False, *, play_ack: bool = True
    ) -> bool:
        """Record → STT → agent → TTS. Returns True if a reply was spoken
        (meaning a follow-up should be listened for)."""
        from bujji.speech.vad import record_until_silence

        try:
            self._emit({"type": "state_change", "from": "idle", "to": "listening"})
            self._emit({"type": "recording_start"})
            if play_ack:
                self._play_ack(rising=True)  # audible "I heard the wake word"

            audio_bytes = record_until_silence()

            self._emit({"type": "recording_end"})

            if not audio_bytes:
                if not follow_up:
                    self._emit({"type": "error", "message": "No audio captured", "code": "no_audio"})
                self._emit({"type": "state_change", "from": "listening", "to": "idle"})
                return False

            self._emit({"type": "state_change", "from": "listening", "to": "thinking"})

            result = self._stt.transcribe(audio_bytes, format="wav")
            transcript = result.text.strip()

            if not transcript:
                self._emit({"type": "state_change", "from": "thinking", "to": "idle"})
                return False

            # Hallucination filter: recording ambient noise makes Whisper
            # invent garbage glyphs — don't send those to the agent.
            # Drop Whisper phantom phrases ("thanks for watching", "please
            # subscribe", etc.) so they never become a real chat turn.
            from bujji.speech.hallucinations import is_hallucination

            if is_hallucination(transcript):
                logger.warning("Discarding phantom transcript: %r", transcript[:60])
                self._emit({"type": "state_change", "from": "thinking", "to": "idle"})
                return False

            import re as _re
            # Latin + Telugu + Devanagari + Tamil + Kannada — every script the
            # pipeline can detect/route a voice for. Missing a script here makes
            # valid speech in it get dropped as "hallucinated".
            real_chars = _re.findall(
                r"[A-Za-zఀ-౿ऀ-ॿ஀-௿ಀ-೿]",
                transcript,
            )
            if len(real_chars) < max(2, 0.4 * len(transcript.replace(" ", ""))):
                logger.warning("Discarding hallucinated transcript: %r", transcript[:60])
                self._emit({"type": "state_change", "from": "thinking", "to": "idle"})
                return False

            if transcript.lower().strip(" .!?") in self._STOP_WORDS:
                self._emit({"type": "transcript", "text": transcript, "final": True})
                self._emit({"type": "state_change", "from": "thinking", "to": "idle"})
                return False

            self._emit({"type": "transcript", "text": transcript, "final": True})
            logger.warning("Turn transcript: %r", transcript)

            from bujji.agents.model_router import route as _route_model
            model = _route_model(transcript)
            self._emit({"type": "model_selected", "model": model})

            # Same path as the chat panel (/v1/chat/completions), but STREAMED:
            # emit each token as it arrives so the text reply shows up instantly
            # instead of after the whole (multi-second) local generation. The
            # HTTP route still injects memory + language routing before the
            # stream branch, so those are preserved. Streaming bypasses the
            # agent's tool loop — fine for conversational turns.
            response = ""
            try:
                import json as _json

                import httpx

                with httpx.stream(
                    "POST",
                    "http://127.0.0.1:8000/v1/chat/completions",
                    json={"model": "default", "stream": True,
                          "messages": [{"role": "user", "content": transcript}]},
                    timeout=240,
                ) as r:
                    if r.status_code == 200:
                        for line in r.iter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = _json.loads(data)
                            except Exception:
                                continue
                            delta = (
                                ((chunk.get("choices") or [{}])[0].get("delta") or {})
                                .get("content") or ""
                            )
                            if delta:
                                response += delta
                                self._emit({"type": "token", "text": delta})
            except Exception:
                logger.exception("Voice turn: chat stream failed, falling back to agent.run")

            response = response.strip()
            if not response:
                agent_result = self._agent.run(transcript)
                response = (
                    agent_result.content.strip()
                    if agent_result and agent_result.content
                    else ""
                )
            logger.warning("Turn response (%d chars): %r", len(response), response[:120])

            if not response:
                self._emit({"type": "state_change", "from": "thinking", "to": "idle"})
                return False

            self._emit({"type": "state_change", "from": "thinking", "to": "speaking"})
            self._emit({"type": "response", "text": response, "user_text": transcript})

            try:
                from bujji.connectors.obsidian_journal import log_exchange
                log_exchange(transcript, response, source="voice")
            except Exception:
                pass
            self._emit({"type": "speaking_start"})

            self._play_tts(response)

            self._emit({"type": "speaking_end"})
            self._emit({"type": "state_change", "from": "speaking", "to": "idle"})
            return True

        except Exception as exc:
            logger.exception("VoicePipeline turn error")
            self._emit({"type": "error", "message": str(exc), "code": "turn_error"})
            self._emit({"type": "state_change", "from": "thinking", "to": "idle"})
            return False

    @staticmethod
    def _clean_for_tts(text: str) -> str:
        import re
        # Extract FINAL_ANSWER if orchestrator format leaked through
        fa = re.search(r"FINAL_ANSWER:\s*(.+)", text, re.DOTALL | re.IGNORECASE)
        if fa:
            text = fa.group(1).strip()
        # Strip markdown: bold/italic, headers, bullet points, code
        text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
        text = re.sub(r"#{1,6}\s+", "", text)
        text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"`[^`]*`", "", text)
        # Strip emoji
        text = re.sub(r"[\U00010000-\U0010ffff]|‍|[☀-➿]", "", text)
        return text.strip()

    @staticmethod
    def _detect_response_lang(text: str) -> str:
        """Return BCP-47 tag based on script found in the response text."""
        if any("ఀ" <= c <= "౿" for c in text):
            return "te"
        if any("ऀ" <= c <= "ॿ" for c in text):
            return "hi"
        if any("஀" <= c <= "௿" for c in text):
            return "ta"
        if any("ಀ" <= c <= "೿" for c in text):
            return "kn"
        return "en"

    def _cached_backend(self, key: str):
        """Build a TTS backend once and reuse it.

        Each backend caches its (often ~150 MB) model on the instance, so
        constructing a fresh one per sentence re-runs the model load. Keep one
        instance per backend key for the life of the pipeline.
        """
        from bujji.core.registry import TTSRegistry

        cache = getattr(self, "_tts_backend_cache", None)
        if cache is None:
            cache = self._tts_backend_cache = {}
        inst = cache.get(key)
        if inst is None:
            inst = cache[key] = TTSRegistry.get(key)()
        return inst

    def _resolve_tts_backend(self, lang: str):
        """Return the right TTS backend instance for the given language."""
        import bujji.speech  # noqa: F401
        from bujji.core.config import load_config
        from bujji.core.registry import TTSRegistry
        from bujji.speech import voice_state

        # 1. Runtime override set by the switch_voice tool wins over config.
        override = voice_state.get_for_language(lang)
        if override and override.get("voice_id"):
            key = override.get("backend", "")
            if key == self._tts.backend_id:
                return self._tts, override["voice_id"], self._tts_speed
            if key and TTSRegistry.contains(key):
                try:
                    alt = self._cached_backend(key)
                    if alt.health():
                        return alt, override["voice_id"], self._tts_speed
                except Exception:
                    pass

        # 2. Config-driven per-language routing.
        cfg = load_config()
        tts_cfg = getattr(cfg, "tts", None)
        if tts_cfg and lang:
            resolved = tts_cfg.for_language(lang)
            key = resolved.backend
            if not key or key == self._tts.backend_id:
                return (
                    self._tts,
                    resolved.voice_id or self._voice_id,
                    resolved.speed if resolved.speed else self._tts_speed,
                )
            if key and key != self._tts.backend_id and TTSRegistry.contains(key):
                try:
                    alt = self._cached_backend(key)
                    if alt.health():
                        return alt, resolved.voice_id, resolved.speed if resolved.speed else self._tts_speed
                except Exception:
                    pass
        return self._tts, self._voice_id, self._tts_speed

    def _offline_fallback(self, lang: str, failed_backend):
        """Return (backend, voice_id) for local MMS TTS, or None if unusable."""
        if lang == "en" or getattr(failed_backend, "backend_id", "") == "mms_tts":
            return None
        from bujji.core.registry import TTSRegistry

        if not TTSRegistry.contains("mms_tts"):
            return None
        try:
            alt = self._cached_backend("mms_tts")
            if alt.health():
                return alt, lang
        except Exception:
            pass
        return None

    def _play_tts(self, text: str) -> None:
        """Speak *text*. Long texts are synthesized sentence-by-sentence with
        the next sentence prefetching while the current one plays — a long
        briefing used to be one giant synth job (minutes before first sound)."""
        text = self._clean_for_tts(text)
        if not text:
            return

        import re as _re

        sentences = [s.strip() for s in _re.findall(r"[^.!?\n।]+(?:[.!?।]+|$)", text) if s.strip()]
        if len(sentences) > 1:
            self._play_tts_sentences(sentences)
            return

        self._play_tts_single(text)

    def _play_tts_sentences(self, sentences: list) -> None:
        """Pipelined playback: synth sentence i+1 while sentence i plays."""
        import concurrent.futures

        self._playing = True
        self._play_stop.clear()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        futures = [pool.submit(self._synth_one, s) for s in sentences]
        try:
            for fut in futures:
                if self._play_stop.is_set():
                    break
                audio = fut.result()
                if audio is not None:
                    self._play_audio(*audio)
        except Exception:
            logger.exception("Sentence-pipelined TTS error")
        finally:
            # On barge-in, don't block on already-queued syntheses — cancel
            # what hasn't started so control returns promptly.
            for fut in futures:
                fut.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            self._playing = False

    def _synth_one(self, sentence: str):
        """Synthesize one sentence -> (data, sr) or None."""
        try:
            import io as _io

            import soundfile as sf

            from bujji.speech.pronounce import respell

            lang = self._detect_response_lang(sentence)
            tts_backend, voice_id, speed = self._resolve_tts_backend(lang)
            result = tts_backend.synthesize(
                respell(sentence, engine=tts_backend.backend_id),
                voice_id=voice_id, speed=speed,
            )
            if not result.audio:
                return None
            data, sr = sf.read(_io.BytesIO(result.audio), dtype="float32")
            return data, sr
        except Exception:
            logger.exception("TTS sentence synth error")
            return None

    def _play_audio(self, data, sr) -> None:
        """Play via a dedicated OutputStream, honoring barge-in."""
        import sounddevice as sd

        if data.ndim == 1:
            data = data.reshape(-1, 1)
        chunk = max(1, sr // 10)
        with sd.OutputStream(samplerate=sr, channels=data.shape[1], dtype="float32") as out:
            for i in range(0, len(data), chunk):
                if self._play_stop.is_set():
                    break
                out.write(data[i : i + chunk])

    def _play_tts_single(self, text: str) -> None:
        self._playing = True
        self._play_stop.clear()
        try:
            import sounddevice as sd
            import soundfile as sf

            from bujji.speech.pronounce import respell

            lang = self._detect_response_lang(text)
            tts_backend, voice_id, speed = self._resolve_tts_backend(lang)
            try:
                tts_result = tts_backend.synthesize(
                    respell(text, engine=tts_backend.backend_id),
                    voice_id=voice_id,
                    speed=speed,
                )
            except Exception as exc:
                # Cloud/online backend failed (e.g. edge_tts with no internet) —
                # retry with the fully-local MMS backend for non-English text.
                fallback = self._offline_fallback(lang, tts_backend)
                if fallback is None:
                    raise
                logger.warning(
                    "TTS backend %s failed (%s) — falling back to mms_tts",
                    tts_backend.backend_id, exc,
                )
                tts_backend, voice_id = fallback
                tts_result = tts_backend.synthesize(
                    respell(text, engine=tts_backend.backend_id),
                    voice_id=voice_id,
                    speed=speed,
                )
            if not tts_result.audio:
                return

            buf = io.BytesIO(tts_result.audio)
            data, sr = sf.read(buf, dtype="float32")
            if data.ndim == 1:
                data = data.reshape(-1, 1)

            # Dedicated OutputStream — sd.play() uses the module-global stream,
            # which the wake-word mic loop (sd.rec every 1.5s) keeps killing,
            # chopping speech into unintelligible fragments.
            chunk = max(1, sr // 10)
            with sd.OutputStream(samplerate=sr, channels=data.shape[1], dtype="float32") as out:
                for i in range(0, len(data), chunk):
                    if self._play_stop.is_set():
                        break
                    out.write(data[i : i + chunk])

        except ImportError as exc:
            logger.warning("TTS playback deps missing: %s", exc)
        except Exception as exc:
            logger.exception("TTS playback error: %s", exc)
        finally:
            self._playing = False

    def interrupt(self) -> None:
        """Interrupt TTS playback for barge-in."""
        if self._playing:
            self._play_stop.set()


def _brand_name() -> str:
    try:
        from bujji.brand import get_branding
        return get_branding().product_name
    except Exception:
        return "B.U.J.J.I"


__all__ = ["VoicePipeline"]
