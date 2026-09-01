import pytest

from ultron.debate import ADVERSARIAL_ROLES, DebateSession, Position


async def fake_produce(role, round_index, best):
    critical = role == "critic" and round_index == 1
    return Position(
        role=role,
        stance="against" if critical else "support",
        summary=f"{role} position round {round_index}",
        verdict="CHANGES_REQUIRED" if critical else "PASS",
        feedback=f"{role} feedback",
    )


def test_debate_participants_always_include_adversaries():
    session = DebateSession(roles=["planner", "developer"], max_rounds=1)

    assert set(session.participants) == {"planner", "developer", *ADVERSARIAL_ROLES}


def test_debate_runs_bounded_rounds_and_records_votes():
    session = DebateSession(roles=["planner", "architect", "developer", "tester"],
                            max_rounds=2)

    outcome = run_debate(session)

    assert len(session.transcripts) == 2
    assert outcome["verdict"] in {"PASS", "CHANGES_REQUIRED"}
    assert outcome["votes"] == len(outcome["participants"])
    transcript_positions = {p["role"] for round_ in session.transcripts for p in round_["positions"]}
    assert {"planner", "critic", "security"} <= transcript_positions


def test_critic_objection_flips_majority_to_changes_required():
    async def forceful_critic(role, round_index, best):
        return Position(role=role, stance="against", summary="blocking flaw",
                        verdict="CHANGES_REQUIRED", feedback="security hole")

    session = DebateSession(roles=["planner", "developer"], max_rounds=1)
    import asyncio
    outcome = asyncio.run(session.run(forceful_critic, "Build it", ""))

    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert "security hole" in outcome["feedback"]


def test_max_rounds_must_be_positive():
    with pytest.raises(ValueError):
        DebateSession(roles=["planner"], max_rounds=0)


def run_debate(session) -> dict:
    import asyncio
    return asyncio.run(session.run(fake_produce, "Build it", "tests red"))