"""self_dev tool — BUJJI improves its own codebase.

Given a task ("fix the TTS gap", "add a weather tool"), this tool:
  1. locates its own repo root,
  2. creates a branch  self-dev/<slug>,
  3. runs a coding agent on the task (Claude Code CLI if installed,
     otherwise the local Ollama-backed `bujji ask` coding agent),
  4. sanity-compiles the Python package,
  5. reports the diff for the user to review, merge, and restart.

The running process is never modified in place — changes land on a branch.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec

_GUARDRAILS = (
    "You are modifying the B.U.J.J.I assistant's own repository. "
    "Read CLAUDE.md and docs/BUJJI_BUILD_PLAN.md first. "
    "Make focused changes only; do not start dev servers or browsers; "
    "verify by compiling/building only. Task: "
)


def _find_repo_root() -> Path | None:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "START_ASSISTANT.bat").exists() or (parent / ".git").is_dir():
            return parent
    return None


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace",
    )


@ToolRegistry.register("self_dev")
class SelfDevTool(BaseTool):
    """Run a coding agent against BUJJI's own repo on a safe branch."""

    tool_id = "self_dev"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="self_dev",
            description=(
                "Improve BUJJI's own source code: fix bugs in the assistant, add "
                "features or tools to it, refactor it. Creates a git branch, runs a "
                "coding agent, verifies the build, and reports the diff. Use whenever "
                "the user asks to change/fix/upgrade BUJJI itself."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear description of the change to make to BUJJI's codebase",
                    },
                    "timeout_minutes": {
                        "type": "number",
                        "description": "Max minutes for the coding agent (default 20)",
                    },
                },
                "required": ["task"],
            },
            category="development",
            latency_estimate=300.0,
            timeout_seconds=1800.0,
        )

    def execute(self, task: str = "", timeout_minutes: float = 20, **_: Any) -> ToolResult:  # type: ignore[override]
        if not task.strip():
            return ToolResult(tool_name=self.tool_id, content="Empty task.", success=False)

        repo = _find_repo_root()
        if repo is None:
            return ToolResult(
                tool_name=self.tool_id,
                content="Could not locate BUJJI repo root (no .git/START_ASSISTANT.bat found).",
                success=False,
            )

        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40] or "task"
        branch = f"self-dev/{slug}-{int(time.time())}"
        timeout_s = max(60.0, float(timeout_minutes) * 60.0)

        # Refuse to run on a dirty tree: creating a branch then `git add -A`
        # would sweep the user's unrelated uncommitted work onto the self-dev
        # branch and remove it from their working tree on switch-back.
        dirty = _run(["git", "status", "--porcelain"], repo, 30).stdout.strip()
        if dirty:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "Working tree has uncommitted changes; refusing to run "
                    "self-dev (it would commit your unrelated edits onto the "
                    "self-dev branch). Commit or stash them first, then retry."
                ),
                success=False,
            )

        try:
            base = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo, 30).stdout.strip()
            if base == "HEAD":
                return ToolResult(
                    tool_name=self.tool_id,
                    content="Repo is in detached-HEAD state; check out a branch before running self-dev.",
                    success=False,
                )
            r = _run(["git", "checkout", "-b", branch], repo, 30)
            if r.returncode != 0:
                return ToolResult(
                    tool_name=self.tool_id,
                    content=f"git branch creation failed: {r.stderr.strip()}",
                    success=False,
                )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"git error: {exc}", success=False)

        prompt = _GUARDRAILS + task
        agent_log = ""
        try:
            claude_path = shutil.which("claude")
            if claude_path:
                cmd = [claude_path, "-p", prompt, "--permission-mode", "acceptEdits"]
                # npm installs claude as a .cmd shim; CreateProcess can't exec
                # those directly, so route through cmd.exe on Windows.
                if claude_path.lower().endswith((".cmd", ".bat")):
                    cmd = ["cmd", "/c"] + cmd
                r = _run(cmd, repo, timeout_s)
                agent_log = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
            else:
                # Local fallback: Ollama-backed coding agent via the bujji CLI.
                bujji_path = shutil.which("bujji") or "bujji"
                r = _run(
                    [bujji_path, "ask", prompt, "--agent", "native_openhands"],
                    repo, timeout_s,
                )
                agent_log = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        except subprocess.TimeoutExpired:
            agent_log += f"\n[coding agent timed out after {timeout_minutes} min]"
        except Exception as exc:
            _run(["git", "checkout", base], repo, 30)
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Coding agent failed to start: {exc}. Branch {branch} left in place.",
                success=False,
            )

        # Verify: byte-compile the package; report the diff.
        compile_ok = True
        compile_msg = ""
        try:
            import sys
            r = _run(
                [sys.executable, "-m", "compileall", "-q",
                 str(repo / "assistant-core" / "src" / "bujji")],
                repo, 300,
            )
            compile_ok = r.returncode == 0
            compile_msg = r.stderr.strip() or r.stdout.strip()
        except Exception as exc:
            compile_ok = False
            compile_msg = str(exc)

        diff = _run(["git", "diff", "--stat", base], repo, 30).stdout.strip()

        # Preserve the work on the branch, then put the running app's working
        # tree back on the original branch so the live code is unchanged.
        _run(["git", "add", "-A"], repo, 30)
        commit = _run(
            ["git", "commit", "-m", f"self-dev: {task[:72]}"], repo, 30
        )
        committed = commit.returncode == 0
        restore = _run(["git", "checkout", base], repo, 30)
        if restore.returncode != 0:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    f"WARNING: work saved on '{branch}' but switching back to "
                    f"'{base}' failed: {restore.stderr.strip()}. Fix manually with "
                    f"`git checkout {base}` before restarting."
                ),
                success=False,
            )

        summary = (
            f"Self-dev run finished. Work is on branch '{branch}' "
            f"({'committed' if committed else 'no changes to commit'}); "
            f"working tree is back on '{base}'.\n"
            f"Compile check: {'PASS' if compile_ok else 'FAIL — ' + compile_msg}\n"
            f"Changes:\n{diff or '(no changes made)'}\n\n"
            f"Agent output (tail):\n{agent_log[-2000:]}\n\n"
            f"Next: review with `git diff {base}...{branch}`, merge into {base}, "
            f"then restart BUJJI to load the new code."
        )
        return ToolResult(tool_name=self.tool_id, content=summary, success=compile_ok)


__all__ = ["SelfDevTool"]
