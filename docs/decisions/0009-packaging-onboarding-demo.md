# ADR-0009: One-file exe, first-run onboarding, 3-minute demo mission

Status: ACCEPTED · Phase 5

## Context

Phase 5 wraps the studio for an operator: a single Windows executable, a
first-run overlay, and a self-running 3-minute demo mission with artifacts.

## Decision

1. **PyInstaller one-file** (no flag change): the existing `Ultron.spec`
   gains explicit hidden imports for every post-phase-0 module
   (`store`, `shadow_git`, `repo_intel`, `sandbox`, `search`,
   `memory_layers`, `model_pool`, `tools_registry`, `builtin_tools`,
   `trace`) plus excludes for `bench`/`tests`/`pytest`/`coverage`, so the
   bundle stays complete and lean. Verified: `dist/Ultron.exe` builds at
   ~41 MB with the dashboard UI, roles.yaml, and the absorbed assistant inside.
2. **First-run onboarding**: the dashboard shows a one-time `#onboard` overlay
   (localStorage-guarded, `?skiponboard=1` escape for captures).
3. **Demo mission** (`scripts/demo_mission.py`): boot the real FastAPI app,
   run a deterministic but *real-pipeline* autonomous mission (EventBus,
   shadow-git gate, speculative beam=2 through the actual workflow), then
   probe every surface (event-chain verify, time travel, layered memory
   + consolidation, repo intel, plugin tools, trace spans), optionally
   snapshot the dashboard via msedge headless, and write `metrics.json`.

## Consequences

- The demo mission doubles as a smoke test for the full stack and validates
  the health gate (`COMPLETED`, verified chain, `search.*` + `shadow.*`
  events, lessons, strict tool grammars).
- Edge absence skips the screenshot; `metrics.json` records `capture: "none"` and the demo still exits 0 when health passes.