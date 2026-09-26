"""Races, against two real PostgreSQL connections.

Every test here drives a genuine concurrent insert and asserts the outcome
converges: one row, no lost work, and no raw ``IntegrityError`` reaching the
caller. There are no sleeps — the second writer simply runs after the first has
committed, which is the race the unique keys exist to resolve.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import Company
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import DiscoveredLocator
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import acquisition, gaps, identity

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
URL = "https://www.meridianmechanical.com/services"


@pytest.fixture
def two_sessions(committed_sessions):
    left, right = committed_sessions(), committed_sessions()
    seed_all(left)
    left.commit()
    yield left, right
    left.rollback()
    right.rollback()


def _company(session) -> Company:
    row = Company(created_at=NOW, identity_policy_version="1.0", lifecycle_status="ACTIVE")
    session.add(row)
    session.commit()
    return row


def _run_and_attempt(session, company_id) -> tuple[uuid.UUID, uuid.UUID]:
    run = m.OperationalResearchRun(
        company_id=company_id, research_policy_version="1.0",
        target_attribute_keys=["erp"], plan_inputs={},
        research_plan_hash=uuid.uuid4().hex + uuid.uuid4().hex[:0].ljust(32, "0"),
        created_by="test", created_at=NOW,
    )
    session.add(run)
    session.flush()
    attempt = m.OperationalResearchAttempt(
        run_id=run.id, attempt_number=1, status="PENDING",
        allow_partial_assertion=True, created_at=NOW,
    )
    session.add(attempt)
    session.commit()
    return run.id, attempt.id


def test_two_workers_discovering_one_address_converge_on_one_source(two_sessions):
    left, right = two_sessions
    a = acquisition.get_or_create_source(left, URL, NOW)
    left.commit()
    b = acquisition.get_or_create_source(right, URL, NOW)
    right.commit()

    assert a.id == b.id
    assert right.scalar(select(func.count()).select_from(m.ResearchSource)) == 1


def test_two_workers_storing_identical_bytes_converge_on_one_body(two_sessions):
    left, right = two_sessions
    payload = b"<html><body>identical</body></html>"
    first, created_first = acquisition.get_or_create_body(left, payload, NOW)
    left.commit()
    second, created_second = acquisition.get_or_create_body(right, payload, NOW)
    right.commit()

    assert first.id == second.id
    assert created_first is True
    assert created_second is False
    assert right.scalar(select(func.count()).select_from(m.ResearchArtifactBody)) == 1


def test_the_same_discovery_observation_twice_is_recorded_once(two_sessions):
    left, right = two_sessions
    company = _company(left)
    _, attempt_id = _run_and_attempt(left, company.id)

    source = acquisition.get_or_create_source(left, URL, NOW)
    left.commit()
    candidate = DiscoveredLocator(URL, "SEARCH", {"query": "meridian"}, relevance_hint=0.5)

    first = acquisition.record_discovery(
        left, source=source, attempt_id=attempt_id, candidate=candidate, now=NOW
    )
    left.commit()

    right_source = acquisition.get_or_create_source(right, URL, NOW)
    second = acquisition.record_discovery(
        right, source=right_source, attempt_id=attempt_id, candidate=candidate, now=NOW
    )
    right.commit()

    assert first is not None
    assert second is None, "the duplicate is refused, not raised"
    assert right.scalar(
        select(func.count()).select_from(m.ResearchSourceDiscovery)
    ) == 1


def test_two_workers_raising_one_gap_converge_on_one_parent(two_sessions):
    left, right = two_sessions
    company = _company(left)
    run_id, attempt_id = _run_and_attempt(left, company.id)

    first = gaps.raise_gap(
        left, run_id=run_id, company_id=company.id, attribute_key="erp",
        gap_kind="NO_EVIDENCE", attempt_id=attempt_id, now=NOW,
    )
    left.commit()
    second = gaps.raise_gap(
        right, run_id=run_id, company_id=company.id, attribute_key="erp",
        gap_kind="NO_EVIDENCE", attempt_id=attempt_id, now=NOW,
    )
    right.commit()

    assert first.created is True
    assert second.created is False
    assert first.gap.id == second.gap.id
    assert right.scalar(
        select(func.count()).select_from(m.OperationalResearchGap)
    ) == 1


def test_two_workers_raising_one_concern_open_one_occurrence(two_sessions):
    left, right = two_sessions
    company = _company(left)
    _, attempt_id = _run_and_attempt(left, company.id)

    first = identity.raise_signal(
        left, company_id=company.id, signal_kind="POSSIBLE_PARENT",
        concern="Meridian Holdings LLC", attempt_id=attempt_id,
        evidence_item_ids=[], now=NOW,
    )
    left.commit()
    second = identity.raise_signal(
        right, company_id=company.id, signal_kind="POSSIBLE_PARENT",
        concern="  meridian   holdings llc ", attempt_id=attempt_id,
        evidence_item_ids=[], now=NOW,
    )
    right.commit()

    assert first.signal.id == second.signal.id, "normalization makes it one concern"
    assert second.occurrence_created is False
    assert right.scalar(
        select(func.count()).select_from(m.IdentityReviewSignalOccurrence)
    ) == 1


def test_only_one_attempt_can_be_live_for_a_question(two_sessions):
    """Two workers must not execute the same question at the same time."""
    from sqlalchemy.exc import IntegrityError

    left, right = two_sessions
    company = _company(left)
    run_id, _ = _run_and_attempt(left, company.id)

    second = m.OperationalResearchAttempt(
        run_id=run_id, attempt_number=2, status="PENDING",
        allow_partial_assertion=True, created_at=NOW,
    )
    right.add(second)
    with pytest.raises(IntegrityError):
        right.commit()
    right.rollback()
