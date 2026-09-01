from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ExecutionRequest:
    mission_id: str
    workspace_path: str
    objective: str


@dataclass(frozen=True)
class ExecutionReceipt:
    provider: str
    external_id: str
    status: str


class ExecutionProvider(ABC):
    @abstractmethod
    async def submit(self, request: ExecutionRequest) -> ExecutionReceipt: ...


class MockExecutionProvider(ExecutionProvider):
    async def submit(self, request: ExecutionRequest) -> ExecutionReceipt:
        return ExecutionReceipt("mock", f"mock-{request.mission_id}", "accepted")


class OpenHandsExecutionProvider(ExecutionProvider):
    """Stable Ultron boundary around the evolving OpenHands Agent Server API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def health(self) -> bool:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.base_url}/health")
            return response.is_success

    async def submit(self, request: ExecutionRequest) -> ExecutionReceipt:
        # Endpoint mapping is finalized against the pinned OpenHands release in Phase 1.
        raise NotImplementedError(
            "OpenHands submission is intentionally gated until a version is pinned and its API contract is verified"
        )


async def ollama_health(base_url: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            names = {item["name"] for item in response.json().get("models", [])}
            return model in names
    except (httpx.HTTPError, KeyError, TypeError):
        return False


async def ollama_models(base_url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
    models = []
    for item in response.json().get("models", []):
        name = item.get("name", "")
        if not name or "embed" in name.lower():
            continue
        models.append({"name": name, "size": item.get("size", 0)})
    return sorted(models, key=lambda item: item["name"].lower())
