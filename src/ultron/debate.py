"""Bounded multi-agent debate with recorded votes.

Planner convenes, participants produce positions, the verifier scores them,
and after a bounded number of rounds the majority position is decided and
the transcript/vote recorded as events. Critic + Security run adversarially
and are always in the debate regardless of mission team composition.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

ADVERSARIAL_ROLES = ("critic", "security")


@dataclass
class Position:
    role: str
    stance: str = "neutral"
    summary: str = ""
    verdict: str = "NOT_APPLICABLE"
    feedback: str = ""
    score: float = 0.0


@dataclass
class DebateSession:
    """One bounded debate. Rounds <= max_rounds; votes recorded per role."""

    roles: list[str]
    max_rounds: int = 2
    verifier=None
    transcripts: list[dict] = field(default_factory=list)
    votes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verifier is None:
            from .search import Verifier
            self.verifier = Verifier()
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        self.session_id = uuid.uuid4().hex[:12]
        self.participants = list(dict.fromkeys([*self.roles, *ADVERSARIAL_ROLES]))

    async def run(self, produce, objective: str, evidence: str = "") -> dict:
        """`produce(role, round_, best_so_far)` -> Position-like dict."""
        best: dict[str, Position] = {}
        for round_index in range(1, self.max_rounds + 1):
            round_ = {"round": round_index, "positions": []}
            for role in self.participants:
                raw = await produce(role, round_index, best.get(role))
                position = raw if isinstance(raw, Position) else Position(**raw)
                position.score = float(self.verifier.score(
                    objective, evidence,
                    {"files_written": [], "verdict": position.verdict,
                     "summary": position.summary, "feedback": position.feedback}))
                round_["positions"].append({
                    "role": role, "stance": position.stance,
                    "summary": position.summary[:200],
                    "verdict": position.verdict,
                    "score": position.score,
                    "feedback": position.feedback[:500],
                })
                best[role] = position
            self.transcripts.append(round_)

        decided = self.decide()
        self.votes = {role: "majority" for role in self.participants}
        return {
            "session_id": self.session_id,
            "rounds": self.max_rounds,
            "participants": self.participants,
            "verdict": decided["verdict"],
            "feedback": decided["feedback"][:2000],
            "score": decided["score"],
            "votes": len(self.participants),
            "transcript": self.transcripts,
        }

    def decide(self) -> dict:
        """Majority verdict across the final round; ties go 'CHANGES_REQUIRED'
        unless any PASS exists at higher total score."""
        final = self.transcripts[-1]["positions"] if self.transcripts else []
        pass_count = sum(1 for p in final if p["verdict"] == "PASS")
        change_count = sum(1 for p in final if p["verdict"] == "CHANGES_REQUIRED")
        if pass_count > len(final) / 2:
            best = max((p for p in final if p["verdict"] == "PASS"),
                       key=lambda p: p["score"], default=None)
            return {"verdict": "PASS", "score": best["score"] if best else 0.0,
                    "feedback": (best or {}).get("feedback", "")}
        if change_count >= len(final) / 2:
            best = max((p for p in final if p["verdict"] == "CHANGES_REQUIRED"),
                       key=lambda p: p["score"], default=None)
            return {"verdict": "CHANGES_REQUIRED",
                    "score": best["score"] if best else 0.0,
                    "feedback": (best or {}).get("feedback", "")}
        best = max(final, key=lambda p: p["score"], default=None)
        return {"verdict": best["verdict"] if best else "CHANGES_REQUIRED",
                "score": best["score"] if best else 0.0,
                "feedback": (best or {}).get("feedback", "")}