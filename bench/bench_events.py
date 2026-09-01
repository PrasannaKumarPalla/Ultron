"""Phase 1 bench: event append throughput, replay speed, chain verify rate, blob spill overhead.

Usage: .venv\\Scripts\\python bench\\bench_events.py [--out bench/metrics-phase1.json]

Every performance claim in docs must cite a number from this file.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ultron.db import Repository  # noqa: E402
from ultron.event_bus import EventBus, replay_state  # noqa: E402
from ultron.models import MissionCreate, ProjectCreate  # noqa: E402


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct / 100))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench/metrics-phase1.json")
    parser.add_argument("--events", type=int, default=500)
    args = parser.parse_args()

    metrics: dict = {
        "phase": "1",
        "generated_at": datetime.now(UTC).isoformat(),
        "event_count": args.events,
        "results": {},
    }

    tmp = Path(tempfile.mkdtemp(prefix="ultron-bench-"))
    repo = Repository(tmp / "bench.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Bench", workspace_path=tmp / "ws"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Bench run", objective="Benchmark event pipeline throughput locally.")
    )
    bus = EventBus()

    inline_latencies: list[float] = []
    for index in range(args.events):
        started = time.perf_counter()
        bus.publish(repo, mission.id, "log", "runtime", {"i": index})
        inline_latencies.append((time.perf_counter() - started) * 1000)

    big_payload = {"snapshot": "x" * 100_000}
    spill_latencies: list[float] = []
    for index in range(10):
        started = time.perf_counter()
        bus.publish(repo, mission.id, "tool.completed", "test-runner", {**big_payload, "i": index})
        spill_latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    events = repo.run_events(mission.id)
    replay_ms = (time.perf_counter() - started) * 1000
    folded = replay_state(events)

    started = time.perf_counter()
    verdict = repo.verify_event_chain(mission.id)
    verify_ms = (time.perf_counter() - started) * 1000

    blob_dir = tmp / "blobs"
    blob_bytes = sum(path.stat().st_size for path in blob_dir.rglob("*") if path.is_file())
    db_bytes = (tmp / "bench.db").stat().st_size

    def summarise(latencies: list[float]) -> dict:
        return {
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(_percentile(latencies, 95), 3),
            "mean_ms": round(statistics.fmean(latencies), 3),
            "events_per_sec": round(len(latencies) / (sum(latencies) / 1000), 1),
        }

    metrics["results"] = {
        "append_inline": summarise(inline_latencies),
        "append_spilled_100kib": summarise(spill_latencies),
        "replay": {"events": len(events), "wall_ms": round(replay_ms, 2),
                   "folded_keys": sorted(folded)},
        "verify_chain": {**verdict, "wall_ms": round(verify_ms, 2)},
        "storage": {"db_bytes": db_bytes, "blob_bytes": blob_bytes},
    }
    metrics["healthy"] = (
        verdict["ok"] is True
        and len(events) == args.events + 10
        and replay_ms > 0
    )

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps({"healthy": metrics["healthy"], "out": str(out_path),
                      "append_inline_eps": metrics["results"]["append_inline"]["events_per_sec"],
                      "verify_ok": verdict["ok"]}))
    return 0 if metrics["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
