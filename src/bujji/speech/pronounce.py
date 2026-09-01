"""Phonetic respelling for TTS — display text keeps real spellings.

English TTS engines read "Bujji" as "buj ji" and "Prasanna" as "pra-SAN-na".
Respell just before synthesis so they say /bˈʊʤi/ (Telugu బుజ్జి) and
/prəˈsʌnə/ (pruh-SUH-nuh). Kokoro supports exact phoneme markdown
([word](/phonemes/)) — loopback+STT verified /bˈʊʤi/ is transcribed back as
"Bujji". Users can add words in ~/.bujji/pronunciations.json
({"word": "respelling", ...}).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict

_BUILTIN: Dict[str, str] = {
    "bujji": "Booji",
    "prasanna": "Prasunna",
}

# Exact phonemes for Kokoro (misaki G2P markdown syntax)
_KOKORO_PHONEMES: Dict[str, str] = {
    "bujji": "[Bujji](/bˈʊʤi/)",
    "prasanna": "[Prasanna](/pɹəsˈʌnə/)",
}

_ACRONYM = r"B\.?U\.?J\.?J\.?I\.?"

_USER_FILE = Path.home() / ".bujji" / "pronunciations.json"
_cache: tuple | None = None  # (mtime, mapping, compiled pattern)


def _load():
    global _cache
    try:
        mtime = _USER_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _cache and _cache[0] == mtime:
        return _cache[1], _cache[2]
    mapping = dict(_BUILTIN)
    if mtime:
        try:
            user = json.loads(_USER_FILE.read_text(encoding="utf-8"))
            mapping.update(
                {str(k).lower(): str(v) for k, v in user.items() if str(k).strip()}
            )
        except Exception:
            pass
    words = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        r"\b(" + "|".join([_ACRONYM] + [re.escape(w) for w in words]) + r")\b",
        re.IGNORECASE,
    )
    _cache = (mtime, mapping, pattern)
    return mapping, pattern


_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty"]


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, one = _TENS[n // 10], n % 10
    return tens if one == 0 else f"{tens} {_ONES[one]}"


_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _speak_times(text: str) -> str:
    """Turn digital clock times into natural speech.

    "04:06" -> "four oh six", "07:34" -> "seven thirty four",
    "12:00" -> "twelve o'clock". Otherwise TTS reads the leading zero and
    colon literally ("zero four zero six").
    """

    def _sub(m: re.Match) -> str:
        h, mnt = int(m.group(1)), int(m.group(2))
        if h == 0:
            h = 12
        elif h > 12:
            h -= 12  # 15:45 -> "three forty five"
        if mnt >= 60:
            return m.group(0)
        hour_w = _num_words(h)
        if mnt == 0:
            return f"{hour_w} o'clock"
        if mnt < 10:
            return f"{hour_w} oh {_num_words(mnt)}"
        return f"{hour_w} {_num_words(mnt)}"

    return _TIME_RE.sub(_sub, text)


def respell(text: str, engine: str = "") -> str:
    """Single-pass phonetic respelling; pass the TTS backend id as *engine*."""
    text = _speak_times(text)
    # Windows SAPI already applies the selected voice's pronunciation rules.
    # The old generic rewrites (Bujji -> Booji, Prasanna -> Prasunna) made the
    # native female voice sound unnatural and inconsistent with UI previews.
    if engine == "windows_sapi":
        return text
    mapping, pattern = _load()
    kokoro = engine == "kokoro"

    def _sub(m: re.Match) -> str:
        word = m.group(1).lower().replace(".", "")
        if kokoro and word in _KOKORO_PHONEMES:
            return _KOKORO_PHONEMES[word]
        return mapping.get(word, mapping.get("bujji", m.group(1)))

    return pattern.sub(_sub, text)


__all__ = ["respell"]
