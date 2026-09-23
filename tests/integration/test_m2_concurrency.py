"""Concurrency against real PostgreSQL, on genuinely separate connections.

These tests do not simulate a race with mocks: two committed transactions
contend for the same row, and the database decides the winner. Committed data
is cleaned up in the fixture teardown.

Covers acceptance C10–C14.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from boro_gtm.discovery import seeds
from boro_gtm.discovery.domain.models import (
    Company,
    DiscoveryProvider,
    DiscoveryQuery,
    DiscoveryRun,
    EntityResolutionDecision,
    ProviderEntity,
    ProviderRecordVersion,
)
from boro_gtm.discovery.providers.fixtures import (
    FixtureJsonDirectoryAdapter,
    json_record,
)
from boro_gtm.discovery.services import resolution
from boro_gtm.discovery.services.ingestion import ingest_record
from tests.conftest import _ALL_TABLES

pytestmark = pytest.mark.integration

RECORD = json_record(
    {"legal_name": "Racy GmbH", "website_domain": "racy.de",
     "location": {"country": "DE", "city": "Berlin"}},
    external_id="RACE-1",
)


@pytest.fixture
def committed(committed_sessions) -> Iterator[tuple]:
    """Seeded, committed state plus a factory for concurrent sessions."""
    setup = committed_sessions()
    setup.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    seeds.seed_all(setup)
    setup.flush()
    provider = setup.scalar(
        select(DiscoveryProvider).where(
            DiscoveryProvider.provider_key == FixtureJsonDirectoryAdapter.PROVIDER_KEY))
    run = DiscoveryRun(provider_id=provider.id, status="FETCHED",
                       allow_partial_resolution=False,
                       fetch_completed_at=datetime.now(UTC),
                       created_at=datetime.now(UTC))
    setup.add(run)
    setup.flush()
    query = DiscoveryQuery(discovery_run_id=run.id, parameters={"q": "race"},
                           page_number=0, result_count=1,
                           issued_at=datetime.now(UTC))
    setup.add(query)
    setup.flush()
    result = ingest_record(setup, FixtureJsonDirectoryAdapter(), provider,
                           RECORD, query)
    setup.commit()
    ids = (result.provider_entity_id, result.version_id, provider.id)
    setup.close()

    yield committed_sessions, ids


def _resolve(session: Session, ids) -> resolution.ResolutionOutcome:
    entity_id, version_id, provider_id = ids
    entity = session.get(ProviderEntity, entity_id)
    version = session.get(ProviderRecordVersion, version_id)
    provider = session.get(DiscoveryProvider, provider_id)
    candidate = FixtureJsonDirectoryAdapter().normalize(version.parsed_payload)
    return resolution.resolve(session, entity, version, candidate, provider)


def test_two_workers_racing_to_create_produce_one_company(committed):
    """The loser must adopt the winner's company, not mint a second one."""
    factory, ids = committed
    a, b = factory(), factory()
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def worker_b() -> None:
        try:
            outcomes["b"] = _resolve(b, ids)
            b.commit()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the assert
            errors.append(exc)

    # A opens the chain but has not committed, so B cannot see it.
    outcomes["a"] = _resolve(a, ids)

    thread = threading.Thread(target=worker_b)
    thread.start()
    time.sleep(0.5)          # B is now blocked on uq_resolution_root
    a.commit()               # A wins
    thread.join(timeout=30)

    assert not thread.is_alive(), "the loser deadlocked instead of yielding"
    assert not errors, f"the race escaped as an exception: {errors}"

    audit = factory()
    try:
        assert audit.scalar(select(func.count()).select_from(Company)) == 1, (
            "a lost race must not leave an orphan identity behind"
        )
        assert audit.scalar(
            select(func.count()).select_from(EntityResolutionDecision)) == 1
        assert outcomes["a"].company_id == outcomes["b"].company_id
        assert outcomes["a"].decision_id == outcomes["b"].decision_id
    finally:
        audit.close()
        a.close()
        b.close()


def test_two_reviewers_racing_to_supersede_produce_one_chain(committed):
    """A fork is impossible: the loser is refused, not silently re-parented.

    Both reviewers read the *same* head, so both try to supersede it. The
    partial unique index lets exactly one through. The loser is told, rather
    than having its decision quietly attached to a head it never saw.
    """
    from sqlalchemy.exc import IntegrityError

    factory, ids = committed
    setup = factory()
    _resolve(setup, ids)
    setup.commit()
    entity_id = ids[0]
    head_id = resolution.current_head(setup, entity_id).id
    setup.close()

    a, b = factory(), factory()
    results: dict[str, str] = {}

    def attempt(session: Session, label: str) -> None:
        entity = session.get(ProviderEntity, entity_id)
        company = Company(created_at=datetime.now(UTC),
                          identity_policy_version="1.0",
                          lifecycle_status="ACTIVE")
        session.add(company)
        session.flush()
        assert resolution.current_head(session, entity_id).id == head_id
        try:
            resolution.append_human_decision(
                session, entity, decision="MATCHED", company_id=company.id,
                decided_by=f"reviewer:{label}")
            session.commit()
            results[label] = "won"
        except IntegrityError:
            session.rollback()
            results[label] = "refused"

    # A writes but does not commit, so B still sees the original head.
    entity_a = a.get(ProviderEntity, entity_id)
    company_a = Company(created_at=datetime.now(UTC),
                        identity_policy_version="1.0", lifecycle_status="ACTIVE")
    a.add(company_a)
    a.flush()
    resolution.append_human_decision(
        a, entity_a, decision="MATCHED", company_id=company_a.id,
        decided_by="reviewer:a")

    thread = threading.Thread(target=attempt, args=(b, "b"))
    thread.start()
    time.sleep(0.5)      # B is now blocked on uq_resolution_supersedes
    a.commit()
    results["a"] = "won"
    thread.join(timeout=30)

    assert not thread.is_alive(), "the loser deadlocked instead of being refused"
    assert results == {"a": "won", "b": "refused"}, results

    audit = factory()
    try:
        rows = audit.scalars(
            select(EntityResolutionDecision).where(
                EntityResolutionDecision.provider_entity_id == entity_id)).all()
        assert len(rows) == 2, "exactly one supersession survived"
        supersessions = [r for r in rows if r.supersedes_decision_id is not None]
        assert len(supersessions) == 1
        assert supersessions[0].supersedes_decision_id == head_id
        assert supersessions[0].decided_by == "reviewer:a"
        # The chain is still linear and its head is unambiguous.
        assert resolution.current_head(audit, entity_id).id == supersessions[0].id
    finally:
        audit.close()
        a.close()
        b.close()


def test_a_second_worker_finds_the_entity_already_resolved(committed):
    """Serial re-resolution writes nothing new — the common non-race case."""
    factory, ids = committed
    a = factory()
    first = _resolve(a, ids)
    a.commit()
    b = factory()
    second = _resolve(b, ids)
    b.commit()

    assert first.decision_id == second.decision_id
    audit = factory()
    try:
        assert audit.scalar(
            select(func.count()).select_from(EntityResolutionDecision)) == 1
        assert audit.scalar(select(func.count()).select_from(Company)) == 1
    finally:
        audit.close()
        a.close()
        b.close()


# --- job queue, on genuinely separate connections --------------------------


def test_skip_locked_hands_one_job_to_exactly_one_worker(committed_sessions):
    """Two *connections*, not two calls on one session.

    Claiming twice from a single session proves nothing: the first claim has
    already set the row to RUNNING inside that transaction, so the second
    would skip it even without ``FOR UPDATE SKIP LOCKED``. The lock only does
    work when another transaction holds it.
    """
    from boro_gtm.discovery.services import jobs

    setup = committed_sessions()
    setup.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    jobs.enqueue(setup, "FETCH", {"n": 1})
    setup.commit()
    setup.close()

    worker_a = committed_sessions()
    worker_b = committed_sessions()
    claimed_a = jobs.claim(worker_a, limit=1)     # holds the row lock, uncommitted
    assert len(claimed_a) == 1

    result: dict[str, int] = {}
    error: list[BaseException] = []

    def second_worker() -> None:
        try:
            result["b"] = len(jobs.claim(worker_b, limit=1))
            worker_b.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert
            error.append(exc)

    thread = threading.Thread(target=second_worker)
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive(), "the second worker blocked instead of skipping"
    assert not error, f"claim raised: {error}"
    assert result["b"] == 0, "a locked job must be skipped, not waited on or duplicated"

    worker_a.commit()
    audit = committed_sessions()
    try:
        rows = audit.execute(text("SELECT status, attempts FROM discovery_jobs")).all()
        assert rows == [("RUNNING", 1)], rows
    finally:
        audit.close()


def test_two_workers_draining_a_queue_never_double_claim(committed_sessions):
    from boro_gtm.discovery.services import jobs

    setup = committed_sessions()
    setup.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    for n in range(8):
        jobs.enqueue(setup, "FETCH", {"n": n})
    setup.commit()
    setup.close()

    claimed: dict[str, list] = {"a": [], "b": []}
    errors: list[BaseException] = []

    def drain(label: str) -> None:
        session = committed_sessions()
        try:
            while True:
                batch = jobs.claim(session, limit=1)
                session.commit()
                if not batch:
                    return
                claimed[label].append(batch[0].id)
                for job in batch:
                    jobs.complete(session, job)
                session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=drain, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(not t.is_alive() for t in threads)
    assert not errors, f"{errors}"
    ids_a, ids_b = claimed["a"], claimed["b"]
    assert set(ids_a) & set(ids_b) == set(), "a job was claimed by both workers"
    assert len(ids_a) + len(ids_b) == 8
    assert len(set(ids_a + ids_b)) == 8


# --- ingestion races -------------------------------------------------------


def test_two_workers_ingesting_the_same_record_create_one_entity(committed):
    """Provider entity, semantic version and byte body are all insert-or-ignore.

    A SELECT-then-INSERT would duplicate here; the unique constraints are what
    actually make this safe.
    """
    from sqlalchemy import func

    from boro_gtm.discovery.domain.models import (
        ProviderRecordBody,
        ProviderRecordVersion,
    )

    factory, ids = committed
    _, _, provider_id = ids

    record = json_record(
        {"legal_name": "Race Ingest GmbH", "website_domain": "race-ingest.de"},
        external_id="RACE-INGEST",
    )
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []

    def ingest(label: str) -> None:
        session = factory()
        try:
            provider = session.get(DiscoveryProvider, provider_id)
            query = session.scalars(select(DiscoveryQuery)).first()
            outcomes[label] = ingest_record(
                session, FixtureJsonDirectoryAdapter(), provider, record, query
            )
            session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=ingest, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert all(not t.is_alive() for t in threads)
    assert not errors, f"an ingest race escaped as an exception: {errors}"

    audit = factory()
    entity_id = outcomes["a"].provider_entity_id
    assert outcomes["b"].provider_entity_id == entity_id
    assert outcomes["a"].version_id == outcomes["b"].version_id
    assert audit.scalar(
        select(func.count()).select_from(ProviderEntity).where(
            ProviderEntity.provider_external_id == "RACE-INGEST"
        )
    ) == 1
    assert audit.scalar(
        select(func.count()).select_from(ProviderRecordVersion).where(
            ProviderRecordVersion.provider_entity_id == entity_id
        )
    ) == 1
    assert audit.scalar(
        select(func.count()).select_from(ProviderRecordBody).where(
            ProviderRecordBody.provider_record_version_id == outcomes["a"].version_id
        )
    ) == 1
    audit.close()
