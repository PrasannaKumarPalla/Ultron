"""Phase 2 bench: five fixture missions run twice (beam=1 baseline vs beam=2 speculative).

Usage: .venv\\Scripts\\python bench\\bench_tasks.py [--out bench/metrics-phase2.json]

Measures wall time, event volume, and developer/test-cycle counts per fixture.
The studio is deterministic (no Ollama in the loop): this isolates orchestration
cost/benefit of speculative tree search from model latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ultron.agent_runtime import RoleResult, WorkspaceGuard  # noqa: E402
from ultron.db import Repository  # noqa: E402
from ultron.event_bus import EventBus  # noqa: E402
from ultron.models import MissionCreate, ProjectCreate  # noqa: E402
from ultron.search import SearchConfig  # noqa: E402
from ultron.workflow import AutonomousMissionWorkflow  # noqa: E402

OBJECTIVE = "Build and verify a small local product."

PASSING = "def test_product():\n    assert 2 + 2 == 4\n"
FAILING = "def test_product():\n    assert False\n"


class TaskStudio:
    """Deterministic studio. The first `fail_first_n` global developer calls
    fail; later ones pass. `always_fail` keeps every candidate red."""

    def __init__(self, fail_first_n: int = 0, always_fail: bool = False):
        self.fail_first_n = fail_first_n
        self.always_fail = always_fail
        self.developer_calls = 0

    async def run_role(self, mission_id, project_id, ws, role, objective,
                       feedback="", test_evidence="", variant=0):
        if role != "developer":
            return RoleResult(role, f"{role} noop", [], "NOT_APPLICABLE", "")
        self.developer_calls += 1
        must_fail = self.always_fail or (self.developer_calls <= self.fail_first_n)
        content = FAILING if must_fail else PASSING
        WorkspaceGuard(ws).write_files([{"path": "test_product.py", "content": content}], role)
        verdict = "CHANGES_REQUIRED" if must_fail else "NOT_APPLICABLE"
        summary = "attempted fix" if must_fail else "implemented feature with tests"
        return RoleResult("developer", summary, ["test_product.py"], verdict, "")

    async def run_specialist(self, mission_id, project_id, ws, role, name, purpose, skills,
                             objective, feedback="", test_evidence="", variant=0):
        if role == "backend-developer":
            WorkspaceGuard(ws).write_files(
                [{"path": "test_seed.py", "content": FAILING}], role)
        return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")


FIXTURES = [
    {"name": "green-after-seed", "fail_first_n": 0, "always_fail": False,
     "extra_files": 0},
    {"name": "repair-rescued-by-speculation", "fail_first_n": 1, "always_fail": False,
     "extra_files": 0},
    {"name": "fan-out-overhead", "fail_first_n": 0, "always_fail": False,
     "extra_files": 50},
    {"name": "exhausted-repairs", "fail_first_n": 99, "always_fail": True,
     "extra_files": 0},
]

EXPECTED_OUTCOMES = {
    # Both beams recover; measures pure fan-out cost.
    "green-after-seed": ("COMPLETED", "COMPLETED"),
    # Baseline burns its single repair slot on the failing candidate and dies;
    # beam=2 carries a passing candidate in the same round and survives.
    "repair-rescued-by-speculation": ("FAILED", "COMPLETED"),
    # Wide workspace: fan-out overhead on a bigger baseline commit.
    "fan-out-overhead": ("COMPLETED", "COMPLETED"),
    # Nothing ever passes: both must give up cleanly (rollback path).
    "exhausted-repairs": ("FAILED", "FAILED"),
}


async def run_fixture(fixture: dict, beam_width: int, tmp: Path) -> tuple[dict, int]:
    tag = f"{fixture['name']}-{beam_width}"
    workspace = tmp / f"ws-{tag}"
    repo = Repository(tmp / f"bench-{tag}.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name=f"F{beam_width}", workspace_path=workspace))
    mission = repo.create_mission(project.id, MissionCreate(title="Bench", objective=OBJECTIVE))

    for index in range(fixture["extra_files"]):
        (workspace / f"module_{index}.py").write_text(f"X = {index}\n", encoding="utf-8")

    studio = TaskStudio(fixture["fail_first_n"], fixture["always_fail"])
    workflow = AutonomousMissionWorkflow(repo, studio, tmp / f"cp-{tag}.db",
                                         max_repair_loops=2, event_bus=EventBus(),
                                         search=SearchConfig(beam_width=beam_width))
    started = time.perf_counter()
    result = await workflow.start(mission, repo.get_project(project.id))
    wall_ms = (time.perf_counter() - started) * 1000

    events = repo.events(mission.id)
    test_cycles = sum(1 for event in events if event.kind == "tests.completed")
    stats = {
        "outcome": str(result.status.value),
        "wall_ms": round(wall_ms, 1),
        "events": len(events),
        "developer_calls": studio.developer_calls,
        "test_cycles": test_cycles,
    }
    return stats, studio.developer_calls


async def main_async(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ultron-bench2-"))
    fixtures_out: list[dict] = []
    healthy = True
    for fixture in FIXTURES:
        row: dict = {"fixture": fixture["name"]}
        for label, beam in (("baseline_beam1", 1), ("speculative_beam2", 2)):
            stats, _ = await run_fixture(fixture, beam, tmp)
            row[label] = stats
        row["overhead_ms"] = round(row["speculative_beam2"]["wall_ms"] - row["baseline_beam1"]["wall_ms"], 1)
        row["cycle_delta"] = row["baseline_beam1"]["test_cycles"] - row["speculative_beam2"]["test_cycles"]
        fixtures_out.append(row)
        print(json.dumps(row))

    for row in fixtures_out:
        want = EXPECTED_OUTCOMES[row["fixture"]]
        got = (row["baseline_beam1"]["outcome"], row["speculative_beam2"]["outcome"])
        if got != want:
            healthy = False
            print(f"MISMATCH {row['fixture']}: {got} != {want}")
    rescued = next(r for r in fixtures_out if r["fixture"] == "repair-rescued-by-speculation")
    if not (rescued["baseline_beam1"]["outcome"] == "FAILED"
            and rescued["speculative_beam2"]["outcome"] == "COMPLETED"):
        healthy = False
        print("MISMATCH: speculation failed to rescue the repair-budget fixture")

    metrics = {
        "phase": "2",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixtures": fixtures_out,
        "healthy": healthy,
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps({"healthy": healthy, "out": str(out_path)}))
    return 0 if healthy else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/metrics-phase2.json")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())


