"""Whisper hallucination detection.

On silence or low-level ambient noise, Whisper "fills in" phrases from its
training data — overwhelmingly YouTube outros ("thanks for watching", "please
subscribe", "see you in the next video", "thank you"). These are NOT real
speech and must not (a) show up in the live-hearing debug feed, (b) trip the
wake word, or (c) become actual chat turns.
"""

from __future__ import annotations

import re

# Exact (normalised) phrases Whisper emits on silence.
_PHANTOM_EXACT = frozenset(
    {
        "you",
        "thank you",
        "thanks",
        "thank you very much",
        "thank you so much",
        "thanks for watching",
        "thank you for watching",
        "thanks for watching!",
        "please subscribe",
        "subscribe",
        "like and subscribe",
        "please like and subscribe",
        "don't forget to subscribe",
        "see you next time",
        "see you in the next video",
        "i'll see you in the next video",
        "the end",
        "bye",
        "bye bye",
        "okay",
        "ok",
        "so",
        "yeah",
        "hmm",
        "mm",
        "mmm",
        "uh",
        "um",
    }
)

# Signature fragments of YouTube-outro hallucinations — if any appears, the
# whole utterance is almost certainly phantom.
_PHANTOM_FRAGMENTS = (
    "for watching",
    "subscribe",
    "next video",
    "next episode",
    "liked this video",
    "like this video",
    "see you next",
    "see you in the next",
    "thanks for watching",
    "this channel",
    "notification bell",
)


def _normalise(text: str) -> str:
    t = text.strip().lower()
    # collapse repeated punctuation/words like "? ? ? ?" or "the world of the world"
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_hallucination(text: str) -> bool:
    """Return True if *text* is almost certainly a Whisper phantom (not speech)."""
    t = _normalise(text)
    if not t:
        return True
    if t in _PHANTOM_EXACT:
        return True
    for frag in _PHANTOM_FRAGMENTS:
        if frag in t:
            return True
    # Degenerate repetition ("the world of the world of the world"): if the text
    # is 6+ words but only has <=3 distinct words, it's a Whisper loop artifact.
    words = t.split()
    if len(words) >= 6 and len(set(words)) <= 3:
        return True
    # Single mashed/stuttered token ("wwwwwww" from "W-w-w-w-w"): long but made
    # of <=2 distinct characters.
    compact = t.replace(" ", "")
    if len(compact) >= 5 and len(set(compact)) <= 2:
        return True
    return False
