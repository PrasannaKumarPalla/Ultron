import sys
import time
from pathlib import Path

import ultron.sandbox as sandbox_module
from ultron.sandbox import _plain_run, sandboxed_run


def test_runs_command_and_captures_output(tmp_path: Path):
    ok, output = sandboxed_run([sys.executable, "-c", "print('hello from job')"],
                               cwd=tmp_path, timeout_s=60)

    assert ok is True
    assert "hello from job" in output


def test_reports_nonzero_exit_as_failure(tmp_path: Path):
    ok, output = sandboxed_run([sys.executable, "-c", "raise SystemExit(3)"],
                               cwd=tmp_path, timeout_s=60)

    assert ok is False


def test_timeout_kills_tree_quickly(tmp_path: Path):
    started = time.monotonic()

    ok, output = sandboxed_run(
        [sys.executable, "-c", "import time; print('started'); time.sleep(60)"],
        cwd=tmp_path, timeout_s=3)

    elapsed = time.monotonic() - started

    assert ok is False
    assert "timed out after 3s" in output
    assert elapsed < 20


def test_plain_fallback_used_off_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sandbox_module, "_is_windows", lambda: False)
    called = {}

    def fake_plain(command, cwd, timeout_s):
        called["command"] = command
        return True, "plain"

    monkeypatch.setattr(sandbox_module, "_plain_run", fake_plain)

    ok, output = sandboxed_run([sys.executable, "-c", "pass"], cwd=tmp_path, timeout_s=5)

    assert (ok, output) == (True, "plain")
    assert called["command"][0] == sys.executable


def test_plain_run_handles_missing_binary_and_timeout(tmp_path: Path):
    ok, output = _plain_run(["definitely-not-a-real-binary-xyz"], cwd=tmp_path, timeout_s=10)
    assert ok is False and output

    ok, output = _plain_run([sys.executable, "-c", "import time; time.sleep(30)"],
                            cwd=tmp_path, timeout_s=2)
    assert ok is False
