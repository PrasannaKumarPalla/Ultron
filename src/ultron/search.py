"""Speculative tree search: fan out candidates, score them, prune to top-k.

The verifier is pluggable; the default scores locally from role output
signals (files written, verdicts, feedback substance) so pruning works
deterministically without any network. LLM-backed verifiers plug into the
same Protocol later.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field


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
    """Scores a candidate branch from observable signals. Higher = better."""

    def score(self, objective: str, evidence: str, candidate: dict) -> float:
        score = 0.0
        files = candidate.get("files_written") or []
        score += min(len(files), 3) * 0.15
        if candidate.get("verdict") == "PASS":
            score += 0.5
        elif candidate.get("verdict") == "CHANGES_REQUIRED":
            score -= 0.2
        summary = (candidate.get("summary") or "").strip()
        score += min(len(summary) / 500.0, 0.15)
        feedback = (candidate.get("feedback") or "").strip()
        score += min(len(feedback) / 2000.0, 0.1)
        return round(max(-1.0, min(1.0, score)), 4)


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

    def history(self) -> list[dict]:
        return [{"id": branch.id, "parent": branch.parent_id, "depth": branch.depth,
                 "score": branch.score, "pruned": branch.pruned,
                 "summary": (branch.payload.get("summary") or "")[:200],
                 "files": branch.payload.get("files_written") or []}
                for branch in self._branches]

    def paths_not_taken(self) -> list[dict]:
        return [entry for entry in self.history() if entry["pruned"]]
