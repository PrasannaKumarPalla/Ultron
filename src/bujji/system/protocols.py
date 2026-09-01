"""Structural protocols for substituting fakes in place of BujjiSystem."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Protocol

if TYPE_CHECKING:
    from bujji.core.config import BujjiConfig
    from bujji.core.events import EventBus
    from bujji.engine._stubs import InferenceEngine
    from bujji.security.capabilities import CapabilityPolicy
    from bujji.sessions.session import SessionStore
    from bujji.tools._stubs import BaseTool
    from bujji.tools.storage._stubs import MemoryBackend
    from bujji.traces.collector import TraceCollector
    from bujji.traces.store import TraceStore


class OrchestratorDeps(Protocol):
    """Minimum surface of BujjiSystem that QueryOrchestrator depends on.

    Tests can satisfy this with a lightweight class â€” no need to construct
    the full BujjiSystem dataclass or materialize every subsystem.
    """

    config: BujjiConfig
    bus: EventBus
    engine: InferenceEngine
    engine_key: str
    model: str
    agent_name: str
    tools: List[BaseTool]
    memory_backend: Optional[MemoryBackend]
    capability_policy: Optional[CapabilityPolicy]
    session_store: Optional[SessionStore]
    trace_store: Optional[TraceStore]
    trace_collector: Optional[TraceCollector]  # written by _run_agent

    # Optional attribute (getattr with default) â€” declared for type clarity.
    _skill_few_shot_examples: Any
