"""Merged model catalog: OmniRoute free-tier pool + local Ollama list.

Cached to SQLite on startup and refreshed every 6h. Roles reference
capability profiles (coding/reasoning/chat), never raw ids — the router's
`hire()` resolves a profile to a concrete agent per call and returns the
badge resolved for the agent that runs the role.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

import httpx

from .llm_providers import OllamaProvider, OmniRouteProvider
from .provider_router import QuotaCooldowns

logger = logging.getLogger(__name__)

CAPABILITY_PROFILES = {
    "coding": ["code", "coder", "devstral", "codestral"],
    "reasoning": ["r1", "qwq", "think", "o1", "o3", "deepseek-r"],
    "chat": [],
}

COOLDOWN_MARK_S = 300


@dataclass
class CatalogEntry:
    id: str
    provider: str  # omniroute | ollama
    source_provider: str | None  # e.g. groq/mistral when behind OmniRoute
    context: int | None
    capabilities: list[str]
    tokens_per_sec_estimate: float | None
    free: bool


def infer_capabilities(model_id: str) -> list[str]:
    lowered = model_id.lower()
    caps = []
    for profile, signals in CAPABILITY_PROFILES.items():
        if not signals:
            continue
        if any(signal in lowered for signal in signals):
            caps.append(profile)
    if not caps:
        caps.append("chat")
    return caps


class ModelCatalog:
    def __init__(self, repository, omniroute: OmniRouteProvider, ollama: OllamaProvider,
                 cooldowns: QuotaCooldowns | None = None,
                 bench_ranking: dict[str, float] | None = None):
        self.repo = repository
        self.omniroute = omniroute
        self.ollama = ollama
        self.cooldowns = cooldowns or QuotaCooldowns()
        self.bench_ranking = bench_ranking or {}
        self.last_refresh: float | None = None

    _AUTO = CatalogEntry(
        id="auto", provider="omniroute", source_provider=None, context=None,
        capabilities=["coding", "reasoning", "chat"],
        tokens_per_sec_estimate=None, free=True)

    async def _omniroute_entries(self) -> list[CatalogEntry]:
        """Merge OmniRoute's `/v1/models` list. That endpoint is gated by a
        MANAGEMENT_TOKEN on some installs while the keyless inference plane
        still routes — so if the sidecar is reachable we always keep a synthetic
        `auto` entry, and append the concrete model list when we can read it."""
        reachable = await self.omniroute.health()
        try:
            async with self.omniroute.client_factory(timeout=5) as client:
                response = await client.get(f"{self.omniroute.base_url}/models",
                                            headers=self.omniroute.auth_headers())
                response.raise_for_status()
            items = response.json().get("data", [])
        except (httpx.HTTPError, ValueError):
            return [self._AUTO] if reachable else []
        entries = [self._AUTO] if reachable else []
        for item in items:
            model_id = item.get("id", "")
            if not model_id:
                continue
            owned_by = item.get("owned_by") or ""
            source = owned_by if owned_by not in {"", "omniroute", "system"} else None
            entries.append(CatalogEntry(
                id=model_id, provider="omniroute", source_provider=source,
                context=item.get("context_length"),
                capabilities=infer_capabilities(model_id),
                tokens_per_sec_estimate=item.get("tokens_per_sec_estimate"),
                free=True))
        return entries

    @staticmethod
    def badge(entry: dict) -> str:
        if entry["provider"] == "omniroute":
            source = entry.get("source_provider") or "auto"
            tier = "FREE" if entry.get("free", True) else "PAID"
            return f"OmniRoute · {source} · {entry['id']} · {tier}"
        return f"Ollama · {entry['id']} · LOCAL"

    @staticmethod
    def light(entry: dict, cooldowns: QuotaCooldowns) -> str:
        """green = free capacity, yellow = rate-limited, red = down."""
        if entry["provider"] == "omniroute":
            key = f"upstream:{entry.get('source_provider')}"
            if cooldowns.active(key) or cooldowns.active("omniroute"):
                return "yellow"
            return "green"
        return "yellow" if cooldowns.active(f"model:{entry['id']}") else "green"

    def rank(self, entry: dict) -> float:
        score = self.bench_ranking.get(f"{entry['provider']}:{entry['id']}", 0.0)
        if entry["provider"] == "ollama":
            score += 0.01  # tie-break towards local when benchmarks are equal
        return score

    def hire(self, profile: str, mode: str = "auto") -> tuple[dict, str]:
        """Resolve a capability profile to an available agent. Returns
        (catalog_entry, badge). Rate-limit aware; falls back across providers."""
        candidates = [entry for entry in self.entries() if profile in entry["capabilities"]]
        if not candidates:
            raise LookupError(f"no model matches capability profile {profile!r}")
        order = {"local": ["ollama"], "hosted": ["omniroute"]}.get(
            mode, ["omniroute", "ollama"])
        for provider_name in order:
            pool = [entry for entry in candidates if entry["provider"] == provider_name
                    and self.light(entry, self.cooldowns) != "yellow"]
            if pool:
                best = max(pool, key=self.rank)
                return best, self.badge(best)
        raise LookupError(f"all agents for profile {profile!r} are cooling down")

    def note_rate_limited(self, entry: dict) -> None:
        if entry["provider"] == "omniroute" and entry.get("source_provider"):
            self.cooldowns.mark(f"upstream:{entry['source_provider']}", COOLDOWN_MARK_S)
        else:
            self.cooldowns.mark(f"model:{entry['id']}", COOLDOWN_MARK_S)

    async def refresh_loop(self, interval_s: int = 21_600) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("catalog refresh failed")
            await asyncio.sleep(interval_s)

    async def _ollama_entries(self) -> list[CatalogEntry]:
        installed = await self.ollama.installed_models()
        return [CatalogEntry(
            id=name, provider="ollama", source_provider=None, context=None,
            capabilities=infer_capabilities(name),
            tokens_per_sec_estimate=None, free=False)
            for name in sorted(installed) if name]

    async def refresh(self) -> dict:
        hosted, local = await asyncio.gather(self._omniroute_entries(), self._ollama_entries())
        merged = {f"{entry.provider}:{entry.id}": entry for entry in (*hosted, *local)}
        entries = list(merged.values())
        self.repo.replace_catalog([asdict(entry) for entry in entries])
        self.last_refresh = time.time()
        return {"hosted": len(hosted), "local": len(local)}

    def entries(self) -> list[dict]:
        return self.repo.catalog_entries()

