"""Application configuration.

All settings are environment driven (``GTM_`` prefix) so that the same
image runs locally, in CI and in a container without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime configuration for the BoRo GTM Core backend."""

    model_config = SettingsConfigDict(
        env_prefix="GTM_",
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "BoRo GTM Core"
    env: str = "local"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+psycopg://gtm:gtm@localhost:5432/gtm_core"
    test_database_url: str = (
        "postgresql+psycopg://gtm:gtm@localhost:5432/gtm_core_test"
    )

    # Engine behaviour
    min_contextual_coverage: float = 0.5
    score_tolerance: float = 0.005

    sql_echo: bool = False

    @property
    def data_dir(self) -> Path:
        return REPO_ROOT / "data"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
