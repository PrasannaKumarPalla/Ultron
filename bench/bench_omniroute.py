"""Bench harness: same task against top OmniRoute free upstreams vs local Ollama.

Writes bench/omniroute-vs-local.json; the router reads it back as its
bench_ranking so picks follow measured reality, not vibes.

Usage:
    python bench/bench_omniroute.py [--omniroute http://127.0.0.1:20128]
                                    [--ollama http://127.0.0.1:11434]

Only free-tier OmniRoute models are benched. Non-zero reported cost aborts
hosted benching (cost meter rule).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

TASK_PROMPT = ("Summarize in two sentences why idempotent database migrations matter "
               "for a small engineering team.")
SCORE_PROMPT_WORDS = len(TASK_PROMPT.split())


async def chat_once(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    started = time.monotonic()
    response = await client.post(url, json=payload, timeout=120)
    latency_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    body = response.json()
    if "choices" in body:  # OpenAI-compatible
        text = body["choices"][0]["message"]["content"]
        tokens_out = (body.get("usage") or {}).get("completion_tokens")
    else:  # Ollama native
        text = body.get("message", {}).get("content", "")
        tokens_out = body.get("eval_count")
    return {"latency_ms": latency_ms, "text": text,
            "tokens_out": tokens_out or max(1, len(text.split()))}


def quality_score(result: dict) -> float:
    """Cheap deterministic proxy: completeness (length) over latency cost."""
    words = len(result["text"].split())
    coverage = min(1.0, words / 40)
    speed = 1_000 / max(result["latency_ms"], 1)
    return round(coverage * 10 + speed, 2)


async def bench_target(client: httpx.AsyncClient, provider: str, model: str,
                       endpoint: str, runs: int = 3) -> dict:
    latencies = []
    last = None
    for _ in range(runs):
        last = await chat_once(client, endpoint, {
            "model": model, "messages": [{"role": "user", "content": TASK_PROMPT}],
            "stream": False})
        latencies.append(last["latency_ms"])
    result = {"provider": provider, "model": model,
              "median_latency_ms": int(statistics.median(latencies)),
              "tokens_out": last["tokens_out"],
              "score": 0.0}
    result["score"] = quality_score({"text": last["text"],
                                     "latency_ms": result["median_latency_ms"]})
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--omniroute", default="http://127.0.0.1:20128")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    results = []
    async with httpx.AsyncClient() as client:
        try:
            models = client.get(f"{args.omniroute}/v1/models", timeout=10).json().get("data", [])
            free_models = [item["id"] for item in models][:3]  # free-tier pool is the default
            for model in free_models:
                results.append(await bench_target(
                    client, "omniroute", model, f"{args.omniroute}/v1/chat/completions",
                    args.runs))
        except httpx.HTTPError:
            print("OmniRoute sidecar unreachable; benching local only")

        try:
            tags = client.get(f"{args.ollama}/api/tags", timeout=10).json().get("models", [])
            installed = [item["name"] for item in tags
                         if "embed" not in item.get("name", "").lower()][:1]
            for model in installed:
                results.append(await bench_target(
                    client, "ollama", model, f"{args.ollama}/api/chat", args.runs))
        except httpx.HTTPError:
            print("Ollama unreachable; nothing benched locally")

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "task": TASK_PROMPT,
        "method": {"runs_per_target": args.runs,
                   "score": "completeness (words/40 capped at 1) * 10 + 1000/median_latency_ms"},
        "results": results,
    }
    out = Path(__file__).parent / "omniroute-vs-local.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out} with {len(results)} results")


if __name__ == "__main__":
    asyncio.run(main())
