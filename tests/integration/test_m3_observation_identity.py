"""Observation identity: which reading is under review, and what it costs.

The third audit found that a review candidate was keyed on the evidence item.
Two extraction rules can match one span — "58 field technicians" is both a
`technician_count` and a `field_workforce_present` — and an evidence item is
keyed on the locator, so those two readings share one. `ON CONFLICT DO NOTHING`
then silently dropped the second, and one decision closed both.

These tests attack that, plus the two ways a confirmation used to depend on the
present rather than the past: it re-ran the extractor, and it appended usage to
a research attempt that had already terminated.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import boro_gtm.research.services.extraction as extraction_module
from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.enums import TERMINAL_ATTEMPT_STATUSES
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import retention, review
from boro_gtm.research.services.extraction import (
    PROSE_EXTRACTOR,
    fingerprint_of_stored,
    observation_fingerprint,
)
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=3)


def _company(session) -> Company:
    row = Company(created_at=NOW, identity_policy_version="1.0",
                  lifecycle_status="ACTIVE")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def weak_prose(session):
    """Every prose reading becomes a durable candidate.

    The prose extractor is deterministic and confident, so its readings assert
    directly and raise nothing. Lowering its confidence is the shortest honest
    route to a queue that contains the corpus's shared-locator pairs — the
    pipeline's own low-confidence path, not a hand-built row.
    """
    seed_all(session)
    session.flush()
    company = _company(session)
    weak = dataclasses.replace(PROSE_EXTRACTOR, extractor_confidence=0.10)
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = tuple(
        weak if x.extractor_id == PROSE_EXTRACTOR.extractor_id else x
        for x in original
    )
    try:
        result = run_pipeline(session, company_id=company.id,
                              transport=FixtureTransport(), now=NOW)
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original
    session.flush()
    return company, result


def _shared_pair(session) -> list[m.ResearchReviewCandidate]:
    """Two candidates that share one evidence item, ordered by attribute."""
    by_item: dict[uuid.UUID, list] = {}
    for candidate in review.pending_candidates(session, limit=500):
        by_item.setdefault(candidate.evidence_item_id, []).append(candidate)
    shared = [v for v in by_item.values() if len(v) > 1]
    assert shared, "the corpus must contain two readings of one span"
    return sorted(shared[0], key=lambda c: c.attribute_key)


# --- §1 / §3: one candidate per observation ---------------------------------


def test_two_observations_of_one_span_are_two_candidates(weak_prose, session):
    """The reproduction. Under the old identity this queue was one short.

    `technician_count` and `field_workforce_present` are read from the same
    offset of the same document, so they share an evidence item. Both are
    questions a human must answer; keying on the item answered only one.
    """
    pair = _shared_pair(session)
    assert len(pair) == 2
    assert pair[0].evidence_item_id == pair[1].evidence_item_id
    assert pair[0].attribute_key != pair[1].attribute_key
    assert pair[0].observation_fingerprint != pair[1].observation_fingerprint
    # The old key was (evidence_item_id, run_id). These two rows agree on both,
    # so they provably collided under it and the second was discarded.
    assert pair[0].run_id == pair[1].run_id

    # Every candidate in the queue is a distinct observation, and the
    # fingerprint it names is the identity of a reading actually on disk.
    everything = review.pending_candidates(session, limit=500)
    fingerprints = [c.observation_fingerprint for c in everything]
    assert len(set(fingerprints)) == len(fingerprints)
    for candidate in everything:
        stored = review.stored_observation(session, candidate)
        assert fingerprint_of_stored(stored) == candidate.observation_fingerprint


def test_the_database_keys_a_candidate_on_the_observation(weak_prose, session):
    """The old constraint is gone, not merely unused."""
    constraints = set(session.scalars(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = "
        "'research_review_candidates'::regclass AND contype = 'u'"
    )).all())
    assert "uq_candidate_observation" in constraints
    assert "uq_candidate_identity" not in constraints

    columns = set(session.scalars(text(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "AND a.attnum = ANY(c.conkey) "
        "WHERE c.conname = 'uq_candidate_observation'"
    )).all())
    assert columns == {"run_id", "observation_fingerprint"}

    # And re-raising the same observation is still idempotent.
    candidate = _shared_pair(session)[0]
    again = review.raise_candidate(
        session, evidence_item_id=candidate.evidence_item_id,
        run_id=candidate.run_id, attempt_id=candidate.attempt_id,
        company_id=candidate.company_id, attribute_key=candidate.attribute_key,
        observation_fingerprint=candidate.observation_fingerprint,
        reason=candidate.reason, now=NOW,
    )
    assert again is False

    # A forged row with a fresh id and the same observation is refused by the
    # database, not merely by the service's ON CONFLICT clause.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(m.ResearchReviewCandidate(
                id=uuid.uuid4(), evidence_item_id=candidate.evidence_item_id,
                run_id=candidate.run_id, attempt_id=candidate.attempt_id,
                company_id=candidate.company_id,
                attribute_key=candidate.attribute_key,
                observation_fingerprint=candidate.observation_fingerprint,
                reason=candidate.reason, raised_at=NOW,
            ))
            session.flush()

    # And the fingerprint itself cannot be rewritten afterwards.
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(text(
                "UPDATE research_review_candidates SET observation_fingerprint "
                "= repeat('f', 64) WHERE id = :i"
            ), {"i": candidate.id})


# --- §6: a decision closes one question -------------------------------------


def test_deciding_one_observation_leaves_its_sibling_pending(weak_prose, session):
    """The defect: pending excluded by evidence item, so a sibling vanished."""
    first, second = _shared_pair(session)
    review.review_candidate(session, candidate_id=first.id, decision="CONFIRM",
                            actor="analyst", now=LATER)
    session.flush()

    waiting = {c.id for c in review.pending_candidates(session, limit=500)}
    assert first.id not in waiting, "the decided observation left the queue"
    assert second.id in waiting, (
        "deciding one reading of a span must not answer the other"
    )

    # The sibling is still answerable, and its answer is its own record.
    outcome = review.review_candidate(
        session, candidate_id=second.id, decision="CONFIRM", actor="analyst",
        now=LATER + timedelta(minutes=1),
    )
    session.flush()
    assert outcome.attribute_key == second.attribute_key
    assert second.id not in {
        c.id for c in review.pending_candidates(session, limit=500)
    }


# --- §7: a rejection names the reading it disbelieves ------------------------


def test_a_rejection_is_specific_to_one_observation(weak_prose, session):
    """Rejecting one reading of a span must not reject the other.

    A reviewer who disbelieves the number "58" has not said the company employs
    no field technicians, and the record must be able to tell those apart.
    """
    first, second = _shared_pair(session)
    claims_before = session.scalar(select(func.count()).select_from(CompanyClaim))

    rejected = review.review_candidate(
        session, candidate_id=first.id, decision="REJECT", actor="analyst",
        note="the figure is a projection, not a headcount", now=LATER,
    )
    session.flush()

    assert rejected.review_candidate_id == first.id
    assert rejected.attribute_key == first.attribute_key
    assert rejected.resulting_claim_id is None
    assert rejected.human_extraction_id is None
    # Disbelief is not evidence of the opposite.
    assert session.scalar(
        select(func.count()).select_from(CompanyClaim)
    ) == claims_before

    # The record says which reading was rejected, and the sibling is untouched.
    assert [r.decision for r in review.reviews_for(session, first.id)] == ["REJECT"]
    assert review.reviews_for(session, second.id) == []
    assert second.id in {c.id for c in review.pending_candidates(session, limit=500)}


# --- §8 / §9: the reviewed reading is historical ----------------------------


def test_a_confirmation_survives_the_extractor_being_replaced(weak_prose, session):
    """The defect: confirmation re-ran the extractor and hoped it agreed.

    A durable candidate raised today may be reviewed after the extractor has
    been upgraded, renamed or withdrawn. The human is confirming what the
    machine said then, which is on disk, not asking today's code what it says
    now.
    """
    candidate = _shared_pair(session)[0]
    stored = review.stored_observation(session, candidate)

    # The extractor that produced it no longer exists at all.
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = tuple(
        x for x in original if x.extractor_id != PROSE_EXTRACTOR.extractor_id
    )
    try:
        outcome = review.review_candidate(
            session, candidate_id=candidate.id, decision="CONFIRM",
            actor="analyst", now=LATER,
        )
        session.flush()
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original

    human = session.get(m.ResearchExtraction, outcome.human_extraction_id)
    assert human.extractor_kind == "HUMAN"
    recorded = (human.observations or {}).get("observations", [])
    assert len(recorded) == 1, "one reviewed span is one confirmed observation"
    assert fingerprint_of_stored(recorded[0]) == candidate.observation_fingerprint
    assert recorded[0]["attribute_key"] == stored["attribute_key"]
    assert recorded[0]["value"] == stored["value"]
    assert outcome.model_extraction_unchanged


def test_a_confirmation_survives_a_pruned_body_and_text(weak_prose, session):
    """Retention must not make a durable review candidate unanswerable.

    `research_extractions.observations` is not prunable — only `raw_output` is —
    so the reading the candidate names outlives the bytes it came from.
    """
    candidate = _shared_pair(session)[0]
    item = session.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
    extraction = session.get(m.ResearchExtraction, item.extraction_id)

    plan = retention.RetentionPlan(
        bodies=[extraction.body_id],
        text_derivations=[extraction.text_derivation_id],
    )
    retention.apply_retention(session, plan, as_of=NOW + timedelta(days=1))

    with pytest.raises(retention.PrunedPayloadError):
        retention.require_text(session, extraction.text_derivation_id)
    with pytest.raises(retention.PrunedPayloadError):
        retention.require_body(session, extraction.body_id)

    # Worst case together: nothing to re-read, and no extractor to re-read it.
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = ()
    try:
        outcome = review.review_candidate(
            session, candidate_id=candidate.id, decision="CONFIRM",
            actor="analyst", now=LATER,
        )
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original
    session.flush()
    assert outcome.resulting_claim_id is not None
    human = session.get(m.ResearchExtraction, outcome.human_extraction_id)
    recorded = (human.observations or {}).get("observations", [])
    assert fingerprint_of_stored(recorded[0]) == candidate.observation_fingerprint


def test_a_candidate_naming_an_absent_observation_is_not_reviewable(
    weak_prose, session
):
    """The queue and the ledger must agree, and say so when they do not."""
    candidate = _shared_pair(session)[0]
    forged = m.ResearchReviewCandidate(
        id=uuid.uuid4(), evidence_item_id=candidate.evidence_item_id,
        run_id=candidate.run_id, attempt_id=candidate.attempt_id,
        company_id=candidate.company_id, attribute_key=candidate.attribute_key,
        observation_fingerprint="0" * 64, reason=candidate.reason,
        raised_at=NOW,
    )
    session.add(forged)
    session.flush()

    with pytest.raises(review.NotReviewableError):
        review.review_candidate(session, candidate_id=forged.id,
                                decision="CONFIRM", actor="analyst", now=LATER)


# --- §11 / §12: the historical attempt is closed ----------------------------


def test_a_review_does_not_append_usage_to_the_terminal_attempt(
    weak_prose, session
):
    """The defect: a review three days later mutated a finished attempt.

    A terminal attempt is a closed record of one execution. A human reading is
    not part of that execution — it happened later, by someone else, and
    nothing ran — so it is not usage of it (M3-ADR-065).
    """
    candidate = _shared_pair(session)[0]
    attempt = session.get(m.OperationalResearchAttempt, candidate.attempt_id)
    assert attempt.status in TERMINAL_ATTEMPT_STATUSES, (
        "the attempt under test must already have terminated"
    )

    def usage() -> set[uuid.UUID]:
        return set(session.scalars(select(m.ResearchAttemptExtraction.extraction_id)
                   .where(m.ResearchAttemptExtraction.attempt_id == attempt.id)).all())

    before = usage()
    completed_at = attempt.completed_at

    outcome = review.review_candidate(
        session, candidate_id=candidate.id, decision="CONFIRM", actor="analyst",
        now=LATER,
    )
    session.flush()

    assert usage() == before, (
        "a human review must not be recorded as usage of a finished attempt"
    )
    assert outcome.human_extraction_id not in before
    assert session.scalar(
        select(func.count()).select_from(m.ResearchAttemptExtraction).where(
            m.ResearchAttemptExtraction.extraction_id == outcome.human_extraction_id
        )
    ) == 0, "the HUMAN extraction belongs to no attempt at all"

    session.refresh(attempt)
    assert attempt.completed_at == completed_at
    assert attempt.status in TERMINAL_ATTEMPT_STATUSES

    # The lineage is still reachable — through the review, not the attempt.
    row = session.get(m.ResearchEvidenceReview, outcome.review_id)
    assert row.human_extraction_id == outcome.human_extraction_id
    assert row.review_candidate_id == candidate.id


def test_the_terminal_attempt_row_cannot_be_rewritten_at_all(weak_prose, session):
    """Belt and braces: the table itself refuses."""
    candidate = _shared_pair(session)[0]
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(text(
                "UPDATE operational_research_attempts SET status = 'OK' "
                "WHERE id = :i"
            ), {"i": candidate.attempt_id})


# --- §13: history is reconstructed from the relationship --------------------


def test_history_distinguishes_two_readings_of_one_span(weak_prose, session):
    """Two decisions about one document, told apart by observation."""
    first, second = _shared_pair(session)
    confirmed = review.review_candidate(
        session, candidate_id=first.id, decision="CONFIRM", actor="analyst",
        now=LATER,
    )
    rejected = review.review_candidate(
        session, candidate_id=second.id, decision="REJECT", actor="reviewer",
        note="not what the sentence says", now=LATER + timedelta(minutes=1),
    )
    session.flush()

    left = review.reviews_for(session, first.id)
    right = review.reviews_for(session, second.id)
    assert [r.id for r in left] == [confirmed.review_id]
    assert [r.id for r in right] == [rejected.review_id]
    assert review.evidence_created_by(session, left[0]) == \
        confirmed.created_evidence_item_ids
    assert review.evidence_created_by(session, right[0]) == []


def test_several_reviewers_may_disagree_and_all_opinions_survive(
    weak_prose, session
):
    """Explicit semantics, not an accident (M3-ADR-065).

    The queue is per candidate and closes on the first decision. Later reviewers
    can still record an opinion — the log is append-only and a disagreement is
    information — but the queue does not reopen and nothing adjudicates. The
    first decision is operative; the rest are recorded dissent.
    """
    candidate = _shared_pair(session)[0]
    review.review_candidate(session, candidate_id=candidate.id,
                            decision="CONFIRM", actor="first", now=LATER)
    session.flush()
    assert candidate.id not in {
        c.id for c in review.pending_candidates(session, limit=500)
    }

    review.review_candidate(session, candidate_id=candidate.id,
                            decision="REJECT", actor="second",
                            now=LATER + timedelta(hours=1))
    session.flush()

    history = review.reviews_for(session, candidate.id)
    assert [(r.actor, r.decision) for r in history] == [
        ("first", "CONFIRM"), ("second", "REJECT")
    ]
    # Still closed: a second opinion does not reopen the question.
    assert candidate.id not in {
        c.id for c in review.pending_candidates(session, limit=500)
    }


# --- §15: the database refuses an unrelatable review ------------------------


def test_the_database_refuses_a_review_of_no_candidate(weak_prose, session):
    """A review row that names nothing reviewable must not be storable."""
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text(
                "INSERT INTO research_evidence_reviews "
                "(id, review_candidate_id, decision, actor, reviewed_at) "
                "VALUES (:i, :c, 'REJECT', 'a', now())"
            ), {"i": uuid.uuid4(), "c": uuid.uuid4()})

    assert session.scalar(text(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = "
        "'research_evidence_reviews' AND column_name = 'evidence_item_id'"
    )) == 0, "the ambiguous key is gone from the table, not merely unused"


def test_a_candidates_company_always_matches_its_provenance(weak_prose, session):
    """Every queued row agrees with the walk that derives its subject."""
    for candidate in review.pending_candidates(session, limit=500):
        derived = review.company_of_evidence(session, candidate.evidence_item_id)
        assert derived == candidate.company_id


def test_the_fingerprint_is_a_function_of_the_reading_alone(weak_prose, session):
    """Identity must not depend on when or by whom it was computed."""
    candidate = _shared_pair(session)[0]
    stored = review.stored_observation(session, candidate)
    rebuilt = extraction_module.observation_from_stored(stored)
    assert observation_fingerprint(rebuilt) == candidate.observation_fingerprint
    assert observation_fingerprint(rebuilt) == fingerprint_of_stored(stored)
