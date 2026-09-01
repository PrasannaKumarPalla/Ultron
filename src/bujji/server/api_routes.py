"""Extended API routes for agents, workflows, memory, traces, etc."""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from bujji.core.config import load_config

logger = logging.getLogger(__name__)

# ---- Request/Response models ----


class AgentCreateRequest(BaseModel):
    agent_type: str
    tools: Optional[List[str]] = None
    agent_id: Optional[str] = None


class AgentMessageRequest(BaseModel):
    message: str


class MemoryStoreRequest(BaseModel):
    content: str
    metadata: Optional[Dict[str, Any]] = None


class MemorySearchRequest(BaseModel):
    query: str
    top_k: int = 5


class MemoryIndexRequest(BaseModel):
    path: str


class BudgetLimitsRequest(BaseModel):
    max_tokens_per_day: Optional[int] = None
    max_requests_per_hour: Optional[int] = None


class FeedbackScoreRequest(BaseModel):
    trace_id: str
    score: float
    source: str = "api"


# ---- Agent routes ----

agents_router = APIRouter(prefix="/v1/agents", tags=["agents"])


@agents_router.get("")
async def list_agents(request: Request):
    """List available agent types and running agents."""
    registered = []
    try:
        import bujji.agents  # noqa: F401 — side-effect registration
        from bujji.core.registry import AgentRegistry

        for key in sorted(AgentRegistry.keys()):
            cls = AgentRegistry.get(key)
            registered.append(
                {
                    "key": key,
                    "class": cls.__name__,
                    "accepts_tools": getattr(cls, "accepts_tools", False),
                }
            )
    except Exception as exc:
        logger.warning("Failed to list registered agents: %s", exc)

    running = []
    try:
        from bujji.tools.agent_tools import _SPAWNED_AGENTS

        running = [{"id": k, **v} for k, v in _SPAWNED_AGENTS.items()]
    except ImportError:
        pass

    return {"registered": registered, "running": running}


@agents_router.post("")
async def create_agent(req: AgentCreateRequest, request: Request):
    """Spawn a new agent."""
    try:
        from bujji.tools.agent_tools import AgentSpawnTool

        tool = AgentSpawnTool()
        params = {"agent_type": req.agent_type}
        if req.tools:
            params["tools"] = ",".join(req.tools)
        if req.agent_id:
            params["agent_id"] = req.agent_id
        result = tool.execute(**params)
        if not result.success:
            raise HTTPException(status_code=400, detail=result.content)
        return {
            "status": "created",
            "content": result.content,
            "metadata": result.metadata,
        }
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")


@agents_router.delete("/{agent_id}")
async def kill_agent(agent_id: str, request: Request):
    """Kill a running agent."""
    try:
        from bujji.tools.agent_tools import AgentKillTool

        tool = AgentKillTool()
        result = tool.execute(agent_id=agent_id)
        if not result.success:
            raise HTTPException(status_code=404, detail=result.content)
        return {"status": "stopped", "agent_id": agent_id}
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")


@agents_router.post("/{agent_id}/message")
async def message_agent(agent_id: str, req: AgentMessageRequest, request: Request):
    """Send a message to a running agent."""
    try:
        from bujji.tools.agent_tools import AgentSendTool

        tool = AgentSendTool()
        result = tool.execute(agent_id=agent_id, message=req.message)
        if not result.success:
            raise HTTPException(status_code=404, detail=result.content)
        return {"status": "sent", "content": result.content}
    except ImportError:
        raise HTTPException(status_code=501, detail="Agent tools not available")


# ---- Memory routes ----

memory_router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _get_memory_backend(request: Request):
    """Return the app-level memory backend, falling back to a fresh SQLiteMemory.

    Raises ``HTTPException(503)`` with an actionable message when the backend
    cannot be built because the mandatory ``bujji_rust`` extension is not
    installed in the serving venv. This is deliberately distinct from a benign
    "memory not configured" case (which returns ``None``): a missing native
    extension must fail loudly, never silently degrade (#502).
    """
    backend = getattr(request.app.state, "memory_backend", None)
    if backend is None:
        from bujji.tools.storage._stubs import MemoryBackendUnavailable

        try:
            from bujji.tools.storage.sqlite import SQLiteMemory

            backend = SQLiteMemory()
        except MemoryBackendUnavailable as exc:
            # The native extension is missing â€” surface a loud, actionable error
            # rather than a misleading "no backend" / silent no-op.
            logger.error("%s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception:
            # Memory is genuinely unconfigured for a benign reason â€” preserve
            # the existing graceful "no backend" behaviour.
            return None
    return backend


@memory_router.post("/store")
async def memory_store(req: MemoryStoreRequest, request: Request):
    """Store content in memory."""
    backend = _get_memory_backend(request)
    if backend is None:
        # Memory is intentionally disabled; report it honestly instead of a
        # 200 that silently discards the write (#502).
        raise HTTPException(status_code=503, detail="Memory is not configured")
    try:
        backend.store(req.content, metadata=req.metadata or {})
        return {"status": "stored"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.post("/search")
async def memory_search(req: MemorySearchRequest, request: Request):
    """Search memory for relevant content."""
    backend = _get_memory_backend(request)
    if backend is None:
        return {"results": []}
    try:
        results = backend.retrieve(req.query, top_k=req.top_k)
        items = [
            {
                "content": r.content,
                "score": getattr(r, "score", 0.0),
                "metadata": getattr(r, "metadata", {}),
            }
            for r in results
        ]
        return {"results": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.get("/stats")
async def memory_stats(request: Request):
    """Get memory backend statistics."""
    backend = _get_memory_backend(request)
    if backend is None:
        return {"entries": 0, "backend": "none", "status": "not_configured"}
    try:
        return {
            "entries": backend.count(),
            "backend": getattr(backend, "backend_id", "unknown"),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.get("/config")
async def memory_config(request: Request):
    """Return current memory configuration.

    Reports memory as *unavailable* (rather than falsely claiming
    ``backend_type: sqlite``) when the native ``bujji_rust`` extension is
    missing, so the UI can show the real cause instead of a healthy-looking
    config that backs a silent no-op (#502).
    """
    try:
        config = getattr(request.app.state, "config", None)
        if config is None:
            from bujji.core.config import load_config

            config = load_config()
        backend = getattr(request.app.state, "memory_backend", None)
        available = True
        detail: Optional[str] = None
        if backend is None:
            from bujji.tools.storage._stubs import MemoryBackendUnavailable

            try:
                from bujji.tools.storage.sqlite import SQLiteMemory

                backend = SQLiteMemory()
            except MemoryBackendUnavailable as exc:
                available = False
                detail = str(exc)
            except Exception:
                # Benign: cannot construct a probe backend here, but the
                # configured default is still what would be used.
                pass
        return {
            "backend_type": (
                backend.backend_id
                if backend is not None
                else config.memory.default_backend
            ),
            "available": available,
            "detail": detail,
            "context_top_k": config.memory.context_top_k,
            "context_min_score": config.memory.context_min_score,
            "context_max_tokens": config.memory.context_max_tokens,
            "context_from_memory": config.agent.context_from_memory,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@memory_router.post("/index")
async def memory_index(req: MemoryIndexRequest, request: Request):
    """Index files from a path into memory."""
    try:
        import os
        from pathlib import Path

        from bujji.security.file_policy import is_sensitive_file
        from bujji.tools.storage.ingest import ingest_path

        target = Path(req.path).expanduser().resolve()
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")

        # Sandbox: when workspace roots are configured via BUJJI_WORKSPACE
        # (os.pathsep-separated), only allow indexing inside them. This endpoint
        # must not become an arbitrary-filesystem read primitive over the API.
        workspace = os.environ.get("BUJJI_WORKSPACE", "").strip()
        if workspace:
            roots = [
                Path(d).expanduser().resolve()
                for d in workspace.split(os.pathsep)
                if d.strip()
            ]
            if not any(
                target == root or root in target.parents for root in roots
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Path is outside the allowed workspace directories.",
                )
        # Never ingest sensitive files (.env, private keys, credentials, ...).
        if target.is_file() and is_sensitive_file(target):
            raise HTTPException(
                status_code=403, detail="Refusing to index a sensitive file."
            )

        backend = _get_memory_backend(request)
        if backend is None:
            raise HTTPException(status_code=503, detail="Memory is not configured")

        chunks = ingest_path(target)
        stored = 0
        for chunk in chunks:
            metadata = {"source": getattr(chunk, "source", str(target))}
            if hasattr(chunk, "metadata") and chunk.metadata:
                metadata.update(chunk.metadata)
            backend.store(chunk.content, metadata=metadata)
            stored += 1

        result = {"status": "indexed", "chunks_indexed": stored}
        if stored == 0:
            # "indexed" must never silently mean "stored nothing". Surface why
            # so a folder of short notes doesn't look like a successful no-op
            # (#502 follow-up).
            result["note"] = (
                "no content was indexed â€” the path contained no readable "
                "documents with indexable text"
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Traces routes ----

traces_router = APIRouter(prefix="/v1/traces", tags=["traces"])


def _serialise_trace(trace) -> dict:
    """Convert a Trace dataclass to a frontend-friendly dict."""
    import datetime
    from dataclasses import asdict

    d = asdict(trace)
    d["id"] = d.pop("trace_id", "")
    started = d.pop("started_at", 0.0)
    d["created_at"] = (
        datetime.datetime.fromtimestamp(started, tz=datetime.timezone.utc).isoformat()
        if started
        else None
    )
    dur = d.pop("total_latency_seconds", 0.0)
    d["duration_ms"] = round(dur * 1000)
    for step in d.get("steps", []):
        st = step.get("step_type")
        if hasattr(st, "value"):
            step["step_type"] = st.value
    return d


@traces_router.get("")
async def list_traces(request: Request, limit: int = 20):
    """List recent traces."""
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            return {"traces": []}
        traces = store.list_traces(limit=limit)
        items = [_serialise_trace(t) for t in traces]
        return {"traces": items}
    except Exception as exc:
        return {"traces": [], "error": str(exc)}


@traces_router.get("/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    """Get a specific trace by ID."""
    try:
        store = getattr(request.app.state, "trace_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        trace = store.get(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return _serialise_trace(trace)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Telemetry routes ----

telemetry_router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])


@telemetry_router.get("/stats")
async def telemetry_stats(request: Request):
    """Get aggregated telemetry statistics."""
    try:
        from dataclasses import asdict

        from bujji.core.config import DEFAULT_CONFIG_DIR
        from bujji.telemetry.aggregator import TelemetryAggregator

        db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
        if not db_path.exists():
            return {"total_requests": 0, "total_tokens": 0}

        session_start = getattr(request.app.state, "session_start", None)
        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary(since=session_start)
            d = asdict(stats)
            d.pop("per_model", None)
            d.pop("per_engine", None)
            d["total_requests"] = d.pop("total_calls", 0)
            return d
        finally:
            agg.close()
    except Exception as exc:
        return {"error": str(exc)}


@telemetry_router.get("/energy")
async def telemetry_energy(request: Request):
    """Get energy monitoring data."""
    try:
        from bujji.core.config import DEFAULT_CONFIG_DIR
        from bujji.telemetry.aggregator import TelemetryAggregator

        db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
        if not db_path.exists():
            return {
                "total_energy_j": 0,
                "energy_per_token_j": 0,
                "avg_power_w": 0,
                "cpu_temp_c": None,
                "gpu_temp_c": None,
            }

        session_start = getattr(request.app.state, "session_start", None)
        agg = TelemetryAggregator(db_path)
        try:
            stats = agg.summary(since=session_start)
            total_energy = stats.total_energy_joules
            total_tokens = stats.total_tokens
            total_latency = stats.total_latency
            return {
                "total_energy_j": total_energy,
                "energy_per_token_j": (
                    total_energy / total_tokens if total_tokens > 0 else 0
                ),
                "avg_power_w": (
                    total_energy / total_latency if total_latency > 0 else 0
                ),
                "cpu_temp_c": None,
                "gpu_temp_c": None,
            }
        finally:
            agg.close()
    except Exception as exc:
        return {"error": str(exc)}


# ---- Skills routes ----

skills_router = APIRouter(prefix="/v1/skills", tags=["skills"])


@skills_router.get("")
async def list_skills(request: Request):
    """List installed skills."""
    try:
        from bujji.core.registry import SkillRegistry

        skills = []
        for key in sorted(SkillRegistry.keys()):
            skills.append({"name": key})
        return {"skills": skills}
    except Exception as exc:
        logger.warning("Failed to list skills: %s", exc)
        return {"skills": []}


@skills_router.post("")
async def install_skill(request: Request):
    """Install a skill (placeholder)."""
    return {
        "status": "not_implemented",
        "message": "Use TOML files in ~/.bujji/skills/",
    }


@skills_router.delete("/{skill_name}")
async def remove_skill(skill_name: str, request: Request):
    """Remove a skill (placeholder)."""
    return {
        "status": "not_implemented",
        "message": "Skill removal not yet supported via API",
    }


# ---- Sessions routes ----

sessions_router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@sessions_router.get("")
async def list_sessions(request: Request, limit: int = 20):
    """List active sessions."""
    try:
        from bujji.sessions.store import SessionStore

        store = SessionStore()
        sessions = store.recent(limit=limit)
        items = [s.to_dict() if hasattr(s, "to_dict") else str(s) for s in sessions]
        return {"sessions": items}
    except Exception as exc:
        return {"sessions": [], "error": str(exc)}


@sessions_router.get("/{session_id}")
async def get_session(session_id: str, request: Request):
    """Get a specific session."""
    try:
        from bujji.sessions.store import SessionStore

        store = SessionStore()
        session = store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session.to_dict() if hasattr(session, "to_dict") else {"id": session_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Budget routes ----

budget_router = APIRouter(prefix="/v1/budget", tags=["budget"])

_budget_limits: Dict[str, Any] = {
    "max_tokens_per_day": None,
    "max_requests_per_hour": None,
}
_budget_usage: Dict[str, int] = {
    "tokens_today": 0,
    "requests_this_hour": 0,
}


@budget_router.get("")
async def get_budget(request: Request):
    """Get current budget usage and limits."""
    return {"limits": _budget_limits, "usage": _budget_usage}


@budget_router.put("/limits")
async def set_budget_limits(req: BudgetLimitsRequest, request: Request):
    """Update budget limits."""
    if req.max_tokens_per_day is not None:
        _budget_limits["max_tokens_per_day"] = req.max_tokens_per_day
    if req.max_requests_per_hour is not None:
        _budget_limits["max_requests_per_hour"] = req.max_requests_per_hour
    return {"status": "updated", "limits": _budget_limits}


# ---- Prometheus metrics ----

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus-compatible metrics endpoint."""
    try:
        from bujji.core.config import DEFAULT_CONFIG_DIR
        from bujji.telemetry.aggregator import TelemetryAggregator

        db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
        if not db_path.exists():
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("# no telemetry data\n", media_type="text/plain")

        agg = TelemetryAggregator(db_path)
        stats = agg.summary()

        lines = [
            "# HELP bujji_requests_total Total requests processed",
            "# TYPE bujji_requests_total counter",
            f"bujji_requests_total {stats.get('total_requests', 0)}",
            "# HELP bujji_tokens_total Total tokens generated",
            "# TYPE bujji_tokens_total counter",
            f"bujji_tokens_total {stats.get('total_tokens', 0)}",
            "# HELP bujji_latency_avg_ms Average latency in milliseconds",
            "# TYPE bujji_latency_avg_ms gauge",
            f"bujji_latency_avg_ms {stats.get('avg_latency_ms', 0)}",
        ]
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")
    except Exception as exc:
        logger.warning("Failed to collect Prometheus metrics: %s", exc)
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("# No metrics available\n", media_type="text/plain")


# ---- WebSocket streaming routes ----

websocket_router = APIRouter(tags=["websocket"])


def _record_ws_trace(
    trace_store,
    *,
    query: str,
    result: str,
    model: str,
    started_at: float,
    ended_at: float,
) -> None:
    """Record a trace for a completed WebSocket chat (best-effort)."""
    if trace_store is None or not result:
        return
    from bujji.traces.collector import record_response_trace

    record_response_trace(
        trace_store,
        query=query,
        result=result,
        model=model,
        started_at=started_at,
        ended_at=ended_at,
    )


@websocket_router.websocket("/v1/chat/stream")
async def websocket_chat_stream(websocket: WebSocket):
    """Stream chat responses over a WebSocket connection.

    Accepts JSON messages of the form::

        {"message": "...", "model": "...", "agent": "..."}

    Sends back JSON chunks::

        {"type": "chunk", "content": "..."}   -- per-token streaming
        {"type": "done",  "content": "..."}   -- final assembled response
        {"type": "error", "detail": "..."}    -- on failure
    """
    from bujji.server.auth_middleware import websocket_authorized

    expected_key = getattr(websocket.app.state, "api_key", "")
    if not websocket_authorized(websocket, expected_key):
        # 1008 = policy violation; reject before accepting the connection.
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await websocket.send_json(
                    {"type": "error", "detail": "Invalid JSON"},
                )
                continue

            message = data.get("message")
            if not message:
                await websocket.send_json(
                    {"type": "error", "detail": "Missing 'message' field"},
                )
                continue

            model = data.get("model") or getattr(
                websocket.app.state,
                "model",
                "default",
            )
            engine = getattr(websocket.app.state, "engine", None)
            if engine is None:
                await websocket.send_json(
                    {"type": "error", "detail": "No engine configured"},
                )
                continue

            messages = [{"role": "user", "content": message}]

            # This WS path streams straight from the engine (no agent /
            # TraceCollector), so record the interaction directly once it
            # finishes â€” otherwise WebSocket chats never reach traces.db.
            import time as _time

            trace_store = getattr(websocket.app.state, "trace_store", None)
            _ws_started_at = _time.time()

            try:
                # Prefer streaming if the engine supports it
                stream_fn = getattr(engine, "stream", None)
                if stream_fn is not None and (
                    inspect.isasyncgenfunction(stream_fn) or callable(stream_fn)
                ):
                    full_content = ""
                    try:
                        gen = stream_fn(messages, model=model)
                        # Handle both async and sync generators
                        if inspect.isasyncgen(gen):
                            async for token in gen:
                                full_content += token
                                await websocket.send_json(
                                    {"type": "chunk", "content": token},
                                )
                        else:
                            # Sync generator — pull each item in a worker
                            # thread so a slow local engine's blocking next()
                            # doesn't stall the whole event loop.
                            loop = asyncio.get_running_loop()
                            _sentinel = object()

                            def _next(g=gen):
                                try:
                                    return next(g)
                                except StopIteration:
                                    return _sentinel

                            while True:
                                token = await loop.run_in_executor(None, _next)
                                if token is _sentinel:
                                    break
                                full_content += token
                                await websocket.send_json(
                                    {"type": "chunk", "content": token},
                                )
                    except TypeError:
                        # stream() didn't return an iterable; fall back to
                        # generate()
                        result = engine.generate(messages, model=model)
                        content = (
                            result.get("content", "")
                            if isinstance(
                                result,
                                dict,
                            )
                            else str(result)
                        )
                        full_content = content
                        await websocket.send_json(
                            {"type": "chunk", "content": content},
                        )
                    await websocket.send_json(
                        {"type": "done", "content": full_content},
                    )
                    _record_ws_trace(
                        trace_store,
                        query=message,
                        result=full_content,
                        model=model,
                        started_at=_ws_started_at,
                        ended_at=_time.time(),
                    )
                else:
                    # No stream method â€” single-shot generate
                    result = engine.generate(messages, model=model)
                    content = (
                        result.get("content", "")
                        if isinstance(
                            result,
                            dict,
                        )
                        else str(result)
                    )
                    await websocket.send_json(
                        {"type": "chunk", "content": content},
                    )
                    await websocket.send_json(
                        {"type": "done", "content": content},
                    )
                    _record_ws_trace(
                        trace_store,
                        query=message,
                        result=content,
                        model=model,
                        started_at=_ws_started_at,
                        ended_at=_time.time(),
                    )
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                await websocket.send_json(
                    {"type": "error", "detail": str(exc)},
                )
    except WebSocketDisconnect:
        pass  # Client disconnected â€” nothing to clean up


# ---- Speech routes ----

speech_router = APIRouter(prefix="/v1/speech", tags=["speech"])

# Shared mic recording state
_rec_active = False
_rec_chunks: List[Any] = []
_rec_thread: Optional[threading.Thread] = None
_rec_lock = threading.Lock()
_REC_SAMPLE_RATE = 16000


def _record_loop() -> None:
    try:
        import sounddevice as sd
    except ImportError:
        return
    blocksize = 1024
    with sd.InputStream(samplerate=_REC_SAMPLE_RATE, channels=1, dtype="float32", blocksize=blocksize) as stream:
        while _rec_active:
            block, _ = stream.read(blocksize)
            with _rec_lock:
                _rec_chunks.append(block.copy())


@speech_router.post("/transcribe")
async def transcribe_speech(request: Request):
    """Transcribe uploaded audio to text."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        raise HTTPException(status_code=501, detail="Speech backend not configured")

    form = await request.form()
    try:
        audio_file = form.get("file")
        if audio_file is None:
            raise HTTPException(status_code=400, detail="Missing 'file' field")

        audio_bytes = await audio_file.read()
        language = form.get("language")

        # Detect format from filename
        filename = getattr(audio_file, "filename", "audio.wav")
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "wav"

        try:
            result = backend.transcribe(
                audio_bytes,
                format=ext,
                language=language or None,
            )
        except Exception as exc:
            logger.exception("Speech transcription failed")
            raise HTTPException(
                status_code=500,
                detail=f"Speech transcription failed: {exc}",
            ) from exc

        return {
            "text": result.text,
            "language": result.language,
            "confidence": result.confidence,
            "duration_seconds": result.duration_seconds,
        }
    finally:
        await form.close()


@speech_router.post("/record/start")
async def record_start(request: Request):
    """Start microphone recording (server-side via sounddevice)."""
    global _rec_active, _rec_thread
    with _rec_lock:
        if _rec_active:
            return {"status": "already_recording"}
        _rec_active = True
        _rec_chunks.clear()
    _rec_thread = threading.Thread(target=_record_loop, daemon=True)
    _rec_thread.start()
    return {"status": "recording"}


@speech_router.post("/record/stop")
async def record_stop(request: Request):
    """Stop recording and return transcription."""
    global _rec_active
    backend = getattr(request.app.state, "speech_backend", None)

    with _rec_lock:
        if not _rec_active:
            raise HTTPException(status_code=400, detail="Not recording")
        _rec_active = False

    if _rec_thread:
        _rec_thread.join(timeout=3.0)

    with _rec_lock:
        chunks = list(_rec_chunks)

    if not chunks:
        return {"text": "", "language": None}

    import numpy as np
    import soundfile as sf

    audio = np.concatenate(chunks).flatten()
    buf = io.BytesIO()
    sf.write(buf, audio, _REC_SAMPLE_RATE, format="WAV")
    wav_bytes = buf.getvalue()

    if backend is None:
        raise HTTPException(status_code=501, detail="Speech backend not configured")

    try:
        result = backend.transcribe(wav_bytes, format="wav")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc

    return {"text": result.text, "language": result.language}


@speech_router.get("/health")
async def speech_health(request: Request):
    """Check if a speech backend is available."""
    backend = getattr(request.app.state, "speech_backend", None)
    if backend is None:
        return {"available": False, "reason": "No speech backend configured"}
    try:
        available = backend.health()
        reason = None
    except Exception as exc:
        logger.exception("Speech health check failed")
        available = False
        reason = str(exc)

    if not available and reason is None:
        last_error = getattr(backend, "last_error", None)
        if callable(last_error):
            reason = last_error()

    return {
        "available": available,
        "backend": backend.backend_id,
        **({"reason": reason} if reason else {}),
    }


# ---- Feedback routes ----

feedback_router = APIRouter(prefix="/v1/feedback", tags=["feedback"])


@feedback_router.post("")
async def submit_feedback(req: FeedbackScoreRequest, request: Request):
    """Submit feedback for a trace."""
    try:
        from bujji.core.config import DEFAULT_CONFIG_DIR
        from bujji.traces.store import TraceStore

        db_path = DEFAULT_CONFIG_DIR / "traces.db"
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="No trace database")

        store = TraceStore(db_path)
        updated = store.update_feedback(req.trace_id, req.score)
        store.close()

        if not updated:
            raise HTTPException(
                status_code=404, detail=f"Trace '{req.trace_id}' not found"
            )
        return {"status": "recorded", "trace_id": req.trace_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@feedback_router.get("/stats")
async def feedback_stats(request: Request):
    """Get feedback statistics."""
    return {"total": 0, "mean_score": 0.0}


# ---- TTS routes ----

tts_router = APIRouter(prefix="/v1/tts", tags=["tts"])
_tts_backend_instance = None
_tts_backend_lock = threading.Lock()
# Max concurrent CPU synth jobs — first sentence must not starve (see /synthesize)
_tts_synth_gate = threading.Semaphore(2)


def _get_tts_backend(language: str = ""):
    """Return TTS backend, routing to the language-specific backend when configured."""
    from bujji.core.config import load_config
    from bujji.core.registry import TTSRegistry

    global _tts_backend_instance
    cfg = load_config()
    tts_cfg = getattr(cfg, "tts", None)

    # Resolve language override (e.g. "te" → edge_tts + te-IN-ShrutiNeural)
    if language and tts_cfg:
        resolved = tts_cfg.for_language(language)
        backend_key = resolved.backend
    else:
        backend_key = (
            getattr(tts_cfg, "backend", "windows_sapi")
            if tts_cfg
            else "windows_sapi"
        )

    # Import all backends
    import bujji.speech  # noqa: F401

    if TTSRegistry.contains(backend_key):
        try:
            backend = TTSRegistry.get(backend_key)()
            if backend.health():
                return backend
        except Exception:
            pass

    # Fallback: use cached default instance
    if _tts_backend_instance is None:
        with _tts_backend_lock:
            if _tts_backend_instance is None:
                from bujji.speech.windows_sapi_tts import WindowsSapiTTSBackend

                _tts_backend_instance = WindowsSapiTTSBackend()
    return _tts_backend_instance


@tts_router.post("/speak")
async def tts_speak(request: Request):
    """Synthesize text and play it through the system speakers."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    language = (body.get("language") or "").strip()

    cfg = load_config()
    tts_cfg = getattr(cfg, "tts", None)
    resolved = tts_cfg.for_language(language) if (tts_cfg and language) else tts_cfg
    voice_id = body.get("voice_id") or (resolved.voice_id if resolved else "af_heart")
    speed = float(body.get("speed") or (resolved.speed if resolved else 1.0))

    def _run():
        try:
            import sounddevice as sd
            import soundfile as sf
            backend = _get_tts_backend(language)
            from bujji.speech.pronounce import respell

            result = backend.synthesize(
                respell(text, engine=backend.backend_id),
                voice_id=voice_id, speed=speed,
            )
            buf = io.BytesIO(result.audio)
            data, sr = sf.read(buf, dtype="float32")
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            # Own OutputStream — the global sd.play() stream gets killed by
            # the wake-word mic loop (see pipeline._play_tts)
            with sd.OutputStream(samplerate=sr, channels=data.shape[1], dtype="float32") as out:
                out.write(data)
        except Exception as exc:
            logger.warning("TTS speak error: %s", exc)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@tts_router.get("/synthesize")
async def tts_synthesize(
    text: str,
    voice_id: str = "",
    speed: float = 0.0,
    language: str = "",
):
    """Synthesize text and return raw audio bytes. Used by the browser Web Audio queue."""
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    try:
        from fastapi.responses import Response as FastResponse

        cfg = load_config()
        tts_cfg = getattr(cfg, "tts", None)
        resolved = tts_cfg.for_language(language) if (tts_cfg and language) else tts_cfg
        effective_voice = voice_id or (resolved.voice_id if resolved else "af_heart")
        effective_speed = speed if speed > 0 else (resolved.speed if resolved else 1.0)

        backend = _get_tts_backend(language)
        from bujji.speech.pronounce import respell

        spoken = respell(text, engine=backend.backend_id)

        # CPU-bound synth must not block the event loop, but unlimited
        # parallelism makes ALL sentences slow (CPU contention pushed
        # first-voice latency to ~7s). Two at a time: first sentence lands
        # fast, the rest stay ahead of playback.
        import time as _time

        _t0 = _time.time()

        def _synth():
            with _tts_synth_gate:
                _t1 = _time.time()
                r = backend.synthesize(
                    spoken, voice_id=effective_voice, speed=effective_speed
                )
                logger.info(
                    "TTS synth: %r queued %.1fs, synth %.1fs (%s)",
                    text[:40], _t1 - _t0, _time.time() - _t1, backend.backend_id,
                )
                return r

        result = await asyncio.to_thread(_synth)
        # Edge TTS returns mp3; Kokoro returns wav — set content-type accordingly
        media_type = "audio/mpeg" if result.format == "mp3" else "audio/wav"
        return FastResponse(content=result.audio, media_type=media_type)
    except Exception as exc:
        logger.warning("TTS synthesize error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@tts_router.post("/stop")
async def tts_stop():
    """Stop current TTS playback."""
    try:
        import sounddevice as sd
        sd.stop()
    except Exception:
        pass
    return {"status": "stopped"}


# ---- Wake word routes ----

wake_router = APIRouter(prefix="/v1/wake", tags=["wake"])
_wake_detector_instance = None
_wake_detector_lock = threading.Lock()


def _get_wake_detector():
    global _wake_detector_instance
    if _wake_detector_instance is None:
        with _wake_detector_lock:
            if _wake_detector_instance is None:
                from bujji.speech.wake_word import BujjiWakeWordDetector  # noqa: PLC0415
                _wake_detector_instance = BujjiWakeWordDetector()
                _wake_detector_instance.start()
    return _wake_detector_instance


@wake_router.get("/sse")
async def wake_sse(request: Request):
    """Server-Sent Events stream â€” pushes 'wake' when 'bujji' is heard."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_wake():
        loop.call_soon_threadsafe(q.put_nowait, "wake")

    detector = _get_wake_detector()
    detector.add_callback(on_wake)

    async def generate():
        try:
            yield "data: connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            detector.remove_callback(on_wake)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def include_all_routes(app) -> None:
    """Include all extended API routers in a FastAPI app."""
    from bujji.server.approval_routes import (
        router as approval_router,  # noqa: PLC0415
    )

    app.include_router(approval_router)
    app.include_router(agents_router)
    app.include_router(memory_router)
    app.include_router(traces_router)
    app.include_router(telemetry_router)
    app.include_router(skills_router)
    app.include_router(sessions_router)
    app.include_router(budget_router)
    app.include_router(metrics_router)
    app.include_router(websocket_router)
    app.include_router(speech_router)
    app.include_router(tts_router)
    app.include_router(wake_router)
    app.include_router(feedback_router)

    # Agent Manager routes (if available)
    try:
        if hasattr(app.state, "agent_manager") and app.state.agent_manager:
            from bujji.server.agent_manager_routes import (  # noqa: PLC0415
                create_agent_manager_router,
            )

            (
                agents_r,
                templates_r,
                global_r,
                tools_r,
                sendblue_r,
            ) = create_agent_manager_router(app.state.agent_manager)
            app.include_router(agents_r)
            app.include_router(templates_r)
            app.include_router(global_r)
            app.include_router(tools_r)
            app.include_router(sendblue_r)
    except ImportError:
        pass

    # WebSocket bridge for real-time agent events
    try:
        from bujji.core.events import get_event_bus
        from bujji.server.ws_bridge import create_ws_router

        ws_router = create_ws_router(get_event_bus())
        app.include_router(ws_router)
    except Exception:
        logger.debug("WebSocket bridge not available", exc_info=True)


__all__ = [
    "include_all_routes",
    "agents_router",
    "memory_router",
    "traces_router",
    "telemetry_router",
    "skills_router",
    "sessions_router",
    "budget_router",
    "metrics_router",
    "websocket_router",
    "speech_router",
    "tts_router",
    "wake_router",
    "feedback_router",
]
