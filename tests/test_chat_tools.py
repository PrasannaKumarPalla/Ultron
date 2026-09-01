from pathlib import Path

import httpx
import pytest

from ultron.agent_runtime import WorkspaceGuard
from ultron.chat_tools import list_dir, read_file, run_command, web_search, write_file


def test_write_then_read_file_round_trips(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")

    write_result = write_file(guard, "notes.txt", "hello world")
    assert write_result["ok"] is True

    read_result = read_file(guard, "notes.txt")
    assert read_result == {"ok": True, "result": "hello world"}


def test_read_file_missing_returns_error(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    result = read_file(guard, "missing.txt")
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_write_file_rejects_path_escape(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    result = write_file(guard, "../outside.txt", "nope")
    assert result["ok"] is False
    assert "escapes" in result["error"].lower()


def test_list_dir_defaults_to_workspace_root(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    write_file(guard, "a.txt", "a")
    write_file(guard, "sub/b.txt", "b")

    result = list_dir(guard)
    assert result["ok"] is True
    assert "a.txt" in result["result"]
    assert "sub/" in result["result"]


def test_list_dir_missing_directory_returns_error(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    result = list_dir(guard, "nope")
    assert result["ok"] is False


def test_run_command_executes_within_workspace(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    write_file(guard, "marker.txt", "present")

    result = run_command(guard, "python -c \"import pathlib; print(pathlib.Path('marker.txt').exists())\"")
    assert result["ok"] is True
    assert "True" in result["result"]


def test_run_command_rejects_unparseable_command(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    result = run_command(guard, "echo 'unterminated")
    assert result["ok"] is False


def test_run_command_preserves_windows_backslash_paths(tmp_path: Path):
    # Regression test: shlex.split()'s default POSIX mode treats an
    # unquoted backslash as an escape character, so an unquoted Windows
    # path like C:\Users\test is silently mangled into C:Userstest instead
    # of erroring or round-tripping intact. Confirmed the two modes differ
    # for exactly this input before wiring the fix into run_command.
    import shlex

    unquoted_path = r"C:\Users\test"
    assert shlex.split(f'echo {unquoted_path}') == ["echo", "C:Userstest"]
    assert shlex.split(f'echo {unquoted_path}', posix=False) != shlex.split(f'echo {unquoted_path}')

    guard = WorkspaceGuard(tmp_path / "workspace")

    result = run_command(
        guard,
        f'python -c "import sys; print(sys.argv[1])" {unquoted_path}',
    )

    assert result["ok"] is True
    assert "C:\\Users\\test" in result["result"]
    assert "C:Userstest" not in result["result"]


SAMPLE_DDG_HTML = """
<div class="results">
  <div class="result">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example Result</a>
    <a class="result__snippet">A short snippet about the example.</a>
  </div>
</div>
"""

SAMPLE_PAGE_HTML = "<html><body><p>Full page content here.</p></body></html>"


@pytest.mark.asyncio
async def test_web_search_parses_results_and_fetches_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        if "html.duckduckgo.com" in str(request.url):
            return httpx.Response(200, text=SAMPLE_DDG_HTML)
        return httpx.Response(200, text=SAMPLE_PAGE_HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await web_search("example query", http_client=client)
    await client.aclose()

    assert result["ok"] is True
    results = result["result"]["results"]
    assert len(results) == 1
    assert results[0]["title"] == "Example Result"
    assert results[0]["url"] == "https://example.com/page"
    assert "short snippet" in results[0]["snippet"]
    assert "Full page content" in results[0]["content"]


@pytest.mark.asyncio
async def test_web_search_retries_then_fails_gracefully():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, text="unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await web_search("example query", http_client=client)
    await client.aclose()

    assert result == {"ok": False, "error": "web search unavailable right now"}
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_web_search_no_results_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<div class='results'>no matches here</div>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await web_search("nothing", http_client=client)
    await client.aclose()

    assert result == {"ok": True, "result": {"query": "nothing", "results": []}}


from ultron.chat_tools import TOOL_SCHEMAS, ToolRegistry, mission_control
from ultron.db import Repository
from ultron.models import Classification, MissionStatus, ProjectCreate


def _repo_with_project(tmp_path: Path) -> tuple[Repository, str]:
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(
        name="Mission Control Project", description="", workspace_path=tmp_path / "workspace",
        classification=Classification.PERSONAL,
    ))
    return repo, project.id


def test_mission_control_create_then_list(tmp_path: Path):
    repo, project_id = _repo_with_project(tmp_path)

    created = mission_control(repo, project_id, "create", title="Build a widget", objective="Ship a working widget end to end.")
    assert created["ok"] is True
    assert created["result"]["title"] == "Build a widget"

    listed = mission_control(repo, project_id, "list")
    assert listed["ok"] is True
    assert len(listed["result"]) == 1


def test_mission_control_pause_transitions_status(tmp_path: Path):
    repo, project_id = _repo_with_project(tmp_path)
    created = mission_control(repo, project_id, "create", title="Build a widget", objective="Ship a working widget end to end.")
    mission_id = created["result"]["id"]

    paused = mission_control(repo, project_id, "pause", mission_id=mission_id)
    assert paused["ok"] is True
    assert paused["result"]["status"] == MissionStatus.BLOCKED.value


def test_mission_control_unknown_action_returns_error(tmp_path: Path):
    repo, project_id = _repo_with_project(tmp_path)
    result = mission_control(repo, project_id, "explode")
    assert result["ok"] is False


def test_tool_registry_dispatches_by_name(tmp_path: Path):
    repo, project_id = _repo_with_project(tmp_path)
    guard = WorkspaceGuard(tmp_path / "workspace")
    registry = ToolRegistry(guard, repo, project_id)

    assert {schema["function"]["name"] for schema in TOOL_SCHEMAS} == {
        "web_search", "read_file", "write_file", "list_dir", "run_command", "mission_control",
    }


@pytest.mark.asyncio
async def test_tool_registry_calls_write_file(tmp_path: Path):
    repo, project_id = _repo_with_project(tmp_path)
    guard = WorkspaceGuard(tmp_path / "workspace")
    registry = ToolRegistry(guard, repo, project_id)

    result = await registry.call("write_file", {"path": "note.txt", "content": "hi"})
    assert result["ok"] is True
    assert (guard.root / "note.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_tool_registry_unknown_tool_returns_error(tmp_path: Path):
    repo, project_id = _repo_with_project(tmp_path)
    guard = WorkspaceGuard(tmp_path / "workspace")
    registry = ToolRegistry(guard, repo, project_id)

    result = await registry.call("not_a_tool", {})
    assert result["ok"] is False
