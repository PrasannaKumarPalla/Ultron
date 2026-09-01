from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .db import Repository
from .event_bus import EventBus, RunCancelled
from .models import EventKind
from .role_registry import RoleRegistry

READ_ONLY_ROLES = {"critic", "reviewer"}

logger = logging.getLogger(__name__)


ROLE_PROMPTS = {
    "architect": "You are Ultron's senior cloud architect. Produce a concrete architecture and implementation brief. Do not implement product files. You may write docs/ARCHITECTURE.md.",
    "developer": "You are Ultron's senior developer. Implement the objective completely using clean, minimal files. Preserve existing good work and fix defects from feedback.",
    "ui-expert": "You are Ultron's senior UI expert. Review and improve usability, accessibility, responsiveness, visual hierarchy, and product copy. Only change UI-related files when justified.",
    "tester": "You are Ultron's senior tester. Inspect implementation and test evidence. Identify concrete defects. Do not claim tests passed unless evidence says so. Normally do not write files.",
}

ROLE_ALIASES = {"cloud-architect": "architect", "backend-developer": "developer",
                "frontend-developer": "ui-expert", "ui-expert": "ui-expert"}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        "verdict": {"type": "string", "enum": ["PASS", "CHANGES_REQUIRED", "NOT_APPLICABLE"]},
        "feedback": {"type": "string"},
    },
    "required": ["summary", "files", "verdict", "feedback"],
}


@dataclass
class RoleResult:
    role: str
    summary: str
    files_written: list[str]
    verdict: str
    feedback: str


class WorkspaceGuard:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate == self.root or self.root not in candidate.parents:
            raise ValueError(f"Path escapes project workspace: {relative}")
        if any(part in {".git", ".venv", "node_modules", "__pycache__", ".ultron-shadow"} for part in candidate.relative_to(self.root).parts):
            raise ValueError(f"Protected path rejected: {relative}")
        return candidate

    def snapshot(self, max_files: int = 80, max_chars: int = 45_000) -> str:
        blocks: list[str] = []
        used = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or any(p in {".git", ".venv", "node_modules", "__pycache__", ".ultron-shadow"} for p in path.parts):
                continue
            if len(blocks) >= max_files or path.stat().st_size > 120_000:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            relative = path.relative_to(self.root).as_posix()
            block = f"\n--- {relative} ---\n{content}"
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return "".join(blocks) or "(empty workspace)"

    def write_files(self, files: list[dict[str, str]], role: str = "developer") -> list[str]:
        written: list[str] = []
        self.last_snapshots: list[dict[str, str]] = []
        for item in files[:30]:
            relative = Path(item["path"])
            if role in {"architect", "cloud-architect"} and relative.as_posix() != "docs/ARCHITECTURE.md":
                continue
            if role in {"product-manager", "security-engineer", "devops-engineer"} and relative.suffix.lower() not in {".md", ".txt", ".yaml", ".yml"}:
                continue
            if role in {"ui-expert", "frontend-developer"} and relative.suffix.lower() not in {".html", ".css", ".js", ".jsx", ".tsx", ".vue", ".svelte"}:
                continue
            if role in {"tester", "qa-engineer"}:
                continue
            target = self.resolve(item["path"])
            before = ""
            if target.exists():
                try:
                    before = target.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    before = ""
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            relative_path = target.relative_to(self.root).as_posix()
            written.append(relative_path)
            self.last_snapshots.append({"path": relative_path, "before": before, "after": item["content"]})
        return written

    async def test(self) -> tuple[bool, str]:
        is_python = (self.root / "pytest.ini").exists() or (self.root / "pyproject.toml").exists() or list(self.root.glob("test*.py"))
        if is_python:
            command = [sys.executable, "-m", "pytest", "-q"] if self._module_available("pytest") else [sys.executable, "-m", "unittest", "discover", "-v"]
        elif (self.root / "package.json").exists():
            command = ["npm", "test", "--", "--run"]
        else:
            return True, "No automated test framework detected; manual checks required."
        install_ok, install_output = await asyncio.to_thread(self._install_dependencies)
        if not install_ok:
            return False, install_output
        return await asyncio.to_thread(self._run, command)

    def _install_dependencies(self) -> tuple[bool, str]:
        if (self.root / "requirements.txt").exists():
            install_command = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
        elif (self.root / "pyproject.toml").exists() or (self.root / "setup.py").exists():
            install_command = [sys.executable, "-m", "pip", "install", "-e", "."]
        elif (self.root / "package.json").exists():
            install_command = ["npm", "install"]
        else:
            return True, ""
        return self._run(install_command)

    @staticmethod
    def _module_available(module: str) -> bool:
        try:
            completed = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return completed.returncode == 0

    def _run(self, command: list[str]) -> tuple[bool, str]:
        from .sandbox import sandboxed_run
        return sandboxed_run(command, self.root, timeout_s=180)


class OllamaAgentStudio:
    def __init__(self, base_url: str, model: str, repository: Repository,
                 event_bus: EventBus | None = None, run_manager=None,
                 registry: RoleRegistry | None = None,
                 layered_memory: "LayeredMemory | None" = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.repository = repository
        self.event_bus = event_bus
        self.run_manager = run_manager
        self.registry = registry
        self.memory = layered_memory

    @staticmethod
    def _seed_for(mission_id: str) -> int:
        """Deterministic per-run seed: the same mission replays with the same sampling seed."""
        return int(hashlib.sha256(mission_id.encode("utf-8")).hexdigest()[:8], 16)

    def _emit(self, mission_id: str, kind: str | EventKind, agent: str, payload: dict) -> None:
        if self.event_bus:
            self.event_bus.publish(self.repository, mission_id, kind, agent, payload)
        else:
            self.repository.add_event(mission_id, str(kind), agent, payload)

    async def run_role(self, mission_id: str, project_id: str, workspace: Path, role: str, objective: str,
                       feedback: str = "", test_evidence: str = "", variant: int = 0) -> RoleResult:
        spec = self.registry.get(role) if self.registry else None
        name = spec.name if spec else role.replace("-", " ").title()
        purpose = spec.system_prompt if spec else ROLE_PROMPTS.get(role,
            f"You are Ultron's {role.replace('-', ' ')}. Complete your responsibility within the objective.")
        return await self.run_specialist(mission_id, project_id, workspace, role, name,
                                         purpose, list(spec.tools) if spec else [], objective,
                                         feedback, test_evidence, variant=variant)

    async def run_specialist(self, mission_id: str, project_id: str, workspace: Path, role: str,
                             name: str, purpose: str, skills: list[str], objective: str,
                             feedback: str = "", test_evidence: str = "", variant: int = 0) -> RoleResult:
        guard = WorkspaceGuard(workspace)
        memories = self.repository.memory_context(project_id, objective, include_global=True)
        memory_text = "\n".join(f"- [{m.role}] {m.content}" for m in memories) or "(none)"
        episodic_text = ""
        if self.memory is not None:
            recalled = self.memory.recall(project_id, objective, limit=5)
            episodic_text = "\n".join(f"- {hit['text']}" for hit in recalled)
        spec = self.registry.get(role) if self.registry else None
        system_prompt = (spec.system_prompt if spec else None) or ROLE_PROMPTS.get(ROLE_ALIASES.get(role, role),
            f"You are Ultron's {name}. {purpose} Apply only these skills: {', '.join(skills)}. Make changes only within your responsibility.")
        model = (spec.model if spec else None) or self.model
        allowed_tools = list(spec.tools) if spec else []
        prompt = f"""MISSION: {objective}
ROLE: {system_prompt}
PROJECT MEMORY:\n{memory_text}
EPISODIC MEMORY:\n{episodic_text or '(none)'}
FEEDBACK FROM PRIOR LOOP:\n{feedback or '(none)'}
TEST EVIDENCE:\n{test_evidence or '(not run yet)'}
CURRENT WORKSPACE:\n{guard.snapshot()}

Return only the requested JSON object. File paths must be relative. Never use .. or absolute paths. Make a small complete change set. Verdict must be PASS only for tester when acceptance criteria and evidence pass; other roles use NOT_APPLICABLE."""
        seed = self._seed_for(mission_id) + variant * 7919
        self._emit(mission_id, "agent.started", role, {"model": model, "seed": seed, "tools": allowed_tools, "variant": variant})
        raw_parts: list[str] = []
        tokens = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json={
                "model": model,
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
                "stream": True, "think": False, "format": RESPONSE_SCHEMA,
                "options": {"temperature": 0.15, "num_ctx": 32768, "num_predict": 8192, "seed": seed},
                "keep_alive": "15m",
            }) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if self.run_manager:
                        self.run_manager.check(mission_id)
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    piece = (chunk.get("message") or {}).get("content") or ""
                    if piece:
                        tokens += 1
                        raw_parts.append(piece)
                        self._emit(mission_id, EventKind.TOKEN, role, {"index": tokens, "text": piece})
                        if self.run_manager:
                            flag = self.run_manager.record_tokens(mission_id, 1)  # may raise BudgetExhausted
                            if flag == "warn":
                                self._emit(mission_id, EventKind.BUDGET_WARNING, role,
                                           self.run_manager.budget_state(mission_id))
                    if chunk.get("done"):
                        break
        raw = "".join(raw_parts)
        if not raw.strip():
            raise ValueError("Ollama returned an empty stream")
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            self._emit(mission_id, "agent.response_invalid", role, {"preview": raw[:1000]})
            data = {"summary": f"{role} response could not be parsed", "files": [],
                    "verdict": "CHANGES_REQUIRED", "feedback": (test_evidence or raw)[-6000:]}
        requested_files = data.get("files", [])
        if role in READ_ONLY_ROLES:
            requested_files = []
        written = guard.write_files(requested_files, role)
        for path in written:
            self._emit(mission_id, EventKind.ARTIFACT_WRITTEN, role, {"path": path})
        for snapshot in guard.last_snapshots:
            self.repository.record_file_snapshot(mission_id, snapshot["path"], snapshot["before"], snapshot["after"])
        result = RoleResult(role, data.get("summary", ""), written, data.get("verdict", "NOT_APPLICABLE"), data.get("feedback", ""))
        if self.memory is not None and result.summary:
            try:
                self.memory.observe(project_id, f"{role}: {result.summary}")
            except Exception:
                logger.exception("episodic memory observation failed")
        self._emit(mission_id, "agent.completed", role, {"summary": result.summary, "files": written, "verdict": result.verdict, "feedback": result.feedback})
        return result
