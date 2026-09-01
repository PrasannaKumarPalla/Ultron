"""Learning primitive -- router policies and reward functions.

The research scaffolding (spec_search, training, optimize, intelligence)
was removed; what remains is the model-routing feature and the skill
discovery/evolution agents.
"""

from __future__ import annotations

from bujji.learning._stubs import (
    QueryAnalyzer,
    RewardFunction,
    RouterPolicy,
    RoutingContext,
)
from bujji.learning.agents.agent_evolver import AgentConfigEvolver
from bujji.learning.routing.complexity import (
    ComplexityQueryAnalyzer,
    score_complexity,
)
from bujji.learning.routing.heuristic_reward import HeuristicRewardFunction
from bujji.learning.routing.router import (
    HeuristicRouter,
    build_routing_context,
)


def ensure_registered() -> None:
    """Ensure all learning policies are registered in RouterPolicyRegistry."""
    from bujji.learning.routing.heuristic_policy import (
        ensure_registered as _reg_heuristic,
    )

    _reg_heuristic()

    from bujji.learning.routing.learned_router import (
        ensure_registered as _reg_learned,
    )

    _reg_learned()

    # Agent optimizers (optional deps)
    try:
        import bujji.learning.agents.dspy_optimizer  # noqa: F401
    except ImportError:
        pass
    try:
        import bujji.learning.agents.gepa_optimizer  # noqa: F401
    except ImportError:
        pass
    try:
        import bujji.learning.agents.ace_optimizer  # noqa: F401
    except ImportError:
        pass


__all__ = [
    "AgentConfigEvolver",
    "ComplexityQueryAnalyzer",
    "HeuristicRewardFunction",
    "HeuristicRouter",
    "QueryAnalyzer",
    "RewardFunction",
    "RouterPolicy",
    "RoutingContext",
    "build_routing_context",
    "ensure_registered",
    "score_complexity",
]
