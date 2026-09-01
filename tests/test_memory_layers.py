import pytest

from ultron.db import Repository
from ultron.memory_layers import (HashEmbedder, LayeredMemory, WorkingMemory,
                                  cosine, summarize)
from ultron.models import ProjectCreate


@pytest.fixture
def repo(tmp_path):
    repo = Repository(tmp_path / "mem.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Mem", workspace_path=tmp_path / "ws"))
    return repo, project.id


def test_hash_embedder_is_deterministic_normalized_and_discriminating():
    embedder = HashEmbedder()
    a1 = embedder.embed("deploy kubernetes cluster with blue-green rollout")
    a2 = embedder.embed("deploy kubernetes cluster with blue-green rollout")
    b = embedder.embed("cook pasta with tomato sauce")

    assert a1 == a2
    assert len(a1) == 256
    assert abs(sum(v * v for v in a1) - 1.0) < 1e-9
    assert cosine(a1, a2) > cosine(a1, b)


def test_summarize_picks_lead_and_keyword_dense_sentence():
    text = ("The deploy failed. Kubernetes pods crashed because the image tag "
            "was missing. The weather was nice.")
    summary = summarize(text)

    assert summary.startswith("The deploy failed.")
    assert "Kubernetes" in summary


def test_working_memory_evicts_with_rolling_summary():
    memory = WorkingMemory(cap_items=4)

    for index in range(6):
        memory.add(f"role{index}", f"message {index} about database migrations")

    assert len(memory) <= 4
    context = memory.context()
    assert "earlier:" in context
    assert "role5: message 5" in context
    assert "role0:" not in context


def test_recall_ranks_related_episodes_above_noise(repo):
    store, project_id = repo
    memory = LayeredMemory(store)

    memory.observe(project_id, "the payment service uses stripe webhooks")
    memory.observe(project_id, "stripe webhook retries need idempotency keys")
    memory.observe(project_id, "garden tomatoes grow best in summer sun")
    memory.observe(project_id, "database migrations run via alembic upgrade head")
    memory.observe(project_id, "alembic autogenerate misses server defaults sometimes")

    hits = memory.recall(project_id, "stripe webhooks idempotency", limit=5)

    assert len(hits) >= 2
    stripe_scores = [hit["score"] for hit in hits if "stripe" in hit["text"]]
    tomato_scores = [hit["score"] for hit in hits if "tomato" in hit["text"]]
    assert stripe_scores and (not tomato_scores or min(stripe_scores) > max(tomato_scores))
    assert hits[0]["score"] >= hits[-1]["score"]


def test_consolidation_distills_recurring_themes_into_unique_lessons(repo):
    store, project_id = repo
    memory = LayeredMemory(store)

    for index in range(4):
        memory.observe(project_id, f"deployment rollback needed after bad release {index}")
    memory.observe(project_id, "unrelated one-off note about coffee")

    created = memory.consolidate(project_id)

    assert created, "expected at least one distilled lesson"
    lessons = memory.lessons(project_id)
    assert len(lessons) == len(created)
    assert any("deployment" in lesson["lesson"] for lesson in lessons)
    # second consolidation is a no-op: episodes were marked consolidated
    assert memory.consolidate(project_id) == []
