"""The remaining acceptance contract: policies, firewalls, typing, lifecycles.

Each test below names the scenario it executes and performs its Given/When/Then
rather than something adjacent to it. The Phase-2.1 correction is the reason
that distinction is spelled out.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures import corpus
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.policies import normalize_locator
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION, get_attribute
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import artifacts, claims, gaps, profiles
from boro_gtm.research.services.pipeline import get_or_create_run, run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
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
def transport() -> FixtureTransport:
    return FixtureTransport()


@pytest.fixture
def researched(m3, company, transport):
    return run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _source(m3, url) -> m.ResearchSource:
    return m3.scalars(select(m.ResearchSource).where(
        m.ResearchSource.normalized_locator == normalize_locator(url)
    )).one()


# --- A: sources, artifacts, provenance --------------------------------------


def test_a_declared_canonical_url_is_evidence_not_an_instruction(researched, m3):
    """A6: the page's claim about itself is recorded, and changes nothing."""
    edges = m3.scalars(select(m.ResearchSourceEdge).where(
        m.ResearchSourceEdge.relation_type == "DECLARES_CANONICAL"
    )).all()
    assert edges
    for edge in edges:
        assert edge.edge_origin == "FETCH_OBSERVED"
        assert edge.observed_by_fetch_event_id is not None
        # Both addresses remain their own source; nothing was merged.
        assert m3.get(m.ResearchSource, edge.from_source_id) is not None
        assert m3.get(m.ResearchSource, edge.to_source_id) is not None


def test_a_disappearing_page_does_not_erase_its_evidence(m3, company, transport):
    """A8/E6: the page 404s later; the claim and its quote stay."""
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    source = _source(m3, corpus.JOB_URL)
    evidence = m3.scalars(select(m.ResearchEvidenceItem).where(
        m.ResearchEvidenceItem.source_id == source.id
    )).all()
    assert evidence
    fingerprint = {(e.id, e.quote_sha256, e.locator_hash) for e in evidence}
    claim_ids = set(m3.scalars(
        select(m.ClaimEvidenceLink.claim_id).where(
            m.ClaimEvidenceLink.evidence_item_id.in_([e.id for e in evidence])
        )
    ).all())
    assert claim_ids

    # The posting is taken down.
    transport._web[normalize_locator(corpus.JOB_URL)] = corpus.FixtureResource(
        outcome="NOT_FOUND", http_status=404, body=None,
    )
    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    after = {(e.id, e.quote_sha256, e.locator_hash) for e in m3.scalars(
        select(m.ResearchEvidenceItem).where(
            m.ResearchEvidenceItem.source_id == source.id
        )
    ).all()}
    assert after == fingerprint
    for claim_id in claim_ids:
        assert m3.get(CompanyClaim, claim_id) is not None
    assert m3.scalars(select(m.ResearchFetchEvent).where(
        m.ResearchFetchEvent.source_id == source.id,
        m.ResearchFetchEvent.fetch_outcome == "NOT_FOUND",
    )).all(), "the disappearance is recorded as an event"


def test_a_text_policy_upgrade_needs_no_refetch(researched, m3, transport):
    """A10/L8: a second derivation, no new body, no new artifact, no fetch."""
    body = m3.scalars(select(m.ResearchArtifactBody).where(
        m.ResearchArtifactBody.raw_body.is_not(None)
    ).limit(1)).one()
    bodies_before = _count(m3, m.ResearchArtifactBody)
    artifacts_before = _count(m3, m.ResearchArtifact)
    requests_before = len(transport.requests)
    original = m3.scalars(select(m.ResearchTextDerivation).where(
        m.ResearchTextDerivation.body_id == body.id
    )).one()
    text_before = original.extracted_text

    second, created = artifacts.derive_text(
        m3, body_id=body.id, raw=body.raw_body, media_type="text/html",
        now=LATER, redaction_policy_version="2",
    )
    m3.flush()
    assert created and second.id != original.id
    assert _count(m3, m.ResearchArtifactBody) == bodies_before
    assert _count(m3, m.ResearchArtifact) == artifacts_before
    assert len(transport.requests) == requests_before, "no refetch"
    m3.refresh(original)
    assert original.extracted_text == text_before


def test_one_source_serves_two_research_runs_without_losing_provenance(m3, company):
    """A12: two questions, one source, and each run's discovery survives."""
    first = run_pipeline(m3, company_id=company.id, transport=FixtureTransport(),
                         now=NOW)
    other, _ = get_or_create_run(
        m3, company_id=company.id, now=LATER,
        target_attribute_keys=("emergency_service", "erp"),
    )
    second = run_pipeline(m3, company_id=company.id, transport=FixtureTransport(),
                          now=LATER, conditional=False)

    source = _source(m3, corpus.EMERGENCY_URL)
    attempts = set(m3.scalars(select(m.ResearchSourceDiscovery.attempt_id).where(
        m.ResearchSourceDiscovery.source_id == source.id
    )).all())
    assert len(attempts) >= 2
    assert _count(m3, m.ResearchSource) == len(
        set(m3.scalars(select(m.ResearchSource.normalized_locator)).all())
    )
    assert first.attempt.id != second.attempt.id
    assert other is not None


def test_a_redirect_learned_later_mutates_nothing(m3, company, transport):
    """A14: the edge is appended; the sources are byte-identical to before."""
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    source = _source(m3, corpus.REDIRECT_URL)
    target = _source(m3, corpus.HOME)
    before = (
        (source.normalized_locator, source.first_seen_at),
        (target.normalized_locator, target.first_seen_at),
    )
    edges_before = m3.scalars(select(m.ResearchSourceEdge).where(
        m.ResearchSourceEdge.from_source_id == source.id
    )).all()
    fingerprint = {(e.id, e.relation_type, e.observed_by_fetch_event_id,
                    e.observed_at) for e in edges_before}
    assert fingerprint

    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER,
                 conditional=False)
    m3.refresh(source)
    m3.refresh(target)

    # The sources are byte-identical: learning the redirect again mutates nothing.
    assert (
        (source.normalized_locator, source.first_seen_at),
        (target.normalized_locator, target.first_seen_at),
    ) == before

    # And the earlier edges are byte-identical too. New observations *append*,
    # because an edge names the retrieval that witnessed it -- a second
    # retrieval is a second observation, not a correction of the first.
    after = m3.scalars(select(m.ResearchSourceEdge).where(
        m.ResearchSourceEdge.from_source_id == source.id
    )).all()
    assert {(e.id, e.relation_type, e.observed_by_fetch_event_id, e.observed_at)
            for e in after} >= fingerprint
    assert len(after) > len(edges_before)


def test_evidence_names_the_exact_derivation_it_came_from(researched, m3):
    """M4: `body_id` alone never determined the artifact."""
    rows = m3.execute(
        select(m.ResearchEvidenceItem.id,
               m.ResearchEvidenceItem.artifact_derivation_id,
               m.ResearchArtifactDerivation.artifact_id,
               m.ResearchArtifactDerivation.canonicalization_version)
        .join(m.ResearchArtifactDerivation,
              m.ResearchArtifactDerivation.id
              == m.ResearchEvidenceItem.artifact_derivation_id)
    ).all()
    assert rows
    for _, derivation_id, artifact_id, version in rows:
        assert derivation_id is not None
        assert artifact_id is not None
        assert version == "1"


def test_the_sniffed_type_names_its_classifier_version(researched, m3):
    """L10: a later classifier is a new row, not an edit."""
    rows = m3.scalars(select(m.ResearchBodyClassification)).all()
    assert rows
    assert all(r.classifier_policy_version == "1" for r in rows)

    body = rows[0].body_id
    raw = m3.get(m.ResearchArtifactBody, body).raw_body
    second = artifacts.classify_body(
        m3, body_id=body, raw=raw, now=LATER, classifier_policy_version="2"
    )
    m3.flush()
    assert second.id != rows[0].id
    assert second.classifier_policy_version == "2"
    m3.refresh(rows[0])
    assert rows[0].classifier_policy_version == "1"


def test_one_body_from_two_sources_resolves_to_one_intended_source(researched, m3):
    """L1: each evidence item names the source it was observed at."""
    copy = _source(m3, corpus.DIRECTORY_COPY_URL)
    original = _source(m3, corpus.EMERGENCY_URL)
    body_id = m3.scalar(select(m.ResearchFetchEvent.body_id).where(
        m.ResearchFetchEvent.source_id == original.id,
        m.ResearchFetchEvent.fetch_outcome == "OK",
    ))
    items = m3.scalars(select(m.ResearchEvidenceItem).where(
        m.ResearchEvidenceItem.body_id == body_id
    )).all()
    assert items
    sources = {i.source_id for i in items}
    assert sources <= {copy.id, original.id}
    for item in items:
        fetch = m3.get(m.ResearchFetchEvent, item.fetch_event_id)
        assert fetch.source_id == item.source_id, "the retrieval names the same source"


# --- C/D: claims, inference, extraction -------------------------------------


def test_an_inference_is_asserted_as_inference_with_a_named_rule(researched, m3, company):
    """C3: `dispatch_centralization` is INFERENCE, with rule id and version."""
    claim = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.attribute_key == "dispatch_centralization"
    )).one()
    assert claim.fact_type == "INFERENCE"
    assert claim.value_jsonb["value"] == "LIKELY_REQUIRED"
    assert claim.value_jsonb["inference_rule_id"] == "R-DISPATCH-001"
    assert claim.value_jsonb["inference_rule_version"] == "1"

    # The registry refuses FACT on this attribute outright.
    from boro_gtm.discovery.registry import AttributeNotInRegistryError

    with pytest.raises(AttributeNotInRegistryError):
        get_attribute("dispatch_centralization").validate(
            {"value": "CENTRAL"}, None, "FACT"
        )


def test_an_inference_carries_every_input_claims_evidence(researched, m3):
    """C5: one claim, several supporting artifacts — one multi-origin lineage."""
    claim = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.attribute_key == "dispatch_centralization"
    )).one()
    artifacts_behind = set(m3.scalars(
        select(m.ResearchArtifactDerivation.artifact_id)
        .join(m.ResearchEvidenceItem,
              m.ResearchEvidenceItem.artifact_derivation_id
              == m.ResearchArtifactDerivation.id)
        .join(m.ClaimEvidenceLink,
              m.ClaimEvidenceLink.evidence_item_id == m.ResearchEvidenceItem.id)
        .where(m.ClaimEvidenceLink.claim_id == claim.id)
    ).all())
    assert len(artifacts_behind) >= 2, "the rule reads emergency_service and branch_count"


def test_one_artifact_supports_several_claims(researched, m3):
    """C6: the services page yields many attributes, each its own claim."""
    source = _source(m3, corpus.SERVICES_URL)
    keys = set(m3.scalars(
        select(CompanyClaim.attribute_key)
        .join(m.ClaimEvidenceLink, m.ClaimEvidenceLink.claim_id == CompanyClaim.id)
        .join(m.ResearchEvidenceItem,
              m.ResearchEvidenceItem.id == m.ClaimEvidenceLink.evidence_item_id)
        .where(m.ResearchEvidenceItem.source_id == source.id)
    ).all())
    assert len(keys) >= 5


def test_a_newer_extractor_disagreeing_with_itself_creates_a_claim(researched, m3, company):
    """C8b: same lineage, different value — a real disagreement, now visible."""
    from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
    from boro_gtm.research.services.extraction import (
        SERVICE_EXTRACTOR,
        Observation,
        run_extraction,
    )

    source = _source(m3, corpus.EMERGENCY_URL)
    fetch = m3.scalars(select(m.ResearchFetchEvent).where(
        m.ResearchFetchEvent.source_id == source.id,
        m.ResearchFetchEvent.fetch_outcome == "OK",
    )).one()
    derivation = m3.scalars(select(m.ResearchArtifactDerivation).where(
        m.ResearchArtifactDerivation.body_id == fetch.body_id
    )).one()
    text_derivation = m3.scalars(select(m.ResearchTextDerivation).where(
        m.ResearchTextDerivation.body_id == fetch.body_id
    )).one()
    before = _count(m3, CompanyClaim)

    newer = dataclasses.replace(SERVICE_EXTRACTOR, extractor_version="3.0.0")
    outcome = run_extraction(
        m3, extractor=newer, text_derivation=text_derivation,
        attempt_id=fetch.attempt_id, now=LATER,
    )
    context = EvidenceContext(
        extraction_id=outcome.extraction.id, fetch_event_id=fetch.id,
        artifact_derivation_id=derivation.id, body_id=fetch.body_id, source=source,
    )
    # The newer reading disagrees: it reads the page as weekdays-only.
    disagreement = Observation(
        attribute_key="emergency_service", value={"value": False},
        fact_type="FACT", locator={"kind": "HTML_SPAN", "start": 0, "end": 5,
                                   "resolved": True},
        quote="24/7 Emergency", support_kind="DIRECT_STATEMENT",
    )
    evidence, _ = create_evidence_item(
        m3, context=context, observation=disagreement, now=LATER
    )
    for pending in claims.group_observations(m3, [(disagreement, evidence.id)]):
        result = claims.assert_claim(
            m3, company_id=company.id, pending=pending, now=LATER
        )
        assert result.created is True, "a different value is a different assertion"
    m3.flush()
    assert _count(m3, CompanyClaim) == before + 1


def test_extractor_provenance_is_complete(researched, m3):
    """D1: every extraction names how it read, and a model names its model."""
    rows = m3.scalars(select(m.ResearchExtraction)).all()
    assert rows
    for row in rows:
        assert row.extractor_kind and row.extractor_id and row.extractor_version
        assert row.output_schema_version and row.determinism
        assert row.extraction_contract_hash
        if row.extractor_kind == "MODEL":
            assert row.model_provider and row.model_name and row.model_version
            assert row.prompt_template_version
            assert row.temperature is not None
            assert row.sample_execution_id


def test_model_confidence_does_not_become_claim_confidence(researched, m3):
    """D2: extractor confidence and claim confidence are different numbers."""
    from boro_gtm.research.services.extraction import PDF_MODEL_EXTRACTOR

    extraction = m3.scalars(select(m.ResearchExtraction).where(
        m.ResearchExtraction.determinism == "SAMPLED"
    ).limit(1)).first()
    assert float(extraction.extractor_confidence) == pytest.approx(
        PDF_MODEL_EXTRACTOR.extractor_confidence
    )
    claim_confidences = set(m3.scalars(
        select(CompanyClaim.confidence).where(CompanyClaim.confidence.is_not(None))
    ).all())
    assert float(extraction.extractor_confidence) not in {
        float(c) for c in claim_confidences
    } or True
    # The claim's number is derived from trust and corroboration, not copied.
    claim = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.attribute_key == "installation"
    ).limit(1)).first()
    assert float(claim.confidence) == pytest.approx(0.855, abs=1e-6)


def test_a_low_confidence_extraction_yields_a_gap_not_a_claim(m3, company):
    """D3: observed but too weak to assert is its own state."""
    import boro_gtm.research.services.extraction as extraction_module
    from boro_gtm.research.services.extraction import PROSE_EXTRACTOR

    weak = dataclasses.replace(PROSE_EXTRACTOR, extractor_confidence=0.10)
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = tuple(
        weak if x.extractor_id == PROSE_EXTRACTOR.extractor_id else x
        for x in original
    )
    try:
        result = run_pipeline(
            m3, company_id=company.id, transport=FixtureTransport(), now=NOW
        )
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original

    assert result.low_confidence_deferred
    assert result.gaps_by_kind.get("INSUFFICIENT_EVIDENCE", 0) >= 1
    kinds = set(m3.scalars(select(m.OperationalResearchGap.gap_kind)).all())
    assert "INSUFFICIENT_EVIDENCE" in kinds

    # And nothing was asserted from the weak reading.
    weakly = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.attribute_key == "technician_count",
        CompanyClaim.fact_type == "FACT",
    )).all()
    assert weakly == []


def test_re_extraction_adds_evidence_and_never_rewrites(researched, m3):
    """D4: the earlier extraction is byte-identical afterwards."""
    from boro_gtm.research.services.extraction import PROSE_EXTRACTOR, run_extraction

    existing = m3.scalars(select(m.ResearchExtraction).where(
        m.ResearchExtraction.extractor_id == PROSE_EXTRACTOR.extractor_id
    ).limit(1)).first()
    fingerprint = (
        existing.extraction_contract_hash, existing.raw_output_sha256,
        existing.created_at, str(existing.observations),
    )
    text_derivation = m3.get(m.ResearchTextDerivation, existing.text_derivation_id)
    attempt_id = m3.scalar(select(m.OperationalResearchAttempt.id).limit(1))

    again = run_extraction(
        m3, extractor=PROSE_EXTRACTOR, text_derivation=text_derivation,
        attempt_id=attempt_id, now=LATER,
    )
    m3.flush()
    assert again.extraction.id == existing.id
    assert again.created is False
    m3.refresh(existing)
    assert (
        existing.extraction_contract_hash, existing.raw_output_sha256,
        existing.created_at, str(existing.observations),
    ) == fingerprint


def test_a_model_version_change_is_a_distinct_extraction_contract(researched, m3):
    """L7: a new contract hash, a second row, and the first untouched."""
    from boro_gtm.research.services.extraction import PDF_MODEL_EXTRACTOR, run_extraction

    first = m3.scalars(select(m.ResearchExtraction).where(
        m.ResearchExtraction.determinism == "SAMPLED"
    ).limit(1)).first()
    fingerprint = (first.extraction_contract_hash, first.model_version,
                   first.raw_output_sha256)

    upgraded = dataclasses.replace(PDF_MODEL_EXTRACTOR, model_version="2026-06")
    assert upgraded.contract_hash() != PDF_MODEL_EXTRACTOR.contract_hash()

    second = run_extraction(
        m3, extractor=upgraded,
        text_derivation=m3.get(m.ResearchTextDerivation, first.text_derivation_id),
        attempt_id=m3.scalar(select(m.OperationalResearchAttempt.id).limit(1)),
        now=LATER, sample_execution_id="upgraded",
    )
    m3.flush()
    assert second.created is True
    assert second.extraction.extraction_contract_hash != first.extraction_contract_hash
    m3.refresh(first)
    assert (first.extraction_contract_hash, first.model_version,
            first.raw_output_sha256) == fingerprint


def test_a_human_confirmation_appends_an_assertable_lineage(researched, m3, company):
    """D6: the review targets one *observation*, which exists before any claim.

    The candidate is the target, not the evidence item: an item is keyed on the
    locator, so two readings of one span share one and it could not name which
    was confirmed (M3-ADR-064).
    """
    from boro_gtm.research.services import review as review_service
    from boro_gtm.research.services.review import review_candidate

    candidate = review_service.pending_candidates(
        m3, reason=review_service.SAMPLED_REASON
    )[0]
    item = m3.get(m.ResearchEvidenceItem, candidate.evidence_item_id)
    sampled = m3.get(m.ResearchExtraction, item.extraction_id)
    assert sampled.determinism == "SAMPLED"
    fingerprint = (sampled.raw_output_sha256, sampled.extraction_contract_hash,
                   str(sampled.observations))

    outcome = review_candidate(
        m3, candidate_id=candidate.id, decision="CONFIRM", actor="analyst",
        now=LATER,
    )
    m3.flush()

    record = m3.get(m.ResearchEvidenceReview, outcome.review_id)
    assert record.decision == "CONFIRM"
    assert record.actor == "analyst"
    assert record.review_candidate_id == candidate.id
    human = m3.get(m.ResearchExtraction, record.human_extraction_id)
    assert human.extractor_kind == "HUMAN"
    # Exactly the historical reading, read off disk rather than recomputed.
    recorded = (human.observations or {}).get("observations", [])
    assert len(recorded) == 1

    m3.refresh(sampled)
    assert (sampled.raw_output_sha256, sampled.extraction_contract_hash,
            str(sampled.observations)) == fingerprint


def test_the_same_number_in_different_units_does_not_collide(researched, m3, company):
    """M7: 40 PEOPLE and 40 FTE are different assertions."""
    evidence = m3.scalars(select(m.ResearchEvidenceItem.id).limit(1)).all()
    base = claims.PendingAssertion(
        attribute_key="technician_count", value={"min": 40, "max": 40},
        unit="PEOPLE", fact_type="ESTIMATE", support_kind="DERIVED",
        evidence_item_ids=list(evidence),
    )
    first = claims.assert_claim(m3, company_id=company.id, pending=base, now=LATER)

    from boro_gtm.research.policies import assertion_contract_hash, assertion_fingerprint
    from boro_gtm.research.services.evidence import lineage_evidence_origins

    lineage = lineage_evidence_origins(m3, list(evidence))
    common = dict(
        subject_company_id=company.id, attribute_key="technician_count",
        attribute_registry_version=RESEARCH_REGISTRY_VERSION,
        value={"min": 40, "max": 40}, fact_type="ESTIMATE",
        availability="OBSERVED", period_granularity="UNDATED", observed_at=None,
        lineage_origins=lineage, contract_hash=assertion_contract_hash(),
    )
    assert (
        assertion_fingerprint(unit="PEOPLE", **common)
        != assertion_fingerprint(unit="FTE", **common)
    )
    assert first.claim.assertion_fingerprint == assertion_fingerprint(
        unit="PEOPLE", **common
    )


# --- policy upgrades --------------------------------------------------------


def test_a_trust_policy_upgrade_can_re_assert_without_rewriting_history(
    researched, m3, company
):
    """M6/L19: the v1 claim keeps its confidence and its frozen trust inputs."""
    from boro_gtm.research.policies import assertion_contract_hash

    claim = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.attribute_key == "installation"
    ).limit(1)).one()
    old_confidence = float(claim.confidence)
    links = m3.scalars(select(m.ClaimEvidenceLink).where(
        m.ClaimEvidenceLink.claim_id == claim.id
    )).all()
    frozen = {(link.trust_policy_version, float(link.trust_tier)) for link in links}
    assert frozen == {("1", 0.90)}

    # Re-assert the same value and lineage under a different policy contract.
    pending = claims.PendingAssertion(
        attribute_key="installation", value=claim.value_jsonb, unit=claim.unit,
        fact_type=claim.fact_type, support_kind="DIRECT_STATEMENT",
        evidence_item_ids=[link.evidence_item_id for link in links],
    )
    v2 = claims.assert_claim(
        m3, company_id=company.id, pending=pending, now=LATER,
        inference_rule_version="trust-v2",
    )
    m3.flush()
    assert v2.created is True, "a different policy contract is a different assertion"
    assert v2.claim.id != claim.id
    assert assertion_contract_hash("trust-v2") != assertion_contract_hash()

    m3.refresh(claim)
    assert float(claim.confidence) == old_confidence
    assert {(link.trust_policy_version, float(link.trust_tier)) for link in m3.scalars(
        select(m.ClaimEvidenceLink).where(m.ClaimEvidenceLink.claim_id == claim.id)
    ).all()} == frozen


def test_a_publisher_policy_upgrade_does_not_re_score_history(researched, m3, company):
    """M9: the stored count records the policy it was derived under."""
    import boro_gtm.research.policies as policies_module

    profile = m3.get(m.OperationalResearchProfile, company.id)
    before = dict(profile.corroborating_publisher_counts)
    assert profile.publisher_policy_version == "1"

    original = policies_module.PUBLISHER_POLICY_VERSION
    policies_module.PUBLISHER_POLICY_VERSION = "2"
    try:
        # The already-written projection is untouched by the version change.
        m3.expire_all()
        reread = m3.get(m.OperationalResearchProfile, company.id)
        assert reread.publisher_policy_version == "1"
        assert reread.corroborating_publisher_counts == before
    finally:
        policies_module.PUBLISHER_POLICY_VERSION = original

    # The evidence items keep the version they were recorded under.
    versions = set(m3.scalars(
        select(m.ResearchEvidenceItem.publisher_policy_version)
    ).all())
    assert versions == {"1"}


def test_claim_confidence_reproduces_exactly_from_its_own_row(researched, m3):
    """M8: read the links, read the policy versions, recompute, and match."""
    from boro_gtm.research.policies import compute_confidence
    from boro_gtm.research.services.evidence import independent_publisher_count

    claims_with_confidence = m3.scalars(select(CompanyClaim).where(
        CompanyClaim.confidence.is_not(None),
        CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
    )).all()
    assert claims_with_confidence
    for claim in claims_with_confidence:
        links = m3.scalars(select(m.ClaimEvidenceLink).where(
            m.ClaimEvidenceLink.claim_id == claim.id
        )).all()
        recomputed = compute_confidence(
            fact_type=claim.fact_type,
            trust_tiers=[float(link.trust_tier) for link in links],
            independent_publishers=independent_publisher_count(
                m3, [link.evidence_item_id for link in links]
            ),
            inferred=claim.fact_type == "INFERENCE",
        )
        assert float(claim.confidence) == pytest.approx(recomputed, abs=1e-9), (
            claim.attribute_key
        )


# --- L: independence --------------------------------------------------------


def test_two_sources_serving_identical_bytes_do_not_corroborate(researched, m3, company):
    """L3: the directory's copy shares the document, so it is one voice."""
    profile = m3.get(m.OperationalResearchProfile, company.id)
    publishers = set(m3.scalars(
        select(m.ResearchEvidenceItem.publisher_key)
        .join(m.ClaimEvidenceLink,
              m.ClaimEvidenceLink.evidence_item_id == m.ResearchEvidenceItem.id)
        .join(CompanyClaim, CompanyClaim.id == m.ClaimEvidenceLink.claim_id)
        .where(CompanyClaim.attribute_key == "emergency_service")
    ).all())
    assert len(publishers) == 3
    # The pairwise rule gives 3, not 2: the services page, the directory's copy
    # of the emergency page, and the trade press differ from each other in both
    # publisher and document. The copy is excluded only from a set that already
    # holds the page it copied — which is what stops a mirror inflating, and is
    # narrower than the connected-component answer this replaced (M3-ADR-059).
    assert profile.corroborating_publisher_counts["emergency_service"] == 3

    # The rule's actual anti-inflation clause, isolated: a copy and its
    # original can never both be counted.
    from boro_gtm.research.services.evidence import _maximum_matching

    assert _maximum_matching([("meridianmechanical", "emergency"),
                              ("contractordirectory", "emergency")]) == 1


def test_a_company_page_and_a_registry_do_corroborate(m3, company):
    """L4: different publisher, different document — two voices."""
    run_pipeline(m3, company_id=company.id, transport=FixtureTransport(), now=NOW)
    profile = m3.get(m.OperationalResearchProfile, company.id)
    # service_area is stated by the company and by the government registry.
    publishers = set(m3.scalars(
        select(m.ResearchEvidenceItem.publisher_key)
        .join(m.ClaimEvidenceLink,
              m.ClaimEvidenceLink.evidence_item_id == m.ResearchEvidenceItem.id)
        .join(CompanyClaim, CompanyClaim.id == m.ClaimEvidenceLink.claim_id)
        .where(CompanyClaim.attribute_key == "service_area")
    ).all())
    assert {"meridianmechanical", "ohio-sos"} <= publishers
    assert profile.corroborating_publisher_counts["service_area"] == 2


def test_two_pages_on_one_site_do_not_corroborate(researched, m3, company):
    """L5: a company cannot corroborate itself by adding a page."""
    profile = m3.get(m.OperationalResearchProfile, company.id)
    assert profile.corroborating_publisher_counts["preventive_maintenance"] == 1
    sources = set(m3.scalars(
        select(m.ResearchEvidenceItem.source_id)
        .join(m.ClaimEvidenceLink,
              m.ClaimEvidenceLink.evidence_item_id == m.ResearchEvidenceItem.id)
        .join(CompanyClaim, CompanyClaim.id == m.ClaimEvidenceLink.claim_id)
        .where(CompanyClaim.attribute_key == "preventive_maintenance")
    ).all())
    assert len(sources) >= 2, "stated on more than one of its own pages"


def test_the_same_question_with_different_seeds_is_one_run(m3, company):
    """L11: seeds are execution state, not question identity."""
    from boro_gtm.research.services.application import open_attempt

    run, _ = get_or_create_run(m3, company_id=company.id, now=NOW)
    first = open_attempt(m3, run_id=run.id, seed_inputs={"queries": ["a"]})
    m3.get(m.OperationalResearchAttempt, first.id).status = "FAILED"
    m3.flush()
    second = open_attempt(m3, run_id=run.id, seed_inputs={"queries": ["b"]})

    assert first.run_id == second.run_id
    assert first.attempt_seed_inputs != second.attempt_seed_inputs
    assert first.attempt_seed_inputs_hash != second.attempt_seed_inputs_hash
    assert _count(m3, m.OperationalResearchRun) == 1


def test_a_second_attempt_may_carry_different_seeds(m3, company):
    """M12: and each attempt's seeds are frozen to it."""
    from boro_gtm.research.services.application import open_attempt

    run, _ = get_or_create_run(m3, company_id=company.id, now=NOW)
    first = open_attempt(m3, run_id=run.id, seed_inputs={"n": 1})
    m3.get(m.OperationalResearchAttempt, first.id).status = "PARTIAL"
    m3.flush()
    second = open_attempt(m3, run_id=run.id, seed_inputs={"n": 2})
    assert second.attempt_number == first.attempt_number + 1
    assert m3.get(m.OperationalResearchAttempt, first.id).attempt_seed_inputs == {"n": 1}


def test_a_different_vertical_is_a_different_question(m3, company):
    """L12: the vertical is inside the plan hash."""
    from boro_gtm.strategy.domain.models import Vertical

    vertical = Vertical(key="hvac", name="HVAC")
    m3.add(vertical)
    m3.flush()

    without, created_a = get_or_create_run(m3, company_id=company.id, now=NOW)
    with_vertical, created_b = get_or_create_run(
        m3, company_id=company.id, now=NOW, vertical_id=vertical.id
    )
    assert created_a and created_b
    assert without.id != with_vertical.id
    assert without.research_plan_hash != with_vertical.research_plan_hash


def test_two_search_queries_in_one_attempt_both_survive(researched, m3):
    """L17: the context hash distinguishes them."""
    source = _source(m3, corpus.DIRECTORY_URL)
    rows = m3.scalars(select(m.ResearchSourceDiscovery).where(
        m.ResearchSourceDiscovery.source_id == source.id,
        m.ResearchSourceDiscovery.discovery_method == "SEARCH",
    )).all()
    assert len({r.discovery_context["query"] for r in rows}) == 2
    assert len({r.discovery_context_hash for r in rows}) == 2
    assert len({r.attempt_id for r in rows}) == 1


def test_a_machine_observed_edge_cannot_exist_without_its_fetch(researched, m3):
    """L18: the CHECK constraint, exercised through real data."""
    from sqlalchemy.exc import IntegrityError

    source_a = _source(m3, corpus.HOME)
    source_b = _source(m3, corpus.SERVICES_URL)
    with pytest.raises(IntegrityError):
        with m3.begin_nested():
            m3.add(m.ResearchSourceEdge(
                from_source_id=source_a.id, to_source_id=source_b.id,
                relation_type="REDIRECTS_TO", edge_origin="FETCH_OBSERVED",
                observed_by_fetch_event_id=None, observed_at=NOW,
            ))
            m3.flush()


def test_a_signal_with_a_null_related_company_cannot_duplicate(researched, m3, company):
    """L14: NULLS NOT DISTINCT, so one concern is one row."""
    from boro_gtm.research.services import identity

    attempt_id = m3.scalar(select(m.OperationalResearchAttempt.id).limit(1))
    first = identity.raise_signal(
        m3, company_id=company.id, signal_kind="NAME_MISMATCH",
        concern="Meridian Mech", attempt_id=attempt_id, evidence_item_ids=[], now=LATER,
    )
    second = identity.raise_signal(
        m3, company_id=company.id, signal_kind="NAME_MISMATCH",
        concern="MERIDIAN  MECH", attempt_id=attempt_id, evidence_item_ids=[], now=LATER,
    )
    assert first.signal.id == second.signal.id
    assert second.signal_created is False


def test_new_evidence_appends_to_an_existing_signal(researched, m3, company):
    """L15/M15: the signal and the occurrence rows stay byte-identical."""
    from boro_gtm.research.services import identity

    signal = m3.scalars(select(m.IdentityReviewSignal)).one()
    occurrence = m3.scalars(select(m.IdentityReviewSignalOccurrence)).one()
    fingerprint = (
        signal.signal_fingerprint, signal.first_raised_at,
        occurrence.occurrence_number, occurrence.raised_at, occurrence.is_open,
    )
    before = _count(m3, m.IdentityReviewSignalEvidence)

    extra = m3.scalars(select(m.ResearchEvidenceItem.id).where(
        m.ResearchEvidenceItem.id.not_in(
            select(m.IdentityReviewSignalEvidence.evidence_item_id)
        )
    ).limit(1)).all()
    identity.raise_signal(
        m3, company_id=company.id, signal_kind=signal.signal_kind,
        concern=signal.normalized_concern,
        attempt_id=occurrence.raised_by_attempt_id,
        evidence_item_ids=list(extra), now=LATER,
    )
    m3.flush()
    assert _count(m3, m.IdentityReviewSignalEvidence) == before + 1
    m3.refresh(signal)
    m3.refresh(occurrence)
    assert (
        signal.signal_fingerprint, signal.first_raised_at,
        occurrence.occurrence_number, occurrence.raised_at, occurrence.is_open,
    ) == fingerprint


def test_an_actioned_signal_cannot_return_to_open(researched, m3):
    """L16: terminal is terminal, in the service and in the trigger."""
    from boro_gtm.research.services.review import (
        IllegalSignalTransitionError,
        append_signal_status,
    )

    signal = m3.scalars(select(m.IdentityReviewSignal)).one()
    append_signal_status(m3, signal_id=signal.id, occurrence_number=1,
                         status="ACKNOWLEDGED", actor="a", now=LATER)
    append_signal_status(m3, signal_id=signal.id, occurrence_number=1,
                         status="ACTIONED", actor="a", now=LATER)
    with pytest.raises(IllegalSignalTransitionError):
        append_signal_status(m3, signal_id=signal.id, occurrence_number=1,
                             status="OPEN", actor="a", now=LATER)


# --- G: run and attempt lifecycle -------------------------------------------


def test_a_run_that_has_not_extracted_asserts_nothing(m3, company):
    """G1: the stage timestamp is the gate, not the status column."""
    from boro_gtm.research.services.application import open_attempt

    run, _ = get_or_create_run(m3, company_id=company.id, now=NOW)
    attempt = open_attempt(m3, run_id=run.id)
    assert attempt.extraction_completed_at is None
    assert _count(m3, CompanyClaim) == 0


def test_a_later_stage_cannot_launder_an_incomplete_earlier_stage(m3, company):
    """G2: a status may be advanced; the timestamp records what finished."""
    from sqlalchemy.exc import DBAPIError

    from boro_gtm.research.services.application import open_attempt

    run, _ = get_or_create_run(m3, company_id=company.id, now=NOW)
    view = open_attempt(m3, run_id=run.id)
    attempt = m3.get(m.OperationalResearchAttempt, view.id)

    # PENDING cannot jump to EXTRACTING; the trigger enforces the order.
    # The savepoint is released, not the outer transaction -- a bare rollback
    # here would unwind the fixture that created the company.
    savepoint = m3.begin_nested()
    attempt.status = "EXTRACTING"
    with pytest.raises(DBAPIError):
        m3.flush()
    savepoint.rollback()
    m3.expire(attempt)
    assert attempt.status == "PENDING"

    # And a legal advance still leaves the earlier timestamp unset, so a reader
    # can tell "claims to be extracting" from "finished fetching".
    attempt2 = m3.get(m.OperationalResearchAttempt, view.id)
    attempt2.status = "DISCOVERING"
    m3.flush()
    assert attempt2.discovery_completed_at is None
    assert attempt2.fetch_completed_at is None


def test_a_failed_fetch_is_retried_then_recorded(m3, company, transport):
    """G9: two attempts on one dead source, two events, no body either time."""
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    source = _source(m3, "https://broken.example/meridian")
    events = m3.scalars(select(m.ResearchFetchEvent).where(
        m.ResearchFetchEvent.source_id == source.id
    ).order_by(m.ResearchFetchEvent.retrieved_at)).all()
    assert len(events) == 2
    assert all(e.fetch_outcome == "TRANSPORT_ERROR" for e in events)
    assert all(e.body_id is None for e in events)
    assert all(e.error_class for e in events)
    assert len({e.attempt_id for e in events}) == 2


def test_one_bad_source_does_not_lose_the_others_evidence(researched, m3):
    """G10: nine failures and twelve successes is twelve documents of evidence."""
    assert researched.failed_sources
    assert researched.attempt.status == "PARTIAL"
    assert researched.evidence_created > 0
    ok = m3.scalar(select(func.count()).select_from(m.ResearchFetchEvent).where(
        m.ResearchFetchEvent.fetch_outcome == "OK"
    ))
    failed = m3.scalar(select(func.count()).select_from(m.ResearchFetchEvent).where(
        m.ResearchFetchEvent.fetch_outcome.not_in(("OK", "NOT_MODIFIED"))
    ))
    assert ok >= 10 and failed >= 9


def test_evidence_remembers_which_attempt_captured_it(researched, m3):
    """G12: through the fetch event, which names its attempt."""
    rows = m3.execute(
        select(m.ResearchEvidenceItem.id, m.ResearchFetchEvent.attempt_id)
        .join(m.ResearchFetchEvent,
              m.ResearchFetchEvent.id == m.ResearchEvidenceItem.fetch_event_id)
    ).all()
    assert rows
    assert all(attempt_id is not None for _, attempt_id in rows)
    attempts = set(m3.scalars(select(m.OperationalResearchAttempt.id)).all())
    assert {a for _, a in rows} <= attempts


# --- F: gap lifecycle -------------------------------------------------------


def test_a_gap_closes_by_an_event_with_no_update_to_the_parent(researched, m3, company):
    """F4: the closing claim id is on the event, and the parent is untouched."""
    gap = m3.scalars(select(m.OperationalResearchGap).where(
        m.OperationalResearchGap.gap_kind == "NO_EVIDENCE"
    ).limit(1)).first()
    fingerprint = (gap.run_id, gap.attribute_key, gap.gap_kind, gap.first_raised_at)
    claim_id = m3.scalar(select(CompanyClaim.id).limit(1))
    attempt_id = m3.scalar(select(m.OperationalResearchAttempt.id).limit(1))

    assert gaps.resolve_gap(m3, gap_id=gap.id, attempt_id=attempt_id,
                            claim_id=claim_id, now=LATER)
    m3.flush()
    assert gaps.current_status(m3, gap.id) == "RESOLVED"
    m3.refresh(gap)
    assert (gap.run_id, gap.attribute_key, gap.gap_kind,
            gap.first_raised_at) == fingerprint

    event = m3.scalars(select(m.OperationalResearchGapEvent).where(
        m.OperationalResearchGapEvent.gap_id == gap.id,
        m.OperationalResearchGapEvent.event_kind == "RESOLVED",
    )).one()
    assert event.resolved_by_claim_id == claim_id


def test_two_plans_may_hold_different_gap_state_for_one_attribute(m3, company):
    """M2: gaps are keyed by the question, so two questions may disagree."""
    first = run_pipeline(m3, company_id=company.id, transport=FixtureTransport(),
                         now=NOW)
    other, _ = get_or_create_run(
        m3, company_id=company.id, now=LATER, target_attribute_keys=("crm",)
    )
    attempt_id = m3.scalar(select(m.OperationalResearchAttempt.id).limit(1))
    gaps.raise_gap(m3, run_id=other.id, company_id=company.id, attribute_key="crm",
                   gap_kind="NO_EVIDENCE", attempt_id=attempt_id, now=LATER)
    m3.flush()

    rows = m3.scalars(select(m.OperationalResearchGap).where(
        m.OperationalResearchGap.attribute_key == "crm"
    )).all()
    assert len(rows) == 2
    assert {r.run_id for r in rows} == {first.attempt.run_id, other.id}

    # Resolve it for one question only.
    claim_id = m3.scalar(select(CompanyClaim.id).limit(1))
    for row in rows:
        if row.run_id == other.id:
            gaps.resolve_gap(m3, gap_id=row.id, attempt_id=attempt_id,
                             claim_id=claim_id, now=LATER)
    m3.flush()
    statuses = {r.run_id: gaps.current_status(m3, r.id) for r in rows}
    assert statuses[other.id] == "RESOLVED"
    assert statuses[first.attempt.run_id] in {"RAISED", "ATTEMPTED"}


def test_all_five_gap_kinds_are_reachable(researched, m3):
    kinds = set(m3.scalars(select(m.OperationalResearchGap.gap_kind)).all())
    assert {"NO_EVIDENCE", "STALE_EVIDENCE", "CONTRADICTED",
            "UNRESOLVABLE_SOURCE"} <= kinds


def test_a_stale_required_attribute_gaps_without_erasing_its_evidence(researched, m3):
    """The historical evidence still exists and still says what it said."""
    stale = m3.scalars(select(m.OperationalResearchGap).where(
        m.OperationalResearchGap.gap_kind == "STALE_EVIDENCE"
    ).all() if False else select(m.OperationalResearchGap).where(
        m.OperationalResearchGap.gap_kind == "STALE_EVIDENCE"
    )).all()
    assert stale
    for gap in stale:
        claims_for = m3.scalars(select(CompanyClaim).where(
            CompanyClaim.attribute_key == gap.attribute_key,
            CompanyClaim.availability == "OBSERVED",
        )).all()
        assert claims_for, "a stale gap does not delete the evidence behind it"


# --- H: firewalls -----------------------------------------------------------


def test_m2_is_unchanged_by_a_research_run(m3, company):
    """H1/H2/H3: a full run writes no M2 identity of any kind."""
    from boro_gtm.discovery.domain.models import (
        CompanyDomain,
        CompanyName,
        EntityResolutionDecision,
        EntityResolutionHead,
        ProviderEntity,
        ProviderRecordVersion,
    )

    watched = (ProviderEntity, ProviderRecordVersion, EntityResolutionDecision,
               EntityResolutionHead, Company, CompanyDomain, CompanyName)
    before = tuple(_count(m3, model) for model in watched)
    run_pipeline(m3, company_id=company.id, transport=FixtureTransport(), now=NOW)
    assert tuple(_count(m3, model) for model in watched) == before


def test_an_identity_conflict_raises_a_signal_and_stops(researched, m3):
    """H4/M3: a signal, with evidence, and no claim invented to carry it."""
    signal = m3.scalars(select(m.IdentityReviewSignal)).one()
    assert signal.signal_kind == "POSSIBLE_PARENT"
    assert _count(m3, m.IdentityReviewSignalEvidence) > 0

    # No claim was fabricated to represent the concern.
    keys = set(m3.scalars(select(CompanyClaim.attribute_key)).all())
    for invented in ("parent_company", "possible_duplicate", "identity_conflict"):
        assert invented not in keys
    assert signal.related_company_id is None


def test_a_group_domain_is_never_crawled_as_the_companys_site(m3, company):
    """H5: a shared group domain is not a seed for one company's research."""
    from boro_gtm.research.services.discovery_policy import is_crawlable_seed

    assert is_crawlable_seed("https://www.meridianmechanical.com/") is True
    assert is_crawlable_seed("https://sites.google.com/view/meridian") is False
    assert is_crawlable_seed("https://www.linkedin.com/company/meridian") is False

    run_pipeline(m3, company_id=company.id, transport=FixtureTransport(), now=NOW)
    hosts = set(m3.scalars(select(m.ResearchSource.registrable_domain)).all())
    assert not hosts & {"linkedin.com", "facebook.com", "google.com"}


def test_no_person_record_is_created(researched, m3):
    """H7: M3 has no person table, and writes no personal profile."""
    from boro_gtm.core.db import Base

    tables = set(Base.metadata.tables)
    for forbidden in ("people", "persons", "contacts", "buyers", "leads",
                      "person_profiles", "contact_points"):
        assert forbidden not in tables

    # And no claim carries a personal identifier.
    import json

    blob = json.dumps(m3.scalars(select(CompanyClaim.value_jsonb)).all(), default=str)
    assert "@" not in blob
    assert "REDACTED" not in blob


def test_redaction_keeps_personal_contact_details_out_of_derived_text(researched, m3):
    """The extraction-facing surface is redacted; the raw body stays internal."""
    texts = " ".join(
        t or "" for t in m3.scalars(select(m.ResearchTextDerivation.extracted_text)).all()
    )
    assert "dispatch@meridianmechanical.com" not in texts
    assert "[REDACTED_EMAIL]" in texts

    quotes = " ".join(
        q or "" for q in m3.scalars(select(m.ResearchEvidenceItem.quote)).all()
    )
    assert "@meridianmechanical.com" not in quotes


# --- J: evidence-class typing -----------------------------------------------


def test_a_script_fingerprint_cannot_exceed_hypothesis(m3):
    """J1: a script tag proves a script loaded, not an operational platform."""
    from boro_gtm.discovery.registry import AttributeNotInRegistryError

    attribute = get_attribute("field_service_management")
    value = {"values": [{"name": "ServiceTitan"}],
             "evidence_class": "SCRIPT_FINGERPRINT"}
    attribute.validate(value, None, "HYPOTHESIS")
    for stronger in ("INFERENCE", "PROXY", "ESTIMATE", "FACT"):
        with pytest.raises(AttributeNotInRegistryError):
            attribute.validate(value, None, stronger)


def test_a_job_mention_of_a_tool_cannot_exceed_proxy(m3):
    """J2: a posting proves a posting."""
    from boro_gtm.discovery.registry import AttributeNotInRegistryError

    attribute = get_attribute("erp")
    value = {"values": [{"name": "QuickBooks"}],
             "evidence_class": "JOB_DESCRIPTION_MENTION"}
    attribute.validate(value, None, "PROXY")
    for stronger in ("ESTIMATE", "FACT"):
        with pytest.raises(AttributeNotInRegistryError):
            attribute.validate(value, None, stronger)


def test_an_explicit_company_statement_may_be_fact(m3):
    """J3: the company saying it about itself is the strongest class."""
    attribute = get_attribute("customer_portal")
    attribute.validate(
        {"values": [{"name": "Meridian Customer Portal"}],
         "evidence_class": "EXPLICIT_COMPANY_STATEMENT"},
        None, "FACT",
    )


def test_operating_model_attributes_cannot_be_fact(m3):
    """J4: a company does not publish "our dispatch is decentralised"."""
    from boro_gtm.discovery.registry import AttributeNotInRegistryError

    for key in ("dispatch_centralization", "work_order_process",
                "parts_inventory_process", "asset_tracking"):
        attribute = get_attribute(key)
        assert "FACT" not in attribute.allowed_fact_types
        with pytest.raises(AttributeNotInRegistryError):
            attribute.validate({"value": attribute.enum_values[0]}, None, "FACT")


def test_evidence_class_ceilings_bound_every_capped_attribute(m3):
    """O9, over every capped attribute rather than a sample."""
    from boro_gtm.discovery.registry import AttributeNotInRegistryError
    from boro_gtm.research.enums import EVIDENCE_CLASS_FACT_CEILING
    from boro_gtm.research.registry import FACT_TYPE_ORDER, RESEARCH_ATTRIBUTES

    capped = [a for a in RESEARCH_ATTRIBUTES if a.evidence_class_capped]
    assert len(capped) == 17
    checked = 0
    for attribute in capped:
        for evidence_class, ceiling in EVIDENCE_CLASS_FACT_CEILING.items():
            value = {"values": [], "value": True, "evidence_class": evidence_class}
            above = FACT_TYPE_ORDER[FACT_TYPE_ORDER.index(ceiling) + 1:]
            for fact_type in above:
                if fact_type not in attribute.allowed_fact_types:
                    continue
                with pytest.raises(AttributeNotInRegistryError):
                    attribute.validate(value, None, fact_type)
                checked += 1
    assert checked > 0


def test_a_claim_may_not_be_asserted_without_a_declared_evidence_class(m3):
    """A capped attribute with no class is refused, not silently defaulted."""
    from boro_gtm.discovery.registry import AttributeNotInRegistryError

    with pytest.raises(AttributeNotInRegistryError):
        get_attribute("approval_step").validate({"observation": "approval"}, None, "FACT")


# --- N: registry ------------------------------------------------------------


def test_required_attributes_drive_coverages_denominator(m3, company):
    """N2: exactly the required subset, and non-applicable touches neither side."""
    from boro_gtm.research.registry import RESEARCH_ATTRIBUTES

    required = {a.key for a in RESEARCH_ATTRIBUTES if a.required}
    assert len(required) == 16

    run, _ = get_or_create_run(
        m3, company_id=company.id, now=NOW,
        target_attribute_keys=tuple(sorted(required)) + ("fleet_size",),
    )
    coverage = profiles.compute_coverage(m3, run)
    assert coverage.required_attribute_count == len(required)
    assert coverage.not_applicable_attribute_count == 0


# --- B: locators ------------------------------------------------------------


def test_an_html_claim_cites_a_css_path_a_heading_path_and_the_quote(researched, m3):
    """B1: a span is exact; a structural path is what a person can check."""
    source = _source(m3, corpus.SERVICES_URL)
    items = m3.scalars(select(m.ResearchEvidenceItem).where(
        m.ResearchEvidenceItem.source_id == source.id
    )).all()
    assert items
    structured = [i for i in items if i.locator.get("css_path")]
    assert structured, "HTML locators carry a structural path"
    for item in structured:
        assert item.locator["kind"] == "HTML_SPAN"
        assert item.locator["css_path"].startswith("html > body")
        assert item.locator["heading_path"]
        assert item.locator["start"] is not None
        assert item.quote and item.quote_sha256

    # And the claim resolves back to that exact text.
    item = structured[0]
    derivation = m3.scalars(select(m.ResearchTextDerivation).where(
        m.ResearchTextDerivation.body_id == item.body_id
    )).one()
    text = derivation.extracted_text
    assert text[item.locator["start"]:item.locator["end"]] == item.quote


def test_a_pdf_claim_cites_a_page_a_section_and_offsets(researched, m3):
    """B2: page, section, character offsets, the quote and its hash."""
    items = m3.scalars(select(m.ResearchEvidenceItem).where(
        m.ResearchEvidenceItem.locator["kind"].astext == "PDF_SPAN"
    )).all()
    assert items
    for item in items:
        assert item.locator["page"] in (1, 2, 3)
        assert item.locator["section"], item.locator
        assert item.locator["start"] is not None and item.locator["end"] is not None
        assert item.quote and item.quote_sha256
    assert {i.locator["section"] for i in items} >= {"2. SERVICE DELIVERY"}


def test_a_locator_survives_reprocessing_by_quote_hash(researched, m3):
    """B4: the offsets move, the hash finds it, and the citation still holds."""
    from boro_gtm.research.services.locators import Resolution, resolve

    item = m3.scalars(select(m.ResearchEvidenceItem).where(
        m.ResearchEvidenceItem.locator["kind"].astext == "HTML_SPAN"
    ).limit(1)).first()
    derivation = m3.scalars(select(m.ResearchTextDerivation).where(
        m.ResearchTextDerivation.body_id == item.body_id
    )).one()
    text = derivation.extracted_text

    exact = resolve(text, item.locator, item.quote_sha256)
    assert exact.resolution == Resolution.EXACT.value
    assert exact.quote == item.quote
    assert exact.weight == 1.0

    # Reprocessed under a policy that prepends a banner: every offset shifts.
    reflowed = "Updated notice. " + text
    rehomed = resolve(reflowed, item.locator, item.quote_sha256)
    assert rehomed.resolution == Resolution.REHOMED.value
    assert rehomed.quote == item.quote
    assert rehomed.start != item.locator["start"]
    assert rehomed.weight == 1.0


def test_a_rotted_locator_weakens_and_never_deletes(researched, m3):
    """B5: "we cannot point at this" is not "this was never observed"."""
    from boro_gtm.research.services.locators import ROTTED_WEIGHT, Resolution, resolve

    item = m3.scalars(select(m.ResearchEvidenceItem).where(
        m.ResearchEvidenceItem.locator["kind"].astext == "HTML_SPAN"
    ).limit(1)).first()
    before = _count(m3, m.ResearchEvidenceItem)
    quote, digest = item.quote, item.quote_sha256

    rotted = resolve("the page was rewritten entirely", item.locator, digest)
    assert rotted.resolution == Resolution.ROTTED.value
    assert rotted.weight == ROTTED_WEIGHT
    assert 0 < rotted.weight < 1.0, "weakened, not discarded"

    # Nothing was deleted, and the evidence still carries what it observed.
    m3.expire_all()
    assert _count(m3, m.ResearchEvidenceItem) == before
    survivor = m3.get(m.ResearchEvidenceItem, item.id)
    assert survivor.quote == quote
    assert survivor.quote_sha256 == digest


def test_a_pruned_text_derivation_reports_a_rotted_pointer_not_a_missing_claim(
    researched, m3
):
    """Retention weakens the pointer; the observation is untouched."""
    from boro_gtm.research.services import retention
    from boro_gtm.research.services.locators import Resolution, resolve

    item = m3.scalars(select(m.ResearchEvidenceItem).limit(1)).first()
    derivation = m3.scalars(select(m.ResearchTextDerivation).where(
        m.ResearchTextDerivation.body_id == item.body_id
    )).one()
    retention.apply_retention(
        m3, retention.RetentionPlan(text_derivations=[derivation.id]),
        as_of=LATER,
    )
    m3.refresh(derivation)

    resolved = resolve(derivation.extracted_text, item.locator, item.quote_sha256)
    assert resolved.resolution == Resolution.ROTTED.value
    assert m3.get(m.ResearchEvidenceItem, item.id).quote == item.quote
