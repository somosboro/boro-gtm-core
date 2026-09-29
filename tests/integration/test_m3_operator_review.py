"""The review workflow as an operator actually uses it.

M3 exists so BoRo can research a target account, see what is weak or unknown,
decide the uncertain readings by hand, and hand clean operational evidence to
the next milestone. These tests are written from that seat: after a decision, is
the account view *true*?

They cover the four things the previous phase left wrong: the same reading on two
sources collapsed into one question; every decision acted, so a second reviewer
could re-confirm what a first had rejected; a confirmation created the claim and
left the gap open and both projections stale; and a gap closed by a person was
recorded as the work of a machine attempt.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import boro_gtm.research.services.extraction as extraction_module
from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import gaps, profiles, review
from boro_gtm.research.services.extraction import PROSE_EXTRACTOR
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=2)


def _company(session) -> Company:
    row = Company(created_at=NOW, identity_policy_version="1.0",
                  lifecycle_status="ACTIVE")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def researched(session):
    """One target company researched, with the prose reader made unconfident.

    The pipeline's own low-confidence path is what puts an attribute in the
    review queue and raises the matching `INSUFFICIENT_EVIDENCE` gap, so this is
    the shortest route to the state an operator actually encounters.
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


def _gapped_candidate(session, company):
    """A queued observation whose attribute also has an open evidence gap.

    That pairing is the interesting one: it is where the operator's screen used
    to contradict itself after a confirmation.
    """
    open_gaps = {
        g.attribute_key: g
        for g in session.scalars(select(m.OperationalResearchGap).where(
            m.OperationalResearchGap.company_id == company.id,
            m.OperationalResearchGap.gap_kind == "INSUFFICIENT_EVIDENCE",
        )).all()
        if gaps.current_status(session, g.id) not in gaps.TERMINAL_GAP_KINDS
    }
    assert open_gaps, "low confidence must raise an INSUFFICIENT_EVIDENCE gap"
    for candidate in review.pending_candidates(session, limit=500):
        gap = open_gaps.get(candidate.attribute_key)
        if gap is not None:
            return candidate, gap
    pytest.fail("no queued observation matched an open evidence gap")


# --- §1 / A: the same reading on two sources is two questions ----------------


def test_one_reading_on_two_sources_is_two_review_questions(researched, session):
    """The defect: `(run, fingerprint)` merged independent publishers.

    The corpus mirrors the company page across three locators, one of them on a
    different domain entirely. Those are three publishers. A reviewer who does
    not believe the copy on `meridian-mechanical.net` has said nothing about the
    company's own site, and the queue has to be able to represent that.
    """
    company, _ = researched
    by_fingerprint: dict[str, list] = {}
    for candidate in review.pending_candidates(session, limit=500):
        by_fingerprint.setdefault(candidate.observation_fingerprint, []).append(candidate)
    shared = [v for v in by_fingerprint.values() if len(v) > 1]
    assert shared, "the corpus must publish one reading on several sources"

    group = sorted(shared[0], key=lambda c: str(c.evidence_item_id))
    assert len({c.evidence_item_id for c in group}) == len(group)
    assert len({c.attribute_key for c in group}) == 1

    # Genuinely different sources, not one source read twice.
    sources = set()
    runs = set()
    for candidate in group:
        item = session.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
        sources.add(session.get(m.ResearchSource, item.source_id).normalized_locator)
        runs.add(review.provenance_of_evidence(session, item.id).run_id)
    assert len(sources) > 1, sources
    assert len(runs) == 1, "same run — which is exactly what the old key merged"

    # Deciding one leaves the others open.
    first = group[0]
    review.review_candidate(session, candidate_id=first.id, decision="REJECT",
                            actor="analyst", note="mirror of unknown provenance",
                            now=LATER)
    session.flush()
    waiting = {c.id for c in review.pending_candidates(session, limit=500)}
    assert first.id not in waiting
    for other in group[1:]:
        assert other.id in waiting, (
            "rejecting one publisher's copy must not reject another's"
        )


def test_the_database_keys_a_candidate_on_the_occurrence(researched, session):
    columns = set(session.scalars(text(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "AND a.attnum = ANY(c.conkey) "
        "WHERE c.conname = 'uq_candidate_occurrence'"
    )).all())
    assert columns == {"evidence_item_id", "observation_fingerprint"}

    # The same occurrence raised again is still one candidate.
    candidate = review.pending_candidates(session, limit=1)[0]
    assert review.raise_candidate(
        session, evidence_item_id=candidate.evidence_item_id,
        attribute_key=candidate.attribute_key,
        observation_fingerprint=candidate.observation_fingerprint,
        reason=candidate.reason, now=NOW,
    ) is False


# --- §2: the fingerprint covers what makes a reading distinct ---------------


def test_the_lineage_tag_is_part_of_observation_identity(researched, session):
    """It decides how the observation groups into a claim, so it is identity.

    Two readings of one span that differ only in the tag assert *different*
    claims — one joins a cross-origin lineage, the other groups by its own
    origin — so they are two review questions, not one.
    """
    from boro_gtm.research.services.extraction import (
        Observation,
        observation_fingerprint,
    )

    base = Observation(
        attribute_key="technician_count", value={"min": 58, "max": 58},
        fact_type="ESTIMATE", locator={"kind": "TEXT_SPAN", "start": 0, "end": 20},
        quote="58 field technicians", unit="PEOPLE",
    )
    tagged = dataclasses.replace(base, lineage_tag="workforce-inference")
    assert observation_fingerprint(base) != observation_fingerprint(tagged)
    assert observation_fingerprint(tagged) == observation_fingerprint(
        dataclasses.replace(base, lineage_tag="workforce-inference")
    )


# --- §3 / C / D: the first decision is the one that acts --------------------


def test_a_later_confirmation_after_a_rejection_is_dissent_only(researched, session):
    """The defect: the second reviewer's CONFIRM ran the whole confirmation.

    It appended another HUMAN extraction, another evidence item and another
    claim — reversing a colleague's decision silently, without anything saying
    so.
    """
    candidate = review.pending_candidates(session, limit=1)[0]
    claims_before = session.scalar(select(func.count()).select_from(CompanyClaim))
    extractions_before = session.scalar(
        select(func.count()).select_from(m.ResearchExtraction))

    first = review.review_candidate(session, candidate_id=candidate.id,
                                    decision="REJECT", actor="first", now=LATER)
    session.flush()
    assert first.is_operative is True

    second = review.review_candidate(
        session, candidate_id=candidate.id, decision="CONFIRM", actor="second",
        note="I read it differently", now=LATER + timedelta(hours=1),
    )
    session.flush()

    assert second.is_operative is False
    assert second.human_extraction_id is None
    assert second.resulting_claim_id is None
    assert second.created_evidence_item_ids == []
    assert second.resolved_gap_id is None
    assert second.profiles_rebuilt is False
    assert session.scalar(
        select(func.count()).select_from(CompanyClaim)) == claims_before
    assert session.scalar(
        select(func.count()).select_from(m.ResearchExtraction)) == extractions_before

    # Both opinions survive; exactly one of them acted.
    history = review.reviews_for(session, candidate.id)
    assert [(r.actor, r.decision, r.is_operative) for r in history] == [
        ("first", "REJECT", True), ("second", "CONFIRM", False)
    ]


def test_a_later_rejection_does_not_withdraw_a_confirmed_claim(researched, session):
    """The operative CONFIRM stands. Dissent is recorded, not applied."""
    company, _ = researched
    candidate, _ = _gapped_candidate(session, company)

    confirmed = review.review_candidate(session, candidate_id=candidate.id,
                                        decision="CONFIRM", actor="first", now=LATER)
    session.flush()
    assert confirmed.is_operative and confirmed.resulting_claim_id

    dissent = review.review_candidate(
        session, candidate_id=candidate.id, decision="REJECT", actor="second",
        note="not convinced", now=LATER + timedelta(hours=2),
    )
    session.flush()

    assert dissent.is_operative is False
    assert session.get(CompanyClaim, confirmed.resulting_claim_id) is not None
    assert gaps.current_status(session, confirmed.resolved_gap_id) == "RESOLVED"


def test_the_database_permits_only_one_operative_review(researched, session):
    """`uq_review_operative`, attacked directly rather than through the service."""
    candidate = review.pending_candidates(session, limit=1)[0]
    review.review_candidate(session, candidate_id=candidate.id, decision="REJECT",
                            actor="first", now=LATER)
    session.flush()
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text(
                "INSERT INTO research_evidence_reviews "
                "(id, review_candidate_id, decision, is_operative, actor, "
                " reviewed_at) VALUES (:i, :c, 'REJECT', true, 'forged', now())"
            ), {"i": uuid.uuid4(), "c": candidate.id})


def test_the_database_forbids_a_dissent_that_asserts(researched, session):
    """A non-operative row may not carry a claim or a HUMAN extraction."""
    company, _ = researched
    candidate, _ = _gapped_candidate(session, company)
    operative = review.review_candidate(session, candidate_id=candidate.id,
                                        decision="CONFIRM", actor="first", now=LATER)
    session.flush()
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text(
                "INSERT INTO research_evidence_reviews "
                "(id, review_candidate_id, decision, is_operative, actor, "
                " resulting_claim_id, reviewed_at) "
                "VALUES (:i, :c, 'CONFIRM', false, 'forged', :k, now())"
            ), {"i": uuid.uuid4(), "c": candidate.id,
                "k": operative.resulting_claim_id})


# --- §5 / F / G: the account view is true immediately after a decision -------


def test_an_operative_confirmation_reconciles_the_whole_account(researched, session):
    """The operator's screen must not contradict itself.

    Before this, a confirmation created the claim and left three things saying
    the opposite: the gap was still open, the company profile still had nothing
    for the attribute, and plan coverage still counted it as uncovered.
    """
    company, result = researched
    candidate, gap = _gapped_candidate(session, company)
    attribute = candidate.attribute_key

    before_profile = session.get(m.OperationalResearchProfile, company.id)
    before_facts = set((before_profile.facts or {}).keys())
    before_plan = session.get(m.OperationalResearchPlanProfile, result.attempt.run_id)
    before_open = before_plan.open_gap_count
    before_covered = before_plan.covered_attribute_count
    assert gaps.current_status(session, gap.id) not in gaps.TERMINAL_GAP_KINDS

    outcome = review.review_candidate(session, candidate_id=candidate.id,
                                      decision="CONFIRM", actor="operator",
                                      now=LATER)
    session.flush()

    # 1. the claim exists and belongs to this company
    claim = session.get(CompanyClaim, outcome.resulting_claim_id)
    assert claim is not None
    assert claim.subject_company_id == company.id
    assert claim.attribute_key == attribute

    # 2 + 3. the gap is closed, by a RESOLVED event naming the review
    assert outcome.resolved_gap_id == gap.id
    assert gaps.current_status(session, gap.id) == "RESOLVED"
    event = session.scalars(select(m.OperationalResearchGapEvent).where(
        m.OperationalResearchGapEvent.gap_id == gap.id,
        m.OperationalResearchGapEvent.event_kind == "RESOLVED",
    )).one()
    assert event.resolved_by_review_id == outcome.review_id
    assert event.resolved_by_claim_id == claim.id
    assert event.attempt_id is None, (
        "a human closed this gap; naming the machine attempt would be a lie"
    )
    assert review.gap_resolved_by(session, session.get(
        m.ResearchEvidenceReview, outcome.review_id)) == gap.id

    # 4. the company profile now knows the attribute
    assert outcome.profiles_rebuilt is True
    session.refresh(session.get(m.OperationalResearchProfile, company.id))
    after_profile = session.get(m.OperationalResearchProfile, company.id)
    assert attribute in (after_profile.facts or {}), (
        f"{attribute} was confirmed and the profile still does not carry it"
    )
    assert set((after_profile.facts or {}).keys()) >= before_facts

    # 5. plan coverage reflects it
    after_plan = session.get(m.OperationalResearchPlanProfile, result.attempt.run_id)
    assert after_plan.open_gap_count < before_open
    assert after_plan.covered_attribute_count >= before_covered

    # And the projections are still pure functions of the ledger.
    rebuilt = profiles.rebuild_all(session, company_id=company.id,
                                   run_id=result.attempt.run_id)
    assert rebuilt[1].open_gap_count == after_plan.open_gap_count
    assert rebuilt[1].covered_attribute_count == after_plan.covered_attribute_count


# --- §6 / H: a rejection does not invent knowledge --------------------------


def test_a_rejection_leaves_the_gap_open_and_claims_nothing(researched, session):
    """"I don't accept this evidence" is not "the company lacks this".

    So the attribute stays unknown, the gap stays open, and the profile does not
    pretend otherwise. That is the whole point of the gap taxonomy.
    """
    company, result = researched
    candidate, gap = _gapped_candidate(session, company)
    attribute = candidate.attribute_key
    claims_before = session.scalar(select(func.count()).select_from(CompanyClaim))
    plan_before = session.get(m.OperationalResearchPlanProfile, result.attempt.run_id)
    open_before = plan_before.open_gap_count

    outcome = review.review_candidate(
        session, candidate_id=candidate.id, decision="REJECT", actor="operator",
        note="the sentence is marketing, not a headcount", now=LATER,
    )
    session.flush()

    assert outcome.is_operative is True
    assert outcome.resulting_claim_id is None
    assert outcome.resolved_gap_id is None
    assert outcome.profiles_rebuilt is False
    assert session.scalar(
        select(func.count()).select_from(CompanyClaim)) == claims_before
    assert gaps.current_status(session, gap.id) not in gaps.TERMINAL_GAP_KINDS

    # Nothing negative was asserted about the attribute either.
    negatives = session.scalars(select(CompanyClaim).where(
        CompanyClaim.subject_company_id == company.id,
        CompanyClaim.attribute_key == attribute,
    )).all()
    assert negatives == []

    profile = session.get(m.OperationalResearchProfile, company.id)
    assert attribute not in (profile.facts or {})
    assert session.get(
        m.OperationalResearchPlanProfile, result.attempt.run_id
    ).open_gap_count == open_before

    # The decision itself is durable, which is what makes it a *record* of a
    # rejection rather than silence.
    row = session.get(m.ResearchEvidenceReview, outcome.review_id)
    assert row.decision == "REJECT"
    assert row.note == "the sentence is marketing, not a headcount"
    assert row.is_operative is True


def test_confirming_every_mirror_of_one_reading_does_not_inflate_confidence(
    researched, session
):
    """Making the queue larger must not make the evidence look stronger.

    Splitting one reading into three reviewable occurrences lets an operator
    confirm the same sentence three times. Each confirmation is its own lineage,
    so three claims exist — but corroboration counts **publishers matched to
    documents**, so it must track the number of distinct documents behind those
    claims, never the number of confirmations. If it did not, the fix for the
    candidate collapse would have bought a worse problem: fabricated agreement.

    In the corpus a three-occurrence group spans **two** documents:
    `meridianmechanical.com/company` and `www.meridianmechanical.com/` are one
    body copied to two addresses, and `meridian-mechanical.net/about` is a
    genuinely different document. Two is therefore the correct answer, and three
    is the wrong one.
    """
    company, _ = researched

    def body_of(candidate) -> str:
        item = session.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
        return session.get(m.ResearchArtifactBody, item.body_id).raw_body_sha256

    # Deterministic selection. Ordering by `(raised_at, id)` puts every row at
    # one `raised_at`, so the tiebreak is a random UUID; an earlier version took
    # "the first group of three" and silently tested a different attribute each
    # run, which is how it asserted a wrong constant and still passed most of
    # the time.
    groups: dict[str, list] = {}
    for candidate in review.pending_candidates(session, limit=500):
        groups.setdefault(candidate.observation_fingerprint, []).append(candidate)
    shared = sorted(
        (sorted(v, key=lambda c: str(c.evidence_item_id)) for v in groups.values()
         if len(v) >= 3),
        key=lambda v: (v[0].attribute_key, str(v[0].evidence_item_id)),
    )
    assert shared, "the corpus must publish one reading at several addresses"
    group = shared[0]
    attribute = group[0].attribute_key
    documents = {body_of(c) for c in group}
    assert len(documents) < len(group), (
        "this group must contain at least one mirrored address, or it tests nothing"
    )

    claim_ids = set()
    for offset, candidate in enumerate(group):
        outcome = review.review_candidate(
            session, candidate_id=candidate.id, decision="CONFIRM",
            actor="operator", now=LATER + timedelta(minutes=offset),
        )
        session.flush()
        assert outcome.is_operative
        claim_ids.add(outcome.resulting_claim_id)

    # One claim per lineage — the occurrences are independent origins.
    assert len(claim_ids) == len(group)

    profile = session.get(m.OperationalResearchProfile, company.id)
    corroboration = (profile.corroborating_publisher_counts or {}).get(attribute)
    assert corroboration is not None
    assert corroboration < len(group), (
        f"{len(group)} confirmations of one reading across {len(documents)} "
        f"documents reported {corroboration} corroborating publishers"
    )
    assert corroboration <= len(documents), (
        "corroboration cannot exceed the number of distinct documents"
    )
    fact = (profile.facts or {})[attribute]
    assert set(fact["claim_ids"]) >= {str(i) for i in claim_ids}
    assert fact["best_claim_id"]


# --- §7 / I: a queued row cannot name the wrong account ---------------------


def test_a_candidate_cannot_claim_another_companys_provenance(researched, session):
    """The attack: write a candidate whose evidence is A but whose company is B.

    It is not rejected — it is **unrepresentable**. `company_id`, `run_id` and
    `attempt_id` all followed from `evidence_item_id` through single-valued
    foreign keys, so storing them was three chances for a queued row to disagree
    with the evidence behind it. They are derived now (M3-ADR-067).
    """
    company, _ = researched
    other = _company(session)
    candidate = review.pending_candidates(session, limit=1)[0]

    for column in ("company_id", "run_id", "attempt_id"):
        with pytest.raises(Exception) as caught:            # noqa: PT011
            with session.begin_nested():
                session.execute(text(
                    f"INSERT INTO research_review_candidates "
                    f"(id, evidence_item_id, attribute_key, "
                    f" observation_fingerprint, reason, raised_at, {column}) "
                    f"VALUES (:i, :e, 'erp', :f, "
                    f"'LOW_CONFIDENCE_REQUIRES_REVIEW', now(), :v)"
                ), {"i": uuid.uuid4(), "e": candidate.evidence_item_id,
                    "f": "b" * 64, "v": other.id})
        assert "column" in str(caught.value).lower()

    # And the operator's own filter cannot show the other account anything,
    # because it walks the evidence rather than trusting a stored column.
    assert review.pending_candidates(session, company_id=other.id, limit=500) == []
    mine = review.pending_candidates(session, company_id=company.id, limit=500)
    assert mine
    for row in mine:
        assert review.provenance_of_evidence(
            session, row.evidence_item_id
        ).company_id == company.id


def test_a_gap_event_must_name_exactly_one_actor(researched, session):
    """Neither zero actors nor two: the ledger tells one story per event."""
    company, _ = researched
    candidate, gap = _gapped_candidate(session, company)
    outcome = review.review_candidate(session, candidate_id=candidate.id,
                                      decision="CONFIRM", actor="operator",
                                      now=LATER)
    session.flush()
    other_gap = session.scalars(select(m.OperationalResearchGap).where(
        m.OperationalResearchGap.id != gap.id,
        m.OperationalResearchGap.company_id == company.id,
    )).first()
    assert other_gap is not None

    provenance = review.provenance_of_evidence(session, candidate.evidence_item_id)
    for attempt_id, review_id in ((None, None),
                                  (provenance.attempt_id, outcome.review_id)):
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.execute(text(
                    "INSERT INTO operational_research_gap_events "
                    "(id, gap_id, event_kind, attempt_id, resolved_by_review_id, "
                    " resolved_by_claim_id, occurred_at) "
                    "VALUES (:i, :g, 'RESOLVED', :a, :r, :k, now())"
                ), {"i": uuid.uuid4(), "g": other_gap.id, "a": attempt_id,
                    "r": review_id, "k": outcome.resulting_claim_id})

    # And a human actor is only legal on a resolution.
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(text(
                "INSERT INTO operational_research_gap_events "
                "(id, gap_id, event_kind, resolved_by_review_id, occurred_at) "
                "VALUES (:i, :g, 'ATTEMPTED', :r, now())"
            ), {"i": uuid.uuid4(), "g": other_gap.id, "r": outcome.review_id})


# --- §9: the operator's round trip, end to end ------------------------------


def test_the_operator_round_trip(researched, session):
    """Company → research → see what is weak → decide → account state is true.

    One test, in the order a person does it, because the individual guarantees
    above are only worth anything if they hold together in sequence.
    """
    company, result = researched

    # The operator opens the queue for this account and sees only this account.
    queue = review.pending_candidates(session, company_id=company.id, limit=500)
    assert queue
    for candidate in queue:
        assert review.provenance_of_evidence(
            session, candidate.evidence_item_id
        ).company_id == company.id

    # They can see why each item is waiting and how weak it was.
    reasons = {c.reason for c in queue}
    assert reasons <= {review.SAMPLED_REASON, review.LOW_CONFIDENCE_REASON}
    assert any(c.extractor_confidence is not None for c in queue)

    # They open one, and can read the source, the quote and the span without
    # touching an internal table.
    candidate, gap = _gapped_candidate(session, company)
    item = session.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
    assert session.get(m.ResearchSource, item.source_id).normalized_locator
    assert item.locator
    stored = review.stored_observation(session, candidate)
    assert stored["attribute_key"] == candidate.attribute_key
    assert stored["quote"]

    # They confirm it.
    outcome = review.review_candidate(session, candidate_id=candidate.id,
                                      decision="CONFIRM", actor="operator",
                                      now=LATER)
    session.flush()

    # The account is immediately correct, and every step is inspectable.
    assert outcome.is_operative
    assert outcome.resulting_claim_id and outcome.resolved_gap_id == gap.id
    assert gaps.current_status(session, gap.id) == "RESOLVED"
    profile = session.get(m.OperationalResearchProfile, company.id)
    assert candidate.attribute_key in (profile.facts or {})

    # The claim is supported by HUMAN evidence whose provenance walks back to
    # this company's own research, and the machine reading is untouched.
    links = session.scalars(select(m.ClaimEvidenceLink.evidence_item_id).where(
        m.ClaimEvidenceLink.claim_id == outcome.resulting_claim_id)).all()
    assert set(outcome.created_evidence_item_ids) <= set(links)
    for evidence_id in links:
        assert review.provenance_of_evidence(
            session, evidence_id
        ).company_id == company.id
    assert outcome.model_extraction_unchanged

    # And the same attribute on another source is still an open question, so the
    # operator has not been told the matter is settled everywhere.
    siblings = [
        c for c in review.pending_candidates(session, limit=500)
        if c.attribute_key == candidate.attribute_key
    ]
    assert siblings, "the mirrored copies remain individually reviewable"
