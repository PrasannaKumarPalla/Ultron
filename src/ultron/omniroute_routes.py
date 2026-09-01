"""HTTP surface for the OmniRoute integration (Phases 0-5).

All state flows through OmniRouteRuntime so privacy mode, cost pause and
consent rules cannot be bypassed by an individual route.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Repository
from .redaction import dry_run
from .omniroute_runtime import OmniRouteRuntime, get_runtime

router = APIRouter(prefix="/api/omniroute", tags=["omniroute"])


def runtime(settings: Settings = Depends(get_settings)) -> OmniRouteRuntime:
    current = get_runtime(settings)
    if current.settings.database_path != Path(settings.database_path):
        from .omniroute_runtime import reset_runtime
        reset_runtime()
        current = get_runtime(settings)
    return current


class InstallRequest(BaseModel):
    preference: str = "auto"  # auto | docker | npm


class SidecarConfigUpdate(BaseModel):
    enabled: bool | None = None
    install_preference: str | None = None
    providers: list[str] | None = None
    combos: list[str] | None = None
    quota_share_policy: str | None = None
    compression: bool | None = None


class RouterModeRequest(BaseModel):
    mode: str = Field(pattern="^(local|hosted|auto)$")


class PrivacyRequest(BaseModel):
    enabled: bool


class ConsentRequest(BaseModel):
    repo_path: str = Field(min_length=1)
    accept: bool


class HireRequest(BaseModel):
    profile: str = Field(pattern="^(coding|reasoning|chat)$")
    mode: str = Field(default="auto", pattern="^(local|hosted|auto)$")


class RedactionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


class ChatRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    tools: list[dict] | None = None
    format: dict | None = None
    stream: bool = True
    mode: str = Field(default="auto", pattern="^(local|hosted|auto)$")
    run_id: str | None = None
    repo_path: str | None = None


@router.get("/status")
async def status(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    healthy = await rt.sidecar.healthy()
    payload = {
        **rt.sidecar.status(),
        "healthy": healthy,
        "privacy_mode": rt.privacy_enabled(),
        "pinned_local": rt._setting("pinned_local") == "1",
        "router_mode": rt.router.default_mode,
        "hosted_paused": rt.hosted_paused(),
        "cooldowns_s": rt.cooldowns.snapshot(),
    }
    free_tiers = None if not healthy else await rt.sidecar.free_tiers()
    if free_tiers is not None:
        payload["free_tiers"] = free_tiers
    return payload


@router.post("/install")
async def install(request: InstallRequest, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    rt.sidecar.config.install_preference = request.preference
    try:
        result = await rt.sidecar.install()
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {**result, "progress": rt.sidecar.install_progress}


@router.post("/start")
async def start_sidecar(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    if rt.privacy_enabled():
        raise HTTPException(409, "Privacy mode is on — stop it before starting the sidecar")
    started = await rt.sidecar.start()
    return {"started": started, "status": rt.sidecar.status()}


@router.post("/restart")
async def restart_sidecar(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    if rt.privacy_enabled():
        raise HTTPException(409, "Privacy mode is on — stop it before restarting the sidecar")
    started = await rt.sidecar.restart_now()
    if started:
        await rt.catalog.refresh()
        import asyncio
        if rt.sidecar.watcher_task is None or rt.sidecar.watcher_task.done():
            rt.sidecar.watcher_task = asyncio.create_task(rt.sidecar.supervise())
    return {"started": started, "status": rt.sidecar.status()}


@router.post("/stop")
async def stop_sidecar(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    rt.sidecar.stop()
    return {"started": False, "status": rt.sidecar.status()}


@router.get("/config")
async def get_config(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    config = rt.sidecar.config
    return {
        "enabled": config.enabled,
        "install_preference": config.install_preference,
        "port": config.port,
        "providers": config.providers,
        "combos": config.combos,
        "quota_share_policy": config.quota_share_policy,
        "compression": config.compression,
    }


@router.put("/config")
async def put_config(request: SidecarConfigUpdate,
                     rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    config = rt.sidecar.config
    for field_name in ("enabled", "install_preference", "providers", "combos",
                       "quota_share_policy", "compression"):
        value = getattr(request, field_name)
        if value is not None:
            setattr(config, field_name, value)
    config.save(rt.sidecar.config_path)
    return await get_config(rt)


@router.get("/models")
async def catalog(refresh: bool = False, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    if rt.privacy_enabled():
        raise HTTPException(403, "Privacy mode is on — catalog is disabled (local only)")
    if refresh or not rt.catalog.entries():
        summary = await rt.catalog.refresh()
    else:
        summary = None
    entries = [{**entry,
                "badge": rt.catalog.badge(entry),
                "light": rt.catalog.light(entry, rt.cooldowns)}
               for entry in rt.catalog.entries()]
    return {"entries": entries, "refresh": summary, "last_refresh": rt.catalog.last_refresh}


@router.post("/hire")
async def hire(request: HireRequest, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    try:
        entry, badge = rt.catalog.hire(request.profile, request.mode)
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"profile": request.profile, "model": entry["id"], "provider": entry["provider"],
            "upstream": entry.get("source_provider"), "free": entry["free"], "badge": badge}


@router.put("/router/mode")
async def set_mode(request: RouterModeRequest, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    return rt.set_router_mode(request.mode)


@router.post("/privacy")
async def privacy(request: PrivacyRequest, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    return rt.set_privacy_mode(request.enabled)


@router.post("/hosted/consent")
async def consent(request: ConsentRequest, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    return rt.record_consent(request.repo_path, request.accept)


@router.get("/hosted/consent")
async def get_consent(repo_path: str, rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    return {"repo_path": repo_path, "consent": rt.consent(repo_path),
            "prompt": ("Prompts routed via OmniRoute may leave your machine to a "
                       "third-party provider (see badge). Free tiers may train on your "
                       "inputs. Continue?")}


@router.post("/redaction/dry-run")
async def redaction_dry_run(request: RedactionRequest,
                            settings: Settings = Depends(get_settings)) -> dict:
    return dry_run(request.text, settings.omniroute_secrets_dir)


@router.get("/dashboard")
async def dashboard(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    usage = rt.repo.usage_summary()
    free_tiers = await rt.sidecar.free_tiers() if await rt.sidecar.healthy() else None
    return {**usage, "privacy_mode": rt.privacy_enabled(),
            "router_mode": rt.router.default_mode,
            "free_tiers": free_tiers}


@router.get("/switches")
def recent_switches(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    events = rt.repo.run_events("app")[-20:]
    return {"switches": [{"ts": str(event.ts), **event.payload}
                         for event in events if str(event.kind) == "provider.switched"]}


@router.post("/costs/acknowledge")
async def acknowledge_costs(rt: OmniRouteRuntime = Depends(runtime)) -> dict:
    rt.acknowledge_costs()
    return {"hosted_paused": False}


def repository_dep(settings: Settings = Depends(get_settings)) -> Repository:
    return Repository(settings.database_path)


class _ChatLog:
    """Records one model_calls row per routed chat call (Phase 5 observability)."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self.started = time.monotonic()
        self.tokens_in = 0
        self.tokens_out = 0
        self.compressed_tokens = 0
        self.provider = ""
        self.model = ""
        self.fallback_reason = None

    def finish(self) -> dict:
        latency_ms = int((time.monotonic() - self.started) * 1000)
        self.repo.record_model_call(
            run_id=None, provider=self.provider, upstream=None, model=self.model or "auto",
            mode="routed", latency_ms=latency_ms, tokens_in=self.tokens_in,
            tokens_out=self.tokens_out, compressed_tokens=self.compressed_tokens,
            fallback_reason=self.fallback_reason)
        return {"latency_ms": latency_ms, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "compressed_tokens": self.compressed_tokens}


@router.post("/chat")
async def chat(request: ChatRequest, rt: OmniRouteRuntime = Depends(runtime),
               repo: Repository = Depends(repository_dep)):
    if rt.hosted_paused() and request.mode != "local":
        raise HTTPException(409, "Hosted mode paused after a non-zero cost — "
                                 "confirm at /api/omniroute/costs/acknowledge")

    async def generate():
        log = _ChatLog(repo)
        try:
            async for event in rt.router.chat(request.messages, request.model,
                                              request.tools, request.format,
                                              request.stream, mode=request.mode):
                if event.kind == "token":
                    log.tokens_out += 1
                elif event.kind == "done":
                    meta = event.meta or {}
                    log.tokens_in = meta.get("tokens_in") or log.tokens_in
                    log.tokens_out = meta.get("tokens_out") or log.tokens_out
                    log.compressed_tokens = meta.get("compressed_tokens") or 0
                yield f"data: {json.dumps({'kind': event.kind, 'text': event.text})}\n\n"
            yield f"event: done\ndata: {json.dumps(log.finish())}\n\n"
        except Exception as exc:
            log.fallback_reason = f"error:{type(exc).__name__}"
            log.finish()
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})

