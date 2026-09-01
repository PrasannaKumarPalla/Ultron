"""Chat startup banner that renders background status in one line."""

from __future__ import annotations

from bujji.cli._bg_state import BgStatus


def render_startup_banner(status: BgStatus) -> str:
    """Return a one-line banner string, or empty if background work is done."""
    if status.all_ready():
        return ""

    parts: list[str] = []

    if status.rust_extension == "pending":
        parts.append("Rust extension building")
    elif status.rust_extension == "failed":
        parts.append("Rust extension failed (run `assistant doctor`)")

    for model_id, state in status.models.items():
        if state == "downloading":
            parts.append(f"{model_id} downloading")
        elif state == "failed":
            parts.append(f"{model_id} failed (run `assistant doctor`)")

    if not parts:
        return ""

    return "Background: " + " | ".join(parts) + ". Type to start."
