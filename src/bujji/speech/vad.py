"""Voice activity detection — records from mic until trailing silence."""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_CHUNK_S = 0.1          # 100ms chunks
# Low absolute floor so quiet mics (wireless units with output pads) register
# as speech; audio is peak-normalized before STT so low gain doesn't matter.
_SILENCE_RMS = 0.0008   # lower = more sensitive
_TRAILING_SILENCE_S = 1.2
_LEAD_IN_S = 0.5        # always capture this much before checking for silence
_MAX_RECORD_S = 12.0    # cap — 30s of CPU STT made replies feel dead


def record_until_silence(
    *,
    sample_rate: int = _SAMPLE_RATE,
    silence_rms: float = _SILENCE_RMS,
    trailing_silence_s: float = _TRAILING_SILENCE_S,
    max_record_s: float = _MAX_RECORD_S,
) -> bytes:
    """Record from mic until trailing silence. Returns WAV bytes, or b'' on error."""
    try:
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
    except ImportError as exc:
        logger.warning("VAD deps missing: %s", exc)
        return b""

    chunk_frames = int(sample_rate * _CHUNK_S)
    trailing_chunks_needed = int(trailing_silence_s / _CHUNK_S)
    lead_in_chunks = int(_LEAD_IN_S / _CHUNK_S)
    max_chunks = int(max_record_s / _CHUNK_S)

    buffers: list = []
    silent_count = 0
    min_rms: float | None = None

    try:
        for i in range(max_chunks):
            audio = sd.rec(chunk_frames, samplerate=sample_rate, channels=1, dtype="float32")
            sd.wait()
            flat = np.nan_to_num(audio.flatten())
            rms = float(np.sqrt(np.mean(flat ** 2)))
            if not np.isfinite(rms):
                continue

            buffers.append(flat)
            if min_rms is None or rms < min_rms:
                min_rms = rms

            # Always capture lead-in chunks before checking for silence
            if i < lead_in_chunks:
                continue

            # Adaptive: "silence" = near the quietest level seen this
            # recording, not an absolute number (rooms hum, mics differ).
            threshold = max(silence_rms, (min_rms or 0.0) * 1.6)
            if rms >= threshold:
                silent_count = 0
            else:
                silent_count += 1
                if silent_count >= trailing_chunks_needed:
                    break
    except Exception as exc:
        logger.warning("VAD recording error: %s", exc)
        if not buffers:
            return b""

    if not buffers:
        return b""

    combined = np.concatenate(buffers)

    # Trim leading silence
    threshold = silence_rms * 0.5
    chunk = int(sample_rate * _CHUNK_S)
    start_idx = 0
    for j in range(0, len(combined) - chunk, chunk):
        if float(np.sqrt(np.mean(combined[j:j+chunk] ** 2))) >= threshold:
            start_idx = max(0, j - chunk)
            break

    combined = combined[start_idx:]
    if len(combined) < sample_rate * 0.3:  # less than 300ms — probably nothing
        return b""

    # Peak-normalize so STT sees healthy amplitude from quiet mics
    peak = float(np.abs(combined).max())
    if 0 < peak < 0.5:
        combined = combined * (0.7 / peak)

    buf = io.BytesIO()
    sf.write(buf, combined, sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf.read()


__all__ = ["record_until_silence"]
