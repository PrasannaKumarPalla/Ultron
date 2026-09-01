from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import httpx
import tkinter as tk
from tkinter import filedialog
from send2trash import send2trash
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .db import Repository
from .models import (Approval, ApprovalCreate, ApprovalDecisionRequest, ChatMessage, ChatMessageCreate, ChatSession,
    ChatSessionCreate, EventKind, FileSnapshot, ForkRunCreate, HealthReport,
    MemoryCreate, MemoryRecord, MemorySupersede, Mission, MissionControl, MissionCreate, MissionEvent,
    MissionStatus, ModelSelection, Project, ProjectCreate, AssistantListenCreate, BujjiChatCreate, RunEvent, TeamMember, WorkspaceDelete)
from .providers import MockExecutionProvider, OpenHandsExecutionProvider, ollama_health, ollama_models
from .agent_runtime import OllamaAgentStudio, WorkspaceGuard
from .chat_engine import ChatEngine
from .chat_tools import GeneralChatToolRegistry, ToolRegistry
from .bujji_bridge import get_bujji_bridge
from .assistant_desk import AssistantDesk, detect_vram_gb
from .event_bus import BudgetExhausted, EventBus, RunCancelled, SQLiteLogHandler, replay_state
from .memory_layers import LayeredMemory
from .model_pool import ModelPool
from .role_registry import RoleRegistry
from .runs import RunManager
from .search import SearchConfig
from .tools_registry import REGISTRY
from .trace import build_spans, cache_hit_estimate
from .workflow import AutonomousMissionWorkflow, DurableMissionWorkflow
from .model_router import AUTO_MODEL, route_general_chat_model, route_model
from .omniroute_runtime import get_runtime
from .omniroute_routes import router as omniroute_router


RUN_MANAGER = RunManager()
mission_tasks = RUN_MANAGER.tasks
EVENT_BUS = EventBus()
MODEL_POOL = ModelPool("http://127.0.0.1:11434")
_background_tasks: list[asyncio.Task] = []
_role_registry: RoleRegistry | None = None


def get_role_registry(settings: Settings) -> RoleRegistry:
    global _role_registry
    path = Path(settings.roles_path) if settings.roles_path else Path(__file__).parent / "roles.yaml"
    if _role_registry is None or _role_registry.path != path:
        _role_registry = RoleRegistry(path)
    return _role_registry


TERMINAL_STATUSES = {MissionStatus.COMPLETED, MissionStatus.COMPLETED_WITH_MANUAL_CHECKS,
                     MissionStatus.FAILED, MissionStatus.CANCELLED}
TERMINAL_EVENT_KINDS = {EventKind.RUN_COMPLETED.value, EventKind.RUN_FAILED.value, EventKind.RUN_CANCELLED.value}


def active_model(repo: Repository, settings: Settings) -> str:
    return repo.get_setting("active_model", settings.default_model) or settings.default_model


async def mission_model(repo: Repository, settings: Settings, project: Project, mission: Mission) -> tuple[str, str]:
    configured = active_model(repo, settings)
    if configured != AUTO_MODEL:
        return configured, "Manual model selection"
    installed = {item["name"] for item in await ollama_models(settings.ollama_url)}
    return route_model(project.name, project.description, mission.objective, installed, settings.default_model)


async def run_autonomous_mission(mission_id: str, settings: Settings, resume: bool = False,
                                 initial_state: dict | None = None) -> None:
    repo = Repository(settings.database_path)
    mission = repo.get_mission(mission_id)
    if not mission or mission.status == MissionStatus.CANCELLED:
        return
    project = repo.get_project(mission.project_id)
    if not project:
        repo.transition(mission_id, MissionStatus.FAILED, "missing_project")
        return
    model, reason = await mission_model(repo, settings, project, mission)
    repo.add_event(mission_id, "model.auto_selected" if active_model(repo, settings) == AUTO_MODEL else "model.selected",
                   "model-router", {"model": model, "reason": reason})
    studio = OllamaAgentStudio(settings.ollama_url, model, repo,
                               event_bus=EVENT_BUS, run_manager=RUN_MANAGER,
                               registry=get_role_registry(settings),
                               layered_memory=LayeredMemory(repo))
    workflow = AutonomousMissionWorkflow(repo, studio, settings.checkpoint_path, settings.max_repair_loops,
                                         event_bus=EVENT_BUS, run_manager=RUN_MANAGER,
                                         enable_critic=settings.critic_enabled,
                                         enable_debate=settings.debate_enabled,
                                         search=SearchConfig(beam_width=settings.search_beam_width,
                                                             depth=settings.search_depth))
    try:
        if initial_state is not None:
            await workflow.start_from_state(initial_state)
        elif resume:
            await workflow.resume(mission)
        else:
            await workflow.start(mission, project)
    except asyncio.CancelledError:
        raise
    except RunCancelled:
        current = repo.get_mission(mission_id)
        if current and current.status is not MissionStatus.CANCELLED:
            repo.transition(mission_id, MissionStatus.CANCELLED, "cancelled")
    except BudgetExhausted as exc:
        repo.transition(mission_id, MissionStatus.FAILED, "budget_exhausted")
        repo.add_event(mission_id, "budget.exhausted", "runtime", {"error": str(exc)})
    except Exception as exc:
        repo.transition(mission_id, MissionStatus.FAILED, "runtime_error")
        repo.add_event(mission_id, "workflow.failed", "runtime", {"error": str(exc)})
    finally:
        RUN_MANAGER.release(mission_id)


def schedule_mission(mission_id: str, settings: Settings, resume: bool = False,
                     initial_state: dict | None = None) -> None:
    existing = mission_tasks.get(mission_id)
    if existing and not existing.done():
        return
    RUN_MANAGER.register(mission_id, token_budget=settings.run_token_budget)
    mission_tasks[mission_id] = asyncio.create_task(
        run_autonomous_mission(mission_id, settings, resume, initial_state))


def repository(settings: Settings = Depends(get_settings)) -> Repository:
    return Repository(settings.database_path)


def execution_provider(settings: Settings = Depends(get_settings)):
    if settings.execution_provider == "openhands":
        return OpenHandsExecutionProvider(settings.openhands_url)
    return MockExecutionProvider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    repo = Repository(settings.database_path)
    repo.initialize()
    ultron_logger = logging.getLogger("ultron")
    if not any(isinstance(handler, SQLiteLogHandler) for handler in ultron_logger.handlers):
        ultron_logger.addHandler(SQLiteLogHandler(repo))
    if settings.execution_provider == "local":
        for mission in repo.unfinished_missions():
            if mission.status == MissionStatus.RUNNING:
                replayed = replay_state(repo.run_events(mission.id))
                breadcrumb = repo.latest_checkpoint(mission.id)
                repo.add_event(mission.id, "workflow.recovered", "runtime",
                               {"from_node": mission.current_node, "replayed_state": replayed,
                                "checkpoint_node": breadcrumb["node"] if breadcrumb else None})
                schedule_mission(mission.id, settings, resume=True)

    MODEL_POOL.ollama_url = settings.ollama_url.rstrip("/")
    MODEL_POOL.size = max(1, settings.warm_pool_size)
    _background_tasks.append(asyncio.create_task(MODEL_POOL.warm([settings.default_model])))

    async def _nightly_consolidation() -> None:
        memory = LayeredMemory(Repository(settings.database_path))
        while True:
            await asyncio.sleep(settings.consolidation_interval_s)
            try:
                memory.consolidate()
            except Exception:
                logging.getLogger(__name__).exception("memory consolidation failed")

    _background_tasks.append(asyncio.create_task(_nightly_consolidation()))

    omniroute = get_runtime(settings)
    await omniroute.start()
    from . import bujji_core_embed
    hosted_mode = omniroute.router.default_mode != "local"
    if not hosted_mode:
        await asyncio.to_thread(bujji_core_embed.start)

    async def _bujji_boot() -> None:
        """In hosted mode, wait for the sidecar and bring the Control Core up
        directly on OmniRoute — never flash the local Ollama model list first.
        Fall back to local only if the sidecar never appears."""
        if not hosted_mode:
            return
        for _ in range(50):  # ~2.5 min
            await asyncio.sleep(3)
            if await omniroute.omniroute.health():
                bujji_core_embed.configure_hosted(settings.omniroute_url,
                                                  omniroute.omniroute.api_key)
                await asyncio.to_thread(bujji_core_embed.start)
                if bujji_core_embed.current_engine() == "omniroute":
                    return
        if not bujji_core_embed.is_running():
            bujji_core_embed.configure_hosted(None, None)
            await asyncio.to_thread(bujji_core_embed.start)

    _background_tasks.append(asyncio.create_task(_bujji_boot()))
    yield
    from .bujji_core_embed import stop as stop_bujji_core
    stop_bujji_core()
    await omniroute.stop()
    for task in _background_tasks:
        task.cancel()
    _background_tasks.clear()
    for task in list(mission_tasks.values()):
        task.cancel()


app = FastAPI(
    title="Ultron 2.0 Control Plane",
    version="0.1.0",
    description="Governed local-first orchestration for autonomous engineering agents.",
    lifespan=lifespan,
    docs_url=None if os.getenv("ULTRON_DESKTOP_MODE") == "1" else "/docs",
    redoc_url=None if os.getenv("ULTRON_DESKTOP_MODE") == "1" else "/redoc",
    openapi_url=None if os.getenv("ULTRON_DESKTOP_MODE") == "1" else "/openapi.json",
)

UI_DIR = Path(__file__).parent / "ui"

app.include_router(omniroute_router)


@app.get("/", include_in_schema=False)
def operator_dashboard():
    return FileResponse(UI_DIR / "index.html")


@app.get("/api/config")
def ui_config(settings: Settings = Depends(get_settings)):
    return {
        "projects_root": str(settings.projects_root.resolve()),
        "roles": [
            {"id": "supervisor", "name": "Supervisor", "purpose": "Plans and hands off work"},
            {"id": "architect", "name": "Cloud Architect", "purpose": "Designs systems and guardrails"},
            {"id": "developer", "name": "Senior Developer", "purpose": "Implements the mission"},
            {"id": "ui", "name": "UI Expert", "purpose": "Reviews product experience"},
            {"id": "tester", "name": "Senior Tester", "purpose": "Validates and loops defects"},
        ],
    }


@app.get("/api/roles")
def list_roles(settings: Settings = Depends(get_settings)):
    return {"roles": get_role_registry(settings).describe()}


@app.get("/api/models")
async def available_models(repo: Repository = Depends(repository), settings: Settings = Depends(get_settings)):
    try:
        models = await ollama_models(settings.ollama_url)
    except Exception as exc:
        raise HTTPException(503, "Ollama model list is unavailable") from exc
    return {"active": active_model(repo, settings), "models": models,
            "auto": {"name": AUTO_MODEL, "label": "Auto — best for each project"}}


@app.put("/api/models/active")
async def select_active_model(request: ModelSelection, repo: Repository = Depends(repository),
                              settings: Settings = Depends(get_settings)):
    try:
        models = {item["name"] for item in await ollama_models(settings.ollama_url)}
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Ollama is unavailable; cannot verify model selection") from exc
    if request.model != AUTO_MODEL and request.model not in models:
        raise HTTPException(404, "That model is not installed in Ollama")
    repo.set_setting("active_model", request.model)
    return {"active": request.model, "applies_to": "newly started projects"}


@app.get("/api/providers")
async def list_providers(repo: Repository = Depends(repository),
                         settings: Settings = Depends(get_settings)):
    """Aggregate provider-selector state: pinned router mode + sidecar health."""
    try:
        from .omniroute_runtime import get_runtime
        rt = get_runtime(settings)
        router_mode = rt.router.default_mode
        healthy = await rt.sidecar.healthy()
        omniroute: dict = {"healthy": healthy, **rt.sidecar.status()}
        if healthy:
            try:
                omniroute["free_tiers"] = await rt.sidecar.free_tiers()
            except Exception:
                pass
    except Exception:
        router_mode = "local"
        omniroute = {"healthy": False}
    return {
        "router_mode": router_mode,
        "local": {"active": active_model(repo, settings)},
        "omniroute": omniroute,
    }


@app.get("/api/bujji-core")
async def bujji_core_state():
    """Live URL of the embedded Bujji Control Core (dynamic port), for the iframe."""
    from . import bujji_core_embed as embed
    return embed.status()


@app.post("/api/bujji-core/reload")
async def bujji_core_reload(settings: Settings = Depends(get_settings)):
    """Rebuild the Control Core against the current provider mode (local Ollama
    vs the OmniRoute sidecar), then hand back the fresh iframe URL."""
    from . import bujji_core_embed as embed
    rt = get_runtime(settings)
    if rt.router.default_mode != "local" and await rt.sidecar.healthy():
        embed.configure_hosted(settings.omniroute_url, rt.omniroute.api_key)
    else:
        embed.configure_hosted(None, None)
    await asyncio.to_thread(embed.reload)
    return embed.status()


@app.post("/api/folders/select")
async def select_project_folder(settings: Settings = Depends(get_settings)):
    def choose() -> str:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="Select an existing workspace folder",
            initialdir=str(settings.projects_root.resolve()),
            mustexist=True,
            parent=root,
        )
        root.destroy()
        return selected

    selected = await asyncio.to_thread(choose)
    path = Path(selected) if selected else None
    return {"path": str(path) if path else "", "name": path.name if path else ""}


@app.get("/health", response_model=HealthReport)
async def health(
    settings: Settings = Depends(get_settings), repo: Repository = Depends(repository)
) -> HealthReport:
    database = "healthy" if repo.ping() else "unhealthy"
    model = active_model(repo, settings)
    model_ok = bool(await ollama_models(settings.ollama_url)) if model == AUTO_MODEL else await ollama_health(settings.ollama_url, model)
    return HealthReport(
        status="healthy" if database == "healthy" else "degraded",
        database=database,
        ollama="healthy" if model_ok else "unavailable",
        ollama_model="Auto model routing" if model == AUTO_MODEL else model,
        execution_provider=settings.execution_provider,
    )


@app.post("/projects", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(request: ProjectCreate, repo: Repository = Depends(repository)) -> Project:
    try:
        return repo.create_project(request)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A project already uses this workspace") from exc


@app.get("/projects", response_model=list[Project])
def list_projects(repo: Repository = Depends(repository)) -> list[Project]:
    return repo.list_projects()


@app.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: str, repo: Repository = Depends(repository)) -> Project:
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


_SITE_ENTRY_NAMES = ("index.html", "index.htm")


def _find_site_entry(workspace: Path) -> str | None:
    for depth_glob in ("", "*/", "*/*/"):
        for name in _SITE_ENTRY_NAMES:
            for match in sorted(workspace.glob(f"{depth_glob}{name}")):
                rel = match.relative_to(workspace)
                if not any(part.startswith(".") for part in rel.parts):
                    return str(rel)
    return None


def _open_with_os(path: Path) -> None:
    if os.name != "nt":
        raise HTTPException(501, "Opening files is only supported on the Windows desktop app")
    os.startfile(str(path))  # noqa: S606  # loopback desktop app, path confined to workspace


@app.get("/projects/{project_id}/deliverables")
def project_deliverables(project_id: str, repo: Repository = Depends(repository)):
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    workspace = project.workspace_path
    exists = workspace.exists()
    files = sum(1 for p in workspace.rglob("*") if p.is_file()
                and not any(part.startswith(".") for part in p.relative_to(workspace).parts)) if exists else 0
    return {"workspace_path": str(workspace), "exists": exists, "file_count": files,
            "site_entry": _find_site_entry(workspace) if exists else None}


@app.post("/projects/{project_id}/reveal")
def reveal_workspace(project_id: str, repo: Repository = Depends(repository)):
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.workspace_path.exists():
        raise HTTPException(404, "Workspace folder no longer exists on disk")
    _open_with_os(project.workspace_path)
    return {"opened": str(project.workspace_path)}


@app.post("/projects/{project_id}/preview")
def preview_site(project_id: str, repo: Repository = Depends(repository)):
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    entry = _find_site_entry(project.workspace_path) if project.workspace_path.exists() else None
    if entry is None:
        raise HTTPException(404, "No index.html found in this workspace")
    target = (project.workspace_path / entry).resolve()
    if project.workspace_path.resolve() not in target.parents:
        raise HTTPException(400, "Preview target escaped the workspace")
    _open_with_os(target)
    return {"opened": str(target)}


@app.delete("/projects/{project_id}")
def delete_project(project_id: str, request: WorkspaceDelete,
                   repo: Repository = Depends(repository), settings: Settings = Depends(get_settings)):
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(404, "Workspace not found")
    if request.confirm_name.strip() != project.name:
        raise HTTPException(409, "Workspace name confirmation does not match")
    active = [mission for mission in repo.list_missions(project_id) if mission.status == MissionStatus.RUNNING]
    if active:
        raise HTTPException(409, "Stop running projects before deleting this workspace")
    workspace = project.workspace_path.resolve()
    projects_root = settings.projects_root.resolve()
    if request.delete_files and workspace.exists():
        if workspace == projects_root or projects_root not in workspace.parents:
            raise HTTPException(409, "File deletion is restricted to the configured Ultron projects folder")
    # DB first: if this fails the workspace folder is still intact and the user
    # can retry. (It used to trash the folder first, then 500 on a FK error.)
    result = repo.delete_project(project_id)
    files_recycled = False
    file_error = None
    if request.delete_files and workspace.exists():
        try:
            send2trash(str(workspace))
            files_recycled = True
        except OSError as exc:
            file_error = str(exc)
            logging.getLogger(__name__).warning("send2trash failed for %s: %s", workspace, exc)
    return {"deleted": True, "files_recycled": files_recycled, "file_error": file_error,
            "missions_deleted": result["missions_deleted"], "workspace": str(workspace)}


@app.post("/projects/{project_id}/missions", response_model=Mission, status_code=201)
def create_mission(
    project_id: str, request: MissionCreate, repo: Repository = Depends(repository)
) -> Mission:
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return repo.create_mission(project_id, request)


@app.get("/projects/{project_id}/missions", response_model=list[Mission])
def list_missions(project_id: str, repo: Repository = Depends(repository)) -> list[Mission]:
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return repo.list_missions(project_id)


@app.get("/missions/{mission_id}", response_model=Mission)
def get_mission(mission_id: str, repo: Repository = Depends(repository)) -> Mission:
    mission = repo.get_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Mission not found")
    return mission


@app.get("/missions/{mission_id}/team", response_model=list[TeamMember])
def mission_team(mission_id: str, repo: Repository = Depends(repository)) -> list[TeamMember]:
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")
    return repo.team(mission_id)


@app.post("/missions/{mission_id}/start", response_model=Mission)
async def start_mission(
    mission_id: str,
    repo: Repository = Depends(repository),
    executor=Depends(execution_provider),
    settings: Settings = Depends(get_settings),
) -> Mission:
    mission = repo.get_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Mission not found")
    if mission.status != MissionStatus.QUEUED:
        raise HTTPException(409, f"Mission cannot start from {mission.status}")
    project = repo.get_project(mission.project_id)
    if not project:
        raise HTTPException(409, "Mission project is missing")
    if settings.execution_provider == "local":
        result = repo.transition(mission_id, MissionStatus.RUNNING, "scheduled")
        repo.add_event(mission_id, "mission.scheduled", "operator", {"provider": "local", "model": active_model(repo, settings)})
        schedule_mission(mission_id, settings)
        return result
    return await DurableMissionWorkflow(
        repo,
        executor,
        settings.checkpoint_path,
    ).start(mission, project)


@app.post("/missions/{mission_id}/retry", response_model=Mission)
async def retry_mission(mission_id: str, repo: Repository = Depends(repository), settings: Settings = Depends(get_settings)) -> Mission:
    mission = repo.get_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Mission not found")
    if mission.status not in {MissionStatus.FAILED, MissionStatus.BLOCKED}:
        raise HTTPException(409, f"Mission cannot retry from {mission.status}")
    result = repo.transition(mission_id, MissionStatus.RUNNING, "retry_scheduled")
    repo.add_event(mission_id, "mission.retried", "operator", {"provider": settings.execution_provider})
    schedule_mission(mission_id, settings)
    return result


@app.get("/missions/{mission_id}/events", response_model=list[MissionEvent])
def mission_events(mission_id: str, repo: Repository = Depends(repository)) -> list[MissionEvent]:
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")
    return repo.events(mission_id)


@app.get("/missions/{mission_id}/artifacts", response_model=list[FileSnapshot])
def mission_artifacts(mission_id: str, repo: Repository = Depends(repository)) -> list[FileSnapshot]:
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")
    return [FileSnapshot(**snapshot) for snapshot in repo.file_snapshots(mission_id)]


@app.get("/missions/{mission_id}/stream")
async def mission_stream(mission_id: str, repo: Repository = Depends(repository)):
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")

    async def generate():
        last_id = 0
        while True:
            for event in repo.events_after(mission_id, last_id):
                last_id = event.id
                yield f"id: {event.id}\nevent: mission-event\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"
            mission = repo.get_mission(mission_id)
            if not mission or mission.status in {MissionStatus.COMPLETED, MissionStatus.COMPLETED_WITH_MANUAL_CHECKS, MissionStatus.FAILED, MissionStatus.CANCELLED}:
                yield f"event: mission-finished\ndata: {json.dumps({'status': str(mission.status) if mission else 'MISSING'})}\n\n"
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/missions/{mission_id}/checkpoint")
async def mission_checkpoint(mission_id: str, repo: Repository = Depends(repository), settings: Settings = Depends(get_settings)):
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")
    state = await DurableMissionWorkflow(repo, MockExecutionProvider(), settings.checkpoint_path).checkpoint_state(mission_id)
    return {"mission_id": mission_id, "state": state}


@app.post("/projects/{project_id}/chat/sessions", response_model=ChatSession, status_code=201)
def create_chat_session(project_id: str, request: ChatSessionCreate, repo: Repository = Depends(repository)) -> ChatSession:
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return repo.create_chat_session(project_id, request)


@app.post("/chat/sessions", response_model=ChatSession, status_code=201)
def create_general_chat_session(request: ChatSessionCreate, repo: Repository = Depends(repository)) -> ChatSession:
    return repo.create_chat_session(None, request)


@app.get("/chat/sessions", response_model=list[ChatSession])
def list_general_chat_sessions(archived: bool = False, repo: Repository = Depends(repository)) -> list[ChatSession]:
    return repo.list_chat_sessions(None, include_archived=archived)


@app.get("/projects/{project_id}/chat/sessions", response_model=list[ChatSession])
def list_chat_sessions(project_id: str, archived: bool = False, repo: Repository = Depends(repository)) -> list[ChatSession]:
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return repo.list_chat_sessions(project_id, include_archived=archived)


@app.get("/chat/sessions/{session_id}/messages", response_model=list[ChatMessage])
def get_chat_messages(session_id: str, repo: Repository = Depends(repository)) -> list[ChatMessage]:
    if not repo.get_chat_session(session_id):
        raise HTTPException(404, "Chat session not found")
    return repo.chat_messages(session_id)


@app.post("/chat/sessions/{session_id}/archive", response_model=ChatSession)
def archive_chat_session(session_id: str, repo: Repository = Depends(repository)) -> ChatSession:
    if not repo.get_chat_session(session_id):
        raise HTTPException(404, "Chat session not found")
    return repo.archive_chat_session(session_id)


@app.post("/chat/sessions/{session_id}/unarchive", response_model=ChatSession)
def unarchive_chat_session(session_id: str, repo: Repository = Depends(repository)) -> ChatSession:
    if not repo.get_chat_session(session_id):
        raise HTTPException(404, "Chat session not found")
    return repo.unarchive_chat_session(session_id)


@app.delete("/chat/sessions/{session_id}")
def delete_chat_session(session_id: str, repo: Repository = Depends(repository)):
    if not repo.get_chat_session(session_id):
        raise HTTPException(404, "Chat session not found")
    repo.delete_chat_session(session_id)
    return {"deleted": True}


@app.post("/chat/sessions/{session_id}/messages")
async def send_chat_message(session_id: str, request: ChatMessageCreate,
                             repo: Repository = Depends(repository), settings: Settings = Depends(get_settings)):
    session = repo.get_chat_session(session_id)
    if not session:
        raise HTTPException(404, "Chat session not found")
    project = repo.get_project(session.project_id) if session.project_id else None
    if session.project_id and not project:
        raise HTTPException(404, "Project not found")

    content = request.content.strip()
    history = []
    for message in repo.chat_messages(session_id):
        if message.role == "assistant" and message.tool_calls:
            history.append({"role": "assistant", "content": message.content,
                             "tool_calls": json.loads(message.tool_calls)})
        elif message.role == "tool":
            history.append({"role": "tool", "content": message.content, "tool_name": message.tool_name})
        else:
            history.append({"role": message.role, "content": message.content})
    repo.add_chat_message(session_id, "user", content)

    if project:
        guard = WorkspaceGuard(project.workspace_path)
        registry = ToolRegistry(guard, repo, project.id)
    else:
        registry = GeneralChatToolRegistry()
    model = active_model(repo, settings)
    if model == AUTO_MODEL:
        installed = {item["name"] for item in await ollama_models(settings.ollama_url)}
        if project:
            model, _ = route_model(project.name, project.description, content, installed, settings.default_model)
        else:
            model, _ = route_general_chat_model(installed, settings.default_model)
    engine = ChatEngine(settings.ollama_url, model, registry)

    async def generate():
        try:
            async for message in engine.turn(history, content):
                if message["role"] == "tool":
                    repo.add_chat_message(session_id, "tool", message["content"], tool_name=message.get("tool_name"))
                    yield f"event: chat-tool\ndata: {json.dumps(message)}\n\n"
                else:
                    is_intermediate = bool(message.get("tool_calls")) and not message.get("content")
                    tool_calls_json = json.dumps(message["tool_calls"]) if message.get("tool_calls") else None
                    saved = repo.add_chat_message(session_id, "assistant", message["content"], tool_calls=tool_calls_json)
                    if not is_intermediate:
                        yield f"event: chat-message\ndata: {json.dumps({'id': saved.id, 'content': saved.content})}\n\n"
        except httpx.HTTPError as exc:
            yield f"event: chat-error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            return
        yield "event: chat-done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/missions/{mission_id}/pause", response_model=Mission)
def pause_mission(mission_id: str, control: MissionControl, repo: Repository = Depends(repository)) -> Mission:
    mission = repo.get_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Mission not found")
    if mission.status not in {MissionStatus.RUNNING, MissionStatus.BLOCKED, MissionStatus.QUEUED}:
        raise HTTPException(409, f"Mission cannot pause from {mission.status}")
    result = repo.transition(mission_id, MissionStatus.BLOCKED, "paused")
    repo.add_event(mission_id, "mission.paused", "operator", {"reason": control.reason})
    return result


@app.post("/missions/{mission_id}/cancel", response_model=Mission)
def cancel_mission(mission_id: str, control: MissionControl, repo: Repository = Depends(repository)) -> Mission:
    mission = repo.get_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Mission not found")
    if mission.status in {MissionStatus.COMPLETED, MissionStatus.CANCELLED}:
        raise HTTPException(409, f"Mission cannot cancel from {mission.status}")
    result = repo.transition(mission_id, MissionStatus.CANCELLED, "cancelled")
    RUN_MANAGER.cancel(mission_id)
    repo.add_event(mission_id, "mission.cancelled", "operator", {"reason": control.reason})
    return result


@app.get("/runs", response_model=list[Mission])
def list_runs(repo: Repository = Depends(repository)) -> list[Mission]:
    return repo.list_runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str, repo: Repository = Depends(repository)):
    mission = repo.get_mission(run_id)
    if not mission:
        raise HTTPException(404, "Run not found")
    checkpoint = repo.latest_checkpoint(run_id)
    return {
        "run": mission,
        "checkpoint": checkpoint,
        "event_count": repo.event_count(run_id),
        "cancel_requested": RUN_MANAGER.is_cancelled(run_id),
    }


def _sse(event: RunEvent) -> str:
    return f"id: {event.id}\nevent: harness-event\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"


@app.get("/runs/{run_id}/events")
async def run_event_stream(run_id: str, after: int = 0, repo: Repository = Depends(repository)):
    mission = repo.get_mission(run_id)
    if not mission:
        raise HTTPException(404, "Run not found")

    async def generate():
        last_id = after
        stream = EVENT_BUS.subscribe(run_id)
        iterator = stream.__aiter__()
        try:
            for event in repo.run_events(run_id, after):
                last_id = event.id or last_id
                yield _sse(event)
            while True:
                current = repo.get_mission(run_id)
                if current and current.status in TERMINAL_STATUSES:
                    break
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=15.0)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event.id is not None and event.id <= last_id:
                    continue
                last_id = event.id or last_id
                yield _sse(event)
                if str(event.kind) in TERMINAL_EVENT_KINDS:
                    break
        finally:
            await stream.aclose()
        final = repo.get_mission(run_id)
        yield ("event: run.finished\ndata: "
               f"{json.dumps({'run_id': run_id, 'status': str(final.status) if final else 'MISSING'})}\n\n")

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/tools")
def tool_schemas():
    from . import builtin_tools  # noqa: F401  (self-registers)
    return {"tools": REGISTRY.schemas()}


@app.get("/api/models/pool")
def model_pool_status():
    return MODEL_POOL.snapshot()


@app.post("/api/memory/consolidate")
def consolidate_memory(repo: Repository = Depends(repository)):
    memory = LayeredMemory(repo)
    created = memory.consolidate()
    return {"lessons_created": len(created), "created": created}


@app.get("/projects/{project_id}/memory")
def project_memory(project_id: str, q: str = "", repo: Repository = Depends(repository)):
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    memory = LayeredMemory(repo)
    episodic = memory.recall(project_id, q, limit=8) if q.strip() else []
    return {"project_id": project_id, "query": q,
            "episodic": episodic, "lessons": memory.lessons(project_id)}


@app.get("/runs/{run_id}/trace")
def run_trace(run_id: str, repo: Repository = Depends(repository)):
    mission = repo.get_mission(run_id)
    if not mission:
        raise HTTPException(404, "Run not found")
    events = repo.run_events(run_id)
    spans = build_spans(events)
    llm_spans = [span for span in spans if span["kind"] == "llm_call"]
    total_tokens = sum(span["tokens"] for span in llm_spans)
    return {"run_id": run_id, "spans": spans, "llm_calls": len(llm_spans),
            "total_tokens": total_tokens, "cache_reuse": cache_hit_estimate(events)}


@app.post("/runs/{run_id}/cancel", response_model=Mission)
async def cancel_run(run_id: str, control: MissionControl, repo: Repository = Depends(repository)) -> Mission:
    mission = repo.get_mission(run_id)
    if not mission:
        raise HTTPException(404, "Run not found")
    if mission.status in TERMINAL_STATUSES:
        raise HTTPException(409, f"Run cannot cancel from {mission.status}")
    RUN_MANAGER.cancel(run_id)
    result = repo.transition(run_id, MissionStatus.CANCELLED, "cancelled")
    EVENT_BUS.publish(repo, run_id, EventKind.RUN_CANCELLED, "operator", {"reason": control.reason})
    return result


@app.get("/runs/{run_id}/timeline")
def run_timeline(run_id: str, repo: Repository = Depends(repository)):
    mission = repo.get_mission(run_id)
    if not mission:
        raise HTTPException(404, "Run not found")
    events = repo.event_timeline(run_id)
    return {"run_id": run_id, "count": len(events), "events": events}


@app.get("/runs/{run_id}/verify")
def run_verify(run_id: str, repo: Repository = Depends(repository)):
    if not repo.get_mission(run_id):
        raise HTTPException(404, "Run not found")
    return repo.verify_event_chain(run_id)


@app.post("/runs/{run_id}/fork", status_code=202)
async def fork_run(run_id: str, request: ForkRunCreate, settings: Settings = Depends(get_settings),
                   repo: Repository = Depends(repository)):
    """Rehydrate the run's state at an event boundary, apply edits, re-run forward as a new run."""
    mission = repo.get_mission(run_id)
    if not mission:
        raise HTTPException(404, "Run not found")
    project = repo.get_project(mission.project_id)
    if not project:
        raise HTTPException(409, "Source run's project is missing; cannot fork")

    events = repo.run_events(run_id)
    if request.event_id is not None:
        cut = next((i for i, event in enumerate(events) if event.id == request.event_id), None)
        if cut is None:
            raise HTTPException(404, f"Event {request.event_id} is not part of run {run_id}")
        prefix = events[:cut + 1]
    else:
        prefix = events

    replayed = replay_state(prefix)
    fork_title = request.title or f"{mission.title} (fork)"
    fork_mission = repo.create_mission(
        mission.project_id, MissionCreate(title=fork_title, objective=mission.objective))
    repo.add_event(fork_mission.id, "run.forked", "operator", {
        "source_run": run_id, "source_event_id": request.event_id,
        "replayed_state_keys": sorted(replayed),
        "applied_edits": sorted(request.edits or {}),
    })

    state = dict(replayed)
    state.update(request.edits or {})
    state.update({
        "mission_id": fork_mission.id,
        "project_id": project.id,
        "workspace_path": str(project.workspace_path),
        "objective": fork_mission.objective,
        "current_node": "intake",
    })
    schedule_mission(fork_mission.id, settings, initial_state=state)
    return {
        "fork_run_id": fork_mission.id,
        "source_run": run_id,
        "source_event_id": request.event_id,
        "replayed_keys": sorted(replayed),
        "edited_keys": sorted(request.edits or {}),
        "status": "QUEUED",
    }


@app.post("/missions/{mission_id}/approvals", response_model=Approval, status_code=201)
def request_approval(mission_id: str, request: ApprovalCreate, repo: Repository = Depends(repository)) -> Approval:
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")
    return repo.create_approval(mission_id, request)


@app.get("/missions/{mission_id}/approvals", response_model=list[Approval])
def list_approvals(mission_id: str, repo: Repository = Depends(repository)) -> list[Approval]:
    if not repo.get_mission(mission_id):
        raise HTTPException(404, "Mission not found")
    return repo.approvals(mission_id)


@app.post("/approvals/{approval_id}/decision", response_model=Approval)
def decide_approval(approval_id: str, request: ApprovalDecisionRequest, repo: Repository = Depends(repository)) -> Approval:
    result = repo.decide_approval(approval_id, request.decision, request.rationale)
    if not result:
        raise HTTPException(409, "Approval is missing or already decided")
    return result


@app.post("/projects/{project_id}/memories", response_model=MemoryRecord, status_code=201)
def create_memory(project_id: str, request: MemoryCreate, repo: Repository = Depends(repository)) -> MemoryRecord:
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return repo.add_memory(project_id, request)


_repo_intel_cache: dict[str, "RepoIntel"] = {}


@app.get("/projects/{project_id}/intel")
def project_intel(project_id: str, repo: Repository = Depends(repository)):
    """Symbol/import/call graphs plus churn hotspots for the project workspace."""
    project = repo.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    workspace_key = str(Path(project.workspace_path).resolve())
    intel = _repo_intel_cache.get(workspace_key)
    if intel is None:
        from .repo_intel import RepoIntel
        intel = RepoIntel(Path(project.workspace_path))
        _repo_intel_cache[workspace_key] = intel
    graph = intel.graph()
    return {"root": graph["root"], "files": graph["files"], "symbols": graph["symbols"],
            "internal_imports": graph["internal_imports"], "calls": graph["calls"],
            "hotspots": intel.hotspots()}


@app.get("/projects/{project_id}/memories", response_model=list[MemoryRecord])
def list_memories(project_id: str, include_global: bool = False, query: str | None = None,
                   repo: Repository = Depends(repository)) -> list[MemoryRecord]:
    if not repo.get_project(project_id):
        raise HTTPException(404, "Project not found")
    return repo.memories(project_id, include_global, query)


@app.post("/memories/{memory_id}/supersede", response_model=MemoryRecord)
def supersede_memory(memory_id: str, request: MemorySupersede, repo: Repository = Depends(repository)) -> MemoryRecord:
    try:
        return repo.supersede_memory(memory_id, request.content, request.role)
    except KeyError:
        raise HTTPException(404, "Memory not found")


def bujji_bridge(settings: Settings = Depends(get_settings)):
    return get_bujji_bridge(settings)


@app.get("/api/bujji/status")
async def bujji_status(bridge=Depends(bujji_bridge)) -> dict:
    return await bridge.status()


def assistant_desk(settings: Settings = Depends(get_settings)) -> AssistantDesk:
    return AssistantDesk(settings)


@app.get("/api/assistant/desk")
async def assistant_desk_info(desk: AssistantDesk = Depends(assistant_desk),
                              settings: Settings = Depends(get_settings)) -> dict:
    registry = get_role_registry(settings)
    role = registry.get("assistant")
    return {
        "role": {"id": "assistant", "name": role.name if role else "Assistant",
                 "desk_position": role.desk_position if role else None},
        "wake_word": settings.assistant_wake_word,
        "sdk": await desk.bridge_status(),
    }


@app.post("/api/assistant/listen")
async def assistant_listen(request: AssistantListenCreate,
                           desk: AssistantDesk = Depends(assistant_desk)) -> dict:
    transcript = request.transcript.strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="transcript must not be empty")
    return await desk.handle_transcript(transcript)


@app.post("/api/assistant/model")
async def assistant_pick_model(desk: AssistantDesk = Depends(assistant_desk)) -> dict:
    installed = await desk.installed_models()
    model, reason = desk.pick_model(installed)
    return {"model": model, "reason": reason, "installed": sorted(installed), "vram_gb": detect_vram_gb()}


@app.post("/api/bujji/chat")
async def bujji_chat(request: BujjiChatCreate, bridge=Depends(bujji_bridge)):
    query = request.query.strip()
    if not query:
        raise HTTPException(400, "Query must not be empty")

    async def generate():
        try:
            parts = []
            async for token in bridge.stream(query, model=request.model):
                if not isinstance(token, str):
                    continue
                parts.append(token)
                yield f"event: bujji-token\ndata: {json.dumps({'token': token})}\n\n"
            yield f"event: bujji-done\ndata: {json.dumps({'content': ''.join(parts)})}\n\n"
        except Exception as exc:
            yield f"event: bujji-error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/assets", StaticFiles(directory=UI_DIR), name="ui-assets")
