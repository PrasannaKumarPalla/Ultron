# Ultron

[![CI](https://github.com/PrasannaKumarPalla/Ultron/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/PrasannaKumarPalla/Ultron/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Local-first autonomous AI engineering studio for Windows. Runs entirely on
your machine against a local Ollama runtime — no hosted model calls, no
telemetry. Ships as a single native desktop app.

## What it does

- Autonomous engineering missions with specialist role hand-offs
  (architect → developer → UI → tester)
- Standalone project chat with workspace-scoped tools (web search, file
  read/write, shell, mission control)
- Persistent, governed project memory with nightly consolidation
- Approval gates and a hash-chained mission event log
- Always-on voice assistant with wake word and hardware-aware model pick
- OmniRoute sidecar for optional hosted-model fallback — off by default,
  the only outbound bridge; failovers are always visible events

## Prerequisites

- **Windows 10/11 x64**
- Practical VRAM floor: **≥ 12 GB** for the recommended chat model;
  smaller Ollama models run on less.

Everything else is offered on first run. On launch Ultron detects:
- **Ollama** — if missing, one click auto-downloads
  [`OllamaSetup.exe`](https://ollama.com/download) into
  `%LOCALAPPDATA%\Ultron\downloads` and installs it silently.
- **At least one chat model** — if none are installed, one click pulls
  the recommended `qwen3.6:27b` (~15 GB) in a separate console window;
  Ultron starts immediately and the model becomes usable as soon as
  the pull completes.

You can decline either step and install Ollama or pull a model manually
later. Advanced users can run `install-recommended-models.ps1` for the
recommended pair.

## Install

Two paths — the built exe or a source build.

### Installer (recommended)

Download `Ultron-Setup-<version>.exe` from the [Releases page](https://github.com/PrasannaKumarPalla/Ultron/releases)
and run it. Installs per-user (no admin), creates Start Menu + Desktop
shortcuts, registers under `Add or remove programs`, and leaves your
data untouched on uninstall.

The installer is **unsigned** — Windows SmartScreen will show a warning
on first run; click "More info" → "Run anyway."

### Built exe (portable)

Place `dist\Ultron.exe` anywhere and double-click. First launch creates:

- `%LOCALAPPDATA%\Ultron\` — SQLite databases, `ultron.log`
- `%USERPROFILE%\Documents\UltronProjects\` — generated project workspaces

No installer, no admin rights, no registry writes outside those paths.

### From source

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Requires Python **≥ 3.12**.

Build the desktop exe:

```powershell
.\.venv\Scripts\python -m PyInstaller --clean --noconfirm Ultron.spec
```

The build produces one file: `dist\Ultron.exe` (~41 MB).

Pull the recommended models:

```powershell
.\install-recommended-models.ps1
```

## Run

Double-click `dist\Ultron.exe`, or from PowerShell:

```powershell
dist\Ultron.exe
```

The native window opens once Ollama reports ready.

### Optional browser mode

For development or troubleshooting, bind loopback-only:

```powershell
.\start-browser.ps1
```

Dashboard at `http://127.0.0.1:8766/`. Stop with `Ctrl+C` or
`.\stop-browser.ps1`.

## Assistant desk

The built-in assistant listens for its wake word (default `assistant`;
legacy `bujji` also accepted) and either answers directly or starts a
mission ("assistant, start a mission called ..."). Voice-triggered work
picks a model filtered by detected VRAM against the bundled catalog.

Configure via `.env`:

```
ULTRON_ASSISTANT_WAKE_WORD=assistant
ULTRON_ASSISTANT_VRAM_GB=12          # optional; auto-detected otherwise
ULTRON_BUJJI_LEGACY_DB=C:\path\old-assistant.db   # one-time fold into unified DB
```

## Test

```powershell
.\.venv\Scripts\python -m pytest -v
```

Current suite: 244 pass, 1 skipped.

## Uninstall

If you installed via the installer: open **Add or remove programs**, find
**Ultron**, and click Uninstall. This removes the app and its shortcuts;
your data (`%LOCALAPPDATA%\Ultron\`) and generated projects
(`%USERPROFILE%\Documents\UltronProjects\`) are deliberately preserved.

If you used the portable `Ultron.exe`:

1. Close Ultron; confirm no `Ultron.exe` in `tasklist`.
2. Delete the file (or wherever you placed it).
3. Optional: delete `%LOCALAPPDATA%\Ultron\` and
   `%USERPROFILE%\Documents\UltronProjects\`.

Ollama and its models are always separate — uninstall via
`Add or remove programs` if you no longer want them.

## License

[AGPL-3.0](LICENSE). You may use, modify, and redistribute Ultron freely.
If you distribute a modified version — or offer it to others over a network — you must
release your changes under the same license. Contributions are accepted under the
Developer Certificate of Origin (see CONTRIBUTING.md).

## Safety model

- File writes are confined to the selected project workspace.
- `.git`, virtual environments, dependency trees, and caches are protected.
- Shell/test commands run without shell interpolation, selected from
  detected test frameworks.
- Mission activity and approval transitions are persisted for auditing.
- Build and test commands run directly on the local machine — Ultron is
  designed for a single trusted operator and does not provide container
  or process isolation.

## Readiness

See [`docs/READY.md`](docs/READY.md) for the acceptance checklist used
to gate "ready to install."
