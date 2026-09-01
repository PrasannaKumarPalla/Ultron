"""Parity: the absorbed subsystem ships inside the single Ultron.exe."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pyinstaller_spec_bakes_bujji_into_one_exe():
    spec = (ROOT / "Ultron.spec").read_text(encoding="utf-8")
    assert 'collect_submodules("bujji")' in spec
    assert 'collect_data_files("bujji")' in spec
    assert 'name="Ultron"' in spec


def test_packaging_declares_bujji_package_data():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.setuptools.package-data]" in pyproject
    assert 'bujji = [' in pyproject


def test_absorbed_capability_modules_are_vendored():
    bujji_root = ROOT / "src" / "bujji"
    expected = [
        "sdk.py",
        "speech/pipeline.py",
        "speech/wake_word.py",
        "intelligence/model_catalog.py",
        "engine/ollama.py",
        "tools/shell_exec.py",
        "tools/windows_control.py",
        "tools/text_to_speech.py",
        "workflow/engine.py",
        "memory",
        "system",
        "security/guardrails.py",
        "telemetry/store.py",
        "traces/store.py",
        "channels",
        "agents/orchestrator.py",
        "cli/__init__.py",
    ]
    for relative in expected:
        candidate = bujji_root / relative
        assert candidate.exists(), f"missing absorbed capability: {relative}"


def test_assistant_assets_are_bundled():
    assert (ROOT / "branding" / "brand.json").exists()
