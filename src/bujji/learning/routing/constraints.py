"""First-class cost / latency / energy constraints for model routing.

B.U.J.J.I already *emits* rich per-call telemetry (cost, latency, energy) via
``telemetry.instrumented_engine`` and aggregates it in
``telemetry.aggregator``. Historically that data was only ever *observed* — it
never *gated* a routing decision. This module closes that gap: it turns
configured ceilings into a filter that drops candidate models whose historical
stats exceed a ceiling *before* the router picks one, so an expensive/slow model
is skipped in favour of a cheaper local fallback.

The checker is pure logic over :class:`~bujji.telemetry.aggregator.ModelStats`
so it is trivially unit-testable; runtime wiring supplies the stats via
:func:`stats_by_model_from_aggregator`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from bujji.telemetry.aggregator import ModelStats, TelemetryAggregator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RoutingConstraints:
    """Optional ceilings a candidate model must satisfy to be routable.

    Any field left ``None`` is not enforced. All ceilings are compared against
    a model's *historical* averages, so a model with no telemetry yet is always
    allowed (there is no evidence to reject it on).
    """

    #: Max average cost (USD) per call.
    max_cost_usd: Optional[float] = None
    #: Max average end-to-end latency (seconds) per call.
    max_latency_s: Optional[float] = None
    #: Max average energy per output token (joules).
    max_energy_per_output_token_joules: Optional[float] = None

    @property
    def enforced(self) -> bool:
        """True if at least one ceiling is set."""
        return any(
            v is not None
            for v in (
                self.max_cost_usd,
                self.max_latency_s,
                self.max_energy_per_output_token_joules,
            )
        )

    @classmethod
    def from_config(cls, routing_cfg: object) -> "RoutingConstraints":
        """Build from a ``config.learning.routing`` object (duck-typed)."""
        return cls(
            max_cost_usd=getattr(routing_cfg, "max_cost_usd", None),
            max_latency_s=getattr(routing_cfg, "max_latency_s", None),
            max_energy_per_output_token_joules=getattr(
                routing_cfg, "max_energy_per_output_token_joules", None
            ),
        )


@dataclass(slots=True)
class ConstraintDecision:
    """Outcome of checking one model against the constraints."""

    model_id: str
    allowed: bool
    reason: str = ""


def _avg_cost_per_call(stats: "ModelStats") -> Optional[float]:
    if stats.call_count <= 0:
        return None
    return stats.total_cost / stats.call_count


def _energy_per_output_token(stats: "ModelStats") -> Optional[float]:
    # Prefer the pre-derived average; fall back to totals when the DB predates
    # the derived column (it defaults to 0.0 there).
    if stats.avg_energy_per_output_token_joules > 0:
        return stats.avg_energy_per_output_token_joules
    if stats.completion_tokens > 0 and stats.total_energy_joules > 0:
        return stats.total_energy_joules / stats.completion_tokens
    return None


class ConstraintChecker:
    """Filters candidate models against :class:`RoutingConstraints`.

    Models without historical stats are allowed (fail-open) — the point is to
    steer away from options we have *evidence* are too expensive/slow, not to
    block never-seen models.
    """

    def __init__(
        self,
        constraints: RoutingConstraints,
        stats_by_model: Dict[str, "ModelStats"],
        *,
        bus: object = None,
    ) -> None:
        self._constraints = constraints
        self._stats = stats_by_model
        self._bus = bus

    def check(self, model_id: str) -> ConstraintDecision:
        c = self._constraints
        if not c.enforced:
            return ConstraintDecision(model_id, True)
        stats = self._stats.get(model_id)
        if stats is None:
            return ConstraintDecision(model_id, True, "no telemetry")

        if c.max_cost_usd is not None:
            avg_cost = _avg_cost_per_call(stats)
            if avg_cost is not None and avg_cost > c.max_cost_usd:
                return ConstraintDecision(
                    model_id,
                    False,
                    f"avg cost ${avg_cost:.4f} > ${c.max_cost_usd:.4f}",
                )

        if c.max_latency_s is not None and stats.avg_latency > c.max_latency_s:
            return ConstraintDecision(
                model_id,
                False,
                f"avg latency {stats.avg_latency:.2f}s > {c.max_latency_s:.2f}s",
            )

        if c.max_energy_per_output_token_joules is not None:
            ept = _energy_per_output_token(stats)
            if ept is not None and ept > c.max_energy_per_output_token_joules:
                return ConstraintDecision(
                    model_id,
                    False,
                    f"energy {ept:.3f} J/tok > "
                    f"{c.max_energy_per_output_token_joules:.3f} J/tok",
                )

        return ConstraintDecision(model_id, True)

    def filter(self, candidates: List[str]) -> List[str]:
        """Return the subset of *candidates* that satisfy the constraints.

        Each dropped candidate is logged and, when a bus is available, emitted
        on the event bus so hybrid experiments can record that a constraint
        changed the route.
        """
        if not self._constraints.enforced:
            return list(candidates)
        allowed: List[str] = []
        for model_id in candidates:
            decision = self.check(model_id)
            if decision.allowed:
                allowed.append(model_id)
            else:
                self._emit_drop(decision)
        return allowed

    def _emit_drop(self, decision: ConstraintDecision) -> None:
        logger.info(
            "Routing constraint dropped model %s: %s",
            decision.model_id,
            decision.reason,
        )
        if self._bus is None:
            return
        try:
            from bujji.core.events import EventType

            self._bus.publish(
                EventType.TELEMETRY_RECORD,
                {
                    "kind": "routing_constraint_drop",
                    "model_id": decision.model_id,
                    "reason": decision.reason,
                },
            )
        except Exception as exc:  # never let telemetry break routing
            logger.debug("Failed to emit routing_constraint_drop: %s", exc)


def stats_by_model_from_aggregator(
    aggregator: "TelemetryAggregator", *, since: Optional[float] = None
) -> Dict[str, "ModelStats"]:
    """Build a ``{model_id: ModelStats}`` map from a telemetry aggregator."""
    return {ms.model_id: ms for ms in aggregator.per_model_stats(since=since)}


__all__ = [
    "ConstraintChecker",
    "ConstraintDecision",
    "RoutingConstraints",
    "stats_by_model_from_aggregator",
]
