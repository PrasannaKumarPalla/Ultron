"""Android companion app endpoints.

POST /api/stt  — accept raw audio bytes, return transcript via Whisper
GET  /api/tts  — synthesize text, return audio file (WAV)
GET  /api/ping — liveness check for mDNS-discovered hosts
"""

from __future__ import annotations

import io
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["android"])


def _require_api_key(request: Request) -> None:
    """Reject requests when an API key is configured but not provided."""
    api_key = getattr(request.app.state, "api_key", "") or ""
    if not api_key:
        return
    provided = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
    if not secrets.compare_digest(provided, api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


@router.get("/ping")
async def ping(request: Request) -> JSONResponse:
    """Liveness probe used by the Android app after mDNS discovery."""
    _require_api_key(request)
    from bujji.brand import get_branding

    b = get_branding()
    return JSONResponse({"status": "ok", "product": b.product_name})


@router.post("/stt")
async def speech_to_text(request: Request) -> JSONResponse:
    """Accept raw audio bytes and return a transcript.

    Content-Type: application/octet-stream (WAV or raw PCM 16 kHz mono s16le)
    Returns: {"transcript": "...", "language": "en"}
    """
    _require_api_key(request)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty audio body")

    speech = getattr(request.app.state, "speech_backend", None)
    if speech is None:
        raise HTTPException(status_code=503, detail="STT backend not available")

    try:
        import asyncio

        # Speech backends take audio bytes (they handle temp files internally)
        result = await asyncio.to_thread(speech.transcribe, body)
    except Exception as exc:
        logger.warning("STT transcription failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")

    transcript = getattr(result, "text", None)
    if transcript is None:
        transcript = result if isinstance(result, str) else ""
    language = getattr(result, "language", None) or "en"
    return JSONResponse({"transcript": transcript, "language": language})


@router.get("/tts")
async def text_to_speech(
    request: Request,
    text: str = Query(..., description="Text to synthesize"),
    voice_id: Optional[str] = Query(None, description="Voice identifier"),
) -> StreamingResponse:
    """Synthesize *text* and stream back a WAV audio response."""
    _require_api_key(request)

    if not text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    tts = getattr(request.app.state, "tts_backend", None)
    if tts is None:
        raise HTTPException(status_code=503, detail="TTS backend not available")

    try:
        import asyncio

        from bujji.speech.pronounce import respell

        tts_kwargs: dict = {}
        if voice_id:
            tts_kwargs["voice_id"] = voice_id
        spoken = respell(text, engine=getattr(tts, "backend_id", ""))
        result = await asyncio.to_thread(tts.synthesize, spoken, **tts_kwargs)
        # TTSResult dataclass or raw bytes
        audio_bytes: bytes = result.audio if hasattr(result, "audio") else bytes(result)
    except Exception as exc:
        logger.warning("TTS synthesis failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {exc}")

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/wav",
        headers={"Content-Disposition": 'attachment; filename="response.wav"'},
    )


@router.post("/voice/start")
async def voice_start(request: Request) -> JSONResponse:
    """Tell the backend to record one utterance from the system mic, run the agent, and play TTS.

    The voice pipeline broadcasts state events to /ws/events so the UI updates in real-time.
    Returns immediately — the pipeline runs in a background thread.
    """
    _require_api_key(request)
    pipeline = getattr(request.app.state, "voice_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Voice pipeline not available")
    try:
        pipeline._on_wake()
        return JSONResponse({"status": "listening"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/voice/stop")
async def voice_stop(request: Request) -> JSONResponse:
    """Interrupt ongoing TTS playback."""
    _require_api_key(request)
    pipeline = getattr(request.app.state, "voice_pipeline", None)
    if pipeline is not None:
        pipeline.interrupt()
    return JSONResponse({"status": "stopped"})


@router.post("/voice/briefing")
async def voice_briefing(request: Request) -> JSONResponse:
    """Speak the startup briefing on demand. Returns the text spoken."""
    _require_api_key(request)
    pipeline = getattr(request.app.state, "voice_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Voice pipeline not available")
    from bujji.speech.briefing import build_briefing

    cfg = getattr(request.app.state, "config", None)
    agent_cfg = getattr(cfg, "agent", None) if cfg else None
    name = getattr(agent_cfg, "user_name", "") or "Prasanna"
    loc = getattr(agent_cfg, "location", "") or ""
    import asyncio

    text = await asyncio.to_thread(build_briefing, name, loc)
    import threading

    threading.Thread(target=pipeline._play_tts, args=(text,), daemon=True).start()
    return JSONResponse({"status": "speaking", "text": text})


@router.get("/voice/status")
async def voice_status(request: Request) -> JSONResponse:
    """Report whether the wake-word pipeline is running and muted."""
    _require_api_key(request)
    pipeline = getattr(request.app.state, "voice_pipeline", None)
    if pipeline is None:
        return JSONResponse({"available": False, "listening": False, "muted": False})
    return JSONResponse(
        {
            "available": True,
            "listening": bool(getattr(pipeline, "_active", False)) and not pipeline.muted,
            "muted": pipeline.muted,
        }
    )


@router.get("/audio/devices")
async def audio_devices(request: Request) -> JSONResponse:
    """List audio devices and which mic/speaker are currently in use."""
    _require_api_key(request)
    try:
        import sounddevice as sd

        try:
            sd.query_devices(kind="input")
            sd.query_devices(kind="output")
        except Exception:
            # Hot-plugged hardware (e.g. a wireless mic) isn't visible until
            # PortAudio re-enumerates; stale defaults also break kind queries.
            sd._terminate()
            sd._initialize()

        devices = sd.query_devices()
        default_in, default_out = sd.default.device
        inputs, outputs = [], []
        for idx, d in enumerate(devices):
            entry = {"id": idx, "name": d["name"], "hostapi": d["hostapi"]}
            if d["max_input_channels"] > 0:
                inputs.append({**entry, "selected": idx == default_in
                               or (default_in in (-1, None) and idx == sd.query_devices(kind="input")["index"])})
            if d["max_output_channels"] > 0:
                outputs.append({**entry, "selected": idx == default_out
                                or (default_out in (-1, None) and idx == sd.query_devices(kind="output")["index"])})
        return JSONResponse({
            "inputs": inputs,
            "outputs": outputs,
            "current_input": sd.query_devices(kind="input")["name"],
            "current_output": sd.query_devices(kind="output")["name"],
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"audio device query failed: {exc}")


@router.post("/audio/devices")
async def set_audio_devices(request: Request) -> JSONResponse:
    """Select mic/speaker. Body: {"input_id": int|null, "output_id": int|null}."""
    _require_api_key(request)
    try:
        import sounddevice as sd

        body = await request.json()
        cur_in, cur_out = sd.default.device
        new_in = body.get("input_id", cur_in)
        new_out = body.get("output_id", cur_out)
        sd.default.device = (new_in, new_out)
        return JSONResponse({
            "current_input": sd.query_devices(kind="input")["name"],
            "current_output": sd.query_devices(kind="output")["name"],
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"audio device select failed: {exc}")


@router.post("/voice/mute")
async def voice_mute(request: Request) -> JSONResponse:
    """Set or toggle mute. Body: {"muted": true|false} — omit to toggle."""
    _require_api_key(request)
    pipeline = getattr(request.app.state, "voice_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Voice pipeline not available")
    try:
        body = await request.json()
    except Exception:
        body = {}
    muted = body.get("muted")
    pipeline.set_muted(not pipeline.muted if muted is None else bool(muted))
    return JSONResponse({"muted": pipeline.muted})


__all__ = ["router"]
