# ADR-0005: Job-Object sandbox without pywin32

Status: ACCEPTED · Phase 2

## Context

Target G6 wants sandboxed execution: network denied by default, filesystem
scoped to the workspace. Windows options were Windows Sandbox (WSB), a
stripped process token, or Job Objects.

## Decision

`src/ultron/sandbox.py` wraps every workspace command in a Job Object built
via raw ctypes (no pywin32):

- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: terminating the studio can never
  orphan a test-runner tree.
- `JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 32`: caps process fan-out.
- `JOB_OBJECT_LIMIT_PROCESS_MEMORY = 2 GiB`: caps runaway allocations.
- Wall-clock cap stays ours: `communicate(timeout=...)` then
  `TerminateJobObject` — exact, and kills the whole tree at once.

Non-Windows hosts fall back to plain `subprocess.run` (dev convenience only;
the shipping target is Windows).

## Alternatives rejected

- **Windows Sandbox / WSB**: needs the optional Windows Sandbox feature,
  per-run VM boot cost of tens of seconds, and a .WBConfig transport story.
  Violates "faster at runtime".
- **Restricted token / AppContainer**: requires LogonUser + profile plumbing
  or a service; heavy and brittle from a desktop app context.
- **pywin32**: a large dep for ~40 lines of ctypes we fully control.

## Honest limitation

Network denial is NOT enforced by this sandbox — that requires WFP filters
or a stripped token, neither reachable cleanly in-process. Filesystem scoping
remains WorkspaceGuard's job. This is recorded as accepted risk for a
single-operator local tool, matching the repo's existing safety model.

## Consequences

- Every shell/test command now dies with its parent and cannot fork-bomb.
- Timeout kills are tree-wide and instant (no zombie pytest children).
- If SetInformationJobObject fails, execution degrades to kill-on-close only.
