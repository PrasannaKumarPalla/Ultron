# Ultron — Repo Instructions

Local-first autonomous AI engineering studio. FastAPI control plane + SQLite + local Ollama models + LangGraph mission role loop, packaged as a native Windows desktop app.

## Stack
- Python 3.12+, FastAPI, SQLite (single datastore — app state + LangGraph checkpoints)
- LangGraph for mission role loop (architect → developer → UI expert → tester → ...)
- Local Ollama (`/api/chat`) is the default engine; the OmniRoute sidecar
  (OpenAI-compatible at `http://localhost:20128/v1`) is the ONLY outbound bridge
  to hosted models. Ultron never calls a hosted provider directly.
- Frontend: dependency-free HTML/CSS/JS (`src/ultron/ui/`), no build step
- Desktop shell: pywebview + PyInstaller (`Ultron.spec` → `dist/Ultron.exe`)

## Layout
- `src/ultron/api.py` — FastAPI app, routes
- `src/ultron/agent_runtime.py`, `workflow.py`, `team_planner.py` — mission/role loop
- `src/ultron/chat_engine.py`, `chat_tools.py` — standalone chat tab (web search, file r/w, shell, mission control)
- `src/ultron/model_router.py`, `providers.py` — Ollama model selection/routing
- `src/ultron/security_scan.py` — pre-mission workspace security gate
- `src/ultron/db.py`, `models.py` — SQLite persistence
- `src/ultron/config.py` — settings (pydantic-settings, reads `.env`)
- `src/ultron/bujji_bridge.py` + `src/bujji/` — embedded assistant subsystem (absorbed from a former standalone project; see `docs/architecture/bujji-absorption.md`). The bridge drives its SDK (`bujji.sdk.Bujji`) on Ultron's Ollama runtime; UI tab in `src/ultron/ui/bujji.js`, routes under `/api/bujji/*`.
- `src/ultron/assistant_desk.py` — the "Assistant" Role: wake-word gating, direct-answer vs mission-trigger routing, VRAM-budgeted model pick. Routes under `/api/assistant/*`.
- `src/ultron/migrations.py` — folds legacy assistant SQLite databases into the single Ultron DB (`bujji_` prefix on name collisions).
- `tests/` — pytest, mirrors `src/ultron/*.py` one-to-one; `tests/bujji_parity/` guards every absorbed capability.
- `docs/architecture/bujji-absorption.md` — module-by-module absorption map and duplicate-resolution decisions.
- `docs/` — living docs (`READY.md`, `omniroute.md`, `architecture/bujji-absorption.md`) plus ADRs under `decisions/` and phase critic notes under `issues/`
- `.worktrees/` — gitignored, do not commit large scratch worktrees here long-term

## Commands
```powershell
.\.venv\Scripts\python -m uvicorn ultron.api:app --host 127.0.0.1 --port 8766   # run
.\.venv\Scripts\python -m pytest -v                                             # test
```

## Execution safety model (do not weaken without explicit ask)
- `WorkspaceGuard` confines all file writes to the selected project workspace; `.git`, venvs, dependency trees, cache dirs are protected.
- Shell/test commands run without shell interpolation, selected from detected test frameworks.
- Security scan gate runs before mission changes are accepted.
- No container/process isolation by design — local-first, single-operator tool. Accepted risk, not a bug.
- Every role/tool call/test result/retry/approval/transition is an auditable event — preserve this when touching `workflow.py` or `agent_runtime.py`.

## Conventions
- No comments unless WHY is non-obvious.
- No error handling for impossible scenarios; validate only at boundaries (user input, Ollama responses, workspace paths).
- Hosted-model traffic flows only through the local OmniRoute sidecar; failover between providers must be visible (`provider.switched` events), never silent.
- New tests go in `tests/test_<module>.py` matching `src/ultron/<module>.py`.

## CI
Push/PR to `main` runs pytest, Semgrep SAST, TruffleHog secret scan. Dependabot auto-merges non-major bumps after checks pass.
