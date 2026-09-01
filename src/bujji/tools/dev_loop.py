"""dev_loop tool — code → test → fix loop for any project.

Given a project directory, a task, and a test command, this repeatedly runs a
coding agent and the tests, feeding failures back to the agent until the tests
pass or the iteration budget runs out. Works on any repo, including BUJJI's
own (where self_dev is a safer single-shot alternative).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec


def _run(cmd, cwd: Path, timeout: float, shell: bool = False):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, shell=shell,
        timeout=timeout, encoding="utf-8", errors="replace",
    )


def _coding_agent(prompt: str, cwd: Path, timeout: float) -> str:
    claude_path = shutil.which("claude")
    if claude_path:
        cmd = [claude_path, "-p", prompt, "--permission-mode", "acceptEdits"]
        if claude_path.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c"] + cmd
    else:
        bujji_path = shutil.which("bujji") or "bujji"
        cmd = [bujji_path, "ask", prompt, "--agent", "native_openhands"]
    r = _run(cmd, cwd, timeout)
    return (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")


@ToolRegistry.register("dev_loop")
class DevLoopTool(BaseTool):
    """Iterate code + tests until green."""

    tool_id = "dev_loop"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="dev_loop",
            description=(
                "Develop or fix software in a loop: run a coding agent on a project, "
                "run its tests, feed failures back, repeat until tests pass. Use for "
                "'build X and make sure tests pass' style work requests. Provide the "
                "project directory, the task, and the test command (e.g. 'pytest -q', "
                "'npm test')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Absolute path to the project to work on",
                    },
                    "task": {
                        "type": "string",
                        "description": "What to build or fix",
                    },
                    "test_command": {
                        "type": "string",
                        "description": "Shell command that exits 0 when the work is correct",
                    },
                    "max_iterations": {
                        "type": "number",
                        "description": "Max code+test rounds (default 3)",
                    },
                },
                "required": ["project_dir", "task", "test_command"],
            },
            category="development",
            latency_estimate=600.0,
            timeout_seconds=3600.0,
        )

    def execute(  # type: ignore[override]
        self,
        project_dir: str = "",
        task: str = "",
        test_command: str = "",
        max_iterations: float = 3,
        **_: Any,
    ) -> ToolResult:
        proj = Path(project_dir).expanduser()
        if not proj.is_dir():
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Project directory not found: {project_dir}",
                success=False,
            )
        if not task.strip() or not test_command.strip():
            return ToolResult(
                tool_name=self.tool_id,
                content="Both task and test_command are required.",
                success=False,
            )

        rounds = max(1, min(int(max_iterations), 10))
        history: list[str] = []
        prompt = (
            f"Work in this project directory. Task: {task}\n"
            f"When done, the command `{test_command}` must exit 0. "
            f"Do not start servers or interactive programs."
        )

        for i in range(1, rounds + 1):
            try:
                agent_out = _coding_agent(prompt, proj, timeout=1500)
            except subprocess.TimeoutExpired:
                history.append(f"Round {i}: coding agent timed out")
                break

            try:
                test = _run(test_command, proj, timeout=900, shell=True)
            except subprocess.TimeoutExpired:
                history.append(f"Round {i}: tests timed out")
                break

            if test.returncode == 0:
                history.append(f"Round {i}: tests PASSED")
                return ToolResult(
                    tool_name=self.tool_id,
                    content=(
                        f"Done in {i} round(s). `{test_command}` passes.\n\n"
                        + "\n".join(history)
                        + f"\n\nLast agent output (tail):\n{agent_out[-1500:]}"
                    ),
                    success=True,
                )

            failure = (test.stdout or "")[-3000:] + "\n" + (test.stderr or "")[-2000:]
            history.append(f"Round {i}: tests failed (exit {test.returncode})")
            prompt = (
                f"The tests are still failing. Original task: {task}\n"
                f"Command `{test_command}` output:\n{failure}\n"
                f"Fix the failures. The command must exit 0."
            )

        return ToolResult(
            tool_name=self.tool_id,
            content=(
                f"Tests still failing after {rounds} round(s).\n" + "\n".join(history)
            ),
            success=False,
        )


__all__ = ["DevLoopTool"]
