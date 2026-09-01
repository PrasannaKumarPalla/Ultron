"""Phase 5 demo mission: exercise every backend feature headless and capture
a dashboard screencap + metrics.json.
Usage: .venv\\Scripts\\python scripts\\demo_mission.py [--out demo/artifacts]"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ultron.agent_runtime import RoleResult, WorkspaceGuard  # noqa: E402
from ultron.db import Repository  # noqa: E402
from ultron.event_bus import EventBus  # noqa: E402
from ultron.memory_layers import LayeredMemory  # noqa: E402
from ultron.models import MissionCreate, ProjectCreate  # noqa: E402
from ultron.repo_intel import RepoIntel  # noqa: E402
from ultron.search import SearchConfig  # noqa: E402
from ultron.workflow import AutonomousMissionWorkflow  # noqa: E402

PASSING = "def test_product():\n    assert 2 + 2 == 4\n"
FAILING = "def test_product():\n    assert False\n"


class DemoStudio:
    """Deterministic studio: developer variant 0 red, variant 1 green."""

    def __init__(self):
        self.developer_variants: list[int] = []

    async def run_role(self, mission_id, project_id, ws, role, objective,
                       feedback="", test_evidence="", variant=0):
        if role != "developer":
            return RoleResult(role, f"{role} reviewed", [], "NOT_APPLICABLE", "lgtm")
        self.developer_variants.append(variant)
        if variant == 0:
            WorkspaceGuard(ws).write_files(
                [{"path": "test_product.py", "content": FAILING}], role)
            return RoleResult("developer", "first pass still red",
                              ["test_product.py"], "CHANGES_REQUIRED",
                              "failing test; retry with a clean variant")
        WorkspaceGuard(ws).write_files(
            [{"path": "test_product.py", "content": PASSING}], role)
        return RoleResult("developer", "implemented with passing tests",
                          ["test_product.py"], "NOT_APPLICABLE", "")

    async def run_specialist(self, mission_id, project_id, ws, role, name, purpose,
                             skills, objective, feedback="", test_evidence="", variant=0):
        if role == "backend-developer":
            WorkspaceGuard(ws).write_files(
                [{"path": "test_seed.py", "content": FAILING}], role)
        return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")


def find_edge() -> str | None:
    roots = [
        os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
        os.environ.get("ProgramFiles", "C:\\Program Files"),
    ]
    for root in roots:
        path = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if path.exists():
            return str(path)
    return None

async def run_mission(repo: Repository, workspace: Path) -> dict:
    project = repo.create_project(ProjectCreate(name="Demo Mission", workspace_path=workspace))
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "product.py").write_text("VALUE = 0\n", encoding="utf-8")
    mission = repo.create_mission(
        project.id, MissionCreate(title="Implement product",
                                  objective="Implement and verify a small local product."))
    studio = DemoStudio()
    workflow = AutonomousMissionWorkflow(
        repo, studio, repo.path.parent / "checkpoints.db", event_bus=EventBus(),
        search=SearchConfig(beam_width=2))
    started = time.perf_counter()
    result = await workflow.start(mission, repo.get_project(project.id))
    wall_ms = (time.perf_counter() - started) * 1000
    return {"run_id": mission.id, "project_id": project.id,
            "title": mission.title, "status": str(result.status.value),
            "mission_ms": round(wall_ms, 1)}
def probe_surfaces(repo: Repository, project_id: str, run_id: str) -> dict:
    """Exercise time travel, search, memory, intel, tools, and trace."""
    from ultron import builtin_tools  # noqa: F401
    from ultron.tools_registry import REGISTRY
    from ultron.trace import build_spans, cache_hit_estimate

    events = repo.run_events(run_id)
    kind_set = {event.kind for event in events}

    memory = LayeredMemory(repo)
    for index in range(3):
        memory.observe(project_id, f"deployment rollback after bad release {index}")
    recall = memory.recall(project_id, "deployment rollback", limit=3)
    consolidated = memory.consolidate(project_id)
    lessons_total = len(consolidated) + len(memory.lessons(project_id))

    intel = RepoIntel(repo.get_project(project_id).workspace_path)
    graph = intel.graph()

    spans = build_spans(events)
    llm_spans = [span for span in spans if span["kind"] == "llm_call"]

    return {
        "event_chain_ok": repo.verify_event_chain(run_id)["ok"],
        "timeline_count": len(events),
        "spans": len(spans),
        "llm_calls": len(llm_spans),
        "total_tokens": sum(span["tokens"] for span in llm_spans),
        "cache_reuse": cache_hit_estimate(events),
        "episodic_recall": len(recall),
        "lessons_created": len(consolidated),
        "lessons_total": lessons_total,
        "intel_files": len(graph["files"]),
        "intel_symbols": len(graph["symbols"]),
        "intel_internal_imports": len(graph["internal_imports"]),
        "tool_count": len(REGISTRY.specs()),
        "tools_strict": all(spec.strict_format()["additionalProperties"] is False
                            for spec in REGISTRY.specs()),
        "search_events": sorted(kind_set & {"search.expanded", "search.pruned",
                                            "search.selected"}),
        "shadow_gate_events": sorted(kind_set & {"shadow.candidate_opened",
                                                 "shadow.forwarded"}),
    }

def _demo_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="demo/artifacts")
    args = parser.parse_args()
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="ultron-demo5-"))
    os.environ["ULTRON_DATABASE_PATH"] = str(tmp / "ultron.db")
    os.environ["ULTRON_CHECKPOINT_PATH"] = str(tmp / "checkpoints.db")
    os.environ["ULTRON_PROJECTS_ROOT"] = str(tmp / "projects")
    os.environ["ULTRON_EXECUTION_PROVIDER"] = "mock"

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    import uvicorn
    from ultron.api import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True, name="demo-server")
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.1)
    if not server.started:
        print(json.dumps({"healthy": False, "error": "server did not start"}))
        return 1

    started = time.monotonic()
    repo = Repository(tmp / "ultron.db")
    repo.initialize()
    mission = asyncio.run(run_mission(repo, tmp / "workspace"))
    run_id, project_id = mission["run_id"], mission["project_id"]

    probes = probe_surfaces(repo, project_id, run_id)
    probes["result"] = mission["status"]

    latency_ms = {}
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, timeout=30) as client:
        for name, path in (("health", "/health"), ("roles", "/api/roles"),
                           ("tools", "/api/tools"), ("runs", "/runs"),
                           ("trace", f"/runs/{run_id}/trace"),
                           ("dashboard", "/"),
                           ("memory", f"/projects/{project_id}/memory")):
            t0 = time.perf_counter()
            response = client.get(path)
            latency_ms[name] = round((time.perf_counter() - t0) * 1000, 2)
            if response.status_code >= 400:
                print(json.dumps({"healthy": False, "bad": path,
                                  "status": response.status_code}))
                return 1

    captured = "none"
    captured_path: Path | None = None
    edge = find_edge()
    if edge:
        png = out_dir / "screencap.png"
        url = f"{base}/?run={run_id}"
        try:
            completed = subprocess.run(
                [edge, "--headless", "--disable-gpu", "--hide-scrollbars",
                 "--window-size=1440,940", f"--screenshot={png}",
                 "--virtual-time-budget=9000", url],
                capture_output=True, text=True, timeout=90)
            if completed.returncode == 0 and png.exists() and png.stat().st_size > 0:
                captured_path = png
                captured = "png"
        except (subprocess.TimeoutExpired, OSError):
            pass

    healthy = (
        probes["result"] == "COMPLETED"
        and probes["event_chain_ok"] is True
        and len(probes["search_events"]) >= 3
        and len(probes["shadow_gate_events"]) >= 2
        and probes["lessons_total"] > 0
        and probes["tools_strict"] is True
    )

    metrics = {
        "phase": "5",
        "generated_at": datetime.now(UTC).isoformat(),
        "mission": mission,
        "surfaces": probes,
        "latency_ms": latency_ms,
        "wall_sec": round(time.monotonic() - started, 1),
        "capture": captured,
        "healthy": healthy,
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps({"healthy": healthy, "run": run_id,
                      "status": mission["status"],
                      "screencap": str(captured_path) if captured_path else None,
                      "surfaces": probes,
                      "artifacts": str(out_dir)}))
    return 0 if healthy else 1

if __name__ == "__main__":
    raise SystemExit(_demo_main())
