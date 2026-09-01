"""#13 cancel rolls the workspace off a candidate branch; #23 a stranded
candidate branch is recovered on the next ensure()."""

from __future__ import annotations

from pathlib import Path

from ultron.shadow_git import CANDIDATE_BRANCH, MAIN_BRANCH, ShadowGit


def _seed(tmp_path: Path) -> ShadowGit:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("baseline\n", encoding="utf-8")
    shadow = ShadowGit(ws)
    shadow.ensure()  # baselines a.py onto main
    return shadow


def test_ensure_recovers_a_stranded_candidate_branch(tmp_path):
    shadow = _seed(tmp_path)
    shadow.begin_variant("v0")
    (shadow.workspace / "a.py").write_text("half-written variant\n", encoding="utf-8")
    assert shadow.branch() == f"{CANDIDATE_BRANCH}-v0"

    # simulate a fresh process: a new ShadowGit over the same dir
    recovered = ShadowGit(shadow.workspace)
    recovered.ensure()
    assert recovered.branch() == MAIN_BRANCH
    assert (shadow.workspace / "a.py").read_text(encoding="utf-8") == "baseline\n"


def test_rollback_restores_workspace_from_a_candidate(tmp_path):
    shadow = _seed(tmp_path)
    shadow.begin_candidate()
    (shadow.workspace / "a.py").write_text("candidate edit\n", encoding="utf-8")
    shadow.rollback()
    assert shadow.branch() == MAIN_BRANCH
    assert (shadow.workspace / "a.py").read_text(encoding="utf-8") == "baseline\n"
