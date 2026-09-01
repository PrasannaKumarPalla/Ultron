"""Preflight resolution tests: pure logic, no network, no subprocess."""

from __future__ import annotations

from ultron import preflight
from ultron.preflight import MachineProfile


def _profile(**over) -> MachineProfile:
    base = dict(
        os="Windows", arch="x86_64", ram_gb=32.0, vram_gb=12.0, gpu_vendor="nvidia",
        disk_free_gb=200.0, ollama_installed=True, ollama_running=True,
        installed_models=("qwen3:8b",),
    )
    base.update(over)
    return MachineProfile(**base)


# --- recommend_model ------------------------------------------------------- #

def test_recommends_largest_model_within_vram():
    tag, _reason, degraded = preflight.recommend_model(_profile(vram_gb=24.0))
    assert tag == "qwen3:14b"
    assert degraded is False


def test_small_vram_gpu_still_gets_a_gpu_model():
    tag, _reason, degraded = preflight.recommend_model(_profile(vram_gb=6.0))
    assert tag == "qwen3:4b"
    assert degraded is False


def test_no_gpu_falls_back_to_cpu_model_and_flags_degraded():
    tag, _reason, degraded = preflight.recommend_model(
        _profile(vram_gb=None, gpu_vendor=None, ram_gb=16.0)
    )
    assert tag == "qwen3:4b"
    assert degraded is True


def test_tiny_ram_no_gpu_recommends_nothing():
    tag, _reason, degraded = preflight.recommend_model(
        _profile(vram_gb=None, gpu_vendor=None, ram_gb=4.0)
    )
    assert tag is None
    assert degraded is True


def test_integrated_intel_gpu_is_not_usable():
    p = _profile(vram_gb=None, gpu_vendor="intel", ram_gb=16.0)
    assert p.has_usable_gpu is False
    tag, _reason, degraded = preflight.recommend_model(p)
    assert tag == "qwen3:4b" and degraded is True


# --- resolve -------------------------------------------------------------- #

def test_fully_provisioned_machine_is_ready_and_not_degraded():
    report = preflight.resolve(_profile())
    assert report.ready is True
    assert report.degraded is False
    assert all(r.status == "ok" for r in report.requirements if r.blocking)


def test_missing_ollama_is_blocking_with_an_install_action():
    report = preflight.resolve(_profile(ollama_installed=False, ollama_running=False))
    ollama = next(r for r in report.requirements if r.key == "ollama")
    assert ollama.status == "missing" and ollama.blocking
    assert ollama.action == "install_ollama"
    assert report.ready is False


def test_missing_model_yields_a_pull_action_for_the_recommended_tag():
    report = preflight.resolve(_profile(installed_models=(), vram_gb=12.0))
    model = next(r for r in report.requirements if r.key == "model")
    assert model.status == "missing" and model.blocking
    assert model.action == "pull_model:qwen3:8b"
    assert model.download_mb and model.download_mb > 0
    assert report.recommended_model == "qwen3:8b"


def test_insufficient_disk_for_the_model_download_blocks():
    report = preflight.resolve(_profile(installed_models=(), disk_free_gb=3.0, vram_gb=12.0))
    disk = next(r for r in report.requirements if r.key == "disk")
    assert disk.status == "insufficient" and disk.blocking
    assert report.ready is False


def test_installed_model_means_disk_need_is_just_headroom():
    report = preflight.resolve(_profile(disk_free_gb=6.0))
    disk = next(r for r in report.requirements if r.key == "disk")
    assert disk.status == "ok"


def test_low_ram_is_blocking():
    report = preflight.resolve(_profile(ram_gb=6.0))
    ram = next(r for r in report.requirements if r.key == "ram")
    assert ram.status == "insufficient" and ram.blocking
    assert report.ready is False


def test_cpu_only_machine_is_ready_but_degraded_with_a_note():
    report = preflight.resolve(
        _profile(vram_gb=None, gpu_vendor=None, ram_gb=32.0, installed_models=("qwen3:4b",))
    )
    assert report.ready is True
    assert report.degraded is True
    assert any("CPU" in n for n in report.notes)


def test_unknown_ram_and_disk_are_not_blocking():
    report = preflight.resolve(_profile(ram_gb=None, disk_free_gb=None))
    for key in ("ram", "disk"):
        req = next(r for r in report.requirements if r.key == key)
        assert req.status == "unknown" and not req.blocking


def test_non_windows_profile_gets_a_detection_only_note():
    report = preflight.resolve(_profile(os="Linux"))
    assert any("not a supported target" in n for n in report.notes)


def test_min_free_disk_override_is_respected():
    report = preflight.resolve(_profile(disk_free_gb=10.0), min_free_disk_gb=50.0)
    disk = next(r for r in report.requirements if r.key == "disk")
    assert disk.status == "insufficient"


# --- to_dict ------------------------------------------------------------- #

def test_to_dict_is_json_serialisable_and_complete():
    import json

    report = preflight.resolve(_profile(installed_models=()))
    blob = json.dumps(preflight.to_dict(report))
    round_tripped = json.loads(blob)
    assert round_tripped["recommended_model"]
    assert round_tripped["profile"]["os"] == "Windows"
    assert {r["key"] for r in round_tripped["requirements"]} == {"ollama", "model", "disk", "ram"}


# --- detection smoke (no assertions on values; just that it never raises) --- #

def test_detect_machine_never_raises(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    monkeypatch.setattr(preflight, "_ollama_running", lambda timeout=2.0: False)
    monkeypatch.setattr(preflight, "_detect_gpu", lambda: (None, None))
    monkeypatch.setattr(preflight, "_detect_ram_gb", lambda: None)
    monkeypatch.setattr(preflight, "_disk_free_gb", lambda: None)
    profile = preflight.detect_machine()
    assert isinstance(profile, MachineProfile)
    assert profile.installed_models == ()
