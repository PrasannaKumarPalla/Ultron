"""Local assistant engine with composable intelligence primitives."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

def _resolve_version() -> str:
    for pkg in ("assistant-core", "ultron-control-plane"):
        try:
            return _pkg_version(pkg)
        except PackageNotFoundError:
            continue
    return "0.0.0+embedded"


__version__ = _resolve_version()

__all__ = ["Bujji", "BujjiSystem", "MemoryHandle", "SystemBuilder", "__version__"]


def __getattr__(name: str):
    """Load SDK exports only when a library consumer asks for them.

    CLI and frozen installer commands import this package before dispatching a
    subcommand. Eager SDK imports initialized engines and telemetry for even
    `--help` and `init`, causing slow startup and lingering worker threads.
    """
    if name in {"Bujji", "BujjiSystem", "MemoryHandle", "SystemBuilder"}:
        from bujji.sdk import Bujji, BujjiSystem, MemoryHandle, SystemBuilder

        exports = {
            "Bujji": Bujji,
            "BujjiSystem": BujjiSystem,
            "MemoryHandle": MemoryHandle,
            "SystemBuilder": SystemBuilder,
        }
        return exports[name]
    raise AttributeError(f"module 'bujji' has no attribute {name!r}")
