"""ChannelAgent â€” bridge between messaging channels and AI agents.

Routes incoming :class:`~bujji.channels._stubs.ChannelMessage` objects
to an agent, classifies queries as "quick" or "deep", and delivers responses
either inline or as a preview with an escalation link to a full report.
"""

from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from bujji.channels._stubs import BaseChannel, ChannelMessage
from bujji.channels.trust import (
    ChannelTrustPolicy,
    PairingManager,
    TrustLevel,
)

# ---------------------------------------------------------------------------
# Query classifier
# ---------------------------------------------------------------------------

_QUICK_PREFIXES = re.compile(
    r"^(when\b|where\b|find\b|who\b|what's\b)",
    re.IGNORECASE,
)

_DEEP_KEYWORDS = re.compile(
    r"\b(summarize|research|context|compare|analyze|overview|timeline|history)\b",
    re.IGNORECASE,
)

_TIME_RANGE = re.compile(
    r"\blast\s+(week|month|quarter|year)\b",
    re.IGNORECASE,
)


def classify_query(text: str) -> str:
    """Return ``'quick'`` or ``'deep'`` based on heuristics.

    Deep signals take priority over quick signals.  A query is classified as
    deep if it contains deep keywords, a time-range phrase, or is longer than
    20 words.  Otherwise it is quick.
    """
    words = text.split()
    if _DEEP_KEYWORDS.search(text):
        return "deep"
    if _TIME_RANGE.search(text):
        return "deep"
    if len(words) > 20:
        return "deep"
    return "quick"


# ---------------------------------------------------------------------------
# ChannelAgent
# ---------------------------------------------------------------------------

_ESCALATION_TEMPLATE = "{preview}...\n\n---\nFull report ready â€” open in Bujji:\nbujji://research/{session_id}"
_LONG_RESPONSE_THRESHOLD = 500

_PAIRING_PROMPT = (
    "You're not authorized to use this assistant yet. Ask the owner to approve "
    "you by running:\n\n    bujji channel pair {code}\n\n"
    "Then send your message again."
)


class ChannelAgent:
    """Bridge between a :class:`BaseChannel` and an agent.

    On each incoming message the agent is invoked in a background thread so
    that :meth:`_handle_message` never blocks the channel's event loop.
    Quick queries with short responses are delivered inline; deep queries or
    long responses trigger a preview + escalation link.

    Parameters
    ----------
    channel:
        A connected :class:`BaseChannel` instance.
    agent:
        Any object that exposes a ``run(input: str) -> AgentResult``-compatible
        method (typically a :class:`~bujji.agents._stubs.BaseAgent`
        subclass).
    max_workers:
        Size of the background :class:`~concurrent.futures.ThreadPoolExecutor`.
    """

    def __init__(
        self,
        channel: BaseChannel,
        agent: Any,
        *,
        max_workers: int = 2,
        trust_policy: Optional[ChannelTrustPolicy] = None,
        bus: Any = None,
    ) -> None:
        self._channel = channel
        self._agent = agent
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        # Opt-in trust gate. When no policy is supplied the gate is inert and
        # behaviour is identical to before (every message runs the agent).
        self._trust_policy = trust_policy
        self._pairing = (
            PairingManager(trust_policy.store) if trust_policy is not None else None
        )
        self._bus = bus
        channel.on_message(self._handle_message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_message(self, msg: ChannelMessage) -> Optional[str]:
        """Submit *msg* to the background pool and return ``None`` immediately."""
        self._pool.submit(self._process_message, msg)
        return None

    def _resolve_trust(self, msg: ChannelMessage) -> Optional[TrustLevel]:
        """Resolve the sender's trust level, or None when the gate is off."""
        if self._trust_policy is None:
            return None
        return self._trust_policy.resolve(
            msg.channel, msg.sender, metadata=msg.metadata
        )

    def _deny_untrusted(self, msg: ChannelMessage) -> None:
        """Issue a pairing prompt to an untrusted sender and record the block."""
        assert self._pairing is not None
        code = self._pairing.code_for(msg.channel, msg.sender)
        self._emit_security(
            "SECURITY_BLOCK",
            {
                "channel": msg.channel,
                "sender": msg.sender,
                "reason": "untrusted sender",
                "pairing_code": code,
            },
        )
        self._channel.send(
            msg.conversation_id,
            _PAIRING_PROMPT.format(code=code),
            conversation_id=msg.message_id,
        )

    def _emit_security(self, event_name: str, data: dict) -> None:
        if self._bus is None:
            return
        try:
            from bujji.core.events import EventType

            self._bus.publish(getattr(EventType, event_name), data)
        except Exception:  # never let telemetry break message handling
            pass

    def _run_agent(self, msg: ChannelMessage, trust: Optional[TrustLevel]) -> Any:
        """Invoke the agent, restoring per-sender identity into its context.

        Historically only ``msg.content`` reached ``agent.run()`` — sender,
        channel and trust level were dropped. When the gate is active we pass an
        :class:`AgentContext` carrying that identity so downstream tools/agents
        can make sender-aware decisions. When the gate is off we keep the
        original single-argument call for full backward compatibility.
        """
        if trust is None:
            return self._agent.run(msg.content)
        try:
            from bujji.agents._stubs import AgentContext

            ctx = AgentContext(
                metadata={
                    "channel": msg.channel,
                    "sender": msg.sender,
                    "trust_level": trust.value,
                }
            )
            return self._agent.run(msg.content, context=ctx)
        except TypeError:
            # Agent doesn't accept a context kwarg — fall back gracefully.
            return self._agent.run(msg.content)

    def _process_message(self, msg: ChannelMessage) -> None:
        """Classify, run the agent, and reply to the channel."""
        # Trust gate (opt-in). Untrusted senders are blocked before the agent
        # runs and offered a pairing code; primary/trusted senders proceed.
        trust = self._resolve_trust(msg)
        if trust is not None and not trust.can_run:
            self._deny_untrusted(msg)
            return

        query_type = classify_query(msg.content)
        session_id = msg.session_id or uuid.uuid4().hex[:16]

        try:
            result = self._run_agent(msg, trust)
            response_text: str = getattr(result, "content", str(result))
        except Exception as exc:  # noqa: BLE001
            friendly = (
                f"Sorry, I ran into an error while processing your request: {exc}"
            )
            # First positional arg is the DESTINATION (per-adapter native
            # ID â€” Discord channel ID, Slack channel ID, etc.); not the
            # channel TYPE label. The `conversation_id=` kwarg is the
            # native message ID for reply threading (per DiscordChannel
            # / SlackChannel / etc. send() contract â€” see #459).
            self._channel.send(
                msg.conversation_id,
                friendly,
                conversation_id=msg.message_id,
            )
            return

        is_long = len(response_text) > _LONG_RESPONSE_THRESHOLD

        if query_type == "deep" or is_long:
            preview = response_text[:_LONG_RESPONSE_THRESHOLD]
            reply = _ESCALATION_TEMPLATE.format(
                preview=preview,
                session_id=session_id,
            )
        else:
            reply = response_text

        # Same field-mapping as the error path above (#459).
        self._channel.send(
            msg.conversation_id,
            reply,
            conversation_id=msg.message_id,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Shut down the background thread pool."""
        self._pool.shutdown(wait=True)


__all__ = ["ChannelAgent", "classify_query"]
