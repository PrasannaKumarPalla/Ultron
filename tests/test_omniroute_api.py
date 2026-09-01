"""API surface for the OmniRoute integration. Uses the shared TestClient
lifespan; conftest keeps the sidecar from ever spawning."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ultron import omniroute_runtime
from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository
from ultron.omniroute_runtime import reset_runtime


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "omni.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
        omniroute_secrets_dir=tmp_path / "secrets",
    )
    runtime = omniroute_runtime.OmniRouteRuntime(settings)

    async def never_healthy(*_a, **_kw):
        return False

    monkeypatch.setattr(runtime.sidecar, "healthy", never_healthy)

    async def fake_start():
        runtime.repo.initialize()

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(omniroute_runtime, "_RUNTIME", runtime)
    monkeypatch.setattr("ultron.api.get_runtime", lambda s=None: runtime)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as tc:
        yield tc, settings
    app.dependency_overrides.pop(get_settings, None)



def test_status_reports_router_defaults(client):
    tc, _ = client
    body = tc.get("/api/omniroute/status").json()
    assert body["router_mode"] == "auto"
    assert body["privacy_mode"] is False
    assert body["healthy"] is False  # no sidecar in tests
    assert isinstance(body["runtime"], dict)


def test_config_round_trip_via_api(client, tmp_path):
    tc, settings = client
    updated = {"providers": ["groq"], "compression": False}
    body = tc.put("/api/omniroute/config", json=updated).json()
    assert body["providers"] == ["groq"]
    assert body["compression"] is False


def test_router_mode_pin_local_and_back(client):
    tc, _ = client
    assert tc.put("/api/omniroute/router/mode", json={"mode": "local"}).json()["mode"] == "local"
    status = tc.get("/api/omniroute/status").json()
    assert status["pinned_local"] is True and status["router_mode"] == "local"
    tc.put("/api/omniroute/router/mode", json={"mode": "auto"})
    assert tc.get("/api/omniroute/status").json()["pinned_local"] is False


def test_privacy_mode_blocks_catalog_and_sidecar_start(client):
    tc, _ = client
    assert tc.post("/api/omniroute/privacy", json={"enabled": True}).json()["privacy_mode"]
    assert tc.get("/api/omniroute/models").status_code == 403
    assert tc.post("/api/omniroute/start").status_code == 409
    assert tc.post("/api/omniroute/privacy", json={"enabled": False}).status_code == 200


def test_consent_is_persisted_per_repo(client):
    tc, _ = client
    repo_a = "c:\\repos\\alpha"
    repo_b = "c:\\repos\\beta"
    prompt = tc.get("/api/omniroute/hosted/consent", params={"repo_path": repo_a}).json()
    assert "may leave your machine" in prompt["prompt"]
    tc.post("/api/omniroute/hosted/consent",
            json={"repo_path": repo_a, "accept": True})
    tc.post("/api/omniroute/hosted/consent",
            json={"repo_path": repo_b, "accept": False})
    assert tc.get("/api/omniroute/hosted/consent",
                  params={"repo_path": repo_a}).json()["consent"] is True
    assert tc.get("/api/omniroute/hosted/consent",
                  params={"repo_path": repo_b}).json()["consent"] is False


def test_redaction_dry_run_endpoint(client):
    tc, _ = client
    body = tc.post("/api/omniroute/redaction/dry-run",
                   json={"text": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"}).json()
    assert body["would_send_verbatim"] is False
    assert body["total"] >= 1


def test_dashboard_shape(client):
    tc, settings = client
    repo = Repository(settings.database_path)
    repo.record_model_call(run_id=None, provider="omniroute", upstream="groq",
                           model="llama-3.3-70b", mode="routed", latency_ms=120,
                           tokens_in=10, tokens_out=5, compressed_tokens=4)
    repo.record_model_call(run_id=None, provider="ollama", upstream=None,
                           model="qwen3:30b", mode="local", latency_ms=300,
                           tokens_in=8, tokens_out=6, compressed_tokens=0)
    body = tc.get("/api/omniroute/dashboard").json()
    providers = {row["provider"]: row for row in body["split"]}
    assert providers["omniroute"]["calls"] == 1 and providers["ollama"]["calls"] == 1
    assert {"upstream": "groq", "calls": 1} in body["upstream_mix"]
    assert body["tokens_saved_by_compression"] == 4
