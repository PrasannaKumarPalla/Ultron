"""Span assembly for the local trace viewer.

Pairs lifecycle events into spans: nodes (started/completed), LLM calls
(agent.started/completed with token counts), and the whole run.
"""

from __future__ import annotations

from datetime import datetime


def _duration_ms(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() * 1000, 2)


def build_spans(events) -> list[dict]:
    events = list(events)
    spans: list[dict] = []

    run_started = next((e for e in events if e.kind == "run.started"), None)
    run_ended = next((e for e in reversed(events)
                      if e.kind in {"run.completed", "run.failed", "run.cancelled"}), None)
    if run_started is not None:
        spans.append({
            "kind": "run", "name": f"run {run_started.run_id}",
            "start_id": run_started.id,
            "end_id": run_ended.id if run_ended else None,
            "duration_ms": _duration_ms(run_started.ts, run_ended.ts) if run_ended else None,
        })

    open_nodes: dict[str, dict] = {}
    open_agents: list[dict] = []
    for event in events:
        payload = event.payload or {}
        if event.kind == "node.started":
            open_nodes[payload.get("node", "?")] = {
                "kind": "node", "name": payload.get("node", "?"),
                "agent": event.agent, "start": event.ts, "start_id": event.id}
        elif event.kind == "node.completed" and payload.get("node") in open_nodes:
            opened = open_nodes.pop(payload["node"])
            spans.append({
                "kind": "node", "name": opened["name"], "agent": opened["agent"],
                "start_id": opened["start_id"], "end_id": event.id,
                "duration_ms": _duration_ms(opened["start"], event.ts),
            })
        elif event.kind == "node.error":
            name = payload.get("node", "?")
            opened = open_nodes.pop(name, None)
            if opened:
                spans.append({"kind": "node_error", "name": name,
                              "agent": opened["agent"],
                              "start_id": opened["start_id"], "end_id": event.id,
                              "error": str(payload.get("error", ""))[:300],
                              "duration_ms": _duration_ms(opened["start"], event.ts)})
        elif event.kind == "agent.started":
            open_agents.append({"role": event.agent, "start": event.ts,
                                "start_id": event.id, "tokens": 0})
        elif event.kind == "token" and open_agents:
            open_agents[-1]["tokens"] += 1
        elif event.kind == "agent.completed" and open_agents:
            agent_span = open_agents.pop()
            spans.append({
                "kind": "llm_call", "name": f"{agent_span['role']} completion",
                "agent": agent_span["role"], "start_id": agent_span["start_id"],
                "end_id": event.id,
                "duration_ms": _duration_ms(agent_span["start"], event.ts),
                "tokens": agent_span["tokens"],
                "tokens_per_s": round(agent_span["tokens"] /
                                      max(_duration_ms(agent_span["start"], event.ts) / 1000,
                                          0.001), 3),
            })
    return spans


def cache_hit_estimate(events) -> dict | None:
    """Rough KV-reuse signal: repeated identical prompt prefixes per role."""
    starts = [e for e in events if e.kind == "agent.started"]
    if len(starts) < 2:
        return None
    by_role: dict[str, int] = {}
    for event in starts:
        by_role[event.agent] = by_role.get(event.agent, 0) + 1
    repeats = sum(count - 1 for count in by_role.values() if count > 1)
    total = sum(by_role.values())
    return {"calls": total, "repeat_calls": repeats,
            "reuse_fraction": round(repeats / total, 3)}
