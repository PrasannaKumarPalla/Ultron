"""Machine detection and prerequisite resolution for Ultron + the embedded bujji subsystem.

IO detection (`detect_machine`) is kept separate from the resolution logic
(`resolve`, `recommend_model`) so the decision-making is pure and fully testable:
feed a `MachineProfile` in, get plain dataclasses out.

Windows x64 is the only supported target today. `MachineProfile` and `Requirement`
carry enough to add macOS / Linux behind the same interface later; the detection
helpers already fall back to ``None`` on other platforms rather than raising.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

OLLAMA_INSTALLER_MB = 700  # Windows OllamaSetup.exe, approximate
MIN_RUNNABLE_RAM_GB = 8.0
DISK_HEADROOM_GB = 4.0

# Curated download targets, largest first. (ollama tag, approx download GB, VRAM floor GB).
# A short hand-maintained list rather than the full bujji catalog: these are the only
# models Ultron will offer to pull on first run, and the catalog has no download sizes.
_RECOMMENDABLE: tuple[tuple[str, float, float], ...] = (
    ("qwen3:14b", 9.0, 16.0),
    ("qwen3:8b", 5.2, 8.0),
    ("qwen3:4b", 2.6, 4.0),
    ("qwen3:1.7b", 1.4, 0.0),
)


@dataclass(frozen=True)
class MachineProfile:
    os: str  # "Windows" | "Darwin" | "Linux" | ""
    arch: str  # "x86_64" | "arm64" | "x86" | ...
    ram_gb: float | None
    vram_gb: float | None
    gpu_vendor: str | None  # "nvidia" | "amd" | "intel" | "apple" | None
    disk_free_gb: float | None
    ollama_installed: bool
    ollama_running: bool
    installed_models: tuple[str, ...]

    @property
    def has_usable_gpu(self) -> bool:
        return self.gpu_vendor in {"nvidia", "apple"} and (self.vram_gb or 0.0) >= 4.0


@dataclass(frozen=True)
class Requirement:
    key: str  # "ollama" | "model" | "disk" | "ram"
    label: str
    status: str  # "ok" | "missing" | "insufficient" | "unknown"
    detail: str
    blocking: bool = False
    download_mb: int | None = None
    disk_needed_mb: int | None = None
    action: str | None = None  # "install_ollama" | "pull_model:<tag>" | None


@dataclass(frozen=True)
class PrereqReport:
    profile: MachineProfile
    requirements: tuple[Requirement, ...]
    recommended_model: str | None
    model_reason: str
    ready: bool  # every blocking requirement is "ok"
    degraded: bool  # will run, but CPU-only or on a fallback-small model
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# detection                                                                    #
# --------------------------------------------------------------------------- #

def _norm_arch(raw: str) -> str:
    raw = raw.lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(raw, raw)


def _detect_ram_gb() -> float | None:
    if platform.system() == "Windows":
        try:
            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / 1024**3, 1)
        except (OSError, AttributeError) as exc:  # pragma: no cover - platform quirk
            logger.debug("GlobalMemoryStatusEx failed: %s", exc)
    try:
        import psutil  # optional; present via the bujji subsystem's deps

        return round(psutil.virtual_memory().total / 1024**3, 1)
    except Exception:  # pragma: no cover - psutil absent
        return None


def _detect_gpu() -> tuple[float | None, str | None]:
    """Return (vram_gb, vendor). Best effort, never raises, never hits the network."""
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            done = subprocess.run(
                [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            mibs = [
                float(line.split(",")[0])
                for line in done.stdout.splitlines()
                if line.strip() and line.split(",")[0].strip().replace(".", "").isdigit()
            ]
            if mibs:
                return round(max(mibs) / 1024.0, 1), "nvidia"
        except (OSError, subprocess.SubprocessError, ValueError) as exc:  # pragma: no cover
            logger.debug("nvidia-smi query failed: %s", exc)

    if platform.system() == "Windows":
        try:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | "
                 "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            import json

            raw = json.loads(done.stdout or "null")
            cards = raw if isinstance(raw, list) else [raw] if raw else []
            best_ram = 0
            vendor = None
            for card in cards:
                name = (card.get("Name") or "").lower()
                ram = int(card.get("AdapterRAM") or 0)
                this_vendor = (
                    "nvidia" if "nvidia" in name or "geforce" in name or "rtx" in name or "quadro" in name
                    else "amd" if "amd" in name or "radeon" in name
                    else "intel" if "intel" in name or "arc" in name
                    else None
                )
                if ram >= best_ram:
                    best_ram, vendor = ram, this_vendor or vendor
            # AdapterRAM is a signed 32-bit field: it saturates/negatives past 4 GB and is
            # unreliable for modern cards. Report the vendor but not a bogus VRAM number.
            vram = round(best_ram / 1024**3, 1) if 0 < best_ram < 4 * 1024**3 else None
            return vram, vendor
        except (OSError, subprocess.SubprocessError, ValueError) as exc:  # pragma: no cover
            logger.debug("Win32_VideoController query failed: %s", exc)

    if platform.system() == "Darwin" and _norm_arch(platform.machine()) == "arm64":
        # Apple Silicon shares system RAM with the GPU; treat total RAM as the budget.
        return _detect_ram_gb(), "apple"

    return None, None


def _ollama_running(timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=timeout):
            return True
    except OSError:
        return False


def _installed_models() -> tuple[str, ...]:
    if not _ollama_running():
        return ()
    try:
        import json
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as resp:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected,python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return ()
    return tuple(m["name"] for m in payload.get("models", []) if m.get("name"))


def _disk_free_gb() -> float | None:
    try:
        target = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return round(shutil.disk_usage(str(target)).free / 1024**3, 1)
    except OSError:  # pragma: no cover
        return None


def detect_machine() -> MachineProfile:
    vram, vendor = _detect_gpu()
    return MachineProfile(
        os=platform.system(),
        arch=_norm_arch(platform.machine()),
        ram_gb=_detect_ram_gb(),
        vram_gb=vram,
        gpu_vendor=vendor,
        disk_free_gb=_disk_free_gb(),
        ollama_installed=shutil.which("ollama") is not None,
        ollama_running=_ollama_running(),
        installed_models=_installed_models(),
    )


# --------------------------------------------------------------------------- #
# resolution (pure)                                                            #
# --------------------------------------------------------------------------- #

def recommend_model(profile: MachineProfile) -> tuple[str | None, str, bool]:
    """(tag, reason, degraded). `degraded` = CPU-only or forced onto a small model."""
    if profile.has_usable_gpu:
        budget = profile.vram_gb or 0.0
        for tag, _gb, floor in _RECOMMENDABLE:
            if floor <= budget:
                return tag, f"largest model within {budget:.0f} GB VRAM ({tag}, floor {floor:.0f} GB)", False
        return _RECOMMENDABLE[-1][0], "smallest model; GPU VRAM is below every floor", True

    ram = profile.ram_gb or 0.0
    if ram >= 32:
        return "qwen3:8b", "no usable GPU; 8B runs on CPU with 32 GB+ RAM (slow)", True
    if ram >= 16:
        return "qwen3:4b", "no usable GPU; 4B is the CPU-friendly choice at 16 GB RAM", True
    if ram >= MIN_RUNNABLE_RAM_GB:
        return "qwen3:1.7b", "no usable GPU and limited RAM; 1.7B only", True
    return None, "insufficient RAM to run any bundled model", True


def resolve(profile: MachineProfile, *, min_free_disk_gb: float | None = None) -> PrereqReport:
    reqs: list[Requirement] = []
    notes: list[str] = []
    tag, reason, degraded = recommend_model(profile)

    # --- Ollama ----------------------------------------------------------- #
    if profile.ollama_installed or profile.ollama_running:
        reqs.append(Requirement(
            "ollama", "Ollama runtime", "ok",
            "running" if profile.ollama_running else "installed, not started",
        ))
    else:
        reqs.append(Requirement(
            "ollama", "Ollama runtime", "missing",
            "not found — required to run any model", blocking=True,
            download_mb=OLLAMA_INSTALLER_MB, action="install_ollama",
        ))

    # --- a chat model ---------------------------------------------------- #
    if profile.installed_models:
        reqs.append(Requirement(
            "model", "Local chat model", "ok",
            f"{len(profile.installed_models)} installed "
            f"({', '.join(profile.installed_models[:3])}"
            f"{'…' if len(profile.installed_models) > 3 else ''})",
        ))
        model_dl_mb = 0
    elif tag is None:
        reqs.append(Requirement(
            "model", "Local chat model", "insufficient",
            reason, blocking=True,
        ))
        model_dl_mb = 0
    else:
        dl_gb = next((gb for t, gb, _ in _RECOMMENDABLE if t == tag), 5.0)
        model_dl_mb = int(dl_gb * 1024)
        reqs.append(Requirement(
            "model", "Local chat model", "missing",
            f"none installed — recommend {tag}: {reason}", blocking=True,
            download_mb=model_dl_mb, disk_needed_mb=model_dl_mb,
            action=f"pull_model:{tag}",
        ))

    # --- disk ----------------------------------------------------------- #
    need_gb = (min_free_disk_gb if min_free_disk_gb is not None
               else (model_dl_mb / 1024) + DISK_HEADROOM_GB)
    if profile.disk_free_gb is None:
        reqs.append(Requirement("disk", "Free disk space", "unknown", "could not read free space"))
    elif profile.disk_free_gb >= need_gb:
        reqs.append(Requirement(
            "disk", "Free disk space", "ok",
            f"{profile.disk_free_gb:.0f} GB free (need ~{need_gb:.0f} GB)",
        ))
    else:
        reqs.append(Requirement(
            "disk", "Free disk space", "insufficient",
            f"{profile.disk_free_gb:.0f} GB free, need ~{need_gb:.0f} GB", blocking=True,
        ))

    # --- RAM ------------------------------------------------------------ #
    ram = profile.ram_gb
    if ram is None:
        reqs.append(Requirement("ram", "System memory", "unknown", "could not read total RAM"))
    elif ram < MIN_RUNNABLE_RAM_GB:
        reqs.append(Requirement(
            "ram", "System memory", "insufficient",
            f"{ram:.0f} GB — {MIN_RUNNABLE_RAM_GB:.0f} GB is the floor", blocking=True,
        ))
    else:
        reqs.append(Requirement("ram", "System memory", "ok", f"{ram:.0f} GB"))

    if not profile.has_usable_gpu and ram is not None and ram >= MIN_RUNNABLE_RAM_GB:
        notes.append("No usable GPU detected — models run on CPU. Expect slow responses.")
    if profile.os and profile.os != "Windows":
        notes.append(f"{profile.os} is not a supported target yet; detection only.")
    if profile.arch and profile.arch not in {"x86_64", "arm64"}:
        notes.append(f"Unsupported CPU architecture: {profile.arch}.")

    ready = all(r.status == "ok" for r in reqs if r.blocking)
    return PrereqReport(
        profile=profile,
        requirements=tuple(reqs),
        recommended_model=tag,
        model_reason=reason,
        ready=ready,
        degraded=degraded and ready,
        notes=tuple(notes),
    )


def to_dict(report: PrereqReport) -> dict:
    """JSON-serialisable view for the /api/preflight surface."""
    p = report.profile
    return {
        "profile": {
            "os": p.os, "arch": p.arch, "ram_gb": p.ram_gb, "vram_gb": p.vram_gb,
            "gpu_vendor": p.gpu_vendor, "disk_free_gb": p.disk_free_gb,
            "ollama_installed": p.ollama_installed, "ollama_running": p.ollama_running,
            "installed_models": list(p.installed_models),
        },
        "requirements": [
            {
                "key": r.key, "label": r.label, "status": r.status, "detail": r.detail,
                "blocking": r.blocking, "download_mb": r.download_mb,
                "disk_needed_mb": r.disk_needed_mb, "action": r.action,
            }
            for r in report.requirements
        ],
        "recommended_model": report.recommended_model,
        "model_reason": report.model_reason,
        "ready": report.ready,
        "degraded": report.degraded,
        "notes": list(report.notes),
    }


__all__ = [
    "MachineProfile", "Requirement", "PrereqReport",
    "detect_machine", "recommend_model", "resolve", "to_dict",
]
