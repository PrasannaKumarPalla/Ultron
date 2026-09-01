from pathlib import Path

import pytest

from ultron.search import SearchConfig, SpeculativeSearch, Verifier


def weak_candidate() -> dict:
    return {"summary": "", "files_written": [], "verdict": "CHANGES_REQUIRED", "feedback": ""}


def strong_candidate() -> dict:
    return {"summary": "implemented feature with tests",
            "files_written": ["feature.py", "test_feature.py"],
            "verdict": "PASS", "feedback": ""}


def test_config_rejects_nonpositive_knobs():
    with pytest.raises(ValueError):
        SearchConfig(beam_width=0)
    with pytest.raises(ValueError):
        SearchConfig(depth=0)
    assert SearchConfig(beam_width=1).active is False
    assert SearchConfig(beam_width=3).active is True


def test_verifier_prefers_pass_with_files_over_empty_changes_required():
    verifier = Verifier()
    objective = "Build it"
    evidence = "tests failed"

    assert verifier.score(objective, evidence, strong_candidate()) > \
        verifier.score(objective, evidence, weak_candidate())
    assert -1.0 <= verifier.score(objective, evidence, weak_candidate()) <= 1.0


def test_expand_caps_candidates_at_beam_width_and_scores_all():
    search = SpeculativeSearch(SearchConfig(beam_width=2))
    candidates = [strong_candidate(), weak_candidate(), {"summary": "third"}]

    branches = search.expand(candidates)

    assert len(branches) == 2
    assert all(branch.score is not None for branch in branches)


def test_prune_keeps_winner_and_records_losers():
    search = SpeculativeSearch(SearchConfig(beam_width=3))
    branches = search.expand([weak_candidate(), strong_candidate(),
                              {"summary": "middle", "files_written": ["a.py"], "verdict": "NOT_APPLICABLE"}])

    winner = search.prune(branches)

    assert winner is not None
    assert winner.payload["verdict"] == "PASS"
    assert winner.pruned is False
    pruned = [branch for branch in branches if branch.pruned]
    assert len(pruned) == 2
    not_taken = search.paths_not_taken()
    assert len(not_taken) == 2
    assert all(entry["pruned"] for entry in not_taken)


def test_select_returns_winner_payload_and_full_history():
    search = SpeculativeSearch(SearchConfig(beam_width=2))

    winner, history = search.select([weak_candidate(), strong_candidate()], parent_id="root", depth=1)

    assert winner["verdict"] == "PASS"
    assert len(history) == 2
    assert all(entry["parent"] == "root" and entry["depth"] == 1 for entry in search.history())


def test_select_on_empty_candidates_is_safe():
    search = SpeculativeSearch(SearchConfig(beam_width=2))

    winner, history = search.select([])

    assert winner is None
    assert history == []


@pytest.mark.asyncio
async def test_speculative_developer_forwards_winning_variant(tmp_path: Path):
    from ultron.agent_runtime import RoleResult, WorkspaceGuard as Guard
    from ultron.db import Repository
    from ultron.event_bus import EventBus
    from ultron.models import MissionCreate, ProjectCreate
    from ultron.workflow import AutonomousMissionWorkflow

    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Spec", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )
    workspace = tmp_path / "workspace"

    class VariantStudio:
        def __init__(self):
            self.developer_variants: list[int] = []

        async def run_role(self, mission_id, project_id, ws, role, objective,
                           feedback="", test_evidence="", variant=0):
            if role != "developer":
                return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")
            self.developer_variants.append(variant)
            if variant == 0:
                Guard(ws).write_files([{"path": "test_product.py",
                                        "content": "def test_product():\n    assert False\n"}], role)
                return RoleResult("developer", "still broken", ["test_product.py"],
                                  "CHANGES_REQUIRED", "nope")
            Guard(ws).write_files([{"path": "test_product.py",
                                    "content": "def test_product():\n    assert 2 + 2 == 4\n"}], role)
            return RoleResult("developer", "fixed it properly",
                              ["test_product.py"], "NOT_APPLICABLE", "")

        async def run_specialist(self, mission_id, project_id, ws, role, name, purpose, skills,
                                 objective, feedback="", test_evidence="", variant=0):
            if role == "backend-developer":
                Guard(ws).write_files([{"path": "test_seed.py",
                                        "content": "def test_seed():\n    assert False\n"}], role)
            return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")

    studio = VariantStudio()
    workflow = AutonomousMissionWorkflow(repo, studio, tmp_path / "checkpoints.db",
                                         max_repair_loops=2, event_bus=EventBus(),
                                         search=SearchConfig(beam_width=2))
    result = await workflow.start(mission, repo.get_project(project.id))

    assert result.status.value == "COMPLETED"
    assert studio.developer_variants == [0, 1]
    kinds = [event.kind for event in repo.events(mission.id)]
    assert "search.expanded" in kinds
    assert "search.pruned" in kinds
    assert "search.selected" in kinds
    selected = [event.payload for event in repo.events(mission.id) if event.kind == "search.selected"]
    assert selected[0]["variant"] == 1
    expanded = [event.payload for event in repo.events(mission.id) if event.kind == "search.expanded"][0]
    assert expanded["scores"][1] > expanded["scores"][0]
    content = (workspace / "test_product.py").read_text(encoding="utf-8")
    assert "assert 2 + 2 == 4" in content
    assert not (workspace / "test_seed.py").exists()
    assert repo.verify_event_chain(mission.id)["ok"] is True


@pytest.mark.asyncio
async def test_speculative_search_degrades_to_single_path_without_shadow(tmp_path: Path, monkeypatch):
    from ultron.agent_runtime import RoleResult as RR
    from ultron.db import Repository
    from ultron.event_bus import EventBus
    from ultron.models import MissionCreate, ProjectCreate
    from ultron.workflow import AutonomousMissionWorkflow

    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Deg", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )

    class NoShadowStudio:
        def __init__(self):
            self.developer_calls = 0

        async def run_role(self, mission_id, project_id, ws, role, objective,
                           feedback="", test_evidence="", variant=0):
            if role == "developer":
                self.developer_calls += 1
            return RR(role, f"{role} done", [], "NOT_APPLICABLE", "")

        async def run_specialist(self, *args, **kwargs):
            return RR(args[3], f"{args[3]} done", [], "NOT_APPLICABLE", "")

    studio = NoShadowStudio()
    workflow = AutonomousMissionWorkflow(repo, studio, tmp_path / "checkpoints.db",
                                         event_bus=EventBus(),
                                         search=SearchConfig(beam_width=3))
    monkeypatch.setattr("ultron.shadow_git.ShadowGit.ensure", lambda self: False)

    await workflow._speculative_developer({
        "mission_id": mission.id, "project_id": project.id,
        "objective": mission.objective,
        "workspace_path": str(tmp_path / "workspace"),
        "current_node": "developer", "iteration": 1,
    })

    assert studio.developer_calls == 1
    kinds = [event.kind for event in repo.events(mission.id)]
    assert "search.degraded" in kinds



def test_real_test_results_outweigh_a_self_reported_pass():
    verifier = Verifier()
    lying = {"summary": "done", "files_written": ["a.py"], "verdict": "PASS",
             "tests_passed": False, "test_output": "1 failed, 2 passed"}
    honest = {"summary": "done", "files_written": ["a.py"], "verdict": "CHANGES_REQUIRED",
              "tests_passed": True, "test_output": "5 passed"}

    assert verifier.score("obj", "", honest) > verifier.score("obj", "", lying)
    assert verifier.score("obj", "", lying) < 0


@pytest.mark.asyncio
async def test_llm_verifier_blends_and_degrades_to_signals_on_error():
    from ultron.search import LLMVerifier

    async def judge(objective, evidence, candidate):
        return 1.0

    async def broken(objective, evidence, candidate):
        raise RuntimeError("model down")

    cand = strong_candidate()
    signal = Verifier().score("obj", "", cand)

    blended = await LLMVerifier(judge).ascore("obj", "", cand)
    assert signal < blended <= 1.0

    degraded = await LLMVerifier(broken).ascore("obj", "", cand)
    assert degraded == signal


@pytest.mark.asyncio
async def test_aselect_uses_the_async_verifier():
    from ultron.search import LLMVerifier

    async def judge(objective, evidence, candidate):
        return 1.0 if candidate.get("verdict") == "PASS" else -1.0

    search = SpeculativeSearch(SearchConfig(beam_width=2), verifier=LLMVerifier(judge))

    winner, history = await search.aselect([weak_candidate(), strong_candidate()])
    assert winner["verdict"] == "PASS"
    assert len(history) == 2
    assert all(branch.score is not None for branch in history)
