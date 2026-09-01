"""The first-run screen is wired into the shell and its assets are served."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository

UI = Path(__file__).resolve().parents[1] / "src" / "ultron" / "ui"


def test_index_html_wires_the_preflight_screen():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert '/assets/preflight.css' in html
    assert '/assets/preflight.js' in html
    assert 'id="preflightDialog"' in html
    assert 'id="preflightBody"' in html
    # loads before app.js so the gate resolves before the dashboard boots
    assert html.index("/assets/preflight.js") < html.index("/assets/app.js")


def test_preflight_assets_are_served(tmp_path):
    settings = Settings(
        database_path=tmp_path / "a.db", checkpoint_path=tmp_path / "c.db",
        projects_root=tmp_path / "p", execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            js = client.get("/assets/preflight.js")
            css = client.get("/assets/preflight.css")
    finally:
        app.dependency_overrides.clear()

    assert js.status_code == 200 and "preflightDialog" in js.text
    assert css.status_code == 200 and "#preflightDialog" in css.text


def test_preflight_js_has_no_confusable_letters():
    # guards against an editor slip like a Cyrillic look-alike in a function name
    src = (UI / "preflight.js").read_text(encoding="utf-8")
    for i, ch in enumerate(src):
        cp = ord(ch)
        if 0x0370 <= cp <= 0x03FF or 0x0400 <= cp <= 0x04FF:  # Greek / Cyrillic
            raise AssertionError(f"confusable char U+{cp:04X} at offset {i}: {src[i-20:i+20]!r}")
