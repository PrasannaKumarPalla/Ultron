# 0012 — Prerequisite preflight (detect / resolve / install)

- **Date:** 2026-09-01
- **Status:** accepted
- **Context:** A public desktop app cannot assume the user has Ollama, a suitable
  model, or the RAM/VRAM/disk to run one. The old first-run flow was Ollama-only,
  pulled one hardcoded model, downloaded the installer in a single in-memory read,
  and made no hardware or disk check.
- **Decision:** a layered preflight subsystem for Ultron **and** the embedded
  bujji subsystem.
  1. `src/ultron/preflight.py` — `detect_machine()` (OS / arch / RAM / VRAM / GPU
     vendor / free disk / Ollama state / installed models; best-effort, never
     raises, no network) and pure `resolve()` / `recommend_model()` returning a
     `PrereqReport` of typed `Requirement`s. Windows x64 is the supported target;
     the shapes carry enough for macOS / Linux later, and non-Windows profiles are
     flagged detection-only.
  2. `src/ultron/downloader.py` — streamed download with `.part` + HTTP `Range`
     resume, sha256 / min-size verification, atomic promote, bounded retry.
     `bootstrap.download_ollama_installer()` uses it.
  3. `GET /api/preflight` + `POST /api/preflight/install` (SSE) —
     `src/ultron/preflight_install.py` runs one consented action (only those a
     report produced) and streams progress. Ollama install via the resumable
     downloader; model pull via Ollama's `/api/pull` stream.
  4. `desktop_app.bootstrap_ollama_and_models()` is now driven by the report:
     overridable warnings on low RAM / disk, resumable Ollama install, and an
     offer to pull the **hardware-matched** recommended model with its real size.
- **Rejected:**
  - Bundle every dependency in the installer — multi-GB installer, and models
    move faster than releases.
  - Keep assume-and-fail — unacceptable for a public app.
- **Consequences:**
  - The installer stays small; first run needs network.
  - A small hand-maintained model matrix (`preflight._RECOMMENDABLE`: tag, size,
    VRAM floor) must track the models we actually offer.
  - Still open: an in-app first-run **screen** (the current UX is the desktop
    shell's message boxes). The `/api/preflight*` endpoints exist for it.
