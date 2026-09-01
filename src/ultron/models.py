from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class Classification(StrEnum):
    PERSONAL = "PERSONAL"
    CLIENT_CONFIDENTIAL = "CLIENT_CONFIDENTIAL"
    INTERNAL = "INTERNAL"
    PUBLIC_OPEN_SOURCE = "PUBLIC_OPEN_SOURCE"
    HIGH_RISK = "HIGH_RISK"


class MissionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_MANUAL_CHECKS = "COMPLETED_WITH_MANUAL_CHECKS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    workspace_path: Path
    classification: Classification = Classification.PERSONAL


class Project(ProjectCreate):
    id: str
    created_at: datetime


class MissionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    objective: str = Field(min_length=10, max_length=20_000)


class Mission(BaseModel):
    id: str
    project_id: str
    title: str
    objective: str
    status: MissionStatus
    current_node: str
    graph_version: str
    created_at: datetime
    updated_at: datetime


class MissionEvent(BaseModel):
    id: int
    mission_id: str
    kind: str
    actor: str
    payload: dict
    created_at: datetime


class FileSnapshot(BaseModel):
    id: str
    mission_id: str
    event_id: int | None = None
    path: str
    before_content: str
    after_content: str
    created_at: datetime


class HealthReport(BaseModel):
    status: str
    database: str
    ollama: str
    ollama_model: str
    execution_provider: str


class ModelSelection(BaseModel):
    model: str = Field(min_length=2, max_length=200)


class MissionControl(BaseModel):
    reason: str = Field(default="Operator control", min_length=2, max_length=1000)


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalCreate(BaseModel):
    action: str = Field(min_length=3, max_length=200)
    risk: str = Field(min_length=3, max_length=2000)


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    rationale: str = Field(min_length=3, max_length=2000)


class Approval(BaseModel):
    id: str
    mission_id: str
    action: str
    risk: str
    decision: ApprovalDecision
    rationale: str
    created_at: datetime
    decided_at: datetime | None = None


class MemoryCreate(BaseModel):
    scope: str = Field(pattern="^(project|personal|global)$")
    role: str = Field(default="supervisor", min_length=2, max_length=80)
    content: str = Field(min_length=3, max_length=50_000)
    provenance: str = Field(min_length=2, max_length=2000)
    confidence: float = Field(default=1.0, ge=0, le=1)
    sensitivity: Classification = Classification.PERSONAL
    expires_at: datetime | None = None


class MemoryRecord(MemoryCreate):
    id: str
    project_id: str | None
    status: str
    created_at: datetime
    superseded_by: str | None = None


class MemorySupersede(BaseModel):
    content: str = Field(min_length=3, max_length=50_000)
    role: str = Field(default="supervisor", min_length=2, max_length=80)


class TeamMember(BaseModel):
    id: str
    mission_id: str
    role_id: str
    name: str
    purpose: str
    skills: list[str]
    permissions: list[str]
    sequence: int
    status: str = "PLANNED"
    created_at: datetime


class WorkspaceDelete(BaseModel):
    confirm_name: str = Field(min_length=2, max_length=120)
    delete_files: bool = False


class ChatSessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ChatSession(BaseModel):
    id: str
    project_id: str | None = None
    title: str
    created_at: datetime
    archived_at: datetime | None = None


class ChatMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class BujjiChatCreate(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    model: str | None = None


class AssistantListenCreate(BaseModel):
    transcript: str = Field(min_length=1, max_length=4_000)


class ChatMessage(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    tool_name: str | None = None
    tool_calls: str | None = None
    created_at: datetime


class EventKind(StrEnum):
    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_ERROR = "node.error"
    STATUS_CHANGED = "status.changed"
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOKEN = "token"
    ARTIFACT_WRITTEN = "artifact.written"
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXHAUSTED = "budget.exhausted"
    LOG = "log"


class RunEvent(BaseModel):
    id: int | None = None
    run_id: str
    agent: str
    kind: str
    payload: dict = Field(default_factory=dict)
    ts: datetime
    hash: str | None = None
    parent_hash: str | None = None
    blob_ref: str | None = None


class ForkRunCreate(BaseModel):
    event_id: int | None = Field(default=None, description="Rewind point; omit to fork from the run's final state")
    title: str | None = Field(default=None, min_length=2, max_length=200)
    edits: dict = Field(default_factory=dict, description="State overrides applied on top of the replayed past")
