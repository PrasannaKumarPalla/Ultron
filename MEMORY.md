# Ultron — Project Memory

Running log of decisions, gotchas, state. Not auto-loaded by Claude's global
memory system — read this at session start when working in this repo.

## Current state (2026-08-30)
- Branch: `main`. Tags: `phase-0` (backfilled) → `phase-6`. Post-phase-6
  work is on `main` untagged; next tag will be `v0.1.0` on release cut.
- Suite: **244 tests passing, 1 skipped**. PyInstaller build produces
  `dist/Ultron.exe` (~41 MB); CI is green on Windows.
- Distribution: NSIS installer (`installer/ultron.nsi`) + release workflow
  (`.github/workflows/release.yml`) that on `v*.*.*` tag push builds the
  exe and packages `Ultron-Setup-<v>.exe`, uploads both to a GitHub Release.
  Unsigned — SmartScreen warning on first run is documented in the README.
- Absorption: the standalone assistant project is fully absorbed. Live
  embedded assistant SDK is `src/bujji/`; the former standalone tree
  (Android/desktop/frontend/rust) is not part of this repo. Decision map:
  `docs/architecture/bujji-absorption.md`.
- Office UI: **removed in commit 8cd3821** with the OmniRoute work; the
  dependency-free HTML dashboard at `/` is the only surface. `/office`
  returns 404. ADR-0008 and the phase-4/5/6 critic notes were retired to
  match. Do not resurrect without an explicit product decision.

## Phase history (for context; do not treat as current)
- Phase 1: event store (blobs, hash chain, spill), time travel
  (timeline/verify/fork), shadow-git gate, crash-resume proof.
- Phase 2: repo intel (ast graphs + churn), Job-Object sandbox,
  speculative tree search + verifier pruning, 5-fixture bench.
- Phase 3: layered memory + consolidation, warm model pool, plugin tools
  + strict grammars, trace spans + repeat-call reuse estimate.
- Phase 4: (retired) The Office UI.
- Phase 5: one-file exe, first-run onboarding, `scripts/demo_mission.py`
  → PNG screencap + `metrics.json`.
- Phase 6: self-critique over phases 1-5.
- Post-phase-6: OmniRoute sidecar (only outbound bridge to hosted
  models; failovers emit visible `provider.switched` events); bounded
  multi-agent debate node; UI resilience fixes; layered-memory row
  cleanup on workspace delete; Semgrep baseline; office UI + companion
  prune; readiness pass (LICENSE, installer, release workflow).

## Decisions
- Local-first only: no cloud LLM calls except through the OmniRoute
  sidecar; failovers must emit visible `provider.switched` events, never
  silent.
- SQLite is sole datastore — app state and LangGraph checkpoints share
  it. Flat content-addressed blob files beside the DB are the sanctioned
  exception (ADR-0002).
- Shadow-git gate: GIT_DIR redirection into `<workspace>/.ultron-shadow`,
  never a nested real `.git` (ADR-0003). Degrades to pass-through when
  git is unavailable.
- Manual-check missions keep their candidate diff; rollback fires only
  on explicit test failure.
- Code signing intentionally skipped (paid cert). SmartScreen warning
  on first run is documented; installer is per-user, no admin needed.
- Crash reports: none. Local log only at `%LOCALAPPDATA%\Ultron\ultron.log`.
- Auto-update: none in-app. Users check the GitHub Releases page.

## Gotchas
- `.worktrees/` is gitignored; worktrees created there can go stale or
  orphaned if the branch is deleted remotely first — clean up manually.
- `dist/Ultron.exe` and `build/` are gitignored; rebuild via PyInstaller.
- Event appends serialize per run via `BEGIN IMMEDIATE`; connections are
  cached per-thread inside each `Repository` instance — don't close them
  externally.
- Windows cannot delete files held open by cached connections; tests must
  let pytest tmp cleanup happen after `Repository` objects are dropped.
- `bujji.__version__` reads package metadata via `importlib.metadata`.
  The frozen exe must bundle the `dist-info` — `Ultron.spec` does this
  via `copy_metadata('ultron-control-plane')`.
- `data/` is fully gitignored; it holds runtime SQLite + logs and is
  not the workspace-root `data/` "published folder".

## Open threads
- Ollama first-run wizard beyond the download-link dialog.
- Trace viewer UI (was G14).
- Windows code-signing decision if/when a cert is available.
