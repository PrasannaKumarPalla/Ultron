"""WebSocket endpoints for voice pipeline.

/ws/events  — server → browser  (voice pipeline state events)
/ws/mic     — browser → server  (browser mic audio → STT → agent → TTS events)
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import tempfile
import os
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


class VoiceEventBroadcaster:
    """Thread-safe broadcaster: voice pipeline threads → WebSocket clients."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def broadcast(self, event: dict) -> None:
        """Call from any thread to push an event to all connected clients."""
        if not self._clients or self._loop is None:
            return
        payload = json.dumps(event)
        asyncio.run_coroutine_threadsafe(self._broadcast_async(payload), self._loop)

    async def _broadcast_async(self, payload: str) -> None:
        dead: Set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.debug("Voice WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.debug("Voice WS client disconnected (%d total)", len(self._clients))


broadcaster = VoiceEventBroadcaster()


def _ws_authorized(websocket: WebSocket) -> bool:
    from bujji.server.auth_middleware import websocket_authorized

    expected = getattr(websocket.app.state, "api_key", "") or ""
    return websocket_authorized(websocket, expected)


@router.websocket("/ws/events")
async def voice_events_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint the frontend connects to for voice pipeline events."""
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await broadcaster.connect(websocket)
    broadcaster.set_loop(asyncio.get_event_loop())
    try:
        while True:
            await websocket.receive_text()  # keep-alive ping/pong
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)


@router.websocket("/ws/mic")
async def browser_mic_ws(websocket: WebSocket) -> None:
    """Browser mic → STT → agent → TTS response events.

    Protocol (browser sends):
      - Binary frames: raw PCM s16le 16 kHz mono chunks
      - Text frame "stop": signals end of recording

    Server sends JSON events (same format as /ws/events):
      state_change, transcript, speaking_start, speaking_end, error
    """
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    async def send(event: dict) -> None:
        try:
            await websocket.send_text(json.dumps(event))
        except Exception:
            pass

    try:
        while True:
            # Collect PCM chunks until "stop" text frame
            pcm_chunks: list[bytes] = []
            await send({"type": "state_change", "from": "idle", "to": "listening"})
            await send({"type": "recording_start"})

            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") == "stop":
                    break
                if msg.get("bytes"):
                    pcm_chunks.append(msg["bytes"])

            await send({"type": "recording_end"})
            await send({"type": "state_change", "from": "listening", "to": "thinking"})

            if not pcm_chunks:
                await send({"type": "error", "message": "No audio received", "code": "no_audio"})
                await send({"type": "state_change", "from": "thinking", "to": "idle"})
                continue

            # Convert raw PCM s16le → WAV file
            pcm_data = b"".join(pcm_chunks)
            sample_rate = 16000
            num_channels = 1
            bits_per_sample = 16
            byte_rate = sample_rate * num_channels * bits_per_sample // 8
            block_align = num_channels * bits_per_sample // 8
            data_size = len(pcm_data)

            wav_header = struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF", 36 + data_size, b"WAVE",
                b"fmt ", 16, 1, num_channels,
                sample_rate, byte_rate, block_align, bits_per_sample,
                b"data", data_size,
            )
            wav_bytes = wav_header + pcm_data

            # Write to temp file for STT
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(wav_bytes)
                    tmp_path = f.name

                # STT
                stt = getattr(websocket.app.state, "speech_backend", None)
                if stt is None:
                    await send({"type": "error", "message": "STT not available", "code": "no_stt"})
                    await send({"type": "state_change", "from": "thinking", "to": "idle"})
                    continue

                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: stt.transcribe(tmp_path))
                transcript = (result.text if hasattr(result, "text") else str(result)).strip()

                if not transcript:
                    await send({"type": "state_change", "from": "thinking", "to": "idle"})
                    continue

                await send({"type": "transcript", "text": transcript, "final": True})

                # Agent
                agent = getattr(websocket.app.state, "agent", None)
                if agent is None:
                    await send({"type": "error", "message": "Agent not available", "code": "no_agent"})
                    await send({"type": "state_change", "from": "thinking", "to": "idle"})
                    continue

                agent_result = await loop.run_in_executor(None, lambda: agent.run(transcript))
                response = (
                    agent_result.content.strip()
                    if agent_result and agent_result.content
                    else ""
                )

                if not response:
                    await send({"type": "state_change", "from": "thinking", "to": "idle"})
                    continue

                await send({"type": "state_change", "from": "thinking", "to": "speaking"})
                await send({"type": "speaking_start"})
                # Send response text so frontend can do TTS
                await send({"type": "response", "text": response})
                await send({"type": "speaking_end"})
                await send({"type": "state_change", "from": "speaking", "to": "idle"})

            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("Browser mic WS error: %s", exc)


__all__ = ["VoiceEventBroadcaster", "broadcaster", "router"]
