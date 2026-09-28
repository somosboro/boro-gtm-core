"""Retention: payloads go, provenance stays.

Each of the three payload classes is tested separately, because each has its own
trigger, its own retention column and its own failure mode. The shared
requirement is the one that matters: after pruning, a claim must still be
explainable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import retention
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
#: Past every default horizon: 90 days for bodies, 24 months for text.
MUCH_LATER = NOW + timedelta(days=900)


@pytest.fixture
def m3(session):
    seed_all(session)
    session.flush()
    return session


@pytest.fixture
def researched(m3):
    company = Company(created_at=NOW, identity_policy_version="1.0",
                      lifecycle_status="ACTIVE")
    m3.add(company)
    m3.flush()
    run_pipeline(m3, company_id=company.id, transport=FixtureTransport(),
                 now=NOW, confirm_sampled=True)
    return company


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- the plan ---------------------------------------------------------------


def test_a_plan_selects_nothing_before_the_horizon(researched, m3):
    """Freshly captured payloads are not eligible."""
    plan = retention.plan_retention(m3, as_of=NOW + timedelta(days=1))
    assert plan.as_counts() == {"bodies": 0, "text_derivations": 0, "model_outputs": 0}


def test_a_plan_is_deterministic_and_mutates_nothing(researched, m3):
    before = (
        m3.scalar(select(func.count()).select_from(m.ResearchArtifactBody).where(
            m.ResearchArtifactBody.raw_body.is_not(None)
        )),
        m3.scalar(select(func.count()).select_from(m.ResearchTextDerivation).where(
            m.ResearchTextDerivation.extracted_text.is_not(None)
        )),
    )
    first = retention.plan_retention(m3, as_of=MUCH_LATER)
    second = retention.plan_retention(m3, as_of=MUCH_LATER)
    assert first.bodies == second.bodies
    assert first.text_derivations == second.text_derivations
    assert first.model_outputs == second.model_outputs
    assert first.total > 0

    after = (
        m3.scalar(select(func.count()).select_from(m.ResearchArtifactBody).where(
            m.ResearchArtifactBody.raw_body.is_not(None)
        )),
        m3.scalar(select(func.count()).select_from(m.ResearchTextDerivation).where(
            m.ResearchTextDerivation.extracted_text.is_not(None)
        )),
    )
    assert before == after, "a dry run must change nothing"


def test_per_content_type_horizons_are_honoured(researched, m3):
    """A PDF is expensive to keep and goes first (30 days, not 90)."""
    at_45_days = NOW + timedelta(days=45)
    plan = retention.plan_retention(m3, as_of=at_45_days)
    pdf_bodies = set(m3.scalars(
        select(m.ResearchArtifactBody.id)
        .join(m.ResearchBodyClassification,
              m.ResearchBodyClassification.body_id == m.ResearchArtifactBody.id)
        .where(m.ResearchBodyClassification.sniffed_media_type == "application/pdf")
    ).all())
    assert pdf_bodies
    assert pdf_bodies <= set(plan.bodies)

    json_bodies = set(m3.scalars(
        select(m.ResearchArtifactBody.id)
        .join(m.ResearchBodyClassification,
              m.ResearchBodyClassification.body_id == m.ResearchArtifactBody.id)
        .where(m.ResearchBodyClassification.sniffed_media_type == "application/json")
    ).all())
    assert json_bodies.isdisjoint(set(plan.bodies)), "JSON keeps for 180 days"


# --- raw body ---------------------------------------------------------------


def test_pruning_a_raw_body_keeps_its_hash_and_its_provenance(researched, m3):
    body = m3.scalars(
        select(m.ResearchArtifactBody).where(
            m.ResearchArtifactBody.raw_body.is_not(None)
        ).limit(1)
    ).first()
    digest, length = body.raw_body_sha256, body.byte_length
    assert body.raw_body is not None

    events_before = m3.scalars(
        select(m.ResearchFetchEvent).where(m.ResearchFetchEvent.body_id == body.id)
    ).all()
    fingerprint = {(e.id, e.source_id, e.retrieved_at, e.fetch_outcome)
                   for e in events_before}
    assert fingerprint

    plan = retention.RetentionPlan(bodies=[body.id])
    retention.apply_retention(m3, plan, as_of=MUCH_LATER)
    m3.refresh(body)

    assert body.raw_body is None
    assert body.body_retention == "PRUNED"
    assert body.pruned_at == MUCH_LATER
    assert body.raw_body_sha256 == digest
    assert body.byte_length == length

    after = {(e.id, e.source_id, e.retrieved_at, e.fetch_outcome) for e in m3.scalars(
        select(m.ResearchFetchEvent).where(m.ResearchFetchEvent.body_id == body.id)
    ).all()}
    assert after == fingerprint


def test_a_claim_remains_explainable_after_its_body_is_pruned(researched, m3):
    """The quoted span and its hash survive; that is the bounded loss."""
    link = m3.scalars(select(m.ClaimEvidenceLink).limit(1)).one()
    item = m3.get(m.ResearchEvidenceItem, link.evidence_item_id)
    quote, quote_hash = item.quote, item.quote_sha256
    assert quote and quote_hash

    retention.apply_retention(
        m3, retention.RetentionPlan(bodies=[item.body_id]), as_of=MUCH_LATER
    )
    m3.expire_all()

    item = m3.get(m.ResearchEvidenceItem, link.evidence_item_id)
    assert item.quote == quote
    assert item.quote_sha256 == quote_hash
    # Walk claim -> link -> evidence -> fetch/derivation -> body hash.
    body = m3.get(m.ResearchArtifactBody, item.body_id)
    assert body.raw_body is None
    assert body.raw_body_sha256
    assert m3.get(m.ResearchFetchEvent, item.fetch_event_id) is not None
    derivation = m3.get(m.ResearchArtifactDerivation, item.artifact_derivation_id)
    assert derivation.canonical_content_hash
    assert m3.get(CompanyClaim, link.claim_id) is not None


def test_restoring_a_pruned_body_is_rejected(researched, m3):
    from sqlalchemy.exc import DBAPIError

    body = m3.scalars(
        select(m.ResearchArtifactBody).where(
            m.ResearchArtifactBody.raw_body.is_not(None)
        ).limit(1)
    ).first()
    retention.apply_retention(
        m3, retention.RetentionPlan(bodies=[body.id]), as_of=MUCH_LATER
    )
    m3.flush()

    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "UPDATE research_artifact_bodies SET raw_body = :b, "
                "body_retention = 'RETAINED' WHERE id = :i"
            ), {"b": b"restored", "i": body.id})


def test_needing_a_pruned_body_raises_rather_than_deriving_from_nothing(researched, m3):
    """Silence and "the bytes are gone" must not look the same."""
    body = m3.scalars(
        select(m.ResearchArtifactBody).where(
            m.ResearchArtifactBody.raw_body.is_not(None)
        ).limit(1)
    ).first()
    assert retention.require_body(m3, body.id) is not None

    retention.apply_retention(
        m3, retention.RetentionPlan(bodies=[body.id]), as_of=MUCH_LATER
    )
    m3.flush()
    with pytest.raises(retention.PrunedPayloadError) as raised:
        retention.require_body(m3, body.id)
    assert body.raw_body_sha256 in str(raised.value)
    assert "refetch" in str(raised.value)


# --- text derivation --------------------------------------------------------


def test_pruning_extracted_text_keeps_the_contract_and_the_locators(researched, m3):
    derivation = m3.scalars(
        select(m.ResearchTextDerivation).where(
            m.ResearchTextDerivation.extracted_text.is_not(None)
        ).limit(1)
    ).first()
    contract = derivation.text_derivation_contract_hash
    locators = {i.locator_hash for i in m3.scalars(
        select(m.ResearchEvidenceItem).where(
            m.ResearchEvidenceItem.body_id == derivation.body_id
        )
    ).all()}

    retention.apply_retention(
        m3, retention.RetentionPlan(text_derivations=[derivation.id]),
        as_of=MUCH_LATER,
    )
    m3.refresh(derivation)
    assert derivation.extracted_text is None
    assert derivation.text_retention == "PRUNED"
    assert derivation.pruned_at == MUCH_LATER
    assert derivation.text_derivation_contract_hash == contract

    after = {i.locator_hash for i in m3.scalars(
        select(m.ResearchEvidenceItem).where(
            m.ResearchEvidenceItem.body_id == derivation.body_id
        )
    ).all()}
    assert after == locators

    with pytest.raises(retention.PrunedPayloadError):
        retention.require_text(m3, derivation.id)


# --- model raw output -------------------------------------------------------


def test_pruning_model_output_keeps_its_hash(researched, m3):
    """M3-ADR-052: the audit trail survives the bulk."""
    extraction = m3.scalars(
        select(m.ResearchExtraction).where(
            m.ResearchExtraction.extractor_kind == "MODEL",
            m.ResearchExtraction.raw_output.is_not(None),
        ).limit(1)
    ).first()
    assert extraction is not None, "the corpus exercises a MODEL extractor"
    digest = extraction.raw_output_sha256
    contract = extraction.extraction_contract_hash

    retention.apply_retention(
        m3, retention.RetentionPlan(model_outputs=[extraction.id]), as_of=MUCH_LATER
    )
    m3.refresh(extraction)
    assert extraction.raw_output is None
    assert extraction.raw_output_retention == "PRUNED"
    assert extraction.raw_output_pruned_at == MUCH_LATER
    assert extraction.raw_output_sha256 == digest
    assert extraction.extraction_contract_hash == contract
    assert extraction.observations, "the observations are the finding, not the bulk"


def test_an_extraction_still_rejects_every_other_update(researched, m3):
    """0005 permits one mutation, not mutability."""
    from sqlalchemy.exc import DBAPIError

    extraction = m3.scalars(select(m.ResearchExtraction).limit(1)).first()
    with pytest.raises(DBAPIError):
        with m3.begin_nested():
            m3.execute(text(
                "UPDATE research_extractions SET extractor_version = '99' WHERE id = :i"
            ), {"i": extraction.id})


# --- what retention may never do --------------------------------------------


def test_retention_deletes_no_row(researched, m3):
    """I3: evidence rows are never deleted by retention."""
    counts_before = {
        model: _count(m3, model) for model in (
            m.ResearchEvidenceItem, m.ClaimEvidenceLink, m.ResearchSource,
            m.ResearchFetchEvent, m.ResearchArtifact, m.ResearchArtifactDerivation,
            m.ResearchArtifactBody, m.ResearchExtraction, m.ResearchTextDerivation,
        )
    }
    plan = retention.plan_retention(m3, as_of=MUCH_LATER)
    assert plan.total > 0
    retention.apply_retention(m3, plan, as_of=MUCH_LATER)
    m3.flush()

    counts_after = {model: _count(m3, model) for model in counts_before}
    assert counts_after == counts_before


def test_pruning_everything_leaves_every_claim_walkable(researched, m3):
    """I1: a pruned body leaves provenance intelligible."""
    plan = retention.plan_retention(m3, as_of=MUCH_LATER)
    retention.apply_retention(m3, plan, as_of=MUCH_LATER)
    m3.flush()

    links = m3.scalars(select(m.ClaimEvidenceLink)).all()
    assert links
    for link in links:
        item = m3.get(m.ResearchEvidenceItem, link.evidence_item_id)
        assert item.quote_sha256
        body = m3.get(m.ResearchArtifactBody, item.body_id)
        assert body.raw_body_sha256, "the terminal node is still a hash"
        assert m3.get(m.ResearchFetchEvent, item.fetch_event_id).retrieved_at
        assert m3.get(
            m.ResearchArtifactDerivation, item.artifact_derivation_id
        ).canonical_content_hash
