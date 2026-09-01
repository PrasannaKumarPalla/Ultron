"""Warm pool of Ollama model processes.

Keeps `size` models resident (`keep_alive`) so the first mission turn skips
cold-load. Best-effort and fully offline-safe: when Ollama is down, warming
records 'unavailable' and everything else keeps working.
"""

from __future__ import annotations

import httpx


class ModelPool:
    def __init__(self, ollama_url: str, size: int = 2, keep_alive: str = "60m",
                 timeout_s: float = 30.0):
        if size < 1:
            raise ValueError("pool size must be >= 1")
        self.ollama_url = ollama_url.rstrip("/")
        self.size = size
        self.keep_alive = keep_alive
        self.timeout_s = timeout_s
        self.status: dict[str, str] = {}

    async def warm(self, models: list[str]) -> dict[str, str]:
        for model in models[:self.size]:
            self.status[model] = "warming"
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    response = await client.post(f"{self.ollama_url}/api/generate", json={
                        "model": model, "prompt": "", "keep_alive": self.keep_alive,
                    })
                self.status[model] = "warm" if response.status_code == 200 else \
                    f"error {response.status_code}"
            except (httpx.HTTPError, OSError) as exc:
                self.status[model] = f"unavailable: {type(exc).__name__}"
        return dict(self.status)

    def snapshot(self) -> dict:
        return {"size": self.size, "keep_alive": self.keep_alive,
                "models": dict(self.status)}
