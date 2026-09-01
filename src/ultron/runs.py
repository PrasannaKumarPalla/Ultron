from __future__ import annotations

import asyncio

from .event_bus import RunCancelled


class RunManager:
    """One kill switch and one token budget per run, checked at node boundaries and inside model streams."""

    WARN_FRACTION = 0.8

    def __init__(self):
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._budgets: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def register(self, run_id: str, token_budget: int | None = None) -> asyncio.Event:
        event = asyncio.Event()
        self._cancel_events[run_id] = event
        self._budgets[run_id] = {"limit": int(token_budget or 0), "used": 0}
        return event

    def record_tokens(self, run_id: str, count: int) -> str:
        """Count streamed tokens. Returns "warn" crossing 80%%; raises BudgetExhausted at 100%."""
        from .event_bus import BudgetExhausted

        budget = self._budgets.get(run_id)
        if not budget:
            return ""
        budget["used"] += count
        limit = budget["limit"]
        if not limit:
            return ""
        if budget["used"] >= limit:
            raise BudgetExhausted(
                f"run {run_id} exhausted its token budget ({budget['used']}/{limit})"
            )
        if budget["used"] / limit >= self.WARN_FRACTION:
            return "warn"
        return ""

    def budget_state(self, run_id: str) -> dict | None:
        budget = self._budgets.get(run_id)
        if not budget:
            return None
        return {"limit": budget["limit"], "used": budget["used"]}

    def cancel(self, run_id: str) -> bool:
        killed = False
        event = self._cancel_events.get(run_id)
        if event is None:
            event = self.register(run_id)
        event.set()
        killed = True
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            killed = True
        return killed

    def is_cancelled(self, run_id: str) -> bool:
        event = self._cancel_events.get(run_id)
        return bool(event and event.is_set())

    def check(self, run_id: str) -> None:
        if self.is_cancelled(run_id):
            raise RunCancelled(f"run {run_id} cancelled by operator")

    def release(self, run_id: str) -> None:
        self._cancel_events.pop(run_id, None)
        self._budgets.pop(run_id, None)
        self.tasks.pop(run_id, None)