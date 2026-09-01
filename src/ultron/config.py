from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ULTRON_", extra="ignore"
    )

    env: str = "local"
    host: str = "127.0.0.1"
    port: int = 8766
    database_path: Path = Path("./data/ultron.db")
    checkpoint_path: Path = Path("./data/checkpoints.db")
    ollama_url: str = "http://127.0.0.1:11434"
    default_model: str = "qwen3:30b"
    execution_provider: str = "local"
    openhands_url: str = "http://127.0.0.1:8000"
    projects_root: Path = Path("./projects")
    max_repair_loops: int = 2
    roles_path: Path | None = None
    critic_enabled: bool = True
    run_token_budget: int = 150_000
    search_beam_width: int = 1
    search_depth: int = 2
    consolidation_interval_s: int = 86_400
    warm_pool_size: int = 2
    debate_enabled: bool = True
    assistant_wake_word: str = "assistant"
    assistant_vram_gb: float | None = None
    bujji_legacy_db: Path | None = None
    omniroute_url: str = "http://127.0.0.1:20128"
    omniroute_enabled: bool = True
    omniroute_config_path: Path | None = None
    omniroute_secrets_dir: Path = Path("./data/omniroute")
    omniroute_sidecar_image: str = "diegosouzapw/omniroute"
    omniroute_sidecar_package: str = "omniroute"
    router_mode: str = "auto"  # local | hosted | auto; per-call overrides via app_settings
    hosted_call_timeout_s: float = 90.0
    catalog_refresh_s: int = 21_600


@lru_cache
def get_settings() -> Settings:
    return Settings()
