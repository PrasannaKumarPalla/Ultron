import pytest

from ultron.tools_registry import REGISTRY, ToolRegistry, ToolSpec
from ultron.trace import build_spans, cache_hit_estimate


def test_register_and_strict_format_compiles_schema():
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="demo", description="d",
        schema={"type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": True},
        handler=lambda: None,
    ))

    compiled = registry.schemas()[0]["format"]

    assert compiled["additionalProperties"] is False
    assert compiled["required"] == ["path"]


def test_tool_decorator_registers_into_global_registry():
    from ultron import builtin_tools  # noqa: F401

    names = {spec.name for spec in REGISTRY.specs()}
    assert {"workspace.read_file", "workspace.write_file",
            "workspace.list_files", "workspace.run_tests"} <= names

    read_spec = REGISTRY.get("workspace.read_file")
    assert read_spec.strict_format()["properties"]["path"]["type"] == "string"


def test_discover_imports_plugin_modules(tmp_path):
    plugin = tmp_path / "my_plugin.py"
    plugin.write_text(
        "from ultron.tools_registry import tool\n"
        "@tool('plugin.echo', 'echo', {'type': 'object', 'properties': {}, 'required': []})\n"
        "def echo(workspace):\n"
        "    return 'pong'\n",
        encoding="utf-8")
    (tmp_path / "_private.py").write_text("raise RuntimeError('should not import')",
                                          encoding="utf-8")
    registry = ToolRegistry()

    added = registry.discover(tmp_path)

    assert added == 1
    assert registry.get("plugin.echo") is not None


class _Event:
    def __init__(self, id, kind, agent, payload, ts):
        self.id = id
        self.kind = kind
        self.agent = agent
        self.payload = payload
        self.ts = ts
        self.run_id = "run-1"


from datetime import UTC, datetime, timedelta


def _ts(base_minutes: float) -> datetime:
    return datetime(2026, 8, 25, 12, 0, tzinfo=UTC) + timedelta(minutes=base_minutes)


def test_build_spans_pairs_nodes_llm_calls_and_run():
    events = [
        _Event(1, "run.started", "supervisor", {}, _ts(0)),
        _Event(2, "node.started", "developer", {"node": "developer"}, _ts(1)),
        _Event(3, "agent.started", "developer", {"seed": 1}, _ts(1.5)),
        _Event(4, "token", "developer", {"index": 1}, _ts(2)),
        _Event(5, "token", "developer", {"index": 2}, _ts(2)),
        _Event(6, "agent.completed", "developer", {}, _ts(2.5)),
        _Event(7, "node.completed", "developer", {"node": "developer"}, _ts(3)),
        _Event(8, "node.started", "test_runner", {"node": "test_runner"}, _ts(4)),
        _Event(9, "node.error", "test_runner", {"node": "test_runner", "error": "boom"}, _ts(5)),
        _Event(10, "run.completed", "supervisor", {"status": "FAILED"}, _ts(6)),
    ]

    spans = build_spans(events)

    kinds = {span["kind"] for span in spans}
    assert {"run", "node", "llm_call", "node_error"} <= kinds
    llm = next(span for span in spans if span["kind"] == "llm_call")
    assert llm["tokens"] == 2
    assert llm["duration_ms"] == 60000.0
    assert llm["tokens_per_s"] > 0
    node = next(span for span in spans if span["kind"] == "node" and span["name"] == "developer")
    assert node["duration_ms"] == 120000.0
    run_span = next(span for span in spans if span["kind"] == "run")
    assert run_span["end_id"] == 10


def test_cache_hit_estimate_counts_repeat_role_calls():
    events = [
        _Event(1, "agent.started", "developer", {}, _ts(0)),
        _Event(2, "agent.started", "developer", {}, _ts(1)),
        _Event(3, "agent.started", "critic", {}, _ts(2)),
    ]

    estimate = cache_hit_estimate(events)

    assert estimate == {"calls": 3, "repeat_calls": 1, "reuse_fraction": 0.333}
    assert cache_hit_estimate([events[0]]) is None
