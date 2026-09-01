# ADR-0003: Shadow-git gate for candidate diffs

Status: ACCEPTED · Phase 1

## Context

Roles currently write candidate files straight into the project workspace;
a red test run leaves the workspace dirty and undo means hand-reverting
files. Target G3 requires: candidate diffs behind `<workspace>/.ultron-shadow`,
fast-forward into the workspace only when tests pass, undo = reset.

## Decision

`src/ultron/shadow_git.py` introduces `ShadowGit`, a git repo whose GIT_DIR
is `<workspace>/.ultron-shadow` and whose work tree is the workspace itself:

- `main` holds the accepted baseline; `ultron-candidate` holds in-flight work.
- Mission nodes call `begin_candidate()` before any write-capable role runs
  (team execution, repair-loop developer). It repoints the candidate branch
  at main and hard-switches the workspace to baseline, so failed candidates
  from prior iterations are wiped before retrying.
- After the deterministic test run, `candidate_commit()` snapshots the diff.
  On green, `fast_forward()` moves main (`--ff-only`) — the workspace keeps
  the change. On explicit test failure at completion, `rollback()` restores
  baseline. Manual-check missions (no test framework detected) keep their
  candidate for operator review instead of discarding it.
- Every transition emits an auditable event: `shadow.candidate_opened`,
  `shadow.candidate_committed`, `shadow.forwarded`, `shadow.rolled_back`,
  `shadow.unavailable`.
- If the git executable is missing or any git operation fails, the gate logs
  `shadow.unavailable` once and degrades to today's direct-write behavior.
  Single-operator local tool: availability degradation beats blocking work.
- `WorkspaceGuard` now rejects writes under `.ultron-shadow` and hides it
  from prompt snapshots.

## Alternatives rejected

- **Real nested .git in each workspace**: collides with projects that are
  already git repos and leaks agent branches into the operator's history.
  GIT_DIR redirection keeps the operator's repo untouched.
- **Windows Job Objects for FS virtualization / copies of the tree**: heavy,
  slow for big trees, new deps. Git already gives content-addressed storage
  and cheap resets.
- **Stashing instead of branches**: stash is single-slot global state; two
  concurrent missions on one workspace would corrupt it. Branches scope the
  candidate to explicit refs.

## Consequences

- Undo is exactly `git checkout main && git reset --hard` semantics — already
  the operator's mental model.
- Workspaces gain one hidden directory; deleting it merely un-gates history.
- Tests must run against the candidate worktree (they do — the workspace IS
  the worktree during the gated window).
