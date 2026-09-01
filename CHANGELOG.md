# Changelog

All notable changes are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

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
