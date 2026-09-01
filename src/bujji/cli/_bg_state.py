"""Read background-work state from ``~/.bujji/.state/``.

Pure-function reader used by the chat banner, completion-notification
dispatcher, and ``bujji doctor``.  No side effects â€” safe to call
between every chat turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, unquote

from bujji.core import config


@dataclass(slots=True)
class BgStatus:
    """Snapshot of background-work state."""

    rust_extension: str = "pending"  # pending | ready | failed
    rust_error: str = ""
    models: Dict[str, str] = field(
        default_factory=dict
    )  # id -> pending|downloading|ready|failed

    def all_ready(self) -> bool:
        if self.rust_extension != "ready":
            return False
        if any(s != "ready" for s in self.models.values()):
            return False
        return True


def _safe_read(path: Path) -> Optional[str]:
    """Read a file, returning None if it disappears mid-read (race)."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def model_marker_path(models_dir: Path, model_id: str, state: str) -> Path:
    """Return a cross-platform marker path for a model download state.

    Model IDs routinely contain ``:`` and may contain ``/``. Both are unsafe
    in Windows filenames, so marker writers must use this helper rather than
    interpolating the raw model ID into a path.
    """
    if state not in {"downloading", "ready", "failed"}:
        raise ValueError(f"unsupported model state: {state}")
    encoded_model = quote(model_id, safe="")
    return models_dir / f"{encoded_model}.{state}"


def get_status(home: Optional[Path] = None) -> BgStatus:
    """Snapshot the background-work state from the state directory."""
    home = home or config.DEFAULT_CONFIG_DIR
    state_dir = home / ".state"
    models_dir = state_dir / "models"

    status = BgStatus()

    # Rust extension: ready supersedes failed; failed supersedes pending.
    if (state_dir / "extension-built").exists():
        status.rust_extension = "ready"
    elif (state_dir / "extension-failed").exists():
        contents = _safe_read(state_dir / "extension-failed")
        if contents is not None:
            status.rust_extension = "failed"
            status.rust_error = contents

    # Models: parse files in models_dir; .ready supersedes .downloading and .failed.
    if models_dir.is_dir():
        # First pass: capture every model id we see.
        seen: Dict[str, str] = {}
        for f in models_dir.iterdir():
            if f.suffix not in (".downloading", ".ready", ".failed"):
                continue
            model_id = unquote(f.name[: -len(f.suffix)])
            new_state = f.suffix.lstrip(".")
            current = seen.get(model_id, "")
            # Precedence: ready > failed > downloading
            if current == "ready":
                continue
            if new_state == "ready":
                seen[model_id] = "ready"
            elif new_state == "failed" and current != "ready":
                seen[model_id] = "failed"
            elif new_state == "downloading" and current == "":
                seen[model_id] = "downloading"
        status.models = seen

    return status
