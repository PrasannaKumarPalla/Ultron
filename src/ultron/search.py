"""Speculative tree search: fan out candidates, score them, prune to top-k.

The verifier is pluggable; the default scores locally from role output
signals (files written, verdicts, feedback substance) so pruning works
deterministically without any network. LLM-backed verifiers plug into the
same Protocol later.
"""

from __future__ import annotations

import itertools
import re
import uuid
from dataclasses import dataclass, field

_PYTEST_TALLY = re.compile(r"(\d+) (passed|failed|error|errors)")


@dataclass(frozen=True)
class SearchConfig:
    beam_width: int = 1
    depth: int = 2

    def __post_init__(self) -> None:
        if self.beam_width < 1:
            raise ValueError("beam_width must be >= 1")
        if self.depth < 1:
            raise ValueError("depth must be >= 1")

    @property
    def active(self) -> bool:
        return self.beam_width > 1


class Verifier:
    """Scores a candidate branch. A real test run, when present, dominates;
    self-reported signals only break ties. Higher = better."""

    def score(self, objective: str, evidence: str, candidate: dict) -> float:
        score = 0.0
        if "tests_passed" in candidate:
            score += 0.6 if candidate["tests_passed"] else -0.6
            score += self._tally_bonus(candidate.get("test_output") or "")
        elif candidate.get("verdict") == "PASS":
            score += 0.5
        elif candidate.get("verdict") == "CHANGES_REQUIRED":
            score -= 0.2
        files = candidate.get("files_written") or []
        score += min(len(files), 3) * 0.1
        summary = (candidate.get("summary") or "").strip()
        score += min(len(summary) / 500.0, 0.1)
        feedback = (candidate.get("feedback") or "").strip()
        score += min(len(feedback) / 2000.0, 0.05)
        return round(max(-1.0, min(1.0, score)), 4)

    @staticmethod
    def _tally_bonus(output: str) -> float:
        passed = failed = 0
        for count, kind in _PYTEST_TALLY.findall(output):
            if kind == "passed":
                passed += int(count)
            else:
                failed += int(count)
        total = passed + failed
        if not total:
            return 0.0
        return round((passed / total - 0.5) * 0.2, 4)


class LLMVerifier(Verifier):
    """Blends the deterministic signal score with an async model judgement.

    `score_fn(objective, evidence, candidate) -> float in [-1, 1]` is any
    callable that asks a model to rate the candidate; its verdict and the
    signal score are averaged so a model outage degrades to signals only.
    """

    def __init__(self, score_fn):
        self._score_fn = score_fn

    async def ascore(self, objective: str, evidence: str, candidate: dict) -> float:
        signal = self.score(objective, evidence, candidate)
        try:
            judged = float(await self._score_fn(objective, evidence, candidate))
        except Exception:
            return signal
        judged = max(-1.0, min(1.0, judged))
        return round((signal + judged) / 2.0, 4)


@dataclass
class Branch:
    payload: dict
    parent_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    depth: int = 0
    score: float | None = None
    pruned: bool = False


class SpeculativeSearch:
    """Beam search over candidate continuations with recorded prune history."""

    def __init__(self, config: SearchConfig, verifier: Verifier | None = None):
        self.config = config
        self.verifier = verifier or Verifier()
        self._branches: list[Branch] = []
        self._ids = itertools.count(1)

    @property
    def active(self) -> bool:
        return self.config.beam_width > 1

    def expand(self, candidates: list[dict], parent_id: str | None = None,
               depth: int = 0, objective: str = "", evidence: str = "") -> list[Branch]:
        limited = candidates[: self.config.beam_width]
        branches = [Branch(payload=candidate, parent_id=parent_id, depth=depth)
                    for candidate in limited]
        for branch in branches:
            branch.score = self.verifier.score(objective, evidence, branch.payload)
            self._branches.append(branch)
        return branches

    def prune(self, branches: list[Branch]) -> Branch | None:
        """Keep only the best branch; mark the rest pruned. Records history."""
        if not branches:
            return None
        ranked = sorted(branches, key=lambda b: b.score if b.score is not None else -2.0,
                        reverse=True)
        winner = ranked[0]
        for loser in ranked[1:]:
            loser.pruned = True
        return winner

    def select(self, candidates: list[dict], parent_id: str | None = None,
               depth: int = 0, objective: str = "", evidence: str = "") -> tuple[dict | None, list[Branch]]:
        """Expand + score + prune in one step; returns (winner_payload, all_branches)."""
        branches = self.expand(candidates, parent_id=parent_id, depth=depth,
                               objective=objective, evidence=evidence)
        winner = self.prune(branches)
        return (winner.payload if winner else None), branches

    async def aselect(self, candidates: list[dict], parent_id: str | None = None,
                      depth: int = 0, objective: str = "", evidence: str = "") -> tuple[dict | None, list[Branch]]:
        """Like select(), but awaits an async verifier (`ascore`) when the
        verifier exposes one — e.g. an LLM-backed judge."""
        ascore = getattr(self.verifier, "ascore", None)
        if ascore is None:
            return self.select(candidates, parent_id, depth, objective, evidence)
        limited = candidates[: self.config.beam_width]
        branches = [Branch(payload=c, parent_id=parent_id, depth=depth) for c in limited]
        for branch in branches:
            branch.score = await ascore(objective, evidence, branch.payload)
            self._branches.append(branch)
        winner = self.prune(branches)
        return (winner.payload if winner else None), branches

    def history(self) -> list[dict]:
        return [{"id": branch.id, "parent": branch.parent_id, "depth": branch.depth,
                 "score": branch.score, "pruned": branch.pruned,
                 "summary": (branch.payload.get("summary") or "")[:200],
                 "files": branch.payload.get("files_written") or []}
                for branch in self._branches]

    def paths_not_taken(self) -> list[dict]:
        return [entry for entry in self.history() if entry["pruned"]]
