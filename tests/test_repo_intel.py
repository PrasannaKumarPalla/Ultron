import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository
from ultron.repo_intel import RepoIntel


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "pkg" / "core.py").write_text(
        "import os\n"
        "from pkg import helper\n"
        "\n"
        "class Engine:\n"
        "    def start(self):\n"
        "        return helper.warm()\n"
        "\n"
        "    def stop(self):\n"
        "        return os.getenv('X')\n",
        encoding="utf-8")
    (ws / "pkg" / "helper.py").write_text(
        "def warm():\n"
        "    return True\n",
        encoding="utf-8")
    (ws / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    return ws


def test_symbols_imports_and_calls_extracted(tmp_path: Path):
    intel = RepoIntel(make_workspace(tmp_path))

    core = intel.analyze_file("pkg/core.py")

    assert {symbol["name"] for symbol in core["symbols"]} == {"Engine", "Engine.start", "Engine.stop"}
    assert any(symbol["kind"] == "class" and symbol["line"] == 4 for symbol in core["symbols"])
    imported_modules = {item["module"] for item in core["imports"]}
    assert imported_modules == {"os", "pkg"}
    calls = {(call["caller"], call["callee"]) for call in core["calls"]}
    assert ("Engine.start", "warm") in calls
    assert ("Engine.stop", "getenv") in calls


def test_graph_joins_files_and_flags_internal_import_edges(tmp_path: Path):
    intel = RepoIntel(make_workspace(tmp_path))
    graph = intel.graph()

    assert set(graph["files"]) == {"pkg/__init__.py", "pkg/core.py", "pkg/helper.py"}
    names = {symbol["name"] for symbol in graph["symbols"]}
    assert {"Engine", "warm"} <= names
    edges = {(edge["from"], edge["to"]) for edge in graph["internal_imports"]}
    assert ("pkg.core", "pkg") in edges
    assert all(not edge["to"].startswith("os") for edge in graph["internal_imports"])


def test_broken_file_yields_none_without_killing_graph(tmp_path: Path):
    intel = RepoIntel(make_workspace(tmp_path))

    assert intel.analyze_file("broken.py") is None
    graph = intel.graph()
    assert "broken.py" not in graph["files"]
    assert graph["files"]


def test_cache_hits_on_unchanged_file_and_invalidates_on_edit(tmp_path: Path):
    ws = make_workspace(tmp_path)
    intel = RepoIntel(ws)

    first = intel.analyze_file("pkg/helper.py")
    cached = intel.analyze_file("pkg/helper.py")
    assert cached is first

    (ws / "pkg" / "helper.py").write_text("def cold():\n    return 1\n", encoding="utf-8")
    fresh = intel.analyze_file("pkg/helper.py")

    assert "cold" in {symbol["name"] for symbol in fresh["symbols"]}


def _git_commit_all(workspace: Path, message: str) -> None:
    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(workspace), *args], check=True,
                       capture_output=True,
                       env=None)
    run("init")
    run("-c", "user.name=T", "-c", "user.email=t@local", "add", "-A")
    run("-c", "user.name=T", "-c", "user.email=t@local", "commit", "-m", message)


def test_churn_counts_commits_and_hotspots_rank_them(tmp_path: Path):
    ws = make_workspace(tmp_path)

    assert RepoIntel(ws).churn() == {}

    _git_commit_all(ws, "first")
    (ws / "pkg" / "helper.py").write_text("def warm():\n    return 2\n", encoding="utf-8")
    _git_commit_all(ws, "second")
    (ws / "pkg" / "helper.py").write_text("def warm():\n    return 3\n", encoding="utf-8")
    _git_commit_all(ws, "third")

    intel = RepoIntel(ws)
    churn = intel.churn()
    hotspots = intel.hotspots(limit=3)

    assert churn["pkg/helper.py"] == 3
    assert churn["pkg/core.py"] == 1
    assert hotspots[0]["file"] == "pkg/helper.py"
    assert hotspots[0]["commits"] == 3


def test_project_intel_endpoint_serves_graph(tmp_path: Path):
    ws = make_workspace(tmp_path)
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            project = client.post("/projects", json={
                "name": "Intel", "workspace_path": str(ws),
            }).json()

            body = client.get(f"/projects/{project['id']}/intel").json()
            assert "Engine" in {symbol["name"] for symbol in body["symbols"]}
            assert isinstance(body["hotspots"], list)

            assert client.get("/projects/zzz/intel").status_code == 404
    finally:
        app.dependency_overrides.pop(get_settings, None)
