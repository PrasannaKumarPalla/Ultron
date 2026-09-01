"""bujji voice — start the local voice pipeline (wake → STT → agent → TTS)."""

from __future__ import annotations

import logging
import time

import click

logger = logging.getLogger(__name__)


@click.command("voice")
@click.option("--model", default="phi4", show_default=True, help="Fast model")
@click.option("--complex-model", default="qwen3:30b", show_default=True, help="Complex model")
@click.option("--voice-id", default="", help="Voice name or backend voice ID")
@click.option("--no-wake", is_flag=True, default=False, help="Skip wake word; activate immediately")
def voice(model: str, complex_model: str, voice_id: str, no_wake: bool) -> None:
    """Start the voice assistant pipeline (fully local, no cloud)."""
    from bujji.agents.model_router import route as _route
    from bujji.agents.orchestrator import OrchestratorAgent
    from bujji.core.config import load_config
    from bujji.engine.ollama import OllamaEngine
    from bujji.speech.faster_whisper import FasterWhisperBackend
    from bujji.speech._tts_discovery import get_tts_backend
    from bujji.speech.pipeline import VoicePipeline
    from bujji.speech.wake_word import BujjiWakeWordDetector

    cfg = load_config()
    click.echo("Initialising voice pipeline...")

    ollama_host = "http://localhost:11434"
    try:
        ollama_host = cfg.engine.ollama.host
    except AttributeError:
        pass

    engine = OllamaEngine(host=ollama_host)

    stt_model = "small"
    stt_device = "cpu"
    stt_compute = "int8"
    try:
        stt_model = cfg.speech.model
        stt_device = cfg.speech.device
        stt_compute = cfg.speech.compute_type
    except AttributeError:
        pass

    stt = FasterWhisperBackend(model_size=stt_model, device=stt_device, compute_type=stt_compute)

    tts_voice = voice_id
    tts_speed = 1.0
    try:
        tts_voice = tts_voice or cfg.tts.voice_id
        tts_speed = cfg.tts.speed
    except AttributeError:
        pass

    tts = get_tts_backend(cfg)
    if tts is None:
        raise click.ClickException("No usable text-to-speech backend was found")

    class _RoutingAgent:
        def __init__(self):
            self._fast = model
            self._complex = complex_model

        def run(self, text):
            chosen = _route(text, fast_model=self._fast, complex_model=self._complex)
            click.echo(f"  → model: {chosen}")
            return OrchestratorAgent(engine, chosen).run(text)

    wake = BujjiWakeWordDetector()

    def _on_event(event: dict) -> None:
        etype = event.get("type", "")
        if etype == "wake":
            click.echo("\n[WAKE] Listening...")
        elif etype == "transcript":
            click.echo(f"[YOU]  {event.get('text', '')}")
        elif etype == "speaking_start":
            click.echo("[BUJJI] Speaking...")
        elif etype == "error":
            click.echo(f"[ERR]  {event.get('message', '')}")

    pipeline = VoicePipeline(
        _RoutingAgent(), stt, tts, wake,
        voice_id=tts_voice,
        tts_speed=tts_speed,
        on_event=_on_event,
    )

    pipeline.start()

    if no_wake:
        click.echo("Triggering immediately (--no-wake).")
        pipeline._on_wake()
    else:
        click.echo("Say 'bujji' to activate. Ctrl+C to quit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pipeline.stop()
        click.echo("\nVoice pipeline stopped.")
