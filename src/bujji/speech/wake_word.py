"""Wake word detector for B.U.J.J.I — triggers on 'bujji'."""

from __future__ import annotations

import difflib
import logging
import threading
import time
from typing import Callable, List, Optional

from bujji.brand import get_branding
from bujji.speech.hallucinations import is_hallucination

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_CHUNK_SECONDS = 1.5
_SILENCE_RMS = 0.005

# Built-in extra wake phrases (multi-word; matched as substrings of the
# transcript). "hey/ok bujji" work via the single-word match already, but are
# listed for clarity. Users can add their own in ~/.bujji/wake_phrases.json
# (a JSON list of strings) without touching code.
_DEFAULT_EXTRA_PHRASES = {
    "hey bujji",
    "ok bujji",
    "wake up bujji",
    "daddy's home",
    "daddys home",
    "daddy is home",
    # whisper-tiny often hears "hey bujji" as "hey buddy" — phrase-guarded so
    # a lone "buddy" in conversation doesn't trigger
    "hey buddy",
    "hey badji",
}

_phrase_cache: tuple[float, set[str]] | None = None


def _extra_phrases() -> set[str]:
    """Built-in phrases plus user additions from ~/.bujji/wake_phrases.json."""
    global _phrase_cache
    import json
    from pathlib import Path

    path = Path.home() / ".bujji" / "wake_phrases.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _DEFAULT_EXTRA_PHRASES
    if _phrase_cache and _phrase_cache[0] == mtime:
        return _phrase_cache[1]
    phrases = set(_DEFAULT_EXTRA_PHRASES)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
        phrases.update(str(p).strip().lower() for p in user if str(p).strip())
    except Exception:
        logger.warning("Could not parse %s — using built-in wake phrases", path)
    _phrase_cache = (mtime, phrases)
    return phrases


def _wake_word_variants() -> set[str]:
    wake_word = get_branding().wake_word.strip().lower() or "bujji"
    collapsed = wake_word.replace(" ", "")
    variants = {
        wake_word,
        collapsed,
        collapsed.replace("jj", "j"),
        f"{collapsed}e",
        f"{collapsed}ee",
        f"{collapsed}y",
    }
    if collapsed == "bujji":
        # /bˈʌ.dʒi/ ("buh-jee") — plus what Whisper commonly mis-hears
        variants.update({"buji", "buje", "bujee", "bujjee", "buggy", "bouji",
                         "budgie", "budji", "budgy", "bhujji", "boojee", "baji", "bajji", "butji", "boji", "bodhi", "buchi", "bougie", "bujie", "abuji", "buhji", "abu ji", "habu ji", "boogie", "buddhi", "budhi", "bhoji"})
    variants.update(_extra_phrases())
    return {value for value in variants if len(value) >= 3}


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation so 'Hey, buddy.' matches 'hey buddy'."""
    import re as _re

    return _re.sub(r"\s+", " ", _re.sub(r"[^a-z' ]", " ", text.lower())).strip()


def _contains_wake_word(text: str) -> bool:
    lower = _normalize(text)
    wake_words = _wake_word_variants()
    for w in wake_words:
        if w in lower:
            return True
    for word in lower.split():
        if len(word) < 3:
            continue
        for target in wake_words:
            if difflib.SequenceMatcher(None, word, target).ratio() >= 0.78:
                return True
    return False


class BujjiWakeWordDetector:
    """Continuously listens for the configured wake word using Whisper on CPU."""

    def __init__(self, wake_word: Optional[str] = None) -> None:
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._model = None
        self._noise_floor: Optional[float] = None
        # Optional live-debug hook: called with (text, rms) for every
        # transcribed chunk so the UI can show what is being heard.
        self.on_text: Optional[Callable[[str, float], None]] = None
        if wake_word:
            # Override branding wake word at instance level
            self._wake_word_override = wake_word.strip().lower()
        else:
            self._wake_word_override = None

    def add_callback(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if fn not in self._callbacks:
                self._callbacks.append(fn)

    def remove_callback(self, fn: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._callbacks.remove(fn)
            except ValueError:
                pass

    def _get_wake_variants(self) -> set[str]:
        word = self._wake_word_override or get_branding().wake_word.strip().lower() or "bujji"
        collapsed = word.replace(" ", "")
        variants = {word, collapsed, collapsed.replace("jj", "j"),
                    f"{collapsed}e", f"{collapsed}ee", f"{collapsed}y"}
        if collapsed == "bujji":
            # /bˈʌ.dʒi/ ("buh-jee") — plus what Whisper commonly mis-hears
            variants.update({"buji", "buje", "bujee", "bujjee", "buggy", "bouji",
                             "budgie", "budji", "budgy", "bhujji", "boojee", "baji", "bajji", "butji", "boji", "bodhi", "buchi", "bougie", "bujie", "abuji", "buhji", "abu ji", "habu ji", "boogie", "buddhi", "budhi", "bhoji"})
        variants.update(_extra_phrases())
        return {v for v in variants if len(v) >= 3}

    def _contains_wake(self, text: str) -> bool:
        lower = _normalize(text)
        wake_words = self._get_wake_variants()
        for w in wake_words:
            if w in lower:
                return True
        for word in lower.split():
            if len(word) < 3:
                continue
            for target in wake_words:
                if difflib.SequenceMatcher(None, word, target).ratio() >= 0.78:
                    return True
        return False

    def _fire(self) -> None:
        with self._lock:
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb()
            except Exception:
                logger.debug("Wake word callback error", exc_info=True)

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel("tiny", device="cpu", compute_type="int8")
            logger.info("B.U.J.J.I wake detector: tiny Whisper model ready")
        except Exception as exc:
            logger.warning("B.U.J.J.I wake detector: model load failed: %s", exc)
        return self._model

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="bujji-wake"
        )
        self._thread.start()
        logger.info(
            "B.U.J.J.I wake word detector started (say '%s')",
            get_branding().wake_word,
        )

    def stop(self, *, wait: bool = True) -> None:
        """Stop listening. With wait=True, blocks until the mic is released —
        starting another recorder while our sd.rec is mid-chunk corrupts both."""
        self._active = False
        t = self._thread
        if wait and t is not None and t is not threading.current_thread():
            t.join(timeout=4.0)

    def _loop(self) -> None:
        model = self._ensure_model()
        if model is None:
            # Model failed to load — clear _active so status/callers don't
            # believe we're listening when the thread has already died.
            self._active = False
            logger.warning("B.U.J.J.I wake detector: not listening (no model)")
            return

        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            self._active = False
            logger.warning("Wake detector: sounddevice/numpy not available")
            return

        chunk = int(_SAMPLE_RATE * _CHUNK_SECONDS)
        chunk_count = 0
        logger.warning("Wake loop started on input device: %s",
                       sd.query_devices(kind="input")["name"])

        while self._active:
            try:
                chunk_count += 1
                if chunk_count % 20 == 1:
                    logger.info(
                        "Wake loop alive: chunk %d, noise floor %s",
                        chunk_count,
                        f"{self._noise_floor:.5f}" if self._noise_floor else "unset",
                    )
                audio = sd.rec(
                    chunk,
                    samplerate=_SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                )
                sd.wait()
                if not self._active:
                    break

                flat = np.nan_to_num(audio.flatten().astype(np.float64))
                rms = float(np.sqrt(np.mean(flat**2)))
                if not np.isfinite(rms):
                    continue

                # Adaptive gate with a MIN-TRACKING floor: the floor may drop
                # instantly but can only creep up very slowly — otherwise
                # speaker bleed/hum ratchets it above a quiet mic's speech
                # level and the user gets gated out as "background noise".
                if self._noise_floor is None:
                    self._noise_floor = rms
                if rms < self._noise_floor:
                    self._noise_floor = rms
                gate = max(self._noise_floor * 2.0, 6e-4)
                if rms < gate:
                    if rms < self._noise_floor * 1.3:
                        self._noise_floor = 0.98 * self._noise_floor + 0.02 * rms
                    continue

                logger.info("Wake gate passed: rms %.5f (floor %.5f) — transcribing",
                            rms, self._noise_floor)
                # Normalize so Whisper sees healthy amplitude regardless of gain
                peak = float(np.abs(flat).max())
                if 0 < peak < 0.5:
                    flat = flat * (0.7 / peak)
                flat = flat.astype(np.float32)

                segments, _ = model.transcribe(
                    flat,
                    language="en",
                    condition_on_previous_text=False,
                    without_timestamps=True,
                    # Let Whisper's own VAD drop non-speech chunks before it
                    # gets a chance to invent words for them.
                    vad_filter=True,
                )
                # Whisper-tiny invents whole plausible sentences from ambient
                # noise. Those phantom segments carry a tell: very high
                # no_speech_prob and/or very low avg_logprob. Keep only segments
                # that actually look like speech, so novel hallucinated
                # sentences (which the phrase blacklist can't enumerate) are
                # dropped by confidence, not by string match.
                kept = []
                for s in segments:
                    no_speech = getattr(s, "no_speech_prob", 0.0) or 0.0
                    avg_logprob = getattr(s, "avg_logprob", 0.0) or 0.0
                    if no_speech > 0.6 or avg_logprob < -1.0:
                        logger.info(
                            "Wake chunk segment dropped (no_speech=%.2f avg_logprob=%.2f): %r",
                            no_speech, avg_logprob, s.text,
                        )
                        continue
                    kept.append(s.text)
                text = " ".join(kept).strip()

                # Drop Whisper phantoms (YouTube-outro hallucinations on
                # silence) — don't show them in the debug feed OR trip the wake.
                if text and is_hallucination(text):
                    logger.info("Wake chunk ignored (hallucination): %r", text)
                    text = ""

                if text:
                    # WARNING level so it shows in server.log — this is the
                    # ground truth for "is she hearing me" debugging.
                    logger.warning(
                        "Wake chunk heard: %r (rms %.4f, floor %.5f)",
                        text, rms, self._noise_floor,
                    )
                    if self.on_text is not None:
                        try:
                            self.on_text(text, rms)
                        except Exception:
                            pass

                if text and self._contains_wake(text):
                    logger.warning("WAKE WORD DETECTED in: %r", text)
                    self._fire()
                    time.sleep(2.0)

            except Exception as exc:
                if self._active:
                    logger.debug("Wake loop error: %s", exc)
                    time.sleep(0.5)
