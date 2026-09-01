"""Headless demo harness: boots Ultron, exercises core surfaces, writes artifacts.

Usage: .venv\\Scripts\\python scripts\\demo.py [--out demo/artifacts]

Produces:
- metrics.json: route count, endpoint latency samples, DB/event counters
- screencap.html: server-rendered dashboard snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="demo/artifacts")
    out_dir = ROOT / parser.parse_args().out
    out_dir.mkdir(parents=True, exist_ok=True)

    import os
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ultron-demo-"))
    os.environ["ULTRON_DATABASE_PATH"] = str(tmp / "ultron.db")
    os.environ["ULTRON_CHECKPOINT_PATH"] = str(tmp / "checkpoints.db")

    from ultron.api import EVENT_BUS, app
    from ultron.db import Repository

    metrics: dict = {"phase": "1", "checks": [], "latency_ms": {}}
    started = time.perf_counter()
    with TestClient(app) as client:
        metrics["routes"] = len(app.routes)

        def check(name: str, path: str) -> None:
            t0 = time.perf_counter()
            response = client.get(path)
            elapsed = (time.perf_counter() - t0) * 1000
            metrics["latency_ms"][name] = round(elapsed, 2)
            metrics["checks"].append({"name": name, "path": path, "status": response.status_code,
                                      "ok": response.status_code < 500})

        check("health", "/health")
        check("config", "/api/config")
        check("roles", "/api/roles")
        check("dashboard", "/")
        check("projects", "/projects")

        project = client.post("/projects", json={
            "name": "Demo", "workspace_path": str(tmp / "workspace"),
        }).json()
        mission = client.post(f"/projects/{project['id']}/missions", json={
            "title": "Demo mission", "objective": "Exercise the phase 1 event chain end to end.",
        }).json()

        repo = Repository(tmp / "ultron.db")
        EVENT_BUS.publish(repo, mission["id"], "node.started", "intake", {"node": "intake"})
        EVENT_BUS.publish(repo, mission["id"], "log", "runtime", {"note": "phase 1 chain"})

        check("run timeline", f"/runs/{mission['id']}/timeline")
        verify = client.get(f"/runs/{mission['id']}/verify").json()
        metrics["event_chain"] = verify

        dashboard = client.get("/").text
    metrics["wall_ms"] = round((time.perf_counter() - started) * 1000, 2)
    metrics["healthy"] = all(item["ok"] for item in metrics["checks"]) and bool(
        verify.get("ok"))

    (out_dir / "screencap.html").write_text(dashboard, encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps({"healthy": metrics["healthy"], "routes": metrics["routes"],
                      "wall_ms": metrics["wall_ms"], "chain_ok": verify.get("ok"),
                      "artifacts": str(out_dir)}))
    return 0 if metrics["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
