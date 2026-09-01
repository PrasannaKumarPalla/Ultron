from pathlib import Path

import desktop_app


def test_available_port_is_bindable():
    port = desktop_app.available_port()
    assert 0 < port <= 65535


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
