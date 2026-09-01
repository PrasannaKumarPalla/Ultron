"""Consented execution of a single prerequisite action, as an event stream.

Layer 3 of the prerequisite installer (decision 0004). ``run_install`` yields
plain dicts describing progress; the API layer turns them into SSE frames and
the UI renders them. Nothing here runs without an explicit action from the
caller, and the only actions accepted are the ones a `PrereqReport` produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from . import bootstrap, preflight

logger = logging.getLogger(__name__)

_ALLOWED_MODEL_TAGS = frozenset(tag for tag, _gb, _floor in preflight._RECOMMENDABLE)


class InvalidAction(ValueError):
    """The requested action is not one the preflight report offers."""


def _parse(action: str) -> tuple[str, str | None]:
    if action == "install_ollama":
        return "install_ollama", None
    if action.startswith("pull_model:"):
        tag = action.split(":", 1)[1]
        if tag not in _ALLOWED_MODEL_TAGS:
            raise InvalidAction(f"model {tag!r} is not an offered pull target")
        return "pull_model", tag
    raise InvalidAction(f"unknown action: {action!r}")


async def _install_ollama(downloads_dir: Path) -> AsyncIterator[dict]:
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _on_progress(p: object) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"phase": "download",
             "downloaded": getattr(p, "downloaded", None),
             "total": getattr(p, "total", None)},
        )

    def _work() -> Path:
        return bootstrap.download_ollama_installer(downloads_dir, on_progress=_on_progress)

    yield {"phase": "download", "downloaded": 0, "total": None}
    task = loop.run_in_executor(None, _work)
    while not task.done():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=0.5)
            if item is not None:
                yield item
        except asyncio.TimeoutError:
            pass
    installer = await task  # re-raises bootstrap errors

    yield {"phase": "install"}
    await asyncio.to_thread(bootstrap.install_ollama_silently, installer)
    await asyncio.to_thread(bootstrap.wait_for_ollama_ready)
    yield {"phase": "done"}


async def _pull_model(tag: str, base_url: str) -> AsyncIterator[dict]:
    url = f"{base_url.rstrip('/')}/api/pull"
    yield {"phase": "pull", "status": f"pulling {tag}", "completed": None, "total": None}
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, json={"name": tag}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                yield {
                    "phase": "pull",
                    "status": msg.get("status"),
                    "completed": msg.get("completed"),
                    "total": msg.get("total"),
                }
    yield {"phase": "done"}


async def run_install(
    action: str, *, base_url: str, downloads_dir: str | Path
) -> AsyncIterator[dict]:
    """Yield progress dicts for ``action``. Raises :class:`InvalidAction` up front."""
    kind, tag = _parse(action)
    downloads_dir = Path(downloads_dir)
    if kind == "install_ollama":
        async for event in _install_ollama(downloads_dir):
            yield event
    else:
        assert tag is not None
        async for event in _pull_model(tag, base_url):
            yield event


__all__ = ["run_install", "InvalidAction"]
