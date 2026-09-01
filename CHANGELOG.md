# Changelog

All notable changes are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [0.1.1] - 2026-09-01

### Added
- Prerequisite preflight (ADR 0012): `GET /api/preflight` machine report
  (OS / arch / RAM / VRAM / GPU / free disk / Ollama state / installed models)
  and `POST /api/preflight/install` (SSE) for consented installs.
- In-app first-run screen: a blocking dialog that lists what is missing, installs
  Ollama and pulls the hardware-matched model with live progress, and closes when
  the machine is ready.
- Resumable, checksum-verified downloads (`ultron.downloader`) with HTTP Range
  resume and bounded retry; the Ollama installer now uses it.

### Changed
- Desktop first-run is driven by the preflight report: overridable warnings on low
  RAM / disk, and it offers the recommended model for the detected hardware rather
  than one hardcoded tag.
- Win32 GPU detection no longer reports a VRAM figure from `AdapterRAM` (unreliable
  on modern cards); only `nvidia-smi` VRAM is trusted.

## [0.1.0] - 2026-09-01

First public release.

### Added
- Autonomous engineering missions with specialist role hand-offs
  (architect -> developer -> UI -> tester) over a LangGraph loop.
- Standalone project chat with workspace-scoped tools (web search, file
  read/write, shell, mission control).
- Local Ollama runtime as the default engine; OmniRoute sidecar as the only
  outbound bridge to hosted models, with visible `provider.switched` failover.
- Embedded assistant subsystem (`src/bujji/`) with wake-word gating and
  VRAM-budgeted model selection.
- Hash-chained event store with time-travel (timeline / verify / fork) and a
  shadow-git gate that never touches a real `.git`.
- Single-file `dist/Ultron.exe` via PyInstaller; per-user NSIS installer
  (`Ultron-Setup-<version>.exe`); GitHub Release on `v*.*.*` tags.
- CI: pytest, Semgrep SAST, TruffleHog secret scan; Dependabot auto-merge for
  non-major bumps after checks pass.

### Notes
- Windows x64 only for now.
- Unsigned build - SmartScreen shows a first-run warning (documented in the README).
- No telemetry, no crash reporting, no in-app auto-update.
