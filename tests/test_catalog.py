"""Model catalog: merge + SQLite cache, capability hiring, badges,
availability lights and upstream cool-down rotation on a 429 storm."""

import pytest

from ultron.catalog import CatalogEntry, ModelCatalog, infer_capabilities
from ultron.db import Repository
from ultron.llm_providers import OllamaProvider, OmniRouteProvider
from ultron.provider_router import QuotaCooldowns


def make_repo(tmp_path) -> Repository:
    repo = Repository(tmp_path / "catalog.db")
    repo.initialize()
    return repo


def make_catalog(repo, entries=None):
    catalog = ModelCatalog(repo, OmniRouteProvider("http://127.0.0.1:20128"),
                           OllamaProvider("http://127.0.0.1:11434"))
    if entries is not None:
        repo.replace_catalog(entries)
    return catalog


HOSTED = {"id": "llama-3.3-70b", "provider": "omniroute", "source_provider": "groq",
          "context": 131072, "capabilities": ["chat"], "tokens_per_sec_estimate": 90.0,
          "free": True}
LOCAL = {"id": "qwen2.5-coder:7b", "provider": "ollama", "source_provider": None,
         "context": None, "capabilities": ["coding", "chat"],
         "tokens_per_sec_estimate": None, "free": False}


def test_infer_capabilities_profiles():
    assert infer_capabilities("qwen2.5-coder:32b") == ["coding"]
    assert infer_capabilities("deepseek-r1:8b") == ["reasoning"]
    assert infer_capabilities("phi4:latest") == ["chat"]


@pytest.mark.asyncio
async def test_refresh_merges_hosted_and_local(tmp_path):
    repo = make_repo(tmp_path)
    catalog = ModelCatalog(repo, OmniRouteProvider("http://x"), OllamaProvider("http://y"))

    async def fake_hosted():
        from ultron.catalog import CatalogEntry
        return [CatalogEntry(id="llama-3.3-70b", provider="omniroute",
                             source_provider="groq", context=131072, capabilities=["chat"],
                             tokens_per_sec_estimate=None, free=True)]

    async def fake_local():
        return [CatalogEntry(id="qwen2.5-coder:7b", provider="ollama", source_provider=None,
                             context=None, capabilities=["coding"], tokens_per_sec_estimate=None,
                             free=False)]

    catalog._omniroute_entries = fake_hosted
    catalog._ollama_entries = fake_local
    summary = await catalog.refresh()

    assert summary == {"hosted": 1, "local": 1}
    ids = {(entry["provider"], entry["id"]) for entry in repo.catalog_entries()}
    assert ("omniroute", "llama-3.3-70b") in ids
    assert ("ollama", "qwen2.5-coder:7b") in ids


def test_badges_match_nameplate_format(tmp_path):
    catalog = make_catalog(make_repo(tmp_path), [HOSTED, LOCAL])
    badges = {catalog.badge(entry) for entry in catalog.entries()}
    assert "OmniRoute · groq · llama-3.3-70b · FREE" in badges
    assert "Ollama · qwen2.5-coder:7b · LOCAL" in badges


def test_hire_prefers_free_tier_for_profile(tmp_path):
    catalog = make_catalog(make_repo(tmp_path), [HOSTED, LOCAL])
    entry, badge = catalog.hire("coding")
    assert entry["id"] == "qwen2.5-coder:7b"
    assert entry["provider"] == "ollama"


def test_hire_respects_mode_pins(tmp_path):
    catalog = make_catalog(make_repo(tmp_path), [HOSTED, LOCAL])
    entry, _ = catalog.hire("chat", mode="local")
    assert entry["provider"] == "ollama"
    entry, _ = catalog.hire("chat", mode="hosted")
    assert entry["provider"] == "omniroute"


def test_hire_skips_rate_limited_and_rotates(tmp_path):
    cooldowns = QuotaCooldowns()
    repo = make_repo(tmp_path)
    catalog = ModelCatalog(repo, OmniRouteProvider("http://x"), OllamaProvider("http://y"),
                           cooldowns=cooldowns)
    second_upstream = {**HOSTED, "source_provider": "mistral"}
    repo.replace_catalog([HOSTED, second_upstream, LOCAL])
    # mark only groq rate-limited; hire must rotate to another agent
    catalog.note_rate_limited(HOSTED)
    assert catalog.light(HOSTED, cooldowns) == "yellow"
    assert catalog.light(second_upstream, cooldowns) == "green"
    entry, _ = catalog.hire("chat")
    assert entry["source_provider"] != "groq"


def test_quota_storm_all_cooling_down_falls_back(tmp_path):
    cooldowns = QuotaCooldowns()
    repo = make_repo(tmp_path)
    catalog = ModelCatalog(repo, OmniRouteProvider("http://x"), OllamaProvider("http://y"),
                           cooldowns=cooldowns)
    repo.replace_catalog([HOSTED, LOCAL])
    # storm: every hosted upstream gets a 429
    catalog.note_rate_limited(HOSTED)
    cooldowns.mark("omniroute", 300)
    entry, badge = catalog.hire("chat")
    assert entry["provider"] == "ollama"  # hosted pool cooling down => work lands locally
    assert "LOCAL" in badge


def test_hire_without_any_candidate_raises(tmp_path):
    catalog = make_catalog(make_repo(tmp_path), [])
    with pytest.raises(LookupError):
        catalog.hire("coding")


def test_bench_ranking_breaks_ties(tmp_path):
    repo = make_repo(tmp_path)
    catalog = ModelCatalog(repo, OmniRouteProvider("http://x"), OllamaProvider("http://y"),
                           bench_ranking={"omniroute:gpt-oss-20b": 0.9})
    repo.replace_catalog([
        HOSTED,
        {"id": "gpt-oss-20b", "provider": "omniroute", "source_provider": "groq",
         "context": None, "capabilities": ["chat"], "tokens_per_sec_estimate": None,
         "free": True},
    ])
    entry, _ = catalog.hire("chat")
    assert entry["id"] == "gpt-oss-20b"
