"""Model router — picks fast (phi4) or complex (qwen3:30b) model per query."""

from __future__ import annotations

import re

_TOOL_KEYWORDS = re.compile(
    r"\b(search|find|open|run|play|turn|set|create|schedule|email|calendar|weather|"
    r"news|calculate|convert|remind|lookup|check|status|list|show|write|generate|"
    r"code|explain|research|compare|analyze|summarize|translate|download|upload|"
    r"install|config|configure|build|deploy|test|debug|fix|help me)\b",
    re.IGNORECASE,
)

_SHORT_THRESHOLD = 80

# Romanized Telugu (Tenglish) markers — common words unlikely in English text
_TENGLISH = re.compile(
    r"\b(nuvvu|nuvu|meeru|nenu|unnav|unnava|unnara|vunnav|vunnava|vunnara|"
    r"undava|cheppu|chepu|cheppava|yela|ela|yala|enti|emiti|emundi|kavali|"
    r"kaavali|chey|cheyi|cheyyi|cheyagalavu|bagunnava|bagunnara|baagunnava|"
    r"vachha|vacha|vastha|velli|telusa|telsa|ledu|leda|avunu|kada|kadha|"
    r"andi|garu|emo|enduku|ekkada|eppudu|evaru)\b",
    re.IGNORECASE,
)


def is_indic(query: str) -> bool:
    """True for Telugu/Devanagari/Tamil/Kannada script or romanized Telugu."""
    if any(
        ("ఀ" <= c <= "౿") or ("ऀ" <= c <= "ॿ") or ("஀" <= c <= "௿") or ("ಀ" <= c <= "೿")
        for c in query
    ):
        return True
    return len(_TENGLISH.findall(query)) >= 1


def route(
    query: str,
    *,
    fast_model: str = "phi4:14b",
    complex_model: str = "qwen3:30b",
    indic_model: str = "",
) -> str:
    """Return the appropriate model name for this query.

    Indic path: Telugu (script or romanized) → a model that actually speaks it.
    Fast path: short greetings, simple factual questions, timers.
    Complex path: anything requiring tools, research, or reasoning.
    """
    if indic_model and is_indic(query):
        return indic_model
    if len(query) <= _SHORT_THRESHOLD and not _TOOL_KEYWORDS.search(query):
        return fast_model
    return complex_model


__all__ = ["route", "is_indic"]
