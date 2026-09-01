"""Bundle dataclasses that group cohesive subsystems of BujjiSystem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bujji.agents._stubs import BaseAgent
    from bujji.agents.executor import AgentExecutor
    from bujji.agents.manager import AgentManager
    from bujji.agents.scheduler import AgentScheduler
    from bujji.scheduler.scheduler import TaskScheduler
    from bujji.scheduler.store import SchedulerStore
    from bujji.security.audit import AuditLogger
    from bujji.security.boundary import BoundaryGuard
    from bujji.security.capabilities import CapabilityPolicy
    from bujji.telemetry.gpu_monitor import GpuMonitor
    from bujji.telemetry.store import TelemetryStore
    from bujji.traces.collector import TraceCollector
    from bujji.traces.store import TraceStore


@dataclass
class SecurityContext:
    """Security policy, audit, and boundary enforcement."""

    capability_policy: Optional[CapabilityPolicy] = None
    audit_logger: Optional[AuditLogger] = None
    boundary_guard: Optional[BoundaryGuard] = None


@dataclass
class Observability:
    """Telemetry, traces, and hardware monitoring."""

    telemetry_store: Optional[TelemetryStore] = None
    trace_store: Optional[TraceStore] = None
    trace_collector: Optional[TraceCollector] = None
    gpu_monitor: Optional[GpuMonitor] = None


@dataclass
class AgentRuntime:
    """Active agent and agent lifecycle managers."""

    agent: Optional[BaseAgent] = None
    agent_name: str = ""
    manager: Optional[AgentManager] = None
    scheduler: Optional[AgentScheduler] = None
    executor: Optional[AgentExecutor] = None


@dataclass
class Scheduling:
    """Task scheduler and its persistent store."""

    store: Optional[SchedulerStore] = None
    runner: Optional[TaskScheduler] = None
