from pathlib import Path

import pytest

import desktop_app
from ultron import preflight


def test_available_port_is_bindable():
    port = desktop_app.available_port()
    assert 0 < port <= 65535


def _profile(**over):
    base = dict(
        os="Windows", arch="x86_64", ram_gb=32.0, vram_gb=12.0, gpu_vendor="nvidia",
        disk_free_gb=200.0, ollama_installed=True, ollama_running=True,
        installed_models=("qwen3:8b",),
    )
    base.update(over)
    return preflight.MachineProfile(**base)


@pytest.fixture
def _quiet_dialogs(monkeypatch):
    calls = {"confirm": [], "info": [], "warn": [], "popen": []}
    monkeypatch.setattr(desktop_app, "_info", lambda t, ti: calls["info"].append(t))
    monkeypatch.setattr(desktop_app, "_warn", lambda t, ti: calls["warn"].append(t))
    monkeypatch.setattr(desktop_app.subprocess, "Popen",
                        lambda *a, **k: calls["popen"].append(a) or object())
    return calls


def test_bootstrap_noops_when_everything_is_ready(monkeypatch, _quiet_dialogs):
    monkeypatch.setattr(preflight, "detect_machine", lambda: _profile())
    monkeypatch.setattr(desktop_app, "_confirm_yesno",
                        lambda *a, **k: pytest.fail("should not prompt when ready"))
    desktop_app.bootstrap_ollama_and_models()
    assert _quiet_dialogs["popen"] == []


def test_bootstrap_offers_the_recommended_model_when_none_installed(monkeypatch, _quiet_dialogs):
    monkeypatch.setattr(preflight, "detect_machine",
                        lambda: _profile(installed_models=(), vram_gb=24.0))
    monkeypatch.setattr(desktop_app, "_confirm_yesno", lambda *a, **k: True)
    desktop_app.bootstrap_ollama_and_models()
    assert _quiet_dialogs["popen"], "expected an `ollama pull` to be launched"
    launched = _quiet_dialogs["popen"][0][0]
    assert "ollama pull qwen3:14b" in launched[-1]


def test_bootstrap_low_ram_prompts_and_aborts_on_decline(monkeypatch, _quiet_dialogs):
    monkeypatch.setattr(preflight, "detect_machine", lambda: _profile(ram_gb=4.0))
    monkeypatch.setattr(desktop_app, "_confirm_yesno", lambda *a, **k: False)
    with pytest.raises(SystemExit):
        desktop_app.bootstrap_ollama_and_models()


def test_bootstrap_installs_ollama_then_rechecks_for_a_model(monkeypatch, _quiet_dialogs):
    from ultron import bootstrap

    profiles = iter([
        _profile(ollama_installed=False, ollama_running=False, installed_models=()),
        _profile(installed_models=(), vram_gb=8.0),  # after install
    ])
    monkeypatch.setattr(preflight, "detect_machine", lambda: next(profiles))
    monkeypatch.setattr(desktop_app, "_confirm_yesno", lambda *a, **k: True)
    monkeypatch.setattr(bootstrap, "download_ollama_installer", lambda *a, **k: Path("x"))
    monkeypatch.setattr(bootstrap, "install_ollama_silently", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "wait_for_ollama_ready", lambda *a, **k: None)

    desktop_app.bootstrap_ollama_and_models()
    launched = _quiet_dialogs["popen"][0][0]
    assert "ollama pull qwen3:8b" in launched[-1]


def test_desktop_environment_uses_private_user_storage(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "User"))
    for name in (
        "ULTRON_DESKTOP_MODE",
        "ULTRON_ENV",
        "ULTRON_HOST",
        "ULTRON_PORT",
        "ULTRON_DATABASE_PATH",
        "ULTRON_CHECKPOINT_PATH",
        "ULTRON_PROJECTS_ROOT",
        "ULTRON_EXECUTION_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)

    desktop_app.configure_desktop_environment(43210)

    assert desktop_app.os.environ["ULTRON_DESKTOP_MODE"] == "1"
    assert desktop_app.os.environ["ULTRON_HOST"] == "127.0.0.1"
    assert desktop_app.os.environ["ULTRON_PORT"] == "43210"
    assert desktop_app.os.environ["ULTRON_DATABASE_PATH"] == str(
        tmp_path / "AppData" / "Ultron" / "ultron.db"
    )
