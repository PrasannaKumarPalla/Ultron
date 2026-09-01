from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict
from contextlib import asynccontextmanager
from typing import Callable

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from .db import Repository
from .models import EventKind, Mission, MissionStatus, Project
from .providers import ExecutionProvider, ExecutionRequest
from .agent_runtime import OllamaAgentStudio, RoleResult, WorkspaceGuard
from .search import SearchConfig, SpeculativeSearch, Verifier
from .security_scan import scan_dependencies, scan_secrets
from .shadow_git import CANDIDATE_BRANCH, ShadowGit, ShadowGitError
from .team_planner import TeamPlanner
from .event_bus import BudgetExhausted, EventBus, RunCancelled, replay_state
from .runs import RunManager


class MissionState(TypedDict, total=False):
    mission_id: str
    project_id: str
    objective: str
    workspace_path: str
    current_node: str
    execution_provider: str
    execution_external_id: str
    execution_status: str


class DurableMissionWorkflow:
    """Durable mission graph whose external work is recorded as evidence."""

    graph_version = "langgraph-v1"

    def __init__(
        self,
        repository: Repository,
        executor: ExecutionProvider,
        checkpoint_path: Path,
        event_bus: EventBus | None = None,
        run_manager: RunManager | None = None,
    ):
        self.repository = repository
        self.executor = executor
        self.event_bus = event_bus
        self.run_manager = run_manager
        self.checkpoint_path = checkpoint_path.resolve()
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def _checkpointer(self):
        async with AsyncSqliteSaver.from_conn_string(
            str(self.checkpoint_path)
        ) as saver:
            yield saver

    def _emit(self, run_id: str, kind: str | EventKind, agent: str, payload: dict) -> None:
        if self.event_bus:
            self.event_bus.publish(self.repository, run_id, kind, agent, payload)
        else:
            self.repository.add_event(run_id, str(kind), agent, payload)

    def _check_cancelled(self, run_id: str) -> None:
        if self.run_manager and self.run_manager.is_cancelled(run_id):
            raise RunCancelled(f"run {run_id} cancelled by operator")

    def _instrument(self, name: str, fn: Callable) -> Callable:
        """Auto-wraps a node: lifecycle events, kill switch, checkpoint breadcrumb."""
        async def wrapped(state):
            run_id = state["mission_id"]
            before = self.repository.get_mission(run_id)
            self._emit(run_id, EventKind.NODE_STARTED, name, {"node": name})
            self._check_cancelled(run_id)
            try:
                result = await fn(state)
            except RunCancelled:
                raise
            except Exception as exc:
                self._emit(run_id, EventKind.NODE_ERROR, name, {"node": name, "error": str(exc)})
                raise
            delta = dict(result or {})
            self.repository.save_checkpoint(run_id, name, delta)
            after = self.repository.get_mission(run_id)
            if after and before and after.status != before.status:
                self._emit(run_id, EventKind.STATUS_CHANGED, name,
                           {"node": name, "from": str(before.status), "to": str(after.status)})
            self._emit(run_id, EventKind.NODE_COMPLETED, name, {"node": name, "writes": sorted(delta)})
            return result

        return wrapped

    def _rollback_open_candidates(self, run_id: str) -> None:
        """On cancel, restore every workspace still sitting on a candidate branch."""
        for shadow in getattr(self, "_shadow_cache", {}).values():
            if not getattr(shadow, "available", False):
                continue
            branch = shadow.branch()
            if branch and branch != "main":
                try:
                    shadow.rollback()
                    self._emit(run_id, "shadow.rolled_back", "shadow-git",
                               {"reason": "run cancelled; workspace restored to baseline"})
                except ShadowGitError as exc:
                    self._emit(run_id, "shadow.unavailable", "shadow-git", {"reason": str(exc)})

    def _note_fork_outcome(self, run_id: str, outcome: str, error: str | None) -> None:
        """When a forked run ends badly, write a back-reference on its source run
        so the source timeline shows that a fork off it failed."""
        source_run = source_event_id = None
        for event in self.repository.events(run_id):
            if event.kind == "run.forked":
                source_run = event.payload.get("source_run")
                source_event_id = event.payload.get("source_event_id")
                break
        if not source_run:
            return
        self._emit(source_run, "run.fork_failed", "supervisor", {
            "fork_run": run_id, "outcome": outcome,
            "source_event_id": source_event_id, "error": error,
        })

    async def _execute(self, mission: Mission, initial: dict | None, *, resume: bool) -> Mission:
        config = {"configurable": {"thread_id": mission.id}, "recursion_limit": 30}
        self._emit(mission.id, EventKind.RUN_STARTED, "supervisor",
                   {"graph_version": self.graph_version, "resumed": resume})
        try:
            async with self._checkpointer() as checkpointer:
                graph = self._builder().compile(checkpointer=checkpointer)
                if resume:
                    snapshot = await graph.aget_state(config)
                    if snapshot.values:
                        await graph.ainvoke(None, config)
                    elif initial is not None:
                        await graph.ainvoke(initial, config)
                else:
                    await graph.ainvoke(initial, config)
        except RunCancelled:
            self._rollback_open_candidates(mission.id)
            self._emit(mission.id, EventKind.RUN_CANCELLED, "supervisor", {})
            self._note_fork_outcome(mission.id, "cancelled", None)
            raise
        except Exception as exc:
            self._emit(mission.id, EventKind.RUN_FAILED, "supervisor", {"error": str(exc)})
            self._note_fork_outcome(mission.id, "failed", str(exc))
            raise
        result = self.repository.get_mission(mission.id)
        if not result:
            raise RuntimeError("Mission disappeared after graph execution")
        self._emit(mission.id, EventKind.RUN_COMPLETED, "supervisor", {"status": str(result.status)})
        return result

    def _builder(self) -> StateGraph:
        builder = StateGraph(MissionState)
        builder.add_node("intake", self._instrument("intake", self._intake))
        builder.add_node("dispatch", self._instrument("dispatch", self._dispatch))
        builder.add_node("execution_integration",
                         self._instrument("execution_integration", self._execution_integration))
        builder.add_edge(START, "intake")
        builder.add_edge("intake", "dispatch")
        builder.add_edge("dispatch", "execution_integration")
        builder.add_edge("execution_integration", END)
        return builder

    async def _intake(self, state: MissionState) -> MissionState:
        mission_id = state["mission_id"]
        self.repository.transition(mission_id, MissionStatus.RUNNING, "intake")
        self._emit(mission_id, "workflow.started", "supervisor", {"graph_version": self.graph_version})
        return {"current_node": "intake"}

    async def _dispatch(self, state: MissionState) -> MissionState:
        mission_id = state["mission_id"]
        self.repository.transition(mission_id, MissionStatus.RUNNING, "dispatch")
        receipt = await self.executor.submit(
            ExecutionRequest(
                mission_id=mission_id,
                workspace_path=state["workspace_path"],
                objective=state["objective"],
            )
        )
        self._emit(mission_id, "execution.submitted", "execution-provider",
            {
                "provider": receipt.provider,
                "external_id": receipt.external_id,
                "status": receipt.status,
            },
        )
        return {
            "current_node": "dispatch",
            "execution_provider": receipt.provider,
            "execution_external_id": receipt.external_id,
            "execution_status": receipt.status,
        }

    async def _execution_integration(self, state: MissionState) -> MissionState:
        self.repository.transition(
            state["mission_id"], MissionStatus.BLOCKED, "execution_integration"
        )
        return {"current_node": "execution_integration"}

    async def start(self, mission: Mission, project: Project) -> Mission:
        initial: MissionState = {
            "mission_id": mission.id,
            "project_id": project.id,
            "objective": mission.objective,
            "workspace_path": str(project.workspace_path),
            "current_node": "intake",
        }
        return await self._execute(mission, initial, resume=False)

    async def start_from_state(self, state: dict) -> Mission:
        """Time-travel entry point: rehydrate an edited past and re-run forward."""
        mission = self.repository.get_mission(state["mission_id"])
        if not mission:
            raise RuntimeError(f"Cannot start run {state.get('mission_id')}: mission missing")
        return await self._execute(mission, state, resume=False)

    async def resume(self, mission: Mission) -> Mission:
        breadcrumb = self.repository.latest_checkpoint(mission.id)
        replayed = replay_state(self.repository.run_events(mission.id))
        self._emit(mission.id, EventKind.RUN_RESUMED, "supervisor",
                   {"last_node": breadcrumb["node"] if breadcrumb else None,
                    "replayed_state": replayed})
        return await self._execute(mission, None, resume=True)

    async def checkpoint_state(self, mission_id: str) -> dict | None:
        config = {"configurable": {"thread_id": mission_id}}
        async with self._checkpointer() as checkpointer:
            graph = self._builder().compile(checkpointer=checkpointer)
            snapshot = await graph.aget_state(config)
            return dict(snapshot.values) if snapshot.values else None


BootstrapWorkflow = DurableMissionWorkflow


class AutonomousMissionState(TypedDict, total=False):
    mission_id: str
    project_id: str
    objective: str
    workspace_path: str
    current_node: str
    feedback: str
    test_evidence: str
    test_passed: bool
    iteration: int
    manual_checks: bool
    team: list[dict]
    security_passed: bool


class AutonomousMissionWorkflow(DurableMissionWorkflow):
    """Compact role graph with deterministic tests and bounded repair loops."""

    graph_version = "autonomous-v1"

    def __init__(self, repository: Repository, studio: OllamaAgentStudio, checkpoint_path: Path,
                 max_repair_loops: int = 2, event_bus: EventBus | None = None,
                 run_manager: RunManager | None = None, enable_critic: bool = False,
                 search: SearchConfig | None = None,
                 verifier: "Verifier | None" = None,
                 enable_debate: bool = False):
        super().__init__(repository, executor=None, checkpoint_path=checkpoint_path,
                         event_bus=event_bus, run_manager=run_manager)  # type: ignore[arg-type]
        self.studio = studio
        self.max_repair_loops = max_repair_loops
        self.enable_critic = enable_critic
        self.enable_debate = enable_debate
        self.search = SpeculativeSearch(search or SearchConfig(), verifier=verifier)
        self._shadow_cache: dict[str, ShadowGit] = {}
        self._shadow_disabled = False

    def _shadow_for(self, workspace_path: str) -> ShadowGit | None:
        if self._shadow_disabled:
            return None
        shadow = self._shadow_cache.get(workspace_path)
        if shadow is None:
            shadow = ShadowGit(Path(workspace_path))
            self._shadow_cache[workspace_path] = shadow
        return shadow

    def _open_candidate(self, state: AutonomousMissionState) -> None:
        """Start a gated candidate branch; degrades to pass-through if git is unusable."""
        mission_id = state["mission_id"]
        shadow = self._shadow_for(state["workspace_path"])
        if shadow is None:
            return
        try:
            if not shadow.ensure():
                self._shadow_disabled = True
                self._emit(mission_id, "shadow.unavailable", "shadow-git",
                           {"reason": "git executable not found"})
                return
            shadow.begin_candidate()
            self._emit(mission_id, "shadow.candidate_opened", "shadow-git",
                       {"branch": CANDIDATE_BRANCH, "baseline": shadow.head()})
        except ShadowGitError as exc:
            self._shadow_disabled = True
            self._emit(mission_id, "shadow.unavailable", "shadow-git", {"reason": str(exc)})

    def _gate_result(self, state: AutonomousMissionState, passed: bool) -> None:
        """Commit the candidate; fast-forward main on green, leave it for rollback on red."""
        mission_id = state["mission_id"]
        shadow = self._shadow_for(state["workspace_path"])
        if shadow is None or not shadow.available:
            return
        try:
            baseline = shadow._must("rev-parse", "--verify", "main").strip()
            sha = shadow.candidate_commit(f"candidate after iteration {state.get('iteration', 0)}")
            changed = shadow.changed_files()
            if not changed and sha == baseline:
                self._emit(mission_id, "shadow.candidate_empty", "shadow-git",
                           {"commit": sha, "reason": "no workspace changes this iteration"})
                return
            self._emit(mission_id, "shadow.candidate_committed", "shadow-git",
                       {"commit": sha, "changed_files": changed})
            if passed:
                forwarded = shadow.fast_forward()
                self._emit(mission_id, "shadow.forwarded", "shadow-git", {"commit": forwarded})
        except ShadowGitError as exc:
            self._emit(mission_id, "shadow.unavailable", "shadow-git", {"reason": str(exc)})

    def _builder(self) -> StateGraph:
        builder = StateGraph(AutonomousMissionState)
        builder.add_node("intake", self._instrument("intake", self._auto_intake))
        builder.add_node("team_execution", self._instrument("team_execution", self._team_execution))
        builder.add_node("developer", self._instrument("developer", self._developer))
        if self.enable_critic:
            builder.add_node("critic", self._instrument("critic", self._critic))
        builder.add_node("reviewer", self._instrument("reviewer", self._reviewer))
        builder.add_node("test_runner", self._instrument("test_runner", self._test_runner))
        if self.enable_debate:
            builder.add_node("deliberate", self._instrument("deliberate", self._deliberate))
        builder.add_node("tester", self._instrument("tester", self._tester))
        builder.add_node("security_gate", self._instrument("security_gate", self._security_gate))
        builder.add_node("complete", self._instrument("complete", self._complete))
        builder.add_edge(START, "intake")
        builder.add_edge("intake", "team_execution")
        if self.enable_debate:
            builder.add_edge("team_execution", "deliberate")
            builder.add_edge("deliberate", "test_runner")
        else:
            builder.add_edge("team_execution", "test_runner")
        if self.enable_critic:
            builder.add_edge("developer", "critic")
            builder.add_conditional_edges("critic", self._route_after_critic,
                                          {"revise": "developer", "continue": "test_runner"})
        else:
            builder.add_edge("developer", "test_runner")
        builder.add_conditional_edges("test_runner", self._route_after_runner, {"repair": "developer", "review": "tester"})
        builder.add_conditional_edges("tester", self._route_after_test,
                                      {"repair_review": "reviewer", "complete": "security_gate"})
        builder.add_edge("reviewer", "developer")
        builder.add_conditional_edges("security_gate", self._route_after_security, {"repair": "developer", "complete": "complete"})
        builder.add_edge("complete", END)
        return builder

    async def _deliberate(self, state: AutonomousMissionState) -> dict:
        """Planner convenes a bounded debate; positions scored, votes recorded."""
        from .debate import DebateSession

        mission_id = state["mission_id"]
        self.repository.transition(mission_id, MissionStatus.RUNNING, "deliberate")
        roles = [member["role_id"] for member in state.get("team", [])]
        session = DebateSession(roles, max_rounds=2)
        self._emit(mission_id, "debate.started", "supervisor", {
            "session": session.session_id, "participants": session.participants})

        async def produce(role, round_index, best):
            result = await self.studio.run_role(
                mission_id, state["project_id"], Path(state["workspace_path"]), role,
                state["objective"], state.get("feedback", ""), state.get("test_evidence", ""))
            return {"role": role, "stance": "support" if result.verdict != "CHANGES_REQUIRED" else "against",
                    "summary": result.summary, "verdict": result.verdict,
                    "feedback": result.feedback}

        outcome = await session.run(produce, state["objective"], state.get("test_evidence", ""))
        for transcript in outcome["transcript"]:
            self._emit(mission_id, "debate.round", "planner", {
                "session": outcome["session_id"], "round": transcript["round"],
                "positions": transcript["positions"]})
        self._emit(mission_id, "debate.vote", "planner", {
            "session": outcome["session_id"], "verdict": outcome["verdict"],
            "votes": outcome["votes"], "score": outcome["score"]})
        self._emit(mission_id, "debate.concluded", "planner", {
            "session": outcome["session_id"], "participants": outcome["participants"],
            "verdict": outcome["verdict"], "feedback": outcome["feedback"]})
        return {"current_node": "deliberate",
                "feedback": outcome["feedback"] or state.get("feedback", ""),
                "debate_verdict": outcome["verdict"]}

    async def _critic(self, state: AutonomousMissionState) -> dict:
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, "critic")
        result = await self.studio.run_role(state["mission_id"], state["project_id"],
                                            Path(state["workspace_path"]), "critic",
                                            state["objective"], state.get("feedback", ""),
                                            state.get("test_evidence", ""))
        return {"current_node": "critic", "critic_verdict": result.verdict,
                "feedback": result.feedback or state.get("feedback", "")}

    def _route_after_critic(self, state: AutonomousMissionState) -> str:
        if state.get("critic_verdict") == "CHANGES_REQUIRED" and state.get("iteration", 0) < self.max_repair_loops:
            return "revise"
        return "continue"

    async def _reviewer(self, state: AutonomousMissionState) -> dict:
        mission_id = state["mission_id"]
        self.repository.transition(mission_id, MissionStatus.RUNNING, "reviewer")
        self._emit(mission_id, "repair.reviewer", "supervisor",
                   {"iteration": state.get("iteration", 0), "strategy": "retry-with-different-role",
                    "prior_feedback": (state.get("feedback") or "")[-500:]})
        result = await self.studio.run_role(mission_id, state["project_id"],
                                            Path(state["workspace_path"]), "reviewer",
                                            state["objective"], state.get("feedback", ""),
                                            state.get("test_evidence", ""))
        return {"current_node": "reviewer",
                "feedback": result.feedback or state.get("feedback", "")}

    async def _auto_intake(self, state: AutonomousMissionState) -> dict:
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, "intake")
        self._emit(state["mission_id"], "workflow.started", "supervisor", {"graph_version": self.graph_version})
        team = TeamPlanner.plan(state["objective"])
        self.repository.save_team(state["mission_id"], team)
        self._emit(state["mission_id"], "team.formed", "supervisor",
            {"roles": [m["role_id"] for m in team], "skills": sorted({s for m in team for s in m["skills"]})})
        return {"current_node": "intake", "iteration": 0, "feedback": "", "team": team}

    async def _team_execution(self, state: AutonomousMissionState) -> dict:
        self._open_candidate(state)
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, "team_execution")
        for member in state["team"]:
            if member["role_id"] == "qa-engineer":
                continue
            self.repository.update_team_member(state["mission_id"], member["role_id"], "RUNNING")
            try:
                result = await self.studio.run_specialist(state["mission_id"], state["project_id"],
                    Path(state["workspace_path"]), member["role_id"], member["name"], member["purpose"],
                    member["skills"], state["objective"], state.get("feedback", ""), state.get("test_evidence", ""))
            except Exception:
                self.repository.update_team_member(state["mission_id"], member["role_id"], "FAILED")
                raise
            self.repository.update_team_member(state["mission_id"], member["role_id"], "COMPLETED")
        return {"current_node": "team_execution", "iteration": 1}

    async def _role(self, state: AutonomousMissionState, role: str, node: str) -> dict:
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, node)
        result = await self.studio.run_role(state["mission_id"], state["project_id"], Path(state["workspace_path"]), role,
                                            state["objective"], state.get("feedback", ""), state.get("test_evidence", ""))
        output = {"current_node": node}
        if role == "tester":
            output.update({"feedback": result.feedback, "test_passed": result.verdict == "PASS" and state.get("test_passed", False)})
        return output

    async def _architect(self, state): return await self._role(state, "architect", "architect")
    async def _developer(self, state): return await self._role(state, "developer", "developer")
    async def _ui_expert(self, state): return await self._role(state, "ui-expert", "ui_expert")
    async def _tester(self, state: AutonomousMissionState) -> dict:
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, "tester")
        passed = state.get("test_passed", False)
        feedback = "Automated checks passed; acceptance evidence verified." if passed else state.get("test_evidence", "Tests failed.")[-6000:]
        self._emit(state["mission_id"], "agent.started", "tester", {"mode": "deterministic-evidence-review"})
        self._emit(state["mission_id"], "agent.completed", "tester", {"summary": "Senior tester reviewed deterministic evidence.", "files": [], "verdict": "PASS" if passed else "CHANGES_REQUIRED", "feedback": feedback})
        self.repository.update_team_member(state["mission_id"], "qa-engineer", "COMPLETED" if passed else "CHANGES_REQUIRED")
        return {"current_node": "tester", "feedback": feedback, "test_passed": passed}

    async def _test_runner(self, state: AutonomousMissionState) -> dict:
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, "test_runner")
        self._emit(state["mission_id"], EventKind.TOOL_CALLED, "test-runner", {"tool": "workspace.test"})
        passed, evidence = await WorkspaceGuard(Path(state["workspace_path"])).test()
        manual = evidence.startswith("No automated test framework")
        self._emit(state["mission_id"], EventKind.TOOL_COMPLETED, "test-runner",
                   {"tool": "workspace.test", "passed": passed})
        self._emit(state["mission_id"], "tests.completed", "test-runner", {"passed": passed, "manual_checks": manual, "output": evidence[-6000:]})
        if passed or not manual:
            self._gate_result(state, passed)
        return {"current_node": "test_runner", "test_passed": passed, "test_evidence": evidence, "manual_checks": manual}

    def _route_after_test(self, state: AutonomousMissionState) -> str:
        if state.get("test_passed") or state.get("iteration", 0) >= self.max_repair_loops:
            return "complete"
        return "repair_review"

    def _route_after_runner(self, state: AutonomousMissionState) -> str:
        if not state.get("test_passed") and state.get("iteration", 0) < self.max_repair_loops:
            return "repair"
        return "review"

    async def _security_gate(self, state: AutonomousMissionState) -> dict:
        self.repository.transition(state["mission_id"], MissionStatus.RUNNING, "security_gate")
        workspace = Path(state["workspace_path"])
        secrets = scan_secrets(workspace)
        deps = scan_dependencies(workspace)
        blocking_deps = [f for f in deps.findings if str(f.get("severity", "")).lower() in {"high", "critical"}]
        passed = not secrets and not blocking_deps
        self._emit(state["mission_id"], "security.scanned", "security-gate", {
            "passed": passed,
            "secrets_findings": len(secrets),
            "dependency_tool": deps.tool,
            "dependency_scan_available": deps.available,
            "blocking_dependency_findings": len(blocking_deps),
        })
        output = {"current_node": "security_gate", "security_passed": passed}
        if not passed:
            secret_summary = "; ".join(f"{f['rule']} in {f['file']}:{f['line']}" for f in secrets[:10])
            dep_summary = "; ".join(f"{f['package']} ({f['vulnerability_id']}, {f['severity']})" for f in blocking_deps[:10])
            output["feedback"] = f"Security gate blocked: secrets=[{secret_summary}] dependencies=[{dep_summary}]"
        return output

    def _route_after_security(self, state: AutonomousMissionState) -> str:
        if state.get("security_passed", True) or state.get("iteration", 0) >= self.max_repair_loops:
            return "complete"
        return "repair"

    async def _complete(self, state: AutonomousMissionState) -> dict:
        passed = state.get("test_passed", False)
        security_passed = state.get("security_passed", True)
        status = MissionStatus.COMPLETED_WITH_MANUAL_CHECKS if state.get("manual_checks") else (
            MissionStatus.COMPLETED if passed and security_passed else MissionStatus.FAILED)
        if status is MissionStatus.FAILED:
            shadow = self._shadow_for(state["workspace_path"])
            if shadow and shadow.available:
                try:
                    shadow.rollback()
                    self._emit(state["mission_id"], "shadow.rolled_back", "shadow-git",
                               {"reason": "tests red; workspace restored to baseline"})
                except ShadowGitError as exc:
                    self._emit(state["mission_id"], "shadow.unavailable", "shadow-git", {"reason": str(exc)})
        self.repository.transition(state["mission_id"], status, "complete")
        self._emit(state["mission_id"], "workflow.completed", "supervisor",
            {"status": status, "test_passed": passed, "security_passed": security_passed})
        return {"current_node": "complete"}

    async def _developer(self, state):
        self._open_candidate(state)
        if self.search.active:
            output = await self._speculative_developer(state)
        else:
            output = await self._role(state, "developer", "developer")
        output["iteration"] = state.get("iteration", 0) + 1
        return output

    async def _speculative_developer(self, state: AutonomousMissionState) -> dict:
        """Fan out beam_width developer candidates on isolated shadow branches,
        score them with the verifier, forward the winner."""
        mission_id = state["mission_id"]
        shadow = self._shadow_for(state["workspace_path"])
        if shadow is None or not shadow.ensure():
            self._emit(mission_id, "search.degraded", "supervisor",
                       {"reason": "shadow-git unavailable; single-path fallback"})
            return await self._role(state, "developer", "developer")

        beam = self.search.config.beam_width
        tags = [f"v{variant}" for variant in range(beam)]
        worktrees = {tag: shadow.add_variant_worktree(tag) for tag in tags}

        async def run_variant(variant: int, tag: str) -> dict:
            try:
                result = await self.studio.run_role(
                    mission_id, state["project_id"], worktrees[tag],
                    "developer", state["objective"], state.get("feedback", ""),
                    state.get("test_evidence", ""), variant=variant)
                files, verdict, summary, feedback = (
                    result.files_written, result.verdict, result.summary, result.feedback)
            except (RunCancelled, BudgetExhausted):
                raise
            except Exception as exc:
                files, verdict, summary, feedback = [], "CHANGES_REQUIRED", f"variant {variant} failed", str(exc)
            shadow.commit_variant_worktree(worktrees[tag], f"speculative candidate {tag}")
            tests_passed, test_output = await WorkspaceGuard(worktrees[tag]).test()
            return {"summary": summary, "files_written": files,
                    "verdict": verdict, "feedback": feedback,
                    "tests_passed": tests_passed, "test_output": test_output[-4000:]}

        try:
            candidates = await asyncio.gather(
                *(run_variant(i, tag) for i, tag in enumerate(tags)))
        finally:
            shadow.remove_variant_worktrees()
        candidates = list(candidates)

        winner_payload, all_branches = await self.search.aselect(
            candidates, depth=state.get("iteration", 0),
            objective=state["objective"], evidence=state.get("test_evidence", ""))
        winner_index = candidates.index(winner_payload) if winner_payload in candidates else 0

        scores = [branch.score for branch in all_branches]
        self._emit(mission_id, "search.expanded", "supervisor",
                   {"beam_width": len(all_branches), "scores": scores})
        pruned = [branch.id for branch in all_branches if branch.pruned]
        if pruned:
            self._emit(mission_id, "search.pruned", "supervisor",
                       {"pruned": pruned, "kept_score": all_branches[winner_index].score})
        forwarded = shadow.forward_variant(tags[winner_index])
        self._emit(mission_id, "search.selected", "supervisor",
                   {"variant": winner_index, "commit": forwarded,
                    "files": winner_payload.get("files_written") or []})

        return {"current_node": "developer", "feedback": winner_payload.get("feedback", ""),
                "summary": winner_payload.get("summary", "")}

    def _initial_state(self, mission: Mission, project: Project) -> AutonomousMissionState:
        # Retry/resume reuses the checkpoint thread, so every volatile channel
        # must be seeded explicitly or verdicts leak in from the prior attempt.
        return {"mission_id": mission.id, "project_id": project.id,
            "objective": mission.objective, "workspace_path": str(project.workspace_path),
            "current_node": "intake", "iteration": 0, "feedback": "", "test_evidence": "",
            "test_passed": False, "manual_checks": False, "security_passed": True}

    async def start(self, mission: Mission, project: Project) -> Mission:
        return await self._execute(mission, self._initial_state(mission, project), resume=False)

    async def resume(self, mission: Mission) -> Mission:
        project = self.repository.get_project(mission.project_id)
        if not project:
            raise RuntimeError(f"Project {mission.project_id} is missing; cannot resume mission")
        breadcrumb = self.repository.latest_checkpoint(mission.id)
        replayed = replay_state(self.repository.run_events(mission.id))
        self._emit(mission.id, EventKind.RUN_RESUMED, "supervisor",
                   {"last_node": breadcrumb["node"] if breadcrumb else None,
                    "replayed_state": replayed})
        return await self._execute(mission, self._initial_state(mission, project), resume=True)
