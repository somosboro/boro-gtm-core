"""Phase 3.1: the defects an independent audit proved against this branch.

Each test reproduces the defect's *mechanism*, not a happy path near it. Where
a fix is a database invariant the test attacks the database directly, because a
guarantee that only the service layer keeps is not an invariant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import profiles, retention, review
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)


@pytest.fixture
def m3(session):
    seed_all(session)
    session.flush()
    return session


@pytest.fixture
def company(m3):
    row = Company(created_at=NOW, identity_policy_version="1.0", lifecycle_status="ACTIVE")
    m3.add(row)
    m3.flush()
    return row


@pytest.fixture
def researched(m3, company):
    return run_pipeline(m3, company_id=company.id, transport=FixtureTransport(),
                        now=NOW)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- §6 / I4: a prune may not launder anything else -------------------------


@pytest.mark.parametrize(
    "table,payload,state,stamp,immutable,value",
    [
        ("research_artifact_bodies", "raw_body", "body_retention", "pruned_at",
         "raw_body_sha256", "'deadbeef'"),
        ("research_text_derivations", "extracted_text", "text_retention",
         "pruned_at", "text_derivation_contract_hash", "'forged'"),
        ("research_extractions", "raw_output", "raw_output_retention",
         "raw_output_pruned_at", "extractor_version", "'99'"),
    ],
)
def test_a_prune_cannot_carry_an_illegal_mutation(
    researched, m3, table, payload, state, stamp, immutable, value
):
    """I4: one UPDATE combining the legal prune with a forged column.

    The body case is the one that matters most: `raw_body_sha256` is the
    terminal node of every provenance walk, and the old trigger let a prune
    rewrite it.
    """
    row_id = m3.scalar(text(
        f"SELECT id FROM {table} WHERE {payload} IS NOT NULL LIMIT 1"  # noqa: S608
    ))
    assert row_id is not None

    with pytest.raises(DBAPIError) as raised:
        with m3.begin_nested():
            m3.execute(text(
                f"UPDATE {table} SET {payload} = NULL, {state} = 'PRUNED', "  # noqa: S608
                f"{stamp} = now(), {immutable} = {value} WHERE id = :i"
            ), {"i": row_id})
    assert "may not change any other column" in str(raised.value)

    # And the payload is still there: the whole statement was rejected.
    still = m3.scalar(text(
        f"SELECT {payload} IS NOT NULL FROM {table} WHERE id = :i"  # noqa: S608
    ), {"i": row_id})
    assert still is True


def test_a_legal_prune_still_succeeds(researched, m3):
    """The tightened trigger must not forbid the transition it exists to allow."""
    body = m3.scalars(select(m.ResearchArtifactBody).where(
        m.ResearchArtifactBody.raw_body.is_not(None)
    ).limit(1)).first()
    retention.apply_retention(
        m3, retention.RetentionPlan(bodies=[body.id]), as_of=LATER
    )
    m3.refresh(body)
    assert body.raw_body is None
    assert body.body_retention == "PRUNED"
    assert body.pruned_at == LATER


def test_a_prune_without_a_timestamp_is_rejected(researched, m3):
    """The stamp is part of the transition, not an optional courtesy."""
    body_id = m3.scalar(text(
        "SELECT id FROM research_artifact_bodies WHERE raw_body IS NOT NULL LIMIT 1"
    ))
    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "UPDATE research_artifact_bodies SET raw_body = NULL, "
                "body_retention = 'PRUNED' WHERE id = :i"
            ), {"i": body_id})


# --- §3 / F7, F8: gap taxonomy ----------------------------------------------


def test_a_low_confidence_attribute_gets_insufficient_and_not_no_evidence(m3, company):
    """F7: the three "we do not have it" kinds are mutually exclusive."""
    import dataclasses

    import boro_gtm.research.services.extraction as extraction_module
    from boro_gtm.research.services.extraction import PROSE_EXTRACTOR

    weak = dataclasses.replace(PROSE_EXTRACTOR, extractor_confidence=0.10)
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = tuple(
        weak if x.extractor_id == PROSE_EXTRACTOR.extractor_id else x
        for x in original
    )
    try:
        result = run_pipeline(m3, company_id=company.id,
                              transport=FixtureTransport(), now=NOW)
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original

    assert result.low_confidence_deferred
    weakened = set(result.low_confidence_deferred)

    by_attribute: dict[str, set[str]] = {}
    for key, kind in m3.execute(
        select(m.OperationalResearchGap.attribute_key,
               m.OperationalResearchGap.gap_kind)
    ).all():
        by_attribute.setdefault(key, set()).add(kind)

    exclusive = {"NO_EVIDENCE", "INSUFFICIENT_EVIDENCE", "UNRESOLVABLE_SOURCE"}
    for key, kinds in by_attribute.items():
        assert len(kinds & exclusive) <= 1, f"{key} has {kinds}"

    deferred_with_gaps = weakened & set(by_attribute)
    assert deferred_with_gaps, "the weakened attributes should be gapped"
    for key in deferred_with_gaps:
        assert "INSUFFICIENT_EVIDENCE" in by_attribute[key]
        assert "NO_EVIDENCE" not in by_attribute[key]


def test_an_unrelated_failed_source_raises_no_unresolvable_gap(researched, m3):
    """F8: `branch_count` has three claims and must not read as unreachable."""
    unresolvable = set(m3.scalars(
        select(m.OperationalResearchGap.attribute_key).where(
            m.OperationalResearchGap.gap_kind == "UNRESOLVABLE_SOURCE"
        )
    ).all())
    evidenced = set(m3.scalars(
        select(CompanyClaim.attribute_key).where(
            CompanyClaim.availability == "OBSERVED"
        )
    ).all())

    assert unresolvable, "the corpus makes one attribute genuinely unreachable"
    assert unresolvable.isdisjoint(evidenced), (
        f"attributes with evidence marked unreachable: {unresolvable & evidenced}"
    )
    assert "branch_count" not in unresolvable


def test_unresolvable_is_attributed_only_to_a_pursued_attribute(researched, m3):
    """The failed sources must have been pursued for that very attribute."""
    gap = m3.scalars(select(m.OperationalResearchGap).where(
        m.OperationalResearchGap.gap_kind == "UNRESOLVABLE_SOURCE"
    )).one()

    pursued = m3.execute(
        select(m.ResearchSourceDiscovery.source_id,
               m.ResearchSourceDiscovery.discovery_context)
    ).all()
    sources_for_key = {
        source_id for source_id, context in pursued
        if gap.attribute_key in (context or {}).get("pursued_for", [])
    }
    assert sources_for_key, "the gap names an attribute nothing pursued"

    outcomes = set(m3.scalars(
        select(m.ResearchFetchEvent.fetch_outcome).where(
            m.ResearchFetchEvent.source_id.in_(sources_for_key)
        )
    ).all())
    from boro_gtm.research.services.pipeline import PERMANENT_FETCH_FAILURES

    assert outcomes <= PERMANENT_FETCH_FAILURES, outcomes


# --- §4 / corroboration -----------------------------------------------------


def test_the_chain_topology_counts_three_independent_witnesses(m3):
    """The counterexample that showed connected components were wrong.

    A–doc1, A–doc2, B–doc2, B–doc3, C–doc3 is one connected component and three
    pairwise-independent lineages. The contract asks for the second number.
    """
    from boro_gtm.research.services.evidence import _maximum_matching

    chain = [("A", "doc1"), ("A", "doc2"), ("B", "doc2"), ("B", "doc3"),
             ("C", "doc3")]
    assert _maximum_matching(sorted(chain)) == 3


@pytest.mark.parametrize("edges,expected", [
    ([("A", "1")], 1),
    ([("A", "1"), ("A", "2")], 1),                      # one publisher, two pages
    ([("A", "1"), ("B", "1")], 1),                      # a mirror
    ([("A", "1"), ("B", "2")], 2),                      # genuinely independent
    ([("A", "1"), ("B", "1"), ("C", "2")], 2),          # mirror plus a witness
    ([("A", "1"), ("A", "2"), ("B", "2"), ("B", "3"), ("C", "3")], 3),
])
def test_independence_matches_the_frozen_rule(edges, expected):
    from boro_gtm.research.services.evidence import _maximum_matching

    assert _maximum_matching(sorted(edges)) == expected


def test_the_matching_is_deterministic_under_input_order(m3):
    from boro_gtm.research.services.evidence import _maximum_matching

    edges = [("A", "1"), ("A", "2"), ("B", "2"), ("B", "3"), ("C", "3")]
    assert _maximum_matching(sorted(edges)) == _maximum_matching(
        sorted(reversed(edges))
    )


def test_corroboration_on_the_corpus_follows_the_pairwise_rule(
    researched, m3, company
):
    """The corpus is itself the chain topology, and the answer moved from 2 to 3.

    `emergency_service = true` rests on four `(publisher, document)` pairs:

        meridianmechanical / services-page
        meridianmechanical / emergency-page
        contractordirectory / emergency-page   (a byte-identical copy)
        tradepress / news-article

    The largest pairwise-independent set is three — the services page, the
    directory's copy, and the trade press — because every pair in it differs in
    **both** publisher and document. It is counter-intuitive and it is what the
    rule says: the copy is excluded only from a set that already contains the
    page it copied, which is exactly what stops a mirror inflating.

    Connected components answered 2 here, and 1 on the same shape with one more
    link. Neither is the contract.
    """
    profiles.rebuild_company_profile(m3, company.id)
    counts = m3.get(
        m.OperationalResearchProfile, company.id
    ).corroborating_publisher_counts
    assert counts["emergency_service"] == 3
    assert counts["installation"] == 1
    assert counts["preventive_maintenance"] == 1


def test_a_mirror_never_joins_a_set_with_the_page_it_copied(researched, m3):
    """The half of the rule that stops inflation, isolated."""
    from boro_gtm.research.services.evidence import _maximum_matching

    # Only the company and its copier, on the one shared document.
    assert _maximum_matching([("meridian", "emergency"),
                              ("directory", "emergency")]) == 1


# --- §5 / precedence --------------------------------------------------------


def test_a_more_recent_earliest_retrieval_wins_when_all_else_ties(m3):
    """Every rung above this one is equal, so this rung decides — and it was
    decided backwards."""
    import types

    from boro_gtm.research.services.profiles import ClaimFacts, precedence_key

    def candidate(retrieved: datetime, number: int) -> ClaimFacts:
        claim = types.SimpleNamespace(
            fact_type="FACT", id=uuid.UUID(int=number), value_jsonb={"value": True},
            confidence=None, unit=None,
        )
        return ClaimFacts(
            claim=claim, trust_tier=0.9, source_published_at=None,
            earliest_retrieved_at=retrieved, evidence_item_ids=(),
        )

    older = candidate(NOW, 1)
    newer = candidate(NOW + timedelta(days=30), 2)

    assert min([older, newer], key=precedence_key) is newer
    assert min([newer, older], key=precedence_key) is newer


def test_the_claim_id_remains_the_final_tiebreak(m3):
    import types

    from boro_gtm.research.services.profiles import ClaimFacts, precedence_key

    def candidate(number: int) -> ClaimFacts:
        claim = types.SimpleNamespace(
            fact_type="FACT", id=uuid.UUID(int=number), value_jsonb={},
            confidence=None, unit=None,
        )
        return ClaimFacts(claim=claim, trust_tier=0.9, source_published_at=None,
                          earliest_retrieved_at=NOW, evidence_item_ids=())

    low, high = candidate(1), candidate(2)
    assert min([high, low], key=precedence_key) is low


# --- §1 / D6, D7: durable review --------------------------------------------


def _pending(m3):
    """The first sampled candidate. Reviewability is a durable state now.

    Returns the candidate itself: it names one observation, which is what a
    decision is addressed to. The evidence item behind it may back several.
    """
    candidates = review.pending_candidates(m3, reason=review.SAMPLED_REASON)
    assert candidates, "the corpus produces a sampled reading awaiting review"
    return candidates[0]


def test_a_sampled_reading_is_reviewable_before_any_claim_exists(researched, m3):
    """The state a claim-keyed route could not address."""
    item = m3.get(m.ResearchEvidenceItem, _pending(m3).evidence_item_id)
    extraction = m3.get(m.ResearchExtraction, item.extraction_id)
    assert extraction.determinism == "SAMPLED"

    linked = m3.scalars(select(m.ClaimEvidenceLink.id).where(
        m.ClaimEvidenceLink.evidence_item_id == item.id
    )).all()
    assert linked == [], "it supports no claim, which is the whole point"


def test_a_rejection_is_durable_and_asserts_nothing(researched, m3, company):
    """D7: the defect was that this persisted nothing at all."""
    candidate = _pending(m3)
    item = m3.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
    sampled = m3.get(m.ResearchExtraction, item.extraction_id)
    before = (sampled.raw_output_sha256, sampled.extraction_contract_hash,
              str(sampled.observations))
    claims_before = _count(m3, CompanyClaim)
    extractions_before = _count(m3, m.ResearchExtraction)

    outcome = review.review_candidate(
        m3, candidate_id=candidate.id, decision="REJECT", actor="analyst",
        note="the phrasing does not support it", now=LATER,
    )
    m3.flush()

    row = m3.get(m.ResearchEvidenceReview, outcome.review_id)
    assert row is not None, "the decision survives the transaction"
    assert row.decision == "REJECT"
    assert row.actor == "analyst"
    assert row.note == "the phrasing does not support it"
    assert row.reviewed_at == LATER
    assert row.review_candidate_id == candidate.id
    assert outcome.evidence_item_id == item.id
    assert outcome.attribute_key == candidate.attribute_key
    assert row.human_extraction_id is None
    assert row.resulting_claim_id is None

    assert _count(m3, CompanyClaim) == claims_before, "no claim from a rejection"
    assert _count(m3, m.ResearchExtraction) == extractions_before
    m3.refresh(sampled)
    assert (sampled.raw_output_sha256, sampled.extraction_contract_hash,
            str(sampled.observations)) == before


def test_a_rejection_never_asserts_the_negative(researched, m3, company):
    """Disbelief is not evidence that the opposite is true."""
    review.review_candidate(m3, candidate_id=_pending(m3).id, decision="REJECT",
                            actor="analyst", now=LATER)
    m3.flush()
    negatives = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.value_jsonb["value"].astext == "false"
    )).all()
    # The only stated negative in the corpus comes from the directory page, not
    # from any review.
    for claim in negatives:
        assert claim.attribute_key == "emergency_service"
        assert claim.fact_type == "PROXY"


def test_the_database_forbids_a_rejection_that_names_a_claim(researched, m3):
    """The CHECK, not the service, is what makes it impossible."""
    candidate = _pending(m3)
    claim_id = m3.scalar(select(CompanyClaim.id).limit(1))
    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "INSERT INTO research_evidence_reviews "
                "(id, review_candidate_id, decision, actor, resulting_claim_id, "
                " reviewed_at) VALUES (:i, :c, 'REJECT', 'a', :k, now())"
            ), {"i": uuid.uuid4(), "c": candidate.id, "k": claim_id})


def test_a_confirmation_appends_a_human_lineage_and_a_durable_record(
    researched, m3, company
):
    """D6: the sampled extraction is byte-identical afterwards."""
    candidate = _pending(m3)
    item = m3.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
    sampled = m3.get(m.ResearchExtraction, item.extraction_id)
    before = (sampled.raw_output_sha256, sampled.extraction_contract_hash,
              str(sampled.observations))

    outcome = review.review_candidate(
        m3, candidate_id=candidate.id, decision="CONFIRM", actor="analyst",
        now=LATER,
    )
    m3.flush()

    row = m3.get(m.ResearchEvidenceReview, outcome.review_id)
    assert row.decision == "CONFIRM"
    assert row.human_extraction_id is not None
    human = m3.get(m.ResearchExtraction, row.human_extraction_id)
    assert human.extractor_kind == "HUMAN"
    assert outcome.created_evidence_item_ids

    m3.refresh(sampled)
    assert (sampled.raw_output_sha256, sampled.extraction_contract_hash,
            str(sampled.observations)) == before


def test_a_reviewed_observation_leaves_the_pending_queue(researched, m3):
    candidate = _pending(m3)
    waiting = {c.id for c in review.pending_candidates(m3, limit=500)}
    assert candidate.id in waiting
    review.review_candidate(m3, candidate_id=candidate.id, decision="REJECT",
                            actor="analyst", now=LATER)
    m3.flush()
    still = {c.id for c in review.pending_candidates(m3, limit=500)}
    assert candidate.id not in still


def test_the_review_table_is_append_only(researched, m3):
    outcome = review.review_candidate(m3, candidate_id=_pending(m3).id,
                                      decision="REJECT", actor="a", now=LATER)
    m3.flush()
    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "UPDATE research_evidence_reviews SET decision = 'CONFIRM' "
                "WHERE id = :i"
            ), {"i": outcome.review_id})


# --- §2 / G16: terminal uniqueness (single-connection half) -----------------


def test_a_gap_cannot_hold_two_terminal_events(researched, m3):
    """The partial unique index, attacked directly."""
    gap = m3.scalars(select(m.OperationalResearchGap).limit(1)).first()
    attempt_id = m3.scalar(select(m.OperationalResearchAttempt.id).limit(1))
    m3.execute(text(
        "INSERT INTO operational_research_gap_events "
        "(id, gap_id, event_kind, attempt_id, occurred_at) "
        "VALUES (:i, :g, 'RESOLVED', :a, :t)"
    ), {"i": uuid.uuid4(), "g": gap.id, "a": attempt_id,
        "t": NOW + timedelta(minutes=5)})
    m3.flush()

    # In one transaction the trigger sees the committed RESOLVED and refuses
    # first; the unique index is the backstop for two that cannot see each
    # other. Either refusal is correct, and both must be a refusal.
    with pytest.raises((IntegrityError, DBAPIError)):
        with m3.begin_nested():
            m3.execute(text(
                "INSERT INTO operational_research_gap_events "
                "(id, gap_id, event_kind, attempt_id, occurred_at) "
                "VALUES (:i, :g, 'ABANDONED', :a, :t)"
            ), {"i": uuid.uuid4(), "g": gap.id, "a": attempt_id,
                "t": NOW + timedelta(minutes=6)})


def test_an_occurrence_cannot_hold_two_terminal_events(researched, m3):
    occurrence = m3.scalars(select(m.IdentityReviewSignalOccurrence)).one()
    m3.add(m.IdentityReviewSignalEvent(
        occurrence_id=occurrence.id, status="ACKNOWLEDGED", actor="a",
        occurred_at=NOW + timedelta(minutes=1)))
    m3.flush()
    m3.add(m.IdentityReviewSignalEvent(
        occurrence_id=occurrence.id, status="ACTIONED", actor="a",
        occurred_at=NOW + timedelta(minutes=2)))
    m3.flush()

    with pytest.raises((IntegrityError, DBAPIError)):
        with m3.begin_nested():
            m3.execute(text(
                "INSERT INTO identity_review_signal_events "
                "(id, occurrence_id, status, actor, occurred_at) "
                "VALUES (:i, :o, 'DISMISSED', 'a', :t)"
            ), {"i": uuid.uuid4(), "o": occurrence.id,
                "t": NOW + timedelta(minutes=3)})


# --- §7 / jobs --------------------------------------------------------------


def test_the_queue_declares_only_the_job_that_runs(m3, company):
    """No name that means "skip and succeed"."""
    from boro_gtm.research.services.application import (
        EXECUTE_RESEARCH,
        JOB_TYPES,
        enqueue_research,
        run_worker_once,
    )
    from boro_gtm.research.services.pipeline import get_or_create_run

    assert JOB_TYPES == (EXECUTE_RESEARCH,)

    run, _ = get_or_create_run(m3, company_id=company.id, now=NOW)
    job = enqueue_research(m3, run_id=run.id)
    m3.flush()
    assert job.job_type == EXECUTE_RESEARCH
    assert job.status == "QUEUED"

    outcome = run_worker_once(m3, transport=FixtureTransport())
    m3.flush()
    assert outcome is not None
    assert "skipped" not in outcome
    assert outcome["status"] in {"COMPLETED", "PARTIAL", "FAILED"}
    assert m3.get(type(job), job.id).status == "DONE"

    # The queue is now empty, and the worker says so rather than inventing work.
    assert run_worker_once(m3, transport=FixtureTransport()) is None


def test_a_worker_failure_is_recorded_on_the_job_not_raised(m3, company):
    """A domain conflict fails the job; it does not escape the worker loop."""
    from boro_gtm.research.services.application import (
        enqueue_research,
        open_attempt,
        run_worker_once,
    )
    from boro_gtm.research.services.pipeline import get_or_create_run

    run, _ = get_or_create_run(m3, company_id=company.id, now=NOW)
    job = enqueue_research(m3, run_id=run.id)
    open_attempt(m3, run_id=run.id)          # a live attempt blocks a second
    m3.flush()

    outcome = run_worker_once(m3, transport=FixtureTransport())
    m3.flush()
    assert outcome is not None
    assert "failed" in outcome
    assert "still PENDING" in outcome["failed"]
    refreshed = m3.get(type(job), job.id)
    assert refreshed.last_error
    assert refreshed.attempts >= 1
