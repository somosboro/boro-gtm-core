"""Shared M2 fixtures. Imported by tests/conftest.py."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from boro_gtm.discovery import seeds
from boro_gtm.discovery.domain.models import DiscoveryProvider
from boro_gtm.discovery.providers.fixtures import (
    FixtureCsvExportAdapter,
    FixtureJsonDirectoryAdapter,
    FixtureScrapeAdapter,
)
from tests.conftest import _ALL_TABLES


@pytest.fixture
def m2_session(session):
    """A session with the M2 registry and fixture providers seeded."""
    seeds.seed_all(session)
    session.flush()
    return session


@pytest.fixture
def provider_json(m2_session) -> DiscoveryProvider:
    return m2_session.scalar(
        select(DiscoveryProvider).where(
            DiscoveryProvider.provider_key == FixtureJsonDirectoryAdapter.PROVIDER_KEY
        )
    )


@pytest.fixture
def provider_csv(m2_session) -> DiscoveryProvider:
    return m2_session.scalar(
        select(DiscoveryProvider).where(
            DiscoveryProvider.provider_key == FixtureCsvExportAdapter.PROVIDER_KEY
        )
    )


@pytest.fixture
def provider_scrape(m2_session) -> DiscoveryProvider:
    return m2_session.scalar(
        select(DiscoveryProvider).where(
            DiscoveryProvider.provider_key == FixtureScrapeAdapter.PROVIDER_KEY
        )
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def make_query(session, provider, page: int = 0):
    """A discovery run + query to hang evidence off."""
    from boro_gtm.discovery.domain.models import DiscoveryQuery, DiscoveryRun

    run = DiscoveryRun(
        provider_id=provider.id, status="FETCHING",
        allow_partial_resolution=False, created_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    query = DiscoveryQuery(
        discovery_run_id=run.id, parameters={"q": "test"}, page_number=page,
        result_count=0, issued_at=datetime.now(UTC),
    )
    session.add(query)
    session.flush()
    return run, query


def run_discovery(session, adapter, provider, records, *, market_id=None,
                  allow_partial: bool = False, query_params: dict | None = None):
    """Fetch → normalize → resolve one batch of fixture records."""
    from boro_gtm.discovery.services import runs

    adapter.load(list(records))
    run = runs.create_run(session, provider, market_id=market_id,
                          allow_partial_resolution=allow_partial)
    report = runs.fetch(session, run, adapter, query_params or {"q": "test"})
    runs.normalize_run(session, run, adapter)
    stats = runs.resolve_run(session, run, adapter)
    return run, report, stats


def company_ids(session) -> set[uuid.UUID]:
    from boro_gtm.discovery.domain.models import Company

    return set(session.scalars(select(Company.id)).all())


@pytest.fixture
def committed_sessions(migrated_engine):
    """Sessions that really commit, on a pool of their own.

    Tests that need genuinely concurrent transactions cannot use the
    transaction-rollback ``session`` fixture. Borrowing connections from the
    shared engine's pool instead starves later tests, so these run on a
    dedicated NullPool engine that is disposed on teardown, and every session
    handed out is closed whether the test closes it or not.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    engine = create_engine(migrated_engine.url, poolclass=NullPool, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    handed_out: list = []

    def make_session():
        session = factory()
        handed_out.append(session)
        return session

    try:
        yield make_session
    finally:
        # Close first, truncate second. A session left holding an open read
        # transaction keeps an AccessShareLock, and TRUNCATE needs
        # AccessExclusiveLock — truncating first hangs the teardown forever
        # instead of failing, which is far harder to diagnose.
        for session in handed_out:
            session.close()
        cleanup = factory()
        try:
            cleanup.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
            cleanup.commit()
        finally:
            cleanup.close()
        engine.dispose()
