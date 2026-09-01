---
name: ultron-dev
description: Use when working in the Ultron repo (Ultron AI/Ultron) — control plane / mission / chat / model-router changes, running tests, packaging desktop app. Auto-trigger on "ultron", "mission", "workflow.py", "agent_runtime", "control plane".
---

# Ultron Dev Workflow

Local-first FastAPI + SQLite + LangGraph + Ollama control plane. Full context in [CLAUDE.md](../../CLAUDE.md), state/decisions in [MEMORY.md](../../MEMORY.md).

## Before touching code
1. `git status` — repo has had orphaned `.worktrees/` before; check for stray dirs.
2. Read `MEMORY.md` for current state/gotchas.

## Workflow
1. Activate venv commands via `.\.venv\Scripts\python`, never bare `python`.
2. Run relevant test file before and after change: `.\.venv\Scripts\python -m pytest tests/test_<module>.py -v`.
3. Touching `workflow.py`, `agent_runtime.py`, or `team_planner.py`? Preserve the auditable-event trail (every role/tool call/test/retry/approval/transition must still emit an event) — this is load-bearing for the governed-memory and approval-gate features.
4. Touching `security_scan.py` or `WorkspaceGuard`-adjacent code (in `api.py`/`config.py`)? Treat as a safety boundary — do not loosen without explicit user confirmation.
5. New source module → matching `tests/test_<module>.py`.
6. Full suite before considering done: `.\.venv\Scripts\python -m pytest -v`.

## Hard constraints (do not violate silently)
- No cloud LLM calls — Ollama only.
- No second datastore — SQLite only.
- No container/process isolation for shell/test execution — accepted design, not a gap to "fix".

## After nontrivial work
Append a line to `MEMORY.md` under Open threads / Decisions if you learned something non-obvious (gotcha, rejected approach, why).
