"""``bujji serve`` â€” OpenAI-compatible API server."""

from __future__ import annotations

import logging
import sys
import warnings

# Suppress noisy third-party startup warnings (Kokoro/PyTorch internals)
warnings.filterwarnings("ignore", category=UserWarning, module=r"torch\.nn")
warnings.filterwarnings("ignore", category=FutureWarning, module=r"torch\.nn")
warnings.filterwarnings("ignore", message=r".*unauthenticated.*HF Hub.*")
warnings.filterwarnings("ignore", message=r".*HF_TOKEN.*")
# Suppress HuggingFace Hub unauthenticated-request noise
import os as _os
_os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
_os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
_os.environ.setdefault("HF_HUB_VERBOSITY", "error")

import click
from rich.console import Console

from bujji.cli._banner import print_banner
from bujji.core.config import load_config
from bujji.core.events import EventBus
from bujji.core.paths import get_config_dir
from bujji.engine import (
    discover_engines,
    discover_models,
    get_engine,
)
from bujji.intelligence import (
    merge_discovered_models,
    register_builtin_models,
)

logger = logging.getLogger(__name__)


def _unique_model_ids(model_ids: list[str]) -> list[str]:
    """Return model ids in first-seen order without duplicates."""
    unique: list[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id and model_id not in seen:
            seen.add(model_id)
            unique.append(model_id)
    return unique


def _safe_list_models(engine: object) -> list[str]:
    try:
        list_models = getattr(engine, "list_models")
        return list(list_models())
    except Exception as exc:
        logger.debug("Failed to list models for selected server engine: %s", exc)
        return []


def _resolve_server_model(
    requested_model: str | None,
    *,
    config: object,
    engine_name: str,
    engine: object,
    all_models: dict[str, list[str]],
) -> str:
    """Pick a startup model that is present on the active server engine.

    CLI ``--model`` remains authoritative. For config-driven startup, prefer the
    configured server/default model only when the active engine can actually
    serve it; otherwise use ``intelligence.fallback_model`` or the first
    reachable model. This prevents MLX-preferred configs from hiding a healthy
    Ollama fallback behind an empty/incorrect model map.
    """
    if requested_model:
        return requested_model

    candidates = [
        getattr(config.server, "model", ""),
        getattr(config.intelligence, "default_model", ""),
        getattr(config.intelligence, "fallback_model", ""),
    ]
    available = _unique_model_ids(
        _safe_list_models(engine) + list(all_models.get(engine_name, []))
    )

    for candidate in candidates:
        if candidate and (not available or candidate in available):
            return candidate

    return available[0] if available else ""


@click.command()
@click.option("--host", default=None, help="Bind address (default: config).")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port number (default: config).",
)
@click.option("-e", "--engine", "engine_key", default=None, help="Engine backend.")
@click.option("-m", "--model", "model_name", default=None, help="Default model.")
@click.option(
    "-a",
    "--agent",
    "agent_name",
    default=None,
    help="Agent for non-streaming requests (simple, orchestrator, react, openhands).",
)
@click.pass_context
def serve(
    ctx: click.Context,
    host: str | None,
    port: int | None,
    engine_key: str | None,
    model_name: str | None,
    agent_name: str | None,
) -> None:
    """Start the OpenAI-compatible API server."""
    print_banner(quiet=(ctx.obj or {}).get("quiet", False))
    console = Console(stderr=True)

    # Check for server dependencies
    try:
        import uvicorn  # noqa: F401
        from fastapi import FastAPI  # noqa: F401
    except ImportError:
        console.print(
            "[red bold]Server dependencies not installed.[/red bold]\n\n"
            "Install the server extra:\n"
            "  [cyan]uv sync --extra server[/cyan]"
        )
        sys.exit(1)

    config = load_config()

    # Resolve host/port from CLI args or config
    bind_host = host or config.server.host
    bind_port = port or config.server.port

    # Set up engine
    register_builtin_models()
    bus = EventBus(record_history=False)

    # Set up telemetry
    telem_store = None
    if config.telemetry.enabled:
        try:
            from pathlib import Path

            from bujji.telemetry.store import TelemetryStore

            db_path = Path(config.telemetry.db_path).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            telem_store = TelemetryStore(str(db_path))
            telem_store.subscribe_to_bus(bus)
        except Exception as exc:
            logger.debug("Telemetry store init failed: %s", exc)

    # Select with the model we'll actually serve so an engine that can't
    # serve it (e.g. the cloud fallback without the matching provider key) is
    # skipped rather than chosen and failing per-request later (see #532).
    selection_model = (
        model_name or config.server.model or config.intelligence.default_model or None
    )
    resolved = get_engine(config, engine_key, model=selection_model)
    if resolved is None:
        console.print(
            "[red bold]No inference engine available.[/red bold]\n\n"
            "Make sure an engine is running."
        )
        sys.exit(1)

    engine_name, engine = resolved

    # Apply security guardrails
    from bujji.security import setup_security

    sec = setup_security(config, engine, bus)
    engine = sec.engine

    # If cloud API keys are set, prepare a cloud engine. We build the
    # MultiEngine after local discovery so healthy local fallbacks such as
    # Ollama stay visible even when the configured preferred engine is MLX.
    import os

    cloud_engine = None
    _has_cloud = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    )
    if _has_cloud and engine_name != "cloud":
        try:
            from bujji.engine.cloud import CloudEngine

            cloud_engine = CloudEngine()
            if cloud_engine.health():
                console.print("  Cloud:  [cyan]enabled[/cyan] (API keys detected)")
            else:
                console.print(
                    "  Cloud:  [yellow]keys set but packages missing[/yellow] "
                    "(run: uv sync --extra inference-cloud --extra inference-google)"
                )
        except Exception as exc:
            logger.debug("Cloud engine init failed: %s", exc)

    # Wrap engine with InstrumentedEngine for telemetry recording
    try:
        from bujji.telemetry.instrumented_engine import InstrumentedEngine

        energy_mon = None
        try:
            from bujji.telemetry.energy_monitor import create_energy_monitor

            energy_mon = create_energy_monitor()
            if energy_mon is not None:
                console.print(
                    f"  Energy: [cyan]{energy_mon.vendor().value}[/cyan] "
                    f"({energy_mon.energy_method()})"
                )
        except Exception as exc:
            logger.debug("Energy monitor creation failed: %s", exc)

        engine = InstrumentedEngine(engine, bus, energy_monitor=energy_mon)
    except Exception as exc:
        logger.debug("Engine instrumentation failed: %s", exc)

    # Discover models
    all_engines = discover_engines(config)
    all_models = discover_models(all_engines)
    for ek, model_ids in all_models.items():
        merge_discovered_models(ek, model_ids)

    multi_entries = [(engine_name, engine)]
    for discovered_name, discovered_engine in all_engines:
        if discovered_name != engine_name:
            multi_entries.append((discovered_name, discovered_engine))
    if cloud_engine is not None:
        multi_entries.append(("cloud", cloud_engine))

    if len(multi_entries) > 1:
        from bujji.engine.multi import MultiEngine

        engine = MultiEngine(multi_entries)
        engine_name = "multi"
        all_models[engine_name] = engine.list_models()
        merge_discovered_models(engine_name, all_models[engine_name])

    # Resolve model
    configured_model = (
        model_name or config.server.model or config.intelligence.default_model
    )
    model_name = _resolve_server_model(
        model_name,
        config=config,
        engine_name=engine_name,
        engine=engine,
        all_models=all_models,
    )
    if configured_model and model_name and model_name != configured_model:
        console.print(
            "[yellow]Configured model "
            f"{configured_model!r} is not reachable; using {model_name!r}.[/yellow]"
        )
    if not model_name:
        console.print(
            "[red]No model available on any reachable engine.[/red]\n\n"
            "Start an inference backend and make sure it lists at least one model.\n"
            "For Ollama: [cyan]ollama serve[/cyan] and "
            "[cyan]ollama pull qwen3.5:9b[/cyan].\n"
            "For MLX: start the MLX OpenAI-compatible server on the configured host."
        )
        sys.exit(1)

    # Resolve agent
    agent = None
    agent_key = agent_name or config.server.agent
    # Tool instances resolved for the primary agent are reused below to build
    # the scheduler's ToolExecutor â€” avoiding a second full SystemBuilder.build()
    # (which would re-discover the engine, re-resolve tools, re-open the channel,
    # etc.). See the scheduler block near the bottom of this function (#263).
    resolved_tools: list = []
    if agent_key:
        try:
            import bujji.agents  # noqa: F401
            from bujji.core.registry import AgentRegistry

            if AgentRegistry.contains(agent_key):
                agent_cls = AgentRegistry.get(agent_key)
                agent_kwargs = {"bus": bus}
                if sec.capability_policy is not None:
                    agent_kwargs["capability_policy"] = sec.capability_policy
                # Pass default system prompt so identity + tool instructions reach the model
                if getattr(config.agent, "default_system_prompt", None):
                    agent_kwargs["system_prompt"] = config.agent.default_system_prompt

                # MCP transports persisted on the agent at the bottom of
                # this block â€” initialise here so the reference is valid
                # even when accepts_tools is False (#461).
                mcp_clients: list = []

                # Load tools for agents that support them
                if getattr(agent_cls, "accepts_tools", False):
                    import bujji.tools  # noqa: F401  # trigger registration
                    from bujji.core.registry import ToolRegistry
                    from bujji.tools._stubs import BaseTool

                    _DEFAULT_TOOLS = {"think", "calculator", "web_search", "shell_exec", "windows_control"}
                    configured = config.agent.tools
                    if configured:
                        if isinstance(configured, list):
                            allowed = {
                                t.strip()
                                for t in configured
                                if isinstance(t, str) and t.strip()
                            }
                        else:
                            allowed = {
                                t.strip() for t in configured.split(",") if t.strip()
                            }
                    else:
                        allowed = _DEFAULT_TOOLS

                    tools = []
                    for name in ToolRegistry.keys():
                        if name not in allowed:
                            continue
                        tool_cls = ToolRegistry.get(name)
                        if isinstance(tool_cls, type) and issubclass(
                            tool_cls, BaseTool
                        ):
                            tools.append(tool_cls())
                        elif isinstance(tool_cls, BaseTool):
                            tools.append(tool_cls)

                    # MCP server tools from config.tools.mcp.servers
                    # (#461 â€” these were silently dropped).
                    from bujji.mcp.loader import load_mcp_tools_from_config

                    mcp_tools, mcp_clients = load_mcp_tools_from_config(
                        config.tools.mcp,
                        allowed_names=allowed if configured else None,
                    )
                    if mcp_tools:
                        existing = {t.spec.name for t in tools}
                        for t in mcp_tools:
                            if t.spec.name not in existing:
                                tools.append(t)
                                existing.add(t.spec.name)

                    # Connector-backed tools (e.g. Obsidian read/write/search)
                    try:
                        _connectors_cfg = getattr(config, "connectors", None)
                        if _connectors_cfg:
                            import bujji.connectors  # noqa: F401  # trigger registration
                            from bujji.core.registry import ConnectorRegistry
                            from bujji.tools.connector_tool import ConnectorTool

                            for conn_id in ConnectorRegistry.keys():
                                conn_cfg = getattr(_connectors_cfg, conn_id, None)
                                if conn_cfg is None:
                                    continue
                                if not getattr(conn_cfg, "enabled", False):
                                    continue
                                conn_cls = ConnectorRegistry.get(conn_id)
                                # Build kwargs from connector config attributes
                                conn_kwargs: dict = {}
                                for attr in ("vault_path", "path", "notes_folder"):
                                    v = getattr(conn_cfg, attr, None)
                                    if v:
                                        conn_kwargs[attr] = v
                                try:
                                    conn_instance = conn_cls(**conn_kwargs)
                                    for spec in conn_instance.mcp_tools():
                                        tool = ConnectorTool(conn_instance, spec)
                                        if tool.spec.name not in {t.spec.name for t in tools}:
                                            tools.append(tool)
                                    console.print(
                                        f"  Connector: [cyan]{conn_id}[/cyan] "
                                        f"([green]{'connected' if conn_instance.is_connected() else 'not connected'}[/green])"
                                    )
                                except Exception as _ce:
                                    logger.debug("Connector %s init failed: %s", conn_id, _ce)
                    except Exception as _ce2:
                        logger.debug("Connector tool loading failed: %s", _ce2)

                    if tools:
                        agent_kwargs["tools"] = tools
                    # Reuse these for the scheduler's ToolExecutor (#263).
                    resolved_tools = tools

                if getattr(agent_cls, "accepts_tools", False):
                    agent_kwargs["max_turns"] = config.agent.max_turns

                agent = agent_cls(engine, model_name, **agent_kwargs)
                # Pin MCP transports to the agent's lifetime so HTTP
                # connections don't close mid-request (#461).
                if mcp_clients:
                    agent._mcp_clients = mcp_clients
        except Exception as exc:
            import traceback

            console.print(f"[yellow]Agent '{agent_key}' failed to load: {exc}[/yellow]")
            traceback.print_exc()

    # Set up channel backend if enabled
    channel_bridge = None
    if config.channel.enabled and config.channel.default_channel:
        try:
            from bujji.system import SystemBuilder

            # Reuse _resolve_channel logic from SystemBuilder
            sb = SystemBuilder(config)
            sb._bus = bus
            channel_bridge = sb._resolve_channel(config, bus)
            if channel_bridge is not None:
                channel_bridge.connect()
                console.print(
                    f"  Channel: [cyan]{config.channel.default_channel}[/cyan]"
                )
        except Exception as exc:
            console.print(f"[yellow]Channel failed to start: {exc}[/yellow]")
            channel_bridge = None

    # Wire channel messages â†’ agent / engine (per-chat session isolation)
    if channel_bridge is not None:
        from bujji.system import BujjiSystem

        channel_agent = config.channel.default_agent or agent_key or "simple"

        _channel_tools: list = []
        # MCP transports persisted at function scope (= server-process
        # lifetime); see the comment near the channel-MCP-load block
        # below. Initialise here so it's always bound. #461.
        _channel_mcp_clients: list = []
        if channel_agent:
            try:
                import bujji.agents
                from bujji.core.registry import AgentRegistry

                if AgentRegistry.contains(channel_agent):
                    _ch_cls = AgentRegistry.get(channel_agent)
                    if getattr(_ch_cls, "accepts_tools", False):
                        import bujji.tools
                        from bujji.core.registry import ToolRegistry
                        from bujji.tools._stubs import BaseTool

                        _DEFAULT_TOOLS = {"think", "calculator", "web_search", "shell_exec", "windows_control"}
                        configured = config.agent.tools
                        if configured:
                            if isinstance(configured, list):
                                _allowed = {
                                    t.strip()
                                    for t in configured
                                    if isinstance(t, str) and t.strip()
                                }
                            else:
                                _allowed = {
                                    t.strip()
                                    for t in configured.split(",")
                                    if t.strip()
                                }
                        else:
                            _allowed = _DEFAULT_TOOLS

                        for _tname in ToolRegistry.keys():
                            if _tname not in _allowed:
                                continue
                            _tcls = ToolRegistry.get(_tname)
                            if isinstance(_tcls, type) and issubclass(_tcls, BaseTool):
                                _channel_tools.append(_tcls())
                            elif isinstance(_tcls, BaseTool):
                                _channel_tools.append(_tcls)

                        # MCP tools for the channel agent too (#461).
                        from bujji.mcp.loader import (
                            load_mcp_tools_from_config,
                        )

                        _ch_mcp_tools, _ch_mcp_clients = load_mcp_tools_from_config(
                            config.tools.mcp,
                            allowed_names=_allowed if configured else None,
                        )
                        if _ch_mcp_tools:
                            _existing = {t.spec.name for t in _channel_tools}
                            for t in _ch_mcp_tools:
                                if t.spec.name not in _existing:
                                    _channel_tools.append(t)
                                    _existing.add(t.spec.name)
                        # Hold a reference at module / function scope â€”
                        # the channel agent is constructed inside
                        # BujjiSystem below; we extend its lifetime by
                        # keeping the list bound here.
                        _channel_mcp_clients = _ch_mcp_clients
            except Exception as exc:
                logger.warning("Channel tools failed to load: %s", exc)
                _channel_mcp_clients = []

        _wire_system = BujjiSystem(
            config=config,
            bus=bus,
            engine=engine,
            engine_key=engine_name,
            model=model_name,
            agent_name=channel_agent,
            tools=_channel_tools,
        )
        _wire_system.wire_channel(channel_bridge)

    # Set up speech backend
    speech_backend = None
    try:
        from bujji.speech._discovery import get_speech_backend

        speech_backend = get_speech_backend(config)
        if speech_backend:
            console.print(f"  Speech: [cyan]{speech_backend.backend_id}[/cyan]")
    except Exception as exc:
        logger.debug("Speech backend discovery failed: %s", exc)

    # Set up TTS backend
    tts_backend = None
    try:
        from bujji.speech._tts_discovery import get_tts_backend

        tts_backend = get_tts_backend(config)
        if tts_backend:
            console.print(f"  TTS:    [cyan]{getattr(tts_backend, 'backend_id', type(tts_backend).__name__)}[/cyan]")
    except Exception as exc:
        logger.debug("TTS backend discovery failed: %s", exc)

    # Set up voice pipeline (system mic + wake word)
    voice_pipeline = None
    if speech_backend and tts_backend and agent:
        try:
            from bujji.speech.pipeline import VoicePipeline
            from bujji.speech.wake_word import BujjiWakeWordDetector
            from bujji.server.voice_ws import broadcaster as _voice_broadcaster

            def _emit_voice_event(event: dict) -> None:
                # broadcast() is a sync, thread-safe method that schedules its
                # own coroutine onto the server loop — call it directly. (The
                # old code wrapped it in run_coroutine_threadsafe(None, ...),
                # which raised TypeError that was silently swallowed.)
                try:
                    _voice_broadcaster.broadcast(event)
                except Exception:
                    pass

            _wake_word = getattr(config, "agent", None)
            _wake_str = getattr(_wake_word, "wake_word", "bujji") if _wake_word else "bujji"
            wake_detector = BujjiWakeWordDetector(wake_word=_wake_str)
            # Live hearing debug: every transcribed mic chunk goes to the UI
            wake_detector.on_text = lambda text, rms: _emit_voice_event(
                {"type": "heard", "text": text, "rms": round(rms, 5)}
            )
            _tts_cfg = getattr(config, "tts", None)
            _voice_id = _tts_cfg.voice_id if _tts_cfg else "af_heart"
            _tts_speed = _tts_cfg.speed if _tts_cfg else 1.0
            voice_pipeline = VoicePipeline(
                agent,
                speech_backend,
                tts_backend,
                wake_detector,
                on_event=_emit_voice_event,
                voice_id=_voice_id,
                tts_speed=_tts_speed,
                conversation_mode=getattr(_wake_word, "conversation_mode", True) if _wake_word else True,
            )
            console.print("  Voice:  [cyan]pipeline ready (system mic)[/cyan]")
        except Exception as exc:
            logger.debug("Voice pipeline init failed: %s", exc)

    # Create app
    from bujji.server.app import create_app

    # Set up memory backend for context injection. Built before the scheduler
    # block so the executor's BujjiSystem can reference it (#263).
    memory_backend = None
    if config.agent.context_from_memory:
        try:
            import bujji.tools.storage  # noqa: F401
            from bujji.core.registry import MemoryRegistry

            mem_key = config.memory.default_backend
            if MemoryRegistry.contains(mem_key):
                memory_backend = MemoryRegistry.create(
                    mem_key,
                    db_path=config.memory.db_path,
                )
                console.print("  Memory:    [cyan]active[/cyan]")
        except Exception as exc:
            logger.debug("Memory backend init failed: %s", exc)

    # Automatic long-term memory service (background fact extraction).
    memory_service = None
    try:
        from bujji.memory import build_memory_service

        memory_service = build_memory_service(
            config,
            engine,
            model_name,
            event_bus=bus,
        )
        if memory_service is not None:
            memory_service.start()
            console.print("  Memory svc: [cyan]active[/cyan]")
    except Exception as exc:
        logger.debug("Memory service init failed: %s", exc)
        memory_service = None

    # Set up agent manager
    agent_manager = None
    if config.agent_manager.enabled:
        try:
            from bujji.agents.manager import AgentManager

            am_db = config.agent_manager.db_path or str(get_config_dir() / "agents.db")
            # The server owns the scheduler and is the authoritative tick
            # runner â€” on boot it holds no locks, so it (and only it) sweeps
            # any zombie runningâ†’idle left by a previous crash.
            agent_manager = AgentManager(db_path=am_db, clear_stale_running=True)
        except Exception as exc:
            logger.debug("Agent manager init failed: %s", exc)

    # Set up agent scheduler for cron/interval agents
    agent_scheduler = None
    if agent_manager is not None:
        try:
            from bujji.agents.executor import AgentExecutor
            from bujji.agents.scheduler import AgentScheduler

            _trace_store = None
            try:
                if config.traces.enabled:
                    from bujji.traces.store import TraceStore

                    _trace_store = TraceStore(db_path=config.traces.db_path)
            except Exception:
                pass

            executor = AgentExecutor(
                manager=agent_manager,
                event_bus=bus,
                trace_store=_trace_store,
            )
            # Reuse the components already built inline above instead of a
            # second full SystemBuilder.build() â€” the original double-build
            # re-discovered the engine, re-instrumented it, re-resolved tools,
            # re-opened the channel and re-created the agent manager, costing
            # ~30-40s on top of an already-paid startup (#263). The executor
            # only reads engine/model/config/memory_backend/tool_executor/
            # session_store/channel_backend from the system (see
            # AgentExecutor), all of which are wired here.
            from bujji.sessions.session import SessionStore
            from bujji.system import BujjiSystem
            from bujji.tools._stubs import ToolExecutor

            _sched_session_store = None
            if config.sessions.enabled:
                try:
                    from pathlib import Path as _SchedPath

                    _sched_session_store = SessionStore(
                        db_path=_SchedPath(config.sessions.db_path).expanduser(),
                        max_age_hours=config.sessions.max_age_hours,
                        consolidation_threshold=(
                            config.sessions.consolidation_threshold
                        ),
                    )
                except Exception as exc:
                    logger.debug("Scheduler session store init failed: %s", exc)

            _sched_tool_executor = (
                ToolExecutor(resolved_tools, bus) if resolved_tools else None
            )

            system = BujjiSystem(
                config=config,
                bus=bus,
                engine=engine,
                engine_key=engine_name,
                model=model_name,
                agent=agent,
                agent_name=agent_key or "",
                tools=resolved_tools,
                tool_executor=_sched_tool_executor,
                memory_backend=memory_backend,
                telemetry_store=telem_store,
                trace_store=_trace_store,
                session_store=_sched_session_store,
                capability_policy=sec.capability_policy,
                agent_manager=agent_manager,
                agent_executor=executor,
            )
            executor.set_system(system)

            agent_scheduler = AgentScheduler(
                manager=agent_manager,
                executor=executor,
                event_bus=bus,
            )
            for ag in agent_manager.list_agents():
                sched_type = ag.get("config", {}).get("schedule_type", "manual")
                if sched_type in ("cron", "interval") and ag["status"] not in (
                    "archived",
                    "error",
                ):
                    agent_scheduler.register_agent(ag["id"])
            agent_scheduler.start()
            console.print("  Scheduler: [cyan]active[/cyan]")
        except Exception as exc:
            logger.debug("Agent scheduler init failed: %s", exc)

    # --- Channel Gateway: API key, sessions, ChannelBridge ---
    import os as _os

    api_key = _os.environ.get("BUJJI_API_KEY", "")
    if not api_key:
        try:
            import tomllib

            _cfg_path = str(get_config_dir() / "config.toml")
            with open(_cfg_path, "rb") as _f:
                _raw = tomllib.load(_f)
            api_key = _raw.get("server", {}).get("auth", {}).get("api_key", "")
        except (FileNotFoundError, ImportError):
            pass

    from bujji.server.auth_middleware import check_bind_safety

    check_bind_safety(bind_host, api_key=api_key)

    # Log credential status at startup
    from bujji.core.credentials import TOOL_CREDENTIALS, get_credential_status

    _cred_parts = []
    for _tool_name in sorted(TOOL_CREDENTIALS):
        _status = get_credential_status(_tool_name)
        _set = sum(1 for v in _status.values() if v)
        _total = len(_status)
        if _set > 0:
            _cred_parts.append(f"{_tool_name}: {_set}/{_total} keys")
    if _cred_parts:
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure  -- logs counts / identifiers / exception type only, never a secret value
        logger.info("Credentials loaded â€” %s", ", ".join(_cred_parts))

    webhook_config = {
        "twilio_auth_token": _os.environ.get("TWILIO_AUTH_TOKEN", ""),
        "bluebubbles_password": _os.environ.get("BLUEBUBBLES_PASSWORD", ""),
        "whatsapp_verify_token": _os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        "whatsapp_app_secret": _os.environ.get("WHATSAPP_APP_SECRET", ""),
    }

    # Wrap existing channel in ChannelBridge orchestrator
    if channel_bridge is not None:
        try:
            from bujji.server.channel_bridge import (
                ChannelBridge,
            )
            from bujji.server.session_store import (
                SessionStore,
            )

            session_store = SessionStore()
            channels = {channel_bridge.channel_id: channel_bridge}
            channel_bridge = ChannelBridge(
                channels=channels,
                session_store=session_store,
                bus=bus,
                system=None,
                agent_manager=agent_manager,
            )
        except Exception as exc:
            logger.debug("ChannelBridge init skipped: %s", exc)

    app = create_app(
        engine,
        model_name,
        agent=agent,
        bus=bus,
        engine_name=engine_name,
        agent_name=agent_key or "",
        channel_bridge=channel_bridge,
        config=config,
        memory_backend=memory_backend,
        memory_service=memory_service,
        speech_backend=speech_backend,
        tts_backend=tts_backend,
        voice_pipeline=voice_pipeline,
        agent_manager=agent_manager,
        agent_scheduler=agent_scheduler,
        api_key=api_key,
        webhook_config=webhook_config,
        cors_origins=config.server.cors_origins,
    )

    console.print(
        f"[green]Starting assistant API server[/green]\n"
        f"  Engine: [cyan]{engine_name}[/cyan]\n"
        f"  Model:  [cyan]{model_name}[/cyan]\n"
        f"  Agent:  [cyan]{agent_key or 'none'}[/cyan]\n"
        f"  URL:    [cyan]http://{bind_host}:{bind_port}[/cyan]"
    )

    # Warn about wildcard CORS on non-loopback
    import ipaddress as _ipa

    try:
        _is_loop = _ipa.ip_address(bind_host).is_loopback
    except ValueError:
        _is_loop = bind_host in ("localhost", "")

    if not _is_loop and "*" in config.server.cors_origins:
        console.print(
            "[yellow bold]WARNING:[/yellow bold] Wildcard CORS with credentials "
            "enabled on non-loopback interface. This allows any website to make "
            "authenticated requests to your instance."
        )

    # Fail loudly if the port is taken (a stale instance keeps serving old
    # code silently otherwise — voice/briefing appear "broken" to the user).
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((bind_host, bind_port))
    except OSError:
        console.print(
            f"[red bold]ERROR:[/red bold] port {bind_port} is already in use — "
            "another Bujji instance is probably still running. Close it (or run "
            f'"Stop-Process" on the python process listening on {bind_port}) and retry.'
        )
        raise SystemExit(1)
    finally:
        probe.close()

    import uvicorn

    # Full visibility: every bujji INFO+ line goes to stdout (→ server.log in
    # desktop mode) with timestamps — voice debugging is impossible blind.
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _bujji_logger = logging.getLogger("bujji")
    _bujji_logger.setLevel(logging.INFO)
    _bujji_logger.addHandler(_h)

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
