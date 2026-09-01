"""Per-channel sender trust + progressive pairing.

Today every inbound :class:`~bujji.channels._stubs.ChannelMessage` is handed
straight to ``agent.run()`` with its ``sender`` discarded — any stranger who can
reach a connected channel (a Discord/Slack/Telegram group, an open bot) gets the
same access as the owner. This module adds a trust boundary, adapted from
OpenClaw's gateway model:

- :class:`TrustLevel` — PRIMARY (owner), TRUSTED (paired), UNTRUSTED (unknown).
- :class:`ChannelTrustPolicy` — resolves ``(channel, sender)`` → level from a
  configured owner allowlist plus a persisted paired-sender store
  (``~/.bujji/trust.json``). Generalises the ad-hoc Telegram ``allowed_chat_ids``.
- :class:`PairingManager` — an UNTRUSTED sender in a direct message is issued a
  one-time code; the owner approves it out-of-band (``bujji channel pair <code>``)
  which promotes the sender to TRUSTED. Group senders can never be paired.

The gate is enforced in ``agents.channel_agent.ChannelAgent`` and is entirely
opt-in: construct ChannelAgent without a policy and behaviour is unchanged.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

from bujji.core.paths import get_config_dir

logger = logging.getLogger(__name__)


class TrustLevel(str, Enum):
    """How much a given sender on a given channel is trusted."""

    PRIMARY = "primary"  # the owner — full access
    TRUSTED = "trusted"  # explicitly paired — reduced access
    UNTRUSTED = "untrusted"  # unknown / group — no execution until paired

    @property
    def can_run(self) -> bool:
        """Whether a sender at this level may invoke the agent at all."""
        return self in (TrustLevel.PRIMARY, TrustLevel.TRUSTED)


def _sender_is_group(msg_metadata: Optional[dict]) -> bool:
    """Best-effort DM-vs-group detection from a message's metadata.

    Group/channel senders can never be paired (there's no single human to
    promote), so they stay UNTRUSTED.
    """
    if not msg_metadata:
        return False
    for key in ("is_group", "is_channel", "group", "chat_is_group"):
        if msg_metadata.get(key):
            return True
    chat_type = str(msg_metadata.get("chat_type", "")).lower()
    return chat_type in ("group", "supergroup", "channel")


@dataclass(slots=True)
class _StoreData:
    """On-disk shape of ``trust.json``."""

    # channel -> {sender -> level}
    paired: Dict[str, Dict[str, str]]
    # code -> [channel, sender]
    pending: Dict[str, list]


class TrustStore:
    """Thread-safe JSON persistence for paired senders and pending codes."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or (get_config_dir() / "trust.json")
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> _StoreData:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return _StoreData(
                paired=raw.get("paired", {}),
                pending=raw.get("pending", {}),
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return _StoreData(paired={}, pending={})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"paired": self._data.paired, "pending": self._data.pending},
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def level_for(self, channel: str, sender: str) -> Optional[str]:
        with self._lock:
            return self._data.paired.get(channel, {}).get(sender)

    def set_level(self, channel: str, sender: str, level: str) -> None:
        with self._lock:
            self._data.paired.setdefault(channel, {})[sender] = level
            self._save()

    def add_pending(self, code: str, channel: str, sender: str) -> None:
        with self._lock:
            self._data.pending[code] = [channel, sender]
            self._save()

    def pop_pending(self, code: str) -> Optional[Tuple[str, str]]:
        with self._lock:
            pair = self._data.pending.pop(code, None)
            if pair is None:
                return None
            self._save()
            return pair[0], pair[1]

    def find_pending(self, channel: str, sender: str) -> Optional[str]:
        with self._lock:
            for code, (ch, snd) in self._data.pending.items():
                if ch == channel and snd == sender:
                    return code
            return None

    def pending_items(self) -> Dict[str, list]:
        with self._lock:
            return dict(self._data.pending)


class ChannelTrustPolicy:
    """Resolves the :class:`TrustLevel` for a ``(channel, sender)`` pair.

    Owners are supplied as ``{channel: {sender, ...}}`` (typically from config,
    generalising Telegram's ``allowed_chat_ids``). Paired senders are read from
    the :class:`TrustStore`. Everyone else is UNTRUSTED.
    """

    def __init__(
        self,
        *,
        owners: Optional[Dict[str, set]] = None,
        store: Optional[TrustStore] = None,
    ) -> None:
        self._owners = {ch: set(map(str, s)) for ch, s in (owners or {}).items()}
        self._store = store or TrustStore()

    @property
    def store(self) -> TrustStore:
        return self._store

    def resolve(
        self,
        channel: str,
        sender: str,
        *,
        metadata: Optional[dict] = None,
    ) -> TrustLevel:
        sender = str(sender)
        if sender in self._owners.get(channel, set()):
            return TrustLevel.PRIMARY
        # Group senders can never be paired; they never rise above UNTRUSTED.
        if _sender_is_group(metadata):
            return TrustLevel.UNTRUSTED
        stored = self._store.level_for(channel, sender)
        if stored is not None:
            try:
                return TrustLevel(stored)
            except ValueError:
                return TrustLevel.UNTRUSTED
        return TrustLevel.UNTRUSTED


class PairingManager:
    """Issues and redeems one-time pairing codes."""

    def __init__(self, store: TrustStore) -> None:
        self._store = store

    def code_for(self, channel: str, sender: str) -> str:
        """Return the existing pending code for a sender, or mint a new one."""
        existing = self._store.find_pending(channel, sender)
        if existing is not None:
            return existing
        code = secrets.token_hex(3).upper()  # 6 hex chars, e.g. "A1B2C3"
        self._store.add_pending(code, channel, sender)
        return code

    def approve(self, code: str) -> Optional[Tuple[str, str]]:
        """Redeem *code*, promoting its sender to TRUSTED.

        Returns ``(channel, sender)`` on success, or None if the code is
        unknown.
        """
        pair = self._store.pop_pending(code.strip().upper())
        if pair is None:
            return None
        channel, sender = pair
        self._store.set_level(channel, sender, TrustLevel.TRUSTED.value)
        logger.info("Paired sender %s on channel %s (code redeemed)", sender, channel)
        return channel, sender

    def pending(self) -> Dict[str, list]:
        return self._store.pending_items()


def capability_policy_for(level: TrustLevel, agent_id: str):
    """Build a :class:`~bujji.security.capabilities.CapabilityPolicy` for *level*.

    PRIMARY gets full access (None → caller uses its default policy). TRUSTED
    is denied the dangerous, side-effectful capabilities. UNTRUSTED is denied
    everything (though the gate blocks untrusted execution outright).

    Provided for callers that construct a per-sender agent; the shared-agent
    ChannelAgent path relies on the ALLOW/PAIR/BLOCK gate instead.
    """
    if level == TrustLevel.PRIMARY:
        return None
    from bujji.security.capabilities import Capability, CapabilityPolicy

    policy = CapabilityPolicy(default_deny=(level == TrustLevel.UNTRUSTED))
    if level == TrustLevel.TRUSTED:
        for cap in (
            Capability.CODE_EXECUTE,
            Capability.FILE_WRITE,
            Capability.SCHEDULE_CREATE,
            Capability.SYSTEM_ADMIN,
        ):
            policy.deny(agent_id, cap.value)
    return policy


__all__ = [
    "ChannelTrustPolicy",
    "PairingManager",
    "TrustLevel",
    "TrustStore",
    "capability_policy_for",
]
