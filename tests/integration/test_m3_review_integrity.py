"""Review integrity: whose evidence, which observation, and what persists.

The second audit found that one review could assert four claims on a company
whose research never captured the evidence. These tests attack that, and the
rest of the review contract, rather than demonstrating the happy path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import review
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


@pytest.fixture
def m3(session):
    seed_all(session)
    session.flush()
    return session


def _company(session) -> Company:
    row = Company(created_at=NOW, identity_policy_version="1.0", lifecycle_status="ACTIVE")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def researched(m3):
    company = _company(m3)
    run_pipeline(m3, company_id=company.id, transport=FixtureTransport(), now=NOW)
    return company


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _sampled_candidate(m3):
    pending = review.pending_candidates(m3, reason=review.SAMPLED_REASON)
    assert pending, "the corpus leaves sampled readings awaiting review"
    return pending[0]


# --- §1: the company is derived, never chosen -------------------------------


def test_the_subject_company_comes_from_provenance(researched, m3):
    candidate = _sampled_candidate(m3)
    derived = review.company_of_evidence(m3, candidate.evidence_item_id)
    assert derived == researched.id

    # And the walk is the one the design specifies.
    walked = m3.scalar(
        select(m.OperationalResearchRun.company_id)
        .join(m.OperationalResearchAttempt,
              m.OperationalResearchAttempt.run_id == m.OperationalResearchRun.id)
        .join(m.ResearchFetchEvent,
              m.ResearchFetchEvent.attempt_id == m.OperationalResearchAttempt.id)
        .join(m.ResearchEvidenceItem,
              m.ResearchEvidenceItem.fetch_event_id == m.ResearchFetchEvent.id)
        .where(m.ResearchEvidenceItem.id == candidate.evidence_item_id)
    )
    assert derived == walked


def test_evidence_captured_for_one_company_cannot_be_confirmed_into_another(
    researched, m3
):
    """The defect: a caller-supplied company id landed A's evidence on B.

    There is no longer a field to supply, so the attack is made at the
    signature — a reviewer cannot express the request at all.
    """
    import inspect

    other = _company(m3)
    candidate = _sampled_candidate(m3)

    signature = inspect.signature(review.review_evidence)
    assert "company_id" not in signature.parameters, (
        "the subject company must not be an input to a review"
    )

    outcome = review.review_evidence(
        m3, evidence_item_id=candidate.evidence_item_id, decision="CONFIRM",
        actor="analyst", now=LATER,
    )
    m3.flush()
    assert outcome.company_id == researched.id
    assert outcome.company_id != other.id

    claim = m3.get(CompanyClaim, outcome.resulting_claim_id)
    assert claim.subject_company_id == researched.id

    # Nothing at all was written about the other company.
    assert m3.scalar(
        select(func.count()).select_from(CompanyClaim).where(
            CompanyClaim.subject_company_id == other.id
        )
    ) == 0


def test_the_api_contract_has_no_company_field(researched):
    from boro_gtm.research.api.schemas import EvidenceReviewIn

    assert "company_id" not in EvidenceReviewIn.model_fields


# --- §2: a review confirms one observation ----------------------------------


def test_confirming_one_observation_confirms_only_that_observation(researched, m3):
    """The defect: one review created four claims from one document.

    The sampled PDF reading holds several observations. Reviewing one must not
    imply the reviewer approved the rest.
    """
    candidate = _sampled_candidate(m3)
    item = m3.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
    sampled = m3.get(m.ResearchExtraction, item.extraction_id)
    total = len((sampled.observations or {}).get("observations", []))
    assert total >= 3, "the sampled reading must hold several observations"

    claims_before = _count(m3, CompanyClaim)
    outcome = review.review_evidence(
        m3, evidence_item_id=item.id, decision="CONFIRM", actor="analyst",
        now=LATER,
    )
    m3.flush()

    assert len(outcome.created_evidence_item_ids) == 1, (
        "one reviewed span is one confirmed observation"
    )
    assert _count(m3, CompanyClaim) - claims_before <= 1

    # The durable HUMAN extraction records exactly what was confirmed — not
    # every model finding in the document.
    human = m3.get(m.ResearchExtraction, outcome.human_extraction_id)
    recorded = (human.observations or {}).get("observations", [])
    assert len(recorded) == 1
    assert recorded[0]["attribute_key"] == candidate.attribute_key

    # And the other observations of the same document remain unreviewed.
    still_waiting = {
        c.attribute_key for c in review.pending_candidates(m3)
        if c.evidence_item_id != item.id
    }
    assert still_waiting


def test_two_observations_on_one_document_need_two_decisions(researched, m3):
    """Each confirmation is its own extraction, keyed by the reviewed span."""
    sampled = [c for c in review.pending_candidates(m3, reason=review.SAMPLED_REASON)]
    assert len(sampled) >= 2

    first = review.review_evidence(
        m3, evidence_item_id=sampled[0].evidence_item_id, decision="CONFIRM",
        actor="analyst", now=LATER,
    )
    second = review.review_evidence(
        m3, evidence_item_id=sampled[1].evidence_item_id, decision="CONFIRM",
        actor="analyst", now=LATER + timedelta(minutes=1),
    )
    m3.flush()

    assert first.human_extraction_id != second.human_extraction_id
    left = m3.get(m.ResearchExtraction, first.human_extraction_id)
    right = m3.get(m.ResearchExtraction, second.human_extraction_id)
    assert left.extraction_contract_hash != right.extraction_contract_hash


# --- §3: cardinality --------------------------------------------------------


def test_a_confirmation_produces_at_most_one_claim_and_records_it(researched, m3):
    candidate = _sampled_candidate(m3)
    outcome = review.review_evidence(
        m3, evidence_item_id=candidate.evidence_item_id, decision="CONFIRM",
        actor="analyst", now=LATER,
    )
    m3.flush()

    row = m3.get(m.ResearchEvidenceReview, outcome.review_id)
    # Every side effect is reachable from the durable record.
    assert row.human_extraction_id == outcome.human_extraction_id
    assert row.resulting_claim_id == outcome.resulting_claim_id

    produced = m3.scalars(select(m.ResearchEvidenceItem.id).where(
        m.ResearchEvidenceItem.extraction_id == row.human_extraction_id
    )).all()
    assert set(produced) == set(outcome.created_evidence_item_ids)

    linked_claims = set(m3.scalars(select(m.ClaimEvidenceLink.claim_id).where(
        m.ClaimEvidenceLink.evidence_item_id.in_(produced)
    )).all())
    assert linked_claims <= {row.resulting_claim_id}


# --- §4: what is reviewable -------------------------------------------------


def test_an_ordinary_deterministic_observation_is_not_reviewable(researched, m3):
    """"Reviewable" is a state, not knowledge of a UUID."""
    candidates = {c.evidence_item_id for c in review.pending_candidates(m3, limit=500)}
    ordinary = m3.scalars(select(m.ResearchEvidenceItem.id).where(
        m.ResearchEvidenceItem.id.not_in(candidates)
    ).limit(1)).first()
    assert ordinary is not None

    with pytest.raises(review.NotReviewableError):
        review.review_evidence(m3, evidence_item_id=ordinary, decision="CONFIRM",
                               actor="analyst", now=LATER)


def test_both_reviewable_reasons_are_distinguished(researched, m3):
    reasons = {c.reason for c in review.pending_candidates(m3, limit=500)}
    assert review.SAMPLED_REASON in reasons


# --- §5 / D3: low-confidence durability -------------------------------------


def test_a_low_confidence_observation_is_discoverable_after_the_process_ends(
    committed_sessions,
):
    """D3, literally: the reviewer arrives later and must still find it.

    Deliberately does **not** take the transactional `session` fixture: its
    open transaction blocks the TRUNCATE this test needs, and the result is a
    hang rather than a failure.
    """
    import dataclasses

    import boro_gtm.research.services.extraction as extraction_module
    from boro_gtm.research.services.extraction import PROSE_EXTRACTOR
    from tests.conftest import _ALL_TABLES

    writer = committed_sessions()
    writer.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    writer.commit()
    seed_all(writer)
    company = _company(writer)
    writer.commit()

    weak = dataclasses.replace(PROSE_EXTRACTOR, extractor_confidence=0.10)
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = tuple(
        weak if x.extractor_id == PROSE_EXTRACTOR.extractor_id else x
        for x in original
    )
    try:
        result = run_pipeline(writer, company_id=company.id,
                              transport=FixtureTransport(), now=NOW)
        writer.commit()
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original
    assert result.low_confidence_deferred

    # A completely separate session, as a reviewer tomorrow would have.
    reader = committed_sessions()
    waiting = review.pending_candidates(
        reader, reason=review.LOW_CONFIDENCE_REASON, limit=500
    )
    assert waiting, "the low-confidence candidates outlived the process"

    candidate = waiting[0]
    assert candidate.company_id == company.id
    assert candidate.attribute_key
    assert candidate.run_id == result.attempt.run_id
    assert float(candidate.extractor_confidence) == pytest.approx(0.10)
    assert reader.get(m.ResearchEvidenceItem, candidate.evidence_item_id) is not None

    # And the gap that reported it is the same attribute.
    gapped = set(reader.scalars(select(m.OperationalResearchGap.attribute_key).where(
        m.OperationalResearchGap.gap_kind == "INSUFFICIENT_EVIDENCE"
    )).all())
    assert {c.attribute_key for c in waiting} & gapped

    # Confirming it asserts only that observation.
    before = reader.scalar(select(func.count()).select_from(CompanyClaim))
    outcome = review.review_evidence(
        reader, evidence_item_id=candidate.evidence_item_id, decision="CONFIRM",
        actor="analyst", now=LATER,
    )
    reader.commit()
    assert len(outcome.created_evidence_item_ids) == 1
    after = reader.scalar(select(func.count()).select_from(CompanyClaim))
    assert after - before <= 1


# --- §6: the history API must not invent -----------------------------------


def test_review_history_round_trips_through_a_fresh_session(committed_sessions):
    """Also avoids the transactional fixture; see the note above."""
    from tests.conftest import _ALL_TABLES

    writer = committed_sessions()
    writer.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    writer.commit()
    seed_all(writer)
    company = _company(writer)
    run_pipeline(writer, company_id=company.id, transport=FixtureTransport(), now=NOW)
    writer.commit()

    candidate = review.pending_candidates(writer, reason=review.SAMPLED_REASON)[0]
    outcome = review.review_evidence(
        writer, evidence_item_id=candidate.evidence_item_id, decision="CONFIRM",
        actor="analyst", now=LATER,
    )
    writer.commit()

    reader = committed_sessions()
    rows = review.reviews_for(reader, candidate.evidence_item_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.human_extraction_id == outcome.human_extraction_id
    assert row.resulting_claim_id == outcome.resulting_claim_id
    # Reconstructed from provenance, not remembered — and not an empty list.
    reconstructed = review.evidence_created_by(reader, row)
    assert reconstructed == outcome.created_evidence_item_ids
    assert reconstructed != []


def test_a_rejection_history_row_reports_no_created_evidence(researched, m3):
    candidate = _sampled_candidate(m3)
    outcome = review.review_evidence(
        m3, evidence_item_id=candidate.evidence_item_id, decision="REJECT",
        actor="analyst", now=LATER,
    )
    m3.flush()
    row = m3.get(m.ResearchEvidenceReview, outcome.review_id)
    assert review.evidence_created_by(m3, row) == []


# --- §7: the company filter must filter -------------------------------------


def test_the_pending_queue_never_leaks_another_companys_candidates(m3):
    first = _company(m3)
    second = _company(m3)
    run_pipeline(m3, company_id=first.id, transport=FixtureTransport(), now=NOW)
    run_pipeline(m3, company_id=second.id, transport=FixtureTransport(),
                 now=NOW, conditional=False)
    m3.flush()

    for owner in (first, second):
        mine = review.pending_candidates(m3, company_id=owner.id, limit=500)
        assert mine
        assert {c.company_id for c in mine} == {owner.id}

    everything = review.pending_candidates(m3, limit=500)
    assert {c.company_id for c in everything} == {first.id, second.id}


# --- §11: a CONFIRM must carry provenance ----------------------------------


def test_the_database_forbids_a_confirmation_with_no_human_extraction(researched, m3):
    candidate = _sampled_candidate(m3)
    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "INSERT INTO research_evidence_reviews "
                "(id, evidence_item_id, decision, actor, reviewed_at) "
                "VALUES (:i, :e, 'CONFIRM', 'a', now())"
            ), {"i": uuid.uuid4(), "e": candidate.evidence_item_id})


def test_a_candidate_row_is_append_only(researched, m3):
    candidate = _sampled_candidate(m3)
    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "UPDATE research_review_candidates SET reason = "
                "'LOW_CONFIDENCE_REQUIRES_REVIEW' WHERE id = :i"
            ), {"i": candidate.id})
