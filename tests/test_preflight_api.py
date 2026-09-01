"""API surface for preflight detection + consented install."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ultron import preflight, preflight_install
from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository


def _client(tmp_path):
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "cp.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _profile(**over):
    base = dict(
        os="Windows", arch="x86_64", ram_gb=32.0, vram_gb=12.0, gpu_vendor="nvidia",
        disk_free_gb=200.0, ollama_installed=True, ollama_running=True,
        installed_models=(),
    )
    base.update(over)
    return preflight.MachineProfile(**base)


def test_get_preflight_returns_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "detect_machine", lambda: _profile(vram_gb=24.0))
    try:
        with _client(tmp_path) as client:
            body = client.get("/api/preflight").json()
    finally:
        app.dependency_overrides.clear()

    assert body["profile"]["gpu_vendor"] == "nvidia"
    assert body["recommended_model"] == "qwen3:14b"
    keys = {r["key"] for r in body["requirements"]}
    assert keys == {"ollama", "model", "disk", "ram"}
    model_req = next(r for r in body["requirements"] if r["key"] == "model")
    assert model_req["action"] == "pull_model:qwen3:14b"


def test_install_streams_progress_frames(tmp_path, monkeypatch):
    async def fake_run(action, *, base_url, downloads_dir):
        assert action == "pull_model:qwen3:8b"
        yield {"phase": "pull", "status": "pulling qwen3:8b", "completed": None, "total": None}
        yield {"phase": "pull", "status": "downloading", "completed": 500, "total": 1000}
        yield {"phase": "done"}

    monkeypatch.setattr(preflight_install, "run_install", fake_run)
    try:
        with _client(tmp_path) as client:
            resp = client.post("/api/preflight/install", json={"action": "pull_model:qwen3:8b"})
            frames = [
                json.loads(line[len("data: "):])
                for line in resp.text.splitlines()
                if line.startswith("data: ")
            ]
    finally:
        app.dependency_overrides.clear()

    assert frames[0]["phase"] == "pull"
    assert frames[1]["completed"] == 500
    assert frames[-1] == {"phase": "done"}


def test_invalid_action_is_reported_as_an_error_frame_not_a_500(tmp_path, monkeypatch):
    try:
        with _client(tmp_path) as client:
            resp = client.post("/api/preflight/install", json={"action": "rm -rf /"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    frames = [
        json.loads(line[len("data: "):])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert frames[-1]["phase"] == "error"
    assert "unknown action" in frames[-1]["error"]


def test_install_failure_mid_stream_becomes_an_error_frame(tmp_path, monkeypatch):
    async def boom(action, *, base_url, downloads_dir):
        yield {"phase": "install"}
        raise RuntimeError("installer exited 1")

    monkeypatch.setattr(preflight_install, "run_install", boom)
    try:
        with _client(tmp_path) as client:
            resp = client.post("/api/preflight/install", json={"action": "install_ollama"})
            frames = [
                json.loads(line[len("data: "):])
                for line in resp.text.splitlines()
                if line.startswith("data: ")
            ]
    finally:
        app.dependency_overrides.clear()

    assert frames[0] == {"phase": "install"}
    assert frames[-1]["phase"] == "error"
    assert "installer exited 1" in frames[-1]["error"]


# --- preflight_install unit-level -------------------------------------------- #

def test_parse_rejects_unlisted_model_tag():
    import pytest

    with pytest.raises(preflight_install.InvalidAction):
        preflight_install._parse("pull_model:llama3:70b")


def test_parse_accepts_a_recommended_tag_and_install_ollama():
    assert preflight_install._parse("install_ollama") == ("install_ollama", None)
    assert preflight_install._parse("pull_model:qwen3:4b") == ("pull_model", "qwen3:4b")
