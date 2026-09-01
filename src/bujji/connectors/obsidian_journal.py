"""Automatic conversation journaling into the Obsidian vault.

Every exchange (voice or chat) is appended to a daily note:
    <vault>/<notes_folder>/Journal/YYYY-MM-DD.md

Fire-and-forget: journaling must never break a conversation turn, so all
errors are swallowed after a debug log. Disabled automatically when the
Obsidian connector is not configured.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached_cfg: Optional[tuple] = None


def _vault_config() -> Optional[tuple[Path, str]]:
    """Return (vault_path, notes_folder) from config, or None if unset."""
    global _cached_cfg
    if _cached_cfg is not None:
        return _cached_cfg if _cached_cfg != () else None
    try:
        from bujji.core.config import load_config

        cfg = load_config()
        ob = getattr(getattr(cfg, "connectors", None), "obsidian", None)
        if ob is None or not getattr(ob, "enabled", False):
            _cached_cfg = ()
            return None
        vault = Path(str(getattr(ob, "vault_path", "")).strip())
        folder = str(getattr(ob, "notes_folder", "Bujji")).strip() or "Bujji"
        if not vault.is_dir():
            _cached_cfg = ()
            return None
        _cached_cfg = (vault, folder)
        return _cached_cfg
    except Exception:
        logger.debug("Obsidian journal: config load failed", exc_info=True)
        _cached_cfg = ()
        return None


def log_exchange(user_text: str, response: str, source: str = "chat") -> None:
    """Append one user/assistant exchange to today's journal note."""
    if not user_text.strip() and not response.strip():
        return
    cfg = _vault_config()
    if cfg is None:
        return
    vault, folder = cfg
    try:
        now = datetime.now()
        note_dir = vault / folder / "Journal"
        note_dir.mkdir(parents=True, exist_ok=True)
        fpath = note_dir / f"{now:%Y-%m-%d}.md"

        entry = (
            f"\n## {now:%H:%M} · {source}\n\n"
            f"**Prasanna:** {user_text.strip()}\n\n"
            f"**Bujji:** {response.strip()}\n"
        )
        with _lock:
            if not fpath.exists():
                header = (
                    f"---\ntitle: Bujji Journal {now:%Y-%m-%d}\n"
                    f"tags: [bujji, journal]\n---\n"
                )
                fpath.write_text(header + entry, encoding="utf-8")
            else:
                with fpath.open("a", encoding="utf-8") as f:
                    f.write(entry)
    except Exception:
        logger.debug("Obsidian journal write failed", exc_info=True)


__all__ = ["log_exchange"]
