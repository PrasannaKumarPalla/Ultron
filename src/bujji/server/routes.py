"""Route handlers for the OpenAI-compatible API server."""

from __future__ import annotations

import logging
import json
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from bujji.core.paths import get_config_dir
from bujji.core.types import Message, Role
from bujji.server.models import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    ComplexityInfo,
    DeltaMessage,
    ModelListResponse,
    ModelObject,
    StreamChoice,
    UsageInfo,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# Cloud vendors are never valid "installed picks" for a local-first setup.
_CLOUD_MODEL_PREFIXES = ("gpt-", "claude-", "gemini-", "o1-", "o3-", "o4-")


def _pick_installed_model(models: list[str]) -> str:
    """Choose the best *already-installed* model: largest local, usable one.

    Excludes cloud-hosted names and embedding/reranker helpers (a library of
    only those has nothing chatty to run). Falls back to ``""`` so callers can
    keep their tier-based recommendation.
    """
    import re as _re

    candidates: list[tuple[float, str]] = []
    for model in models:
        low = model.lower()
        if any(model.startswith(prefix) for prefix in _CLOUD_MODEL_PREFIXES):
            continue
        if "embed" in low or "rerank" in low:
            continue
        match = _re.search(r":(\d+(?:\.\d+)?)b", low)
        candidates.append((float(match.group(1)) if match else 0.0, model))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


@router.get("/v1/setup/status")
async def setup_status(request: Request) -> dict[str, Any]:
    """Return consumer onboarding readiness and hardware recommendation."""
    from bujji.core.config import detect_hardware, recommend_model

    engine_name = getattr(request.app.state, "engine_name", "")
    engine_id = getattr(request.app.state.engine, "engine_id", "")
    if engine_name not in {"", "ollama", "local"} and engine_id != "ollama":
        # A remote/hosted engine (e.g. OmniRoute) needs no local model install;
        # the welcome wizard would strand the user on a "pick a local model" gate.
        return {"ready": True, "config_exists": True, "ollama_ready": True,
                "recommended_model": None, "recommended_installed": True,
                "installed_models": ["auto"], "free_disk_gb": 0.0, "remote": True,
                "hardware": {"cpu": "", "cpu_count": 0, "ram_gb": 0.0,
                             "gpu": engine_name, "vram_gb": 0.0, "gpu_vendor": "remote"}}

    hardware = detect_hardware()
    recommended = recommend_model(hardware, "ollama")
    engine = request.app.state.engine
    ollama_ready = True
    try:
        installed = [str(model) for model in engine.list_models()]
    except Exception:
        installed = []
        ollama_ready = False

    config_dir = get_config_dir()
    disk_root = config_dir if config_dir.exists() else config_dir.parent
    try:
        free_disk_gb = round(shutil.disk_usage(disk_root).free / (1024**3), 1)
    except OSError:
        free_disk_gb = 0.0

    gpu = hardware.gpu
    config_exists = (config_dir / "config.toml").exists()

    # Installed-aware recommendation — but only while the welcome wizard is
    # actually showing (!ready). Configured installs get pure hardware-tier
    # guidance unchanged.
    ready_flag = config_exists and bool(installed)
    if not ready_flag:
        recommended_installed = bool(recommended) and recommended in installed
        if not recommended_installed:
            pick = _pick_installed_model(installed)
            if pick:
                recommended = pick

        # Self-provision: models are present but no config exists yet, so
        # finish setup here with the picked model instead of showing the
        # install nag forever.
        if installed and not config_exists and ollama_ready:
            try:
                from bujji.core.config import generate_minimal_toml

                config_dir.mkdir(parents=True, exist_ok=True)
                target = config_dir / "config.toml"
                target.write_text(
                    generate_minimal_toml(hardware, "ollama", model=recommended or None),
                    encoding="utf-8",
                )
                config_exists = True
                ready_flag = True
                logger.info(
                    "Setup self-provisioned %s using installed model %s",
                    target,
                    recommended or "(engine default)",
                )
            except OSError:
                logger.warning("Could not write default config.toml", exc_info=True)
    else:
        recommended_installed = False

    return {
        "ready": ready_flag,
        "config_exists": config_exists,
        "ollama_ready": ollama_ready,
        "recommended_model": recommended,
        "recommended_installed": recommended_installed,
        "installed_models": installed,
        "free_disk_gb": free_disk_gb,
        "hardware": {
            "cpu": hardware.cpu_brand,
            "cpu_count": hardware.cpu_count,
            "ram_gb": hardware.ram_gb,
            "gpu": gpu.name if gpu else "No dedicated GPU detected",
            "vram_gb": gpu.vram_gb if gpu else 0.0,
            "gpu_vendor": gpu.vendor if gpu else "cpu",
        },
    }


def _to_messages(chat_messages) -> list[Message]:
    """Convert Pydantic ChatMessage objects to core Message objects."""
    messages = []
    for m in chat_messages:
        role = Role(m.role) if m.role in {r.value for r in Role} else Role.USER
        messages.append(
            Message(
                role=role,
                content=m.content or "",
                name=m.name,
                tool_call_id=m.tool_call_id,
            )
        )
    return messages


def _ensure_identity_prompt(messages: list[Message], app_config) -> list[Message]:
    """Prepend Bujji's identity system prompt when the client omits one.

    The desktop UI's chat backend posts only user/assistant turns to
    ``/v1/chat/completions`` (see ``frontend/.../Chat/InputArea.tsx``), so
    nothing grounds the model's identity. Without a system prompt the model
    answers from its training identity (e.g. "I'm Claude", "I am Qwen"),
    which is what #540 reported. The CLI paths inject this via
    ``SystemPromptBuilder`` / ``BaseAgent``; the engine-direct server paths
    did not. This mirrors the agent fallback in ``agents/_stubs.py``.

    If any message already carries a system role, the caller has supplied
    their own grounding and we leave the list untouched (no double-prompting).

    Resolution of the identity text: ``app_config.agent.default_system_prompt``
    when a config is wired onto ``app.state``; otherwise fall back to
    ``load_config()``. Config resolution is wrapped so a broken/missing
    config degrades to "no injection" rather than crashing the endpoint, but
    the failure is logged (per REVIEW.md â€” never silently swallow).
    """
    if any(m.role == Role.SYSTEM for m in messages):
        return messages

    prompt = ""
    try:
        if app_config is not None:
            prompt = app_config.agent.default_system_prompt or ""
        else:
            from bujji.core.config import load_config

            prompt = load_config().agent.default_system_prompt or ""
    except Exception:
        logging.getLogger("bujji.server").debug(
            "Identity system prompt resolution failed; "
            "serving request without identity grounding",
            exc_info=True,
        )
        return messages

    if not prompt:
        return messages

    return [Message(role=Role.SYSTEM, content=prompt), *messages]


@router.post("/v1/chat/completions")
async def chat_completions(request_body: ChatCompletionRequest, request: Request):
    """Handle chat completion requests (streaming and non-streaming)."""
    engine = request.app.state.engine
    agent = getattr(request.app.state, "agent", None)
    model = request_body.model
    # OpenAI-compat clients often send "default" / empty — resolve to the
    # server's configured model instead of passing it to the engine verbatim.
    if not model or model.lower() in ("default", "auto"):
        model = getattr(request.app.state, "model", "") or model

    # Language-aware routing: an Indic-language query (Telugu/Hindi/… script or
    # romanized Tenglish) is sent to the model that actually speaks it. This runs
    # even when the user has manually picked another model in the dropdown —
    # small English-centric locals reply in broken/wrong-language text, and the
    # whole point of typing in Telugu is to be answered in Telugu. Skip the
    # override only if the selected model already IS the Indic model.
    try:
        cfg = getattr(request.app.state, "config", None)
        intel = getattr(cfg, "intelligence", None) if cfg else None
        indic = getattr(intel, "indic_model", "") if intel else ""
        if indic and model != indic:
            from bujji.agents.model_router import is_indic

            last_user = next(
                (m.content for m in reversed(request_body.messages)
                 if m.role == "user" and m.content),
                "",
            )
            if last_user and is_indic(last_user):
                model = indic
    except Exception:
        pass

    # Inject memory context into messages before dispatching
    config = getattr(request.app.state, "config", None)
    memory_backend = getattr(request.app.state, "memory_backend", None)
    if (
        config is not None
        and memory_backend is not None
        and config.agent.context_from_memory
        and request_body.messages
    ):
        try:
            from bujji.tools.storage.context import ContextConfig, inject_context

            # Extract query from the last user message
            query_text = ""
            for m in reversed(request_body.messages):
                if m.role == "user" and m.content:
                    query_text = m.content
                    break

            if query_text:
                messages = _to_messages(request_body.messages)
                ctx_cfg = ContextConfig(
                    top_k=config.memory.context_top_k,
                    min_score=config.memory.context_min_score,
                    max_context_tokens=config.memory.context_max_tokens,
                )
                enriched = inject_context(
                    query_text,
                    messages,
                    memory_backend,
                    config=ctx_cfg,
                )
                # Rebuild request messages from enriched Message objects
                if len(enriched) > len(messages):
                    from bujji.server.models import ChatMessage

                    new_msgs = []
                    for msg in enriched:
                        new_msgs.append(
                            ChatMessage(
                                role=msg.role.value,
                                content=msg.content,
                                name=msg.name,
                                tool_call_id=getattr(msg, "tool_call_id", None),
                            )
                        )
                    request_body.messages = new_msgs
        except Exception:
            logging.getLogger("bujji.server").debug(
                "Memory context injection failed",
                exc_info=True,
            )

    # Run complexity analysis on the last user message
    complexity_info = None
    query_text_for_complexity = ""
    for m in reversed(request_body.messages):
        if m.role == "user" and m.content:
            query_text_for_complexity = m.content
            break
    if query_text_for_complexity:
        try:
            from bujji.learning.routing.complexity import (
                adjust_tokens_for_model,
                score_complexity,
            )

            cr = score_complexity(query_text_for_complexity)
            suggested = adjust_tokens_for_model(
                cr.suggested_max_tokens,
                model,
            )
            complexity_info = ComplexityInfo(
                score=cr.score,
                tier=cr.tier,
                suggested_max_tokens=suggested,
            )
            # Bump max_tokens when complexity suggests more than what
            # the client requested â€” never reduce below the request value.
            if suggested > request_body.max_tokens:
                request_body.max_tokens = suggested
        except Exception:
            logging.getLogger("bujji.server").debug(
                "Complexity analysis failed",
                exc_info=True,
            )

    if request_body.stream:
        # When the client passes `tools`, stream the model's raw
        # OpenAI-compat function-calling decision directly from the engine
        # (bypassing the agent) â€” the streaming mirror of the non-streaming
        # #454 fix.  Routing tools through the agent stream bridge ignored
        # `request_body.tools`, ran the agent's own tool loop, and
        # word-split generic filler content into fake token deltas, so the
        # caller's tool_calls were dropped entirely (the streaming analog of
        # #414).  For plain chat (no tools), stream token-by-token directly
        # from the engine for true real-time output.
        if request_body.tools:
            return await _handle_stream_tools(
                engine,
                model,
                request_body,
                complexity_info,
                app_config=config,
                bus=getattr(request.app.state, "bus", None),
                memory_service=getattr(request.app.state, "memory_service", None),
            )
        return await _handle_stream(
            engine,
            model,
            request_body,
            complexity_info,
            trace_store=getattr(request.app.state, "trace_store", None),
            app_config=config,
            bus=getattr(request.app.state, "bus", None),
            memory_service=getattr(request.app.state, "memory_service", None),
        )

    # Non-streaming: use agent if available, otherwise direct engine call.
    #
    # EXCEPTION: when the client explicitly passed `tools`, they're asking
    # for raw OpenAI-compat function-calling â€” return the model's
    # tool_call decision verbatim. Routing through `_handle_agent` would
    # call `agent.run(input_text)`, which IGNORES `request_body.tools`,
    # runs the agent's own internal tool loop with its own (different)
    # tool spec, and returns only `result.content` â€” so the model's
    # tool_calls vanish and the user sees a generic acknowledgement
    # (e.g. "Understood. If you have another request...") that the
    # agent's re-prompted LLM produced. See #414.
    #
    # If a future caller needs agent orchestration WITH client-supplied
    # tools (e.g. injecting MCP tools through this endpoint and wanting
    # the agent to execute them), add an explicit opt-in header rather
    # than removing this guard â€” silent re-routing is what produced #414.
    if agent is not None and not request_body.tools:
        response = _handle_agent(
            agent,
            model,
            request_body,
            complexity_info,
            trace_store=getattr(request.app.state, "trace_store", None),
            bus=getattr(request.app.state, "bus", None),
        )
    else:
        bus = getattr(request.app.state, "bus", None)
        response = _handle_direct(
            engine,
            model,
            request_body,
            bus=bus,
            complexity_info=complexity_info,
            app_config=config,
        )

    # Hand the completed exchange to the background memory service.
    _remember_exchange(
        getattr(request.app.state, "memory_service", None),
        query_text_for_complexity,
        response,
        bus=getattr(request.app.state, "bus", None),
        source="server.chat",
    )
    return response


def _response_content(response) -> str:
    """Extract assistant text from an OpenAI-compatible response object."""
    content = ""
    choices = getattr(response, "choices", None)
    if choices:
        content = getattr(choices[0].message, "content", "") or ""
    return content


def _record_completed_exchange(
    memory_service,
    user_text: str,
    assistant_text: str,
    *,
    bus=None,
    source: str = "server.chat",
) -> None:
    """Publish or submit a completed exchange without blocking a reply."""
    if not user_text:
        return
    try:
        if bus is not None:
            from bujji.memory import publish_completed_exchange

            publish_completed_exchange(
                bus,
                user_text,
                assistant_text,
                source=source,
            )
        elif memory_service is not None:
            memory_service.submit(user_text, assistant_text)
    except Exception:  # noqa: BLE001 â€” memory is best-effort, never fail a reply
        logging.getLogger("bujji.server").debug(
            "Memory submit failed",
            exc_info=True,
        )


def _obsidian_save_exchange(user_text: str, assistant_text: str, app_config) -> None:
    """Append a Q&A exchange to today's Obsidian daily note (best-effort, non-blocking)."""
    try:
        cfg = getattr(app_config, "connectors", None)
        obs_cfg = getattr(cfg, "obsidian", None)
        if obs_cfg is None or not getattr(obs_cfg, "enabled", False):
            return
        vault_path = getattr(obs_cfg, "vault_path", "")
        notes_folder = getattr(obs_cfg, "notes_folder", "Bujji")
        if not vault_path:
            return

        from bujji.connectors.obsidian import ObsidianConnector
        import datetime as _dt

        conn = ObsidianConnector(vault_path=vault_path)
        if not conn.is_connected():
            return

        today = _dt.date.today().isoformat()  # e.g. "2026-06-29"
        title = f"Chat {today}"
        # Trim very long responses to keep notes readable
        MAX = 2000
        response_snippet = assistant_text if len(assistant_text) <= MAX else assistant_text[:MAX] + "…"
        block = f"**You:** {user_text.strip()}\n\n**Bujji:** {response_snippet.strip()}\n\n---"
        conn.append_note(title, block, folder=notes_folder)
    except Exception:
        logging.getLogger("bujji.server").debug("Obsidian auto-save failed", exc_info=True)


def _remember_exchange(
    memory_service,
    user_text: str,
    response,
    *,
    bus=None,
    source: str = "server.chat",
) -> None:
    """Record a completed non-streaming exchange."""
    _record_completed_exchange(
        memory_service,
        user_text,
        _response_content(response),
        bus=bus,
        source=source,
    )


def _handle_direct(
    engine,
    model: str,
    req: ChatCompletionRequest,
    bus=None,
    complexity_info=None,
    app_config=None,
) -> ChatCompletionResponse:
    """Direct engine call without agent."""
    messages = _to_messages(req.messages)
    messages = _ensure_identity_prompt(messages, app_config)
    kwargs: dict[str, Any] = {}
    if req.tools:
        kwargs["tools"] = req.tools
    if bus:
        from bujji.telemetry.instrumented_engine import InstrumentedEngine
        from bujji.telemetry.wrapper import instrumented_generate

        # `app.state.engine` may already be an InstrumentedEngine (the
        # common case when telemetry is wired in). If we then wrap it
        # with `instrumented_generate`, BOTH layers fire a
        # TELEMETRY_RECORD per call:
        #
        #   - InstrumentedEngine.generate() publishes a FULL record
        #     (energy_joules, GPU stats, token_counting_version, ...).
        #   - instrumented_generate() publishes a BARE record (timing +
        #     tokens only; no energy meter, no version stamp).
        #
        # The doubled count was the dominant driver of the bimodal
        # Wh/token distribution on the public leaderboard.
        #
        # The fix below is NOT "unwrap and call instrumented_generate":
        # that would have replaced "doubled records" with "every
        # request emits only a bare record with no energy / no version",
        # which the leaderboard's `current_methodology_only=True` filter
        # would then drop entirely. Instead, when the engine is already
        # an InstrumentedEngine, skip the wrapper and call `generate`
        # directly â€” InstrumentedEngine publishes the full per-record
        # event itself with energy + version intact. Only fall back to
        # the lightweight wrapper for engines that aren't already
        # instrumented.
        if isinstance(engine, InstrumentedEngine):
            result = engine.generate(
                messages,
                model=model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                **kwargs,
            )
        else:
            result = instrumented_generate(
                engine,
                messages,
                model=model,
                bus=bus,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                **kwargs,
            )
    else:
        result = engine.generate(
            messages,
            model=model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            **kwargs,
        )
    content = result.get("content", "")
    usage = result.get("usage", {})

    choice_msg = ChoiceMessage(role="assistant", content=content)
    # Include tool calls if present
    tool_calls = result.get("tool_calls")
    if tool_calls:
        choice_msg.tool_calls = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", "{}"),
                },
            }
            for tc in tool_calls
        ]

    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                message=choice_msg,
                finish_reason=result.get("finish_reason", "stop"),
            )
        ],
        usage=UsageInfo(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        ),
        complexity=complexity_info,
    )


def _handle_agent(
    agent,
    model: str,
    req: ChatCompletionRequest,
    complexity_info=None,
    *,
    trace_store=None,
    bus=None,
) -> ChatCompletionResponse:
    """Run through agent.

    When *trace_store* is set, the agent run is wrapped in a
    ``TraceCollector`` (mirroring ``system/orchestrator.py``) so every
    completion records a ``Trace`` to ``traces.db``. Previously this endpoint
    called ``agent.run()`` raw, so the server never produced traces:
    ``traces.db`` stayed empty and spec_search's cold-start gate
    (``check_readiness``, min 20 traces) could never open.
    """
    from bujji.agents._stubs import AgentContext

    # Build context from prior messages
    ctx = AgentContext()
    if len(req.messages) > 1:
        prior = _to_messages(req.messages[:-1])
        for m in prior:
            ctx.conversation.add(m)

    # Last message is the input
    input_text = req.messages[-1].content if req.messages else ""

    # Override agent model for this request if the caller specified one
    original_model = agent._model
    if model:
        agent._model = model
    try:
        if trace_store is not None:
            from bujji.traces.collector import TraceCollector

            collector = TraceCollector(agent, store=trace_store, bus=bus)
            result = collector.run(input_text, context=ctx)
        else:
            result = agent.run(input_text, context=ctx)
    finally:
        agent._model = original_model

    try:
        from bujji.connectors.obsidian_journal import log_exchange

        log_exchange(input_text, result.content or "", source="chat")
    except Exception:
        pass

    usage = UsageInfo(
        prompt_tokens=result.metadata.get("prompt_tokens", 0),
        completion_tokens=result.metadata.get("completion_tokens", 0),
        total_tokens=result.metadata.get("total_tokens", 0),
    )

    # Include audio metadata if the agent produced audio (e.g. morning digest)
    audio_meta = None
    audio_path = result.metadata.get("audio_path", "")
    if audio_path:
        from pathlib import Path

        from bujji.server.models import AudioMeta

        if Path(audio_path).exists():
            audio_meta = AudioMeta(url="/api/digest/audio")

    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                message=ChoiceMessage(
                    role="assistant",
                    content=result.content,
                    audio=audio_meta,
                ),
                finish_reason="stop",
            )
        ],
        usage=usage,
        complexity=complexity_info,
    )


async def _handle_stream_tools(
    engine,
    model: str,
    req: ChatCompletionRequest,
    complexity_info=None,
    *,
    app_config=None,
    bus=None,
    memory_service=None,
):
    """Stream a raw OpenAI-compat function-calling response via SSE.

    Used when the client passes `tools` together with `stream:true`.  Sources
    tool_calls from ``engine.stream_full()`` (which forwards the tools to the
    backend and parses tool_calls out of the streamed response) and emits them
    as SSE deltas, bypassing the agent entirely.  This is the streaming mirror
    of the non-streaming ``_handle_direct`` tool path.

    Engines without a tool-aware ``stream_full`` override fall back to the
    base-class default (content tokens + a ``stop`` finish_reason, no
    tool_calls) â€” identical to the prior plain-stream behaviour, so this never
    regresses non-tool-capable engines.
    """
    from bujji.server.cloud_router import is_cloud_model

    messages = _to_messages(req.messages)
    messages = _ensure_identity_prompt(messages, app_config)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    use_cloud = is_cloud_model(model)
    query_text = ""
    for _m in reversed(req.messages):
        if _m.role == "user" and _m.content:
            query_text = _m.content
            break

    async def generate():
        full_content = ""
        # Send the role chunk first (OpenAI convention).
        first_chunk = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[StreamChoice(delta=DeltaMessage(role="assistant"))],
        )
        yield f"data: {first_chunk.model_dump_json()}\n\n"

        finish_reason = "stop"
        try:
            async for sc in engine.stream_full(
                messages,
                model=model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                tools=req.tools,
            ):
                if sc.content:
                    full_content += sc.content
                    content_chunk = ChatCompletionChunk(
                        id=chunk_id,
                        model=model,
                        choices=[StreamChoice(delta=DeltaMessage(content=sc.content))],
                    )
                    yield f"data: {content_chunk.model_dump_json()}\n\n"
                if sc.tool_calls:
                    tc_chunk = ChatCompletionChunk(
                        id=chunk_id,
                        model=model,
                        choices=[
                            StreamChoice(delta=DeltaMessage(tool_calls=sc.tool_calls))
                        ],
                    )
                    yield f"data: {tc_chunk.model_dump_json()}\n\n"
                if sc.finish_reason:
                    finish_reason = sc.finish_reason
        except Exception as exc:
            import logging

            logging.getLogger("bujji.server").error(
                "Tool stream error: %s",
                exc,
                exc_info=True,
            )
            error_chunk = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[
                    StreamChoice(
                        delta=DeltaMessage(
                            content=f"\n\nError during generation: {exc}",
                        ),
                        finish_reason="stop",
                    )
                ],
            )
            yield f"data: {error_chunk.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
            return

        import json as _json

        finish_data = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[StreamChoice(delta=DeltaMessage(), finish_reason=finish_reason)],
        )
        finish_dict = _json.loads(finish_data.model_dump_json())
        # Tag the finish chunk with the engine label, matching _handle_stream
        # so UI/telemetry consumers see the same field on the tools path.
        finish_dict.setdefault("telemetry", {})
        finish_dict["telemetry"]["engine"] = "cloud" if use_cloud else "ollama"
        if complexity_info is not None:
            finish_dict["complexity"] = complexity_info.model_dump()
        yield f"data: {_json.dumps(finish_dict)}\n\n"
        if full_content:
            _record_completed_exchange(
                memory_service,
                query_text,
                full_content,
                bus=bus,
                source="server.chat.stream",
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


async def _handle_stream(
    engine,
    model: str,
    req: ChatCompletionRequest,
    complexity_info=None,
    *,
    trace_store=None,
    app_config=None,
    bus=None,
    memory_service=None,
):
    """Stream response using SSE format.

    This path streams straight from the engine, bypassing the agent /
    ``TraceCollector``. When *trace_store* is set we accumulate the streamed
    tokens and record a minimal ``Trace`` once the stream completes
    successfully â€” otherwise streamed chats (the desktop GUI's main path)
    would never populate ``traces.db``.
    """
    import time

    from bujji.server.cloud_router import (
        is_cloud_model,
        stream_cloud,
        stream_local,
    )

    messages = _to_messages(req.messages)
    messages = _ensure_identity_prompt(messages, app_config)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Last user message â€” recorded as the trace query.
    query_text = ""
    for _m in reversed(req.messages):
        if _m.role == "user" and _m.content:
            query_text = _m.content
            break

    # Route directly to the right backend â€” bypasses engine routing entirely
    # so broken MultiEngine state can never misdirect requests.
    use_cloud = is_cloud_model(model)

    async def generate():
        started_at = time.time()
        full_content = ""
        # Send role chunk first
        first_chunk = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(role="assistant"),
                )
            ],
        )
        yield f"data: {first_chunk.model_dump_json()}\n\n"

        try:
            # Cloud models â†’ direct cloud API (reads keys from disk).
            # Local models â†’ engine.stream() first so mock engines work in
            # tests.  Fall back to stream_local() only when the engine would
            # mis-route the request to a cloud backend (MultiEngine routing
            # confusion), which is detected by checking the routed engine's
            # is_cloud attribute.
            if use_cloud:
                token_iter = stream_cloud(
                    model, messages, req.temperature, req.max_tokens
                )
            else:
                # Use engine.stream() by default (preserves mock-engine
                # compatibility in tests).  Only fall back to stream_local()
                # when a real MultiEngine would mis-route the local model to a
                # cloud backend â€” detected via isinstance so mocks are not
                # accidentally matched.
                _use_local_fallback = False
                try:
                    from bujji.engine.multi import MultiEngine

                    _inner = getattr(engine, "_inner", engine)
                    if isinstance(_inner, MultiEngine):
                        _routed = _inner._engine_for(model)
                        if _routed is not None and getattr(_routed, "is_cloud", False):
                            _use_local_fallback = True
                except Exception:
                    pass
                if _use_local_fallback:
                    token_iter = stream_local(
                        model, messages, req.temperature, req.max_tokens
                    )
                else:
                    token_iter = engine.stream(
                        messages,
                        model=model,
                        temperature=req.temperature,
                        max_tokens=req.max_tokens,
                    )
            async for token in token_iter:
                full_content += token
                chunk = ChatCompletionChunk(
                    id=chunk_id,
                    model=model,
                    choices=[
                        StreamChoice(
                            delta=DeltaMessage(content=token),
                        )
                    ],
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
        except Exception as exc:
            # Surface errors as a content chunk so the frontend can
            # display them instead of silently failing.
            import logging

            logging.getLogger("bujji.server").error(
                "Stream error: %s",
                exc,
                exc_info=True,
            )
            error_chunk = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[
                    StreamChoice(
                        delta=DeltaMessage(
                            content=f"\n\nError during generation: {exc}",
                        ),
                        finish_reason="stop",
                    )
                ],
            )
            yield f"data: {error_chunk.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Record a trace for the completed stream (best-effort; never breaks
        # the response). Mirrors the agent path so streamed chats also
        # populate traces.db.
        if trace_store is not None and full_content:
            from bujji.traces.collector import record_response_trace

            record_response_trace(
                trace_store,
                query=query_text,
                result=full_content,
                model=model,
                engine="cloud" if use_cloud else "ollama",
                started_at=started_at,
                ended_at=time.time(),
            )

        if full_content:
            _record_completed_exchange(
                memory_service,
                query_text,
                full_content,
                bus=bus,
                source="server.chat.stream",
            )
            # Auto-save every exchange to Obsidian (background, never blocks stream)
            _obsidian_save_exchange(query_text, full_content, app_config)

        # Send finish chunk with usage data if available
        import json as _json

        finish_data = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(),
                    finish_reason="stop",
                )
            ],
        )
        finish_dict = _json.loads(finish_data.model_dump_json())

        # Tag the finish chunk with the correct engine label.
        # We use the routing decision (use_cloud) directly rather than
        # unwrapping the engine chain, which can be in a broken state.
        finish_dict.setdefault("telemetry", {})
        finish_dict["telemetry"]["engine"] = "cloud" if use_cloud else "ollama"

        if complexity_info is not None:
            finish_dict["complexity"] = complexity_info.model_dump()

        yield f"data: {_json.dumps(finish_dict)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/v1/provider")
async def provider_get(request: Request) -> dict[str, Any]:
    """Report the current LLM provider selection (Local vs OmniRoute)."""
    from bujji.server.provider import provider_payload

    return await provider_payload()


@router.put("/v1/provider")
async def provider_put(request: Request) -> dict[str, Any]:
    """Switch the LLM provider: ``{"mode": "local" | "hosted" | "auto"}``."""
    from bujji.server.provider import BadProviderMode, provider_payload

    body = await request.json()
    try:
        return await provider_payload(apply_mode=body.get("mode"))
    except BadProviderMode as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/models")
async def list_models(request: Request) -> ModelListResponse:
    """List locally installed models (Ollama).

    Cloud models are not included here â€” they live in the Cloud Models tab
    of the UI and are selected there, not from this endpoint.
    """
    from bujji.server.cloud_router import is_cloud_model, list_local_models

    # Prefer engine.list_models() so mock engines work in tests.
    # Filter out any cloud model IDs that may appear via MultiEngine.
    # Fall back to direct Ollama query only when the engine returns nothing.
    engine = request.app.state.engine
    engine_name = getattr(request.app.state, "engine_name", "")
    if engine_name not in {"", "ollama", "local"} and getattr(engine, "engine_id", "") != "ollama":
        # Remote engine (e.g. OmniRoute): routing is automatic, there is no
        # local model to pick. Don't fall back to the Ollama list.
        try:
            remote = [m for m in engine.list_models() if not is_cloud_model(m)]
        except Exception:
            remote = []
        return ModelListResponse(data=[ModelObject(id=mid) for mid in (remote or ["auto"])])

    all_ids = engine.list_models()
    model_ids = [m for m in all_ids if not is_cloud_model(m)]
    if not model_ids:
        model_ids = await list_local_models()

    return ModelListResponse(
        data=[ModelObject(id=mid) for mid in model_ids],
    )


@router.post("/v1/models/pull")
async def pull_model(request: Request):
    """Pull / download a model from the Ollama registry."""
    body = await request.json()
    model_name = body.get("model", "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="'model' field is required")

    engine = request.app.state.engine
    engine_name = getattr(request.app.state, "engine_name", "")
    # Only Ollama supports pulling
    if engine_name != "ollama" and getattr(engine, "engine_id", "") != "ollama":
        raise HTTPException(
            status_code=501,
            detail="Model pulling is only supported with the Ollama engine",
        )

    import httpx as _httpx

    host = getattr(engine, "_host", "http://localhost:11434")
    client = _httpx.Client(base_url=host, timeout=600.0)
    try:
        resp = client.post(
            "/api/pull",
            json={"name": model_name, "stream": False},
        )
        resp.raise_for_status()
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        raise HTTPException(status_code=502, detail=f"Ollama unreachable: {exc}")
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Ollama error: {exc.response.text[:300]}",
        )
    finally:
        client.close()

    return {"status": "ok", "model": model_name}


async def _ollama_pull_progress(host, model_name, downloading, ready, failed):
    """Yield SSE progress frames and maintain durable model-state markers."""
    import asyncio
    import httpx as _httpx

    downloading.write_text(
        json.dumps({"model": model_name, "status": "starting", "percent": 0})
    )
    ready.unlink(missing_ok=True)
    failed.unlink(missing_ok=True)
    try:
        timeout = _httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        async with _httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{host}/api/pull",
                json={"name": model_name, "stream": True},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    payload = json.loads(line)
                    # Registry failures arrive as {"error": "..."} frames with
                    # NO status key — without this check they masquerade as
                    # progress and the download falsely reports "ready".
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    total = int(payload.get("total") or 0)
                    completed = int(payload.get("completed") or 0)
                    percent = round(completed * 100 / total, 1) if total else 0.0
                    event = {
                        "model": model_name,
                        "status": payload.get("status", "downloading"),
                        "total": total,
                        "completed": completed,
                        "percent": percent,
                    }
                    downloading.write_text(json.dumps(event))
                    yield f"data: {json.dumps(event)}\n\n"

        downloading.unlink(missing_ok=True)
        ready.write_text(json.dumps({"model": model_name, "status": "ready"}))
        yield f"data: {json.dumps({'model': model_name, 'status': 'ready', 'percent': 100})}\n\n"
    except asyncio.CancelledError:
        downloading.write_text(
            json.dumps({"model": model_name, "status": "paused", "percent": 0})
        )
        raise
    except Exception as exc:
        downloading.unlink(missing_ok=True)
        failed.write_text(str(exc)[:1000])
        error = {"model": model_name, "status": "failed", "error": str(exc)[:300]}
        yield f"data: {json.dumps(error)}\n\n"


@router.post("/v1/models/pull/stream")
async def pull_model_stream(request: Request):
    """Stream Ollama model-download progress as server-sent events.

    Ollama resumes already-downloaded blobs, so retrying this endpoint after a
    disconnect or app restart continues rather than discarding prior work.
    """
    body = await request.json()
    model_name = str(body.get("model", "")).strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="'model' field is required")

    engine = request.app.state.engine
    engine_name = getattr(request.app.state, "engine_name", "")
    if engine_name != "ollama" and getattr(engine, "engine_id", "") != "ollama":
        raise HTTPException(status_code=501, detail="Model pulling requires Ollama")

    from bujji.cli._bg_state import model_marker_path
    from bujji.cli.model import find_model_spec
    from bujji.core.config import estimated_download_gb
    from bujji.core.paths import get_config_dir

    state_dir = get_config_dir() / ".state" / "models"
    state_dir.mkdir(parents=True, exist_ok=True)
    downloading = model_marker_path(state_dir, model_name, "downloading")
    ready = model_marker_path(state_dir, model_name, "ready")
    failed = model_marker_path(state_dir, model_name, "failed")

    spec = find_model_spec(model_name)
    estimated_gb = (
        estimated_download_gb(spec.parameter_count_b, spec.model_id) if spec else 0.0
    )
    free_gb = shutil.disk_usage(state_dir).free / (1024**3)
    required_gb = estimated_gb * 1.2 + 2.0
    if estimated_gb and free_gb < required_gb:
        raise HTTPException(
            status_code=507,
            detail=(
                f"Not enough free disk space. {model_name} needs about "
                f"{required_gb:.1f} GB including safety margin; {free_gb:.1f} GB is free."
            ),
        )

    host = getattr(engine, "_host", "http://localhost:11434").rstrip("/")

    return StreamingResponse(
        _ollama_pull_progress(host, model_name, downloading, ready, failed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/v1/models/{model_name:path}")
async def delete_model(model_name: str, request: Request):
    """Delete a model from Ollama."""
    engine = request.app.state.engine
    engine_name = getattr(request.app.state, "engine_name", "")
    if engine_name != "ollama" and getattr(engine, "engine_id", "") != "ollama":
        raise HTTPException(status_code=501, detail="Only supported with Ollama engine")

    import httpx as _httpx

    host = getattr(engine, "_host", "http://localhost:11434")
    client = _httpx.Client(base_url=host, timeout=30.0)
    try:
        resp = client.request(
            "DELETE",
            "/api/delete",
            json={"name": model_name},
        )
        resp.raise_for_status()
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        raise HTTPException(status_code=502, detail=f"Ollama unreachable: {exc}")
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Ollama error: {exc.response.text[:300]}",
        )
    finally:
        client.close()

    return {"status": "deleted", "model": model_name}


@router.post("/v1/cloud/reload")
async def reload_cloud_engine(request: Request):
    """Hot-reload cloud API keys and (re-)initialize the cloud engine.

    Called by the desktop app immediately after the user saves a cloud API
    key so that cloud models become available without a full app restart.
    """
    import os

    submitted_keys: dict[str, str] | None = None
    try:
        body = await request.json()
        raw_keys = body.get("keys") if isinstance(body, dict) else None
        if isinstance(raw_keys, dict):
            submitted_keys = {
                str(k): str(v)
                for k, v in raw_keys.items()
                if str(k).endswith("_API_KEY")
            }
    except Exception:
        submitted_keys = None

    if submitted_keys is not None:
        for key, value in submitted_keys.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
    else:
        # Compatibility fallback for non-desktop/manual configurations.
        keys_path = get_config_dir() / "cloud-keys.env"
        if keys_path.exists():
            for raw_line in keys_path.read_text().splitlines():
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

    # Try to build a fresh CloudEngine.
    try:
        from bujji.engine.cloud import CloudEngine
        from bujji.engine.multi import MultiEngine

        cloud = CloudEngine()
        if not cloud.health():
            return {
                "status": "no_cloud",
                "message": "No cloud models available (check API keys)",
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    # Locate the innermost engine, working through InstrumentedEngine layers.
    outer = request.app.state.engine
    inner = getattr(outer, "_inner", outer)

    if isinstance(inner, MultiEngine):
        # Replace or insert the cloud entry in the existing MultiEngine.
        new_engines = [(k, e) for k, e in inner._engines if k != "cloud"]
        new_engines.append(("cloud", cloud))
        inner._engines = new_engines
        inner._refresh_map()
    else:
        # Wrap the existing engine (which may be security-wrapped) with a new
        # MultiEngine that includes the cloud engine.
        engine_name = getattr(request.app.state, "engine_name", "local")
        new_multi = MultiEngine([(engine_name, inner), ("cloud", cloud)])
        if hasattr(outer, "_inner"):
            outer._inner = new_multi
        else:
            request.app.state.engine = new_multi
        request.app.state.engine_name = "multi"

    return {"status": "ok", "message": "Cloud engine reloaded"}


@router.get("/v1/savings")
async def savings(request: Request):
    """Return savings summary compared to cloud providers.

    Only includes telemetry from the current server session so that
    counters start at zero each time a new model + agent is launched.
    """
    from bujji.core.config import DEFAULT_CONFIG_DIR
    from bujji.server.savings import compute_savings, savings_to_dict
    from bujji.telemetry.aggregator import TelemetryAggregator

    db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
    if not db_path.exists():
        empty = compute_savings(0, 0, 0)
        return savings_to_dict(empty)

    session_start = getattr(request.app.state, "session_start", None)

    agg = TelemetryAggregator(db_path)
    try:
        # current_methodology_only excludes pre-fix legacy rows from
        # the leaderboard's per-token efficiency numerator/denominator
        # â€” see the comment on _time_filter for the bimodal-Wh/token
        # background.
        summary = agg.summary(since=session_start, current_methodology_only=True)
        # Exclude cloud model tokens from savings â€” only local
        # inference counts toward cost savings.
        _cloud_prefixes = (
            "gpt-",
            "o1-",
            "o3-",
            "o4-",
            "claude-",
            "gemini-",
            "openrouter/",
        )
        local_models = [
            m
            for m in summary.per_model
            if not any(m.model_id.startswith(p) for p in _cloud_prefixes)
        ]
        result = compute_savings(
            prompt_tokens=sum(m.prompt_tokens for m in local_models),
            completion_tokens=sum(m.completion_tokens for m in local_models),
            total_calls=sum(m.call_count for m in local_models),
            session_start=session_start if session_start else 0.0,
            prompt_tokens_evaluated=sum(
                m.prompt_tokens_evaluated for m in local_models
            ),
        )
        return savings_to_dict(result)
    finally:
        agg.close()


@router.post("/v1/telemetry/reset")
async def reset_telemetry():
    """Clear all stored telemetry records.

    Useful after updating token-counting methodology â€” clears
    historical records that were computed under the old rules so
    that the savings dashboard and leaderboard submissions start
    fresh with corrected values.
    """
    from bujji.core.config import DEFAULT_CONFIG_DIR
    from bujji.telemetry.aggregator import TelemetryAggregator

    db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
    if not db_path.exists():
        return {"status": "ok", "records_cleared": 0}

    agg = TelemetryAggregator(db_path)
    try:
        count = agg.clear()
    finally:
        agg.close()
    return {"status": "ok", "records_cleared": count}


@router.get("/v1/info")
async def server_info(request: Request):
    """Return server configuration: model, agent, engine."""
    agent = getattr(request.app.state, "agent", None)
    agent_id = getattr(agent, "agent_id", None) if agent else None
    # Fall back to configured agent name if agent didn't instantiate
    if agent_id is None:
        agent_id = getattr(request.app.state, "agent_name", None)
    branding = getattr(request.app.state, "branding", None)
    try:
        from bujji.server.provider import provider_payload

        provider = await provider_payload()
    except Exception:
        provider = {}
    return {
        "model": getattr(request.app.state, "model", ""),
        "agent": agent_id,
        "engine": getattr(request.app.state, "engine_name", ""),
        "provider": provider,
        "branding": {
            "product_name": getattr(branding, "product_name", "Assistant"),
            "display_name": getattr(branding, "display_name", "Assistant"),
            "tagline": getattr(branding, "tagline", ""),
            "command_name": getattr(branding, "command_name", "assistant"),
            "wake_word": getattr(branding, "wake_word", "assistant"),
        },
    }


@router.get("/api/youtube/resolve")
async def youtube_resolve(q: str):
    """Return the first YouTube video URL for a search query (non-blocking)."""
    import asyncio
    import re
    import urllib.request
    import urllib.parse

    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"

    def _fetch() -> str:
        try:
            req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  -- fixed host / operator-configured model endpoint; URL is not request-derived
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            if match:
                return f"https://www.youtube.com/watch?v={match.group(1)}&autoplay=1"
        except Exception:
            pass
        return search_url

    url = await asyncio.to_thread(_fetch)
    return {"url": url}


@router.get("/health")
async def health(request: Request):
    """Health check endpoint."""
    engine = request.app.state.engine
    healthy = engine.health()
    if not healthy:
        raise HTTPException(status_code=503, detail="Engine unhealthy")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Channel endpoints
# ---------------------------------------------------------------------------


@router.get("/v1/channels")
async def list_channels(request: Request):
    """List available messaging channels."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        return {"channels": [], "message": "Channel bridge not configured"}
    channels = bridge.list_channels()
    return {"channels": channels, "status": bridge.status().value}


@router.post("/v1/channels/send")
async def channel_send(request: Request):
    """Send a message to a channel."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="Channel bridge not configured")

    body = await request.json()
    channel_name = body.get("channel", "")
    content = body.get("content", "")
    conversation_id = body.get("conversation_id", "")

    if not channel_name or not content:
        raise HTTPException(
            status_code=400,
            detail="'channel' and 'content' are required",
        )

    ok = bridge.send(channel_name, content, conversation_id=conversation_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to send message")
    return {"status": "sent", "channel": channel_name}


@router.get("/v1/channels/status")
async def channel_status(request: Request):
    """Return channel bridge connection status."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        return {"status": "not_configured"}
    return {"status": bridge.status().value}


# ---------------------------------------------------------------------------
# Security scan endpoint
# ---------------------------------------------------------------------------


@router.get("/v1/security/scan")
async def security_scan():
    """Run a read-only security environment audit and return findings."""
    from bujji.cli.scan_cmd import PrivacyScanner

    scanner = PrivacyScanner()
    results = scanner.run_all()
    return {
        "has_warnings": any(r.status == "warn" for r in results),
        "has_failures": any(r.status == "fail" for r in results),
        "findings": [
            {
                "name": r.name,
                "status": r.status,
                "message": r.message,
                "platform": r.platform,
            }
            for r in results
        ],
    }


__all__ = ["router"]
