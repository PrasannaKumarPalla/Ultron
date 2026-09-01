"""Content filter for personal-use local AI.

Called from security/guardrails.py before inference.
No existing file. Schema: FilterResult(blocked: bool, reason: str, category: str).
Permissive for personal use — only blocks genuine harm categories.
User instruction: do all remaining ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(slots=True)
class FilterResult:
    """Result from a content filter check."""

    blocked: bool
    reason: str = ""
    category: str = ""


# ---------------------------------------------------------------------------
# Pattern definitions  (permissive personal-use policy)
# ---------------------------------------------------------------------------

# Each entry: (category, reason, pattern)
_RAW_PATTERNS: List[Tuple[str, str, str]] = [
    # Self-harm — only direct explicit requests for methods, not discussion
    (
        "self_harm",
        "Explicit self-harm method request",
        r"(?i)\b(how\s+to\s+(kill|hang|overdose|cut)\s+(my)?self"
        r"|step[s\s\-]*by[s\s\-]*step.*sui?cide\s+method"
        r"|best\s+way\s+to\s+commit\s+sui?cide)\b",
    ),
    # Credential / key extraction attacks
    (
        "credential_extraction",
        "Credential or API key extraction attempt",
        r"(?i)(ignore\s+(previous|all|above|prior)\s+(instructions?|prompt|rules?)"
        r".*?(password|api[\s_-]?key|secret|token|credential)"
        r"|reveal\s+(your\s+)?(system\s+prompt|api[\s_-]?key|secret\s+key)"
        r"|print\s+your\s+(api[\s_-]?key|secret|password)"
        r"|what\s+is\s+your\s+(api[\s_-]?key|system\s+prompt|password))",
    ),
    # Weapons of mass destruction synthesis — specific synthesis routes only
    (
        "wmd_synthesis",
        "WMD synthesis instructions",
        r"(?i)(synthesis\s+(route|procedure|protocol)\s+for\s+(VX|sarin|novichok|ricin|anthrax|botulinum)"
        r"|how\s+to\s+(make|synthesize|produce|weaponize)\s+(nerve\s+agent|bio(logical)?\s+weapon"
        r"|chemical\s+weapon|dirty\s+bomb|nuclear\s+device)"
        r"|step[s\s\-]*by[s\s\-]*step.*?(chemical|biological|nuclear|radiological)\s+weapon)",
    ),
]

_COMPILED: List[Tuple[str, str, re.Pattern]] = [
    (cat, reason, re.compile(pattern))
    for cat, reason, pattern in _RAW_PATTERNS
]


class ContentFilter:
    """Permissive content filter for personal-use local AI.

    Blocks only:
    - Explicit self-harm method requests
    - Credential/system-prompt extraction jailbreak attempts
    - WMD synthesis instructions

    Everything else (adult content, profanity, hacking discussion, dark topics,
    security research, etc.) is allowed — this is a personal local assistant.
    """

    BLOCKED_PATTERNS = _COMPILED

    def check(self, text: str) -> FilterResult:
        """Check *text* against all block patterns.

        Returns a :class:`FilterResult` with ``blocked=False`` if content
        is allowed, or ``blocked=True`` with category/reason if blocked.
        """
        if not text:
            return FilterResult(blocked=False)

        for category, reason, pattern in self.BLOCKED_PATTERNS:
            if pattern.search(text):
                return FilterResult(blocked=True, reason=reason, category=category)

        return FilterResult(blocked=False)


def create_content_filter() -> ContentFilter:
    """Factory function — returns a default :class:`ContentFilter` instance."""
    return ContentFilter()


__all__ = ["ContentFilter", "FilterResult", "create_content_filter"]
