# Absorbing the Assistant Subsystem into Ultron

The standalone personal-assistant project (a separate repository, now retired)
was folded into Ultron. This document maps every original module to its new home
and records which side won each duplicate. Paths in the left column below refer to
that former project's layout.

## Ground rules applied

- Python + FastAPI + SQLite + LangGraph + Ollama only; local-first, no cloud.
- One event bus, one SQLite database, one config file, one packaged `.exe`.
- Nothing from the assistant regresses; parity is enforced by
  `tests/bujji_parity/`.

## Module map

| Original module (former standalone project) | New home | Decision |
|---|---|---|
| `assistant-core/src/bujji/` (Python package) | `src/bujji/` (live, importable) | **Vendored forward.** Ultron imports it directly (`bujji.sdk.Bujji`). |
| `assistant-core/src/bujji/sdk.py` (`Bujji` SDK) | `src/ultron/bujji_bridge.py` wraps it | Kept as-is; the bridge pins it to Ultron's Ollama settings so there is exactly one inference configuration. |
| `core/events.py` (`EventBus`, in-process pub/sub) | **Killed** — `src/ultron/event_bus.py` is the only bus | Ultron's bus persists every event to SQLite and fans out to SSE subscribers; the assistant's was volatile. All desk activity flows through it. |
| `intelligence/model_catalog.py` (hardware-aware catalog with `min_vram_gb`) | Kept in `src/bujji`; surfaced via `src/ultron/assistant_desk.py::pick_hardware_model` | **Assistant version wins** for the assistant desk: VRAM-budgeted selection beats Ultron's keyword lists. Ultron's `model_router.py` stays authoritative for the mission role loop, and is the fallback when detection fails. |
| `engine/ollama.py` (+ other engines) | Kept in `src/bujji` alongside `src/ultron/providers.py` | Both talk to the same loopback Ollama. Missions keep Ultron's provider stack; the assistant keeps its engine abstraction (needed by its agents/tools). Cloud engines remain present but unreachable: no API keys, no network-first config. |
| `speech/wake_word.py`, `speech/pipeline.py`, TTS/STT backends | Kept in `src/bujji`; gated through `src/ultron/assistant_desk.py::heard_wake_word` | The detector's branding resolves the wake word **“assistant”**; a union matcher also accepts the legacy **“bujji”** so nothing stops responding. Mic/audio loops stay optional extras — text-transcript paths are fully testable offline. |
| `tools/` (43 files), `workflow/`, `agents/`, `channels/`, `connectors/`, `mcp/`, `a2a/` | Kept in `src/bujji`, exposed through the bridge/UI where relevant | Capability parity is asserted by `test_parity_packaging.py::test_absorbed_capability_modules_are_vendored`. Ultron's `chat_tools.py` remains the mission/chat tool surface; the assistant's registry serves its own runtime. |
| Memory layer (`tools/storage/sqlite.py`, memory registries) | Folded into Ultron's single SQLite DB via `src/ultron/migrations.py::fold_bujji_database` | **Ultron wins** for storage: one database file, governed memories. Colliding table names land with a `bujji_` prefix; unique names copy verbatim. Idempotent (`fold_if_present`). |
| `configs/bujji/config.toml` | Superseded by `src/ultron/config.py` (`Settings`, one `.env`) | **Ultron wins**: pydantic-settings with `ULTRON_` env overrides is the single config file. The TOML ships as package-default data only. New keys: `ULTRON_ASSISTANT_WAKE_WORD`, `ULTRON_ASSISTANT_VRAM_GB`, `ULTRON_BUJJI_LEGACY_DB`. |
| React frontend (`frontend/`), Tauri desktop (`desktop/`, `rust/`), Android lib | Replaced by Ultron's dependency-free UI tab (`src/ultron/ui/bujji.js`) inside the pywebview shell | **Ultron wins**: one window, one build path. |
| Deploy scripts (`deploy/windows/*.ps1`, PyInstaller/Tauri packaging) | **Killed** — `Ultron.spec` is the only spec | It now collects `bujji` submodules and data files, producing a single `dist/Ultron.exe`. |
| Branding (`branding/brand.json`, wake word) | Repo-root `branding/` bundled by `Ultron.spec` | Display identity is Ultron's; the legacy brand survives only as an accepted wake word. |

## Assistant role

The Assistant is a Role like any other specialist: see the `assistant:` entry in
`src/ultron/roles.yaml` — always-on. It answers directly without launching a
mission, or hands "start mission …" utterances off to the Supervisor with a
hardware-aware model pick attached. HTTP surface: `GET /api/assistant/desk`,
`POST /api/assistant/listen`, `POST /api/assistant/model`.

## Provenance

The former standalone project's own history lives in its original repository.
This repo carries the absorbed result; `tests/bujji_parity/` asserts that no
capability regressed in the move.
