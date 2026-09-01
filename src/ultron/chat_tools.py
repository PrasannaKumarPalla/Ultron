from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .agent_runtime import WorkspaceGuard
from .db import Repository
from .models import MissionCreate, MissionStatus

PROTECTED_NAMES = {".git", ".venv", "node_modules", "__pycache__"}


MAX_READ_CHARS = 200_000


def read_file(guard: WorkspaceGuard, path: str) -> dict:
    try:
        target = guard.resolve(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"File not found: {path}"}
    try:
        content = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    if len(content) > MAX_READ_CHARS:
        content = content[:MAX_READ_CHARS] + f"\n...(truncated at {MAX_READ_CHARS} chars)"
    return {"ok": True, "result": content}


def write_file(guard: WorkspaceGuard, path: str, content: str) -> dict:
    try:
        target = guard.resolve(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "result": f"Wrote {len(content)} chars to {path}"}


def list_dir(guard: WorkspaceGuard, path: str = ".") -> dict:
    if path == ".":
        target = guard.root
    else:
        try:
            target = guard.resolve(path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    if not target.exists() or not target.is_dir():
        return {"ok": False, "error": f"Directory not found: {path}"}
    entries = sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in target.iterdir()
        if entry.name not in PROTECTED_NAMES
    )
    return {"ok": True, "result": entries}


def _split_command(command: str) -> list[str]:
    # shlex.split()'s default POSIX mode treats backslash as an escape
    # character, silently mangling Windows paths like C:\Users\x into
    # C:Usersx. Disabling backslash-escaping (while keeping POSIX quote
    # stripping, e.g. "a b" -> a b) preserves backslashes literally.
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def run_command(guard: WorkspaceGuard, command: str, timeout: int | float | str = 60) -> dict:
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 60
    timeout = max(1, min(timeout, 600))
    try:
        parts = _split_command(command)
    except ValueError as exc:
        return {"ok": False, "error": f"Could not parse command: {exc}"}
    if not parts:
        return {"ok": False, "error": "Empty command"}
    try:
        completed = subprocess.run(parts, cwd=guard.root, capture_output=True, text=True,
                                    timeout=timeout, shell=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    output = (completed.stdout + "\n" + completed.stderr)[-8000:]
    return {"ok": completed.returncode == 0, "result": output}


_TAG_RE = re.compile(r"<[^>]+>")
_RESULT_RE = re.compile(
    r'<a rel="nofollow" class="result__a" href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.S,
)
SEARCH_USER_AGENT = "Mozilla/5.0 (compatible; UltronChat/1.0)"


def _strip_tags(fragment: str) -> str:
    return unescape(_TAG_RE.sub(" ", fragment)).strip()


def _resolve_ddg_url(href: str) -> str:
    if href.startswith("//duckduckgo.com/l/"):
        parsed = urlparse("https:" + href)
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


async def _fetch_page_text(client: httpx.AsyncClient, url: str, max_chars: int = 2000) -> str:
    try:
        response = await client.get(url, headers={"User-Agent": SEARCH_USER_AGENT}, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return "(page fetch failed)"
    text = re.sub(r"\s+", " ", _strip_tags(response.text)).strip()
    return text[:max_chars]


async def web_search(query: str, max_results: int = 5, http_client: httpx.AsyncClient | None = None) -> dict:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15)
    try:
        response = None
        delay = 1.0
        for attempt in range(3):
            try:
                candidate = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                    headers={"User-Agent": SEARCH_USER_AGENT},
                )
                candidate.raise_for_status()
                response = candidate
                break
            except httpx.HTTPError:
                if attempt < 2:
                    await asyncio.sleep(delay)
                    delay *= 2
        if response is None:
            return {"ok": False, "error": "web search unavailable right now"}

        results = []
        for match in _RESULT_RE.finditer(response.text):
            if len(results) >= max_results:
                break
            results.append({
                "title": _strip_tags(match.group("title")),
                "url": _resolve_ddg_url(match.group("href")),
                "snippet": _strip_tags(match.group("snippet")),
            })
        if not results:
            return {"ok": True, "result": {"query": query, "results": []}}

        for item in results[:3]:
            item["content"] = await _fetch_page_text(client, item["url"])
        for item in results[3:]:
            item["content"] = ""

        return {"ok": True, "result": {"query": query, "results": results}}
    finally:
        if owns_client:
            await client.aclose()


def mission_control(repo: Repository, project_id: str, action: str, mission_id: str | None = None,
                     title: str | None = None, objective: str | None = None) -> dict:
    if action == "list":
        missions = repo.list_missions(project_id)
        return {"ok": True, "result": [m.model_dump(mode="json") for m in missions]}
    if action == "get":
        if not mission_id:
            return {"ok": False, "error": "mission_id is required for get"}
        mission = repo.get_mission(mission_id)
        if not mission:
            return {"ok": False, "error": "Mission not found"}
        return {"ok": True, "result": mission.model_dump(mode="json")}
    if action == "create":
        if not title or not objective:
            return {"ok": False, "error": "title and objective are required to create a mission"}
        mission = repo.create_mission(project_id, MissionCreate(title=title, objective=objective))
        return {"ok": True, "result": mission.model_dump(mode="json")}
    if action == "pause":
        if not mission_id:
            return {"ok": False, "error": "mission_id is required for pause"}
        mission = repo.get_mission(mission_id)
        if not mission:
            return {"ok": False, "error": "Mission not found"}
        if mission.status not in {MissionStatus.RUNNING, MissionStatus.BLOCKED, MissionStatus.QUEUED}:
            return {"ok": False, "error": f"Mission cannot pause from {mission.status}"}
        result = repo.transition(mission_id, MissionStatus.BLOCKED, "paused")
        repo.add_event(mission_id, "mission.paused", "chat", {"reason": "Paused from chat"})
        return {"ok": True, "result": result.model_dump(mode="json")}
    return {"ok": False, "error": f"Unknown mission_control action: {action}"}


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web and return titles, URLs, snippets, and page text for the top results.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The search query"},
            "max_results": {"type": "integer", "description": "Maximum number of results (default 5)"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the active project workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the project workspace root"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a text file in the active project workspace, creating or overwriting it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the project workspace root"},
            "content": {"type": "string", "description": "Full file content to write"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and folders at a path in the active project workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root; defaults to the root"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the active project workspace and return its output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Command to run, e.g. 'python -m pytest -q'"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "mission_control",
        "description": "List, inspect, create, or pause missions for the active project.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "create", "pause"]},
            "mission_id": {"type": "string", "description": "Required for get and pause"},
            "title": {"type": "string", "description": "Required for create"},
            "objective": {"type": "string", "description": "Required for create"},
        }, "required": ["action"]},
    }},
]


class ToolRegistry:
    def __init__(self, guard: WorkspaceGuard, repo: Repository, project_id: str):
        self.guard = guard
        self.repo = repo
        self.project_id = project_id

    def schemas(self) -> list[dict]:
        return TOOL_SCHEMAS

    async def call(self, name: str, arguments: dict) -> dict:
        try:
            if name == "web_search":
                return await web_search(arguments.get("query", ""), arguments.get("max_results", 5))
            if name == "read_file":
                return read_file(self.guard, arguments["path"])
            if name == "write_file":
                return write_file(self.guard, arguments["path"], arguments.get("content", ""))
            if name == "list_dir":
                return list_dir(self.guard, arguments.get("path", "."))
            if name == "run_command":
                return run_command(self.guard, arguments["command"], arguments.get("timeout", 60))
            if name == "mission_control":
                return mission_control(self.repo, self.project_id, arguments["action"],
                                        mission_id=arguments.get("mission_id"), title=arguments.get("title"),
                                        objective=arguments.get("objective"))
            return {"ok": False, "error": f"Unknown tool: {name}"}
        except KeyError as exc:
            return {"ok": False, "error": f"Missing required argument: {exc}"}


class GeneralChatToolRegistry:
    def schemas(self) -> list[dict]:
        return []

    async def call(self, name: str, arguments: dict) -> dict:
        return {"ok": False, "error": "Attach a workspace to use tools"}
