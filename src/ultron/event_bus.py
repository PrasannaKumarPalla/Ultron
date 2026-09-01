from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from .models import EventKind, RunEvent

if TYPE_CHECKING:
    from .db import Repository


class RunCancelled(Exception):
    """Raised at a node boundary or inside the model stream when the kill switch is thrown."""


class BudgetExhausted(Exception):
    """Raised inside the model stream once a run's token budget is spent."""


class EventBus:
    """Single funnel: every typed event lands in SQLite and fans out to live subscribers."""

    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def publish(self, repository: Repository, run_id: str, kind: str | EventKind,
                agent: str, payload: dict) -> RunEvent:
        kind = str(kind)
        event = repository.append_run_event(run_id, kind, agent, payload)
        if kind != EventKind.TOKEN.value:
            repository.add_event(run_id, kind, agent, payload)
        for queue in self._subscribers.get(run_id, set()):
            queue.put_nowait(event)
        return event

    def subscribe(self, run_id: str) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(queue)

        async def _stream() -> AsyncIterator[RunEvent]:
            try:
                while True:
                    yield await queue.get()
            finally:
                self._subscribers.get(run_id, set()).discard(queue)

        return _stream()

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(run_id, set()))


def replay_state(events) -> dict:
    """Fold an event history back into the volatile parts of a run's state."""
    state: dict = {}
    for event in events:
        kind = str(event.kind)
        if kind == EventKind.NODE_COMPLETED.value:
            node = event.payload.get("node")
            if node:
                state["current_node"] = node
        elif kind == "agent.completed" and event.agent == "developer":
            state["iteration"] = state.get("iteration", 0) + 1
        elif kind == "tests.completed":
            state["test_passed"] = bool(event.payload.get("passed"))
            state["manual_checks"] = bool(event.payload.get("manual_checks", False))
        elif kind == "security.scanned":
            state["security_passed"] = bool(event.payload.get("passed"))
    return state


class SQLiteLogHandler(logging.Handler):
    """Structured logs go to the events table, never to files."""

    def __init__(self, repository: Repository):
        super().__init__()
        self.repository = repository

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.repository.append_run_event("app", EventKind.LOG.value, record.name,
                                             {"level": record.levelname, "message": record.getMessage()})
        except Exception:
            self.handleError(record)