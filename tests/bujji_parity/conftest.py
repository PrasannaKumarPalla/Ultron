from pathlib import Path

import pytest

from ultron.config import Settings
from ultron.db import Repository


class FakeBujjiSdk:
    version = "0.0.0+parity"

    def list_engines(self):
        return ["ollama"]

    def list_models(self):
        return ["qwen2.5:7b", "qwen3:30b"]

    def ask_full(self, query, *, model=None):
        return {
            "content": f"echo:{query}",
            "usage": {},
            "model": model or "qwen2.5:7b",
            "engine": "ollama",
        }

    async def ask_stream(self, query, *, model=None, **_kwargs):
        for token in ["echo:", query]:
            yield token


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        database_path=tmp_path / "ultron.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(value.database_path).initialize()
    return value


@pytest.fixture
def fake_sdk() -> FakeBujjiSdk:
    return FakeBujjiSdk()


@pytest.fixture
def roles_path() -> Path:
    return Path(__file__).resolve().parents[2] / "src" / "ultron" / "roles.yaml"
