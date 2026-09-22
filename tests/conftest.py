"""Shared fixtures.

Integration tests run against ``GTM_TEST_DATABASE_URL``. The schema is created
by running the real Alembic migrations, so "migrations work from an empty
database" is exercised by every integration run rather than asserted once.

No test reaches the network: the source document is read from ``data/``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_JSON = REPO_ROOT / "data" / "boro_market_intelligence_top50.json"


def _test_database_url() -> str:
    from boro_gtm.core.config import get_settings

    return os.environ.get("GTM_TEST_DATABASE_URL") or get_settings().test_database_url


def _database_available(url: str) -> bool:
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def source_payload() -> dict:
    """The supplied dataset, read from disk."""
    return json.loads(SOURCE_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def source_bytes() -> bytes:
    return SOURCE_JSON.read_bytes()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not _database_available(url):
        pytest.skip(f"Test database unavailable at {url}")
    return url


@pytest.fixture(scope="session")
def migrated_engine(database_url: str):
    """A clean database with the Alembic migrations applied."""
    from alembic import command
    from alembic.config import Config

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")

    yield engine
    engine.dispose()


#: Truncated in dependency-free order at the start of every `session` test.
_ALL_TABLES = (
    "market_score_components, market_scores, score_runs, observation_sources, "
    "market_observations, market_snapshot_categories, "
    "market_competition_assessments, market_size_estimates, market_deep_dives, "
    "market_vertical_profiles, research_gaps, scoring_model_components, "
    "scoring_models, market_snapshots, markets, sources, verticals, icps, "
    "offers, channels, market_categories"
)


@pytest.fixture
def session(migrated_engine) -> Iterator[Session]:
    """A session wrapped in a transaction that is always rolled back.

    The truncate runs *inside* that transaction, so each test starts from an
    empty database regardless of what an API-level test committed earlier,
    and the rollback leaves the other tests' data intact.
    """
    connection = migrated_engine.connect()
    transaction = connection.begin()
    connection.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        future=True,
        # A rollback inside a test (e.g. after a deliberately failed import)
        # must unwind only that test's work, not the fixture's outer
        # transaction, so the session joins via a savepoint.
        join_transaction_mode="create_savepoint",
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def imported_session(session: Session):
    """A session with the supplied snapshot already imported."""
    from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter

    SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()
    return session


@pytest.fixture
def api_client(migrated_engine, database_url, monkeypatch):
    """A TestClient bound to the migrated test database."""
    from fastapi.testclient import TestClient

    import boro_gtm.core.db as core_db
    from boro_gtm.api.main import create_app

    monkeypatch.setenv("GTM_DATABASE_URL", database_url)
    core_db.reset_engine()
    from boro_gtm.core.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        yield client

    core_db.reset_engine()
    get_settings.cache_clear()
