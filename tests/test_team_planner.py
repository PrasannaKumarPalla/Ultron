from ultron.team_planner import TeamPlanner


def test_team_planner_expands_for_ai_dashboard_deployment():
    team = TeamPlanner.plan("Build an AI analytics dashboard with RAG, authentication, database, Docker deployment and monitoring")
    roles = [member["role_id"] for member in team]
    assert roles == [
        "cloud-architect", "product-manager", "backend-developer", "frontend-developer",
        "ui-expert", "security-engineer", "data-engineer", "ml-engineer",
        "devops-engineer", "qa-engineer",
    ]
    assert all(member["skills"] and member["permissions"] for member in team)


def test_team_planner_keeps_small_backend_team_small():
    roles = [member["role_id"] for member in TeamPlanner.plan("Build a tiny command line calculator")]
    assert roles == ["cloud-architect", "product-manager", "backend-developer", "qa-engineer"]
