"""Built-in workspace tools: auto-registered via @tool, WorkspaceGuard-scoped."""

from __future__ import annotations

from pathlib import Path

from .agent_runtime import WorkspaceGuard
from .tools_registry import tool


@tool("workspace.read_file", "Read a UTF-8 text file from the project workspace.",
      {"type": "object", "properties": {"path": {"type": "string"}},
       "required": ["path"]})
def read_file(workspace: Path, path: str) -> str:
    target = WorkspaceGuard(workspace).resolve(path)
    return target.read_text(encoding="utf-8")


@tool("workspace.write_file", "Write a UTF-8 text file inside the project workspace.",
      {"type": "object",
       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
       "required": ["path", "content"]})
def write_file(workspace: Path, path: str, content: str) -> str:
    target = WorkspaceGuard(workspace).resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


@tool("workspace.list_files", "List workspace files (relative paths).",
      {"type": "object", "properties": {}, "required": []})
def list_files(workspace: Path) -> list[str]:
    guard = WorkspaceGuard(workspace)
    return [path.relative_to(guard.root).as_posix()
            for path in sorted(guard.root.rglob("*"))
            if path.is_file() and ".ultron-shadow" not in path.parts][:200]


@tool("workspace.run_tests", "Run the workspace test suite in the sandbox.",
      {"type": "object", "properties": {}, "required": []})
def run_tests(workspace: Path) -> dict:
    import asyncio

    passed, evidence = asyncio.run(WorkspaceGuard(workspace).test())
    return {"passed": passed, "evidence": evidence[-4000:]}
