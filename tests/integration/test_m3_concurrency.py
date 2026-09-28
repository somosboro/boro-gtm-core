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
from sqlalchemy import func, select, text

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import DiscoveredLocator
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import acquisition, gaps, identity
from boro_gtm.research.services.pipeline import start_attempt

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
URL = "https://www.meridianmechanical.com/services"


@pytest.fixture
def two_sessions(committed_sessions):
    """Two real connections, over a database these tests own exclusively.

    The truncate is at setup as well as teardown: every test here asserts an
    absolute row count, so anything another file committed and did not clean up
    would read as a concurrency failure.
    """
    from tests.conftest import _ALL_TABLES

    left, right = committed_sessions(), committed_sessions()
    left.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    left.commit()
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
    """G15: the caller gets a domain error, and the index is the backstop.

    Both halves matter. The service refuses first, with a sentence naming the
    live attempt; the partial unique index still refuses a caller that goes
    around the service, and that path must not surface a raw `IntegrityError`
    as the normal outcome.
    """
    from sqlalchemy.exc import IntegrityError

    from boro_gtm.research.services.pipeline import AttemptAlreadyLiveError

    left, right = two_sessions
    company = _company(left)
    run_id, _ = _run_and_attempt(left, company.id)
    run = left.get(m.OperationalResearchRun, run_id)

    with pytest.raises(AttemptAlreadyLiveError) as raised:
        start_attempt(left, run=run, now=NOW)
    assert "still PENDING" in str(raised.value)
    left.rollback()

    # The index is the backstop for a writer that bypasses the service.
    second = m.OperationalResearchAttempt(
        run_id=run_id, attempt_number=2, status="PENDING",
        allow_partial_assertion=True, created_at=NOW,
    )
    right.add(second)
    with pytest.raises(IntegrityError):
        right.commit()
    right.rollback()


# --- extraction, claim and projection races --------------------------------


def _researched(session, company_id, *, now=NOW):
    from boro_gtm.research.fixtures.transport import FixtureTransport
    from boro_gtm.research.services.pipeline import run_pipeline

    result = run_pipeline(session, company_id=company_id,
                         transport=FixtureTransport(), now=now)
    session.commit()
    return result


def test_two_workers_extracting_one_text_derivation_produce_one_result(two_sessions):
    """G6: the deterministic contract key converges both writers."""
    from boro_gtm.research.services.extraction import PROSE_EXTRACTOR, run_extraction

    left, right = two_sessions
    company = _company(left)
    result = _researched(left, company.id)

    derivation = left.scalars(
        select(m.ResearchTextDerivation)
        .join(m.ResearchExtraction,
              m.ResearchExtraction.text_derivation_id == m.ResearchTextDerivation.id)
        .where(m.ResearchExtraction.extractor_id == PROSE_EXTRACTOR.extractor_id)
        .limit(1)
    ).first()
    before = left.scalar(
        select(func.count()).select_from(m.ResearchExtraction).where(
            m.ResearchExtraction.text_derivation_id == derivation.id,
            m.ResearchExtraction.extractor_id == PROSE_EXTRACTOR.extractor_id,
        )
    )

    first = run_extraction(
        left, extractor=PROSE_EXTRACTOR, text_derivation=derivation,
        attempt_id=result.attempt.id, now=NOW,
    )
    left.commit()
    second = run_extraction(
        right, extractor=PROSE_EXTRACTOR,
        text_derivation=right.get(m.ResearchTextDerivation, derivation.id),
        attempt_id=result.attempt.id, now=NOW,
    )
    right.commit()

    assert first.extraction.id == second.extraction.id
    assert right.scalar(
        select(func.count()).select_from(m.ResearchExtraction).where(
            m.ResearchExtraction.text_derivation_id == derivation.id,
            m.ResearchExtraction.extractor_id == PROSE_EXTRACTOR.extractor_id,
        )
    ) == before


def test_two_workers_asserting_one_claim_converge_on_one_row(two_sessions):
    """The partial unique fingerprint index, over two real connections."""
    from boro_gtm.research.services.claims import PendingAssertion, assert_claim

    left, right = two_sessions
    company = _company(left)
    _researched(left, company.id)

    evidence = left.scalars(select(m.ResearchEvidenceItem.id).limit(1)).all()
    before = left.scalar(select(func.count()).select_from(CompanyClaim))

    def pending():
        return PendingAssertion(
            attribute_key="fleet_size", value={"min": 9, "max": 9},
            unit="VEHICLES", fact_type="ESTIMATE", support_kind="DERIVED",
            evidence_item_ids=list(evidence),
        )

    first = assert_claim(left, company_id=company.id, pending=pending(), now=NOW)
    left.commit()
    second = assert_claim(right, company_id=company.id, pending=pending(), now=NOW)
    right.commit()

    assert first.claim.id == second.claim.id
    assert first.created is True
    assert second.created is False
    assert right.scalar(select(func.count()).select_from(CompanyClaim)) == before + 1


def test_two_concurrent_company_profile_rebuilds_converge(two_sessions):
    """One row, one result, and no raw IntegrityError."""
    from boro_gtm.research.services import profiles

    left, right = two_sessions
    company = _company(left)
    _researched(left, company.id)

    profiles.rebuild_company_profile(left, company.id)
    left.commit()
    first = left.get(m.OperationalResearchProfile, company.id)
    snapshot = (first.facts, first.contradictions,
                first.corroborating_publisher_counts)

    profiles.rebuild_company_profile(right, company.id)
    right.commit()

    assert right.scalar(
        select(func.count()).select_from(m.OperationalResearchProfile)
    ) == 1
    second = right.get(m.OperationalResearchProfile, company.id)
    assert (second.facts, second.contradictions,
            second.corroborating_publisher_counts) == snapshot


def test_two_concurrent_plan_profile_rebuilds_converge(two_sessions):
    from boro_gtm.research.services import profiles

    left, right = two_sessions
    company = _company(left)
    result = _researched(left, company.id)
    run_id = result.attempt.run_id

    profiles.rebuild_plan_profile(left, run_id)
    left.commit()
    first = left.get(m.OperationalResearchPlanProfile, run_id)
    snapshot = (float(first.coverage), first.required_attribute_count,
                first.covered_attribute_count, first.open_gap_count)

    profiles.rebuild_plan_profile(right, run_id)
    right.commit()

    assert right.scalar(
        select(func.count()).select_from(m.OperationalResearchPlanProfile)
    ) == 1
    second = right.get(m.OperationalResearchPlanProfile, run_id)
    assert (float(second.coverage), second.required_attribute_count,
            second.covered_attribute_count, second.open_gap_count) == snapshot


def test_a_decision_computed_from_a_stale_read_does_not_land(two_sessions):
    """Two reviewers, one occurrence: the loser is told, not silently applied.

    The stale read is made explicit rather than left to transaction timing — a
    test that depends on which snapshot a session happens to hold is a test that
    passes or fails for reasons unrelated to the invariant.

    The second reviewer decides what to do while the state is `OPEN`, the first
    reviewer commits `ACKNOWLEDGED`, and the second then tries to apply the move
    that was legal when it looked. It is refused.
    """
    from boro_gtm.research.services.review import (
        IllegalSignalTransitionError,
        append_signal_status,
        occurrence_status,
    )

    left, right = two_sessions
    company = _company(left)
    _researched(left, company.id)

    signal = left.scalars(select(m.IdentityReviewSignal)).one()
    occurrence = right.scalars(select(m.IdentityReviewSignalOccurrence)).one()

    # What the second reviewer saw, and therefore what it decided to do.
    observed = occurrence_status(right, occurrence.id)
    assert observed == "OPEN"
    intended = "ACKNOWLEDGED"          # legal from OPEN

    # Meanwhile the first reviewer acknowledges it.
    append_signal_status(left, signal_id=signal.id, occurrence_number=1,
                         status="ACKNOWLEDGED", actor="left", now=NOW)
    left.commit()

    with pytest.raises(IllegalSignalTransitionError) as raised:
        append_signal_status(right, signal_id=signal.id, occurrence_number=1,
                             status=intended, actor="right", now=NOW)
    assert "ACKNOWLEDGED" in str(raised.value)
    right.rollback()

    # Having re-read, the same reviewer can make the move that is now legal.
    assert occurrence_status(right, occurrence.id) == "ACKNOWLEDGED"
    append_signal_status(right, signal_id=signal.id, occurrence_number=1,
                         status="ACTIONED", actor="right", now=NOW)
    right.commit()
    assert occurrence_status(right, occurrence.id) == "ACTIONED"


def test_two_workers_creating_one_evidence_item_converge(two_sessions):
    """The evidence identity key, over two connections."""
    from boro_gtm.research.services.evidence import (
        EvidenceContext,
        create_evidence_item,
    )
    from boro_gtm.research.services.extraction import Observation

    left, right = two_sessions
    company = _company(left)
    _researched(left, company.id)

    item = left.scalars(select(m.ResearchEvidenceItem).limit(1)).first()
    before = left.scalar(select(func.count()).select_from(m.ResearchEvidenceItem))
    observation = Observation(
        attribute_key="installation", value={"value": True}, fact_type="FACT",
        locator={"kind": "HTML_SPAN", "start": 4242, "end": 4250, "resolved": True},
        quote="installed", support_kind="DIRECT_STATEMENT",
    )

    def context(session):
        return EvidenceContext(
            extraction_id=item.extraction_id, fetch_event_id=item.fetch_event_id,
            artifact_derivation_id=item.artifact_derivation_id,
            body_id=item.body_id,
            source=session.get(m.ResearchSource, item.source_id),
        )

    first, created_first = create_evidence_item(
        left, context=context(left), observation=observation, now=NOW
    )
    left.commit()
    second, created_second = create_evidence_item(
        right, context=context(right), observation=observation, now=NOW
    )
    right.commit()

    assert first.id == second.id
    assert created_first is True and created_second is False
    assert right.scalar(
        select(func.count()).select_from(m.ResearchEvidenceItem)
    ) == before + 1
