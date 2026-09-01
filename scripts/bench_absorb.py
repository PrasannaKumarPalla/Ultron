"""Record the absorption baseline bench. Local-only measurements.

Usage: .\\.venv\\Scripts\\python scripts\\bench_absorb.py
"""

from __future__ import annotations

import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLLAMA = "http://127.0.0.1:11434"

PREFERRED_MODELS = (
    "qwen3.6:27b", "qwen3:30b", "qwen3:8b", "qwen3:4b", "qwen2.5:7b", "phi4:latest",
)

VOICE_PROMPT = "hey assistant, what needs my attention today?"
MISSION_PROMPT = ("You are Ultron's supervisor. Plan a mission titled 'Smoke check'. "
                  "Reply with the first planning line only.")


def cold_start_ms() -> float:
    samples = []
    code = ("import time; t0 = time.perf_counter(); import ultron.api; "
            "print((time.perf_counter() - t0) * 1000)")
    for _ in range(3):
        done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=600, check=True)
        samples.append(float(done.stdout.strip().splitlines()[-1]))
    return round(statistics.median(samples), 1)


def vram_snapshot() -> dict | None:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        done = subprocess.run(
            [smi, "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True)
        fields = [part.strip() for part in done.stdout.splitlines()[0].split(",")]
        return {"gpu": fields[0], "used_gb": round(float(fields[1]) / 1024, 2),
                "total_gb": round(float(fields[2]) / 1024, 2)}
    except Exception:
        return None


def ollama_ready() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as response:
            tags = [m["name"] for m in json.load(response).get("models", [])]
        model = next((name for name in PREFERRED_MODELS if name in tags),
                     tags[0] if tags else "")
        return bool(model), model
    except Exception:
        return False, ""


def first_token_ms(model: str, prompt: str) -> float:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }).encode()
    request = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=payload,
        headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        response.readline()
    return round((time.perf_counter() - start) * 1000, 1)


def main() -> None:
    ready, model = ollama_ready()
    bench = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "machine": {"os": platform.platform(), "python": platform.python_version()},
        "method": {
            "cold_start_ms": "median of 3 fresh `import ultron.api` processes",
            "first_token_ms": "TTFB of POST /api/chat stream against local Ollama",
            "voice_query": VOICE_PROMPT,
            "mission_start": MISSION_PROMPT,
        },
        "ollama_available": ready,
        "model": model,
        "vram_idle": vram_snapshot(),
        "cold_start_ms": cold_start_ms(),
        "first_token_voice_query_ms": first_token_ms(model, VOICE_PROMPT) if ready else None,
        "first_token_mission_start_ms": first_token_ms(model, MISSION_PROMPT) if ready else None,
    }
    target = ROOT / "bench" / "absorb-baseline.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(bench, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(bench, indent=2))


if __name__ == "__main__":
    main()
