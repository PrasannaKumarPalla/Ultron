import asyncio

import pytest

from ultron.sidecar import RESTART_CAP, OmniRouteSidecar, SidecarConfig, detect_runtime


def test_config_round_trip(tmp_path):
    path = tmp_path / "omniroute.yaml"
    config = SidecarConfig(providers=["groq", "mistral"], combos=["coder+review"],
                           quota_share_policy="weighted", compression=False)
    config.save(path)
    loaded = SidecarConfig.load(path)
    assert loaded == config


def test_config_defaults_when_missing(tmp_path):
    assert SidecarConfig.load(tmp_path / "absent.yaml") == SidecarConfig()


def test_detect_runtime_shape():
    runtime = detect_runtime()
    assert set(runtime) == {"docker", "docker_path", "docker_daemon", "node", "node_path"}
    assert isinstance(runtime["docker"], bool)
    assert isinstance(runtime["docker_daemon"], bool)


def test_pick_install_method_prefers_docker(monkeypatch):
    monkeypatch.setattr("ultron.sidecar.detect_runtime",
                        lambda: {"docker": True, "docker_path": "docker", "docker_daemon": True,
                                 "node": True, "node_path": "node"})
    sidecar = OmniRouteSidecar("http://127.0.0.1:20128")
    assert sidecar.pick_install_method() == "docker"


def test_pick_install_method_falls_back_to_npm_when_daemon_down(monkeypatch):
    monkeypatch.setattr("ultron.sidecar.detect_runtime",
                        lambda: {"docker": True, "docker_path": "docker", "docker_daemon": False,
                                 "node": True, "node_path": "node"})
    sidecar = OmniRouteSidecar("http://127.0.0.1:20128")
    assert sidecar.pick_install_method() == "npm"


def test_pick_install_method_falls_back_to_npm(monkeypatch):
    monkeypatch.setattr("ultron.sidecar.detect_runtime",
                        lambda: {"docker": False, "docker_path": None, "docker_daemon": False,
                                 "node": True, "node_path": "node"})
    sidecar = OmniRouteSidecar("http://127.0.0.1:20128")
    assert sidecar.pick_install_method() == "npm"


def test_pick_install_method_raises_without_node(monkeypatch):
    monkeypatch.setattr("ultron.sidecar.detect_runtime",
                        lambda: {"docker": False, "docker_path": None,
                                 "docker_daemon": False, node_key: None})
    sidecar = OmniRouteSidecar("http://127.0.0.1:20128")
    sidecar.config.install_preference = "npm"
    with pytest.raises(RuntimeError):
        sidecar.pick_install_method()


node_key = "node"


def test_status_shape():
    sidecar = OmniRouteSidecar("http://127.0.0.1:20128")
    status = sidecar.status()
    assert status["base_url"] == "http://127.0.0.1:20128"
    assert status["running"] is False
    assert status["install_progress"]["stage"] == "idle"
    assert "runtime" in status


def test_restart_cap_is_bounded():
    assert RESTART_CAP == 3


def test_supervise_gives_up_after_cap(monkeypatch):
    sidecar = OmniRouteSidecar("http://127.0.0.1:20128")
    sidecar.restart_count = RESTART_CAP
    calls = {"start": 0}

    async def never_healthy():
        return False

    async def fail_start():
        calls["start"] += 1
        return False

    monkeypatch.setattr(sidecar, "healthy", never_healthy)
    monkeypatch.setattr(sidecar, "start", fail_start)

    asyncio.run(asyncio.wait_for(sidecar.supervise(poll_s=0.01), timeout=2))
    assert sidecar.last_error.startswith("restart cap")

