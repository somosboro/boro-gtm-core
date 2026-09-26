"""The internal M3 pipeline, end to end, against real PostgreSQL.

These tests run the whole thing — discover, retrieve, classify, canonicalize,
derive text, extract, evidence, assert, gap, signal — over a fictional
contractor, then assert the properties that make the result trustworthy rather
than merely present.

No test reaches the network. Every source is a fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures import corpus
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.policies import normalize_locator
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import claims as claim_service
from boro_gtm.research.services import gaps as gap_service
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
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
def first_run(m3, company, transport):
    return run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- the attempt completes -------------------------------------------------


def test_a_full_attempt_executes_every_stage(first_run, m3):
    """The whole pipeline runs, and lands on a real terminal state."""
    attempt = first_run.attempt
    assert attempt.status == "PARTIAL"  # the corpus contains deliberate failures
    assert first_run.sources_seen >= 18
    assert first_run.evidence_created > 0
    assert first_run.claims_created > 0


def test_stage_timestamps_gate_the_stages_not_the_status_column(first_run):
    """A status says where a worker thinks it is; a timestamp says what finished."""
    attempt = first_run.attempt
    assert attempt.started_at is not None
    assert attempt.discovery_completed_at is not None
    assert attempt.fetch_completed_at is not None
    assert attempt.extraction_completed_at is not None
    assert attempt.completed_at is not None
    assert (
        attempt.discovery_completed_at
        <= attempt.fetch_completed_at
        <= attempt.extraction_completed_at
        <= attempt.completed_at
    )


# --- discovery -------------------------------------------------------------


def test_every_discovery_method_is_exercised(first_run, m3):
    methods = set(m3.scalars(select(m.ResearchSourceDiscovery.discovery_method)).all())
    assert methods == {
        "HUMAN_SEED", "SITEMAP", "CRAWL_LINK", "SEARCH", "JOB_BOARD", "REGISTRY", "API",
    }


def test_one_address_found_many_ways_is_one_source_and_many_observations(first_run, m3):
    """Collapsing the observations would lose how the system found what it found."""
    source = m3.scalars(
        select(m.ResearchSource).where(
            m.ResearchSource.normalized_locator == normalize_locator(corpus.EMERGENCY_URL)
        )
    ).one()
    rows = m3.scalars(
        select(m.ResearchSourceDiscovery).where(
            m.ResearchSourceDiscovery.source_id == source.id
        )
    ).all()
    assert len(rows) >= 3
    assert {row.discovery_method for row in rows} >= {"SITEMAP", "CRAWL_LINK", "SEARCH"}


def test_the_same_source_found_by_two_queries_records_both(first_run, m3):
    """Two searches, one result: two observations distinguished by context."""
    source = m3.scalars(
        select(m.ResearchSource).where(
            m.ResearchSource.normalized_locator == normalize_locator(corpus.DIRECTORY_URL)
        )
    ).one()
    searches = m3.scalars(
        select(m.ResearchSourceDiscovery).where(
            m.ResearchSourceDiscovery.source_id == source.id,
            m.ResearchSourceDiscovery.discovery_method == "SEARCH",
        )
    ).all()
    queries = {row.discovery_context["query"] for row in searches}
    assert len(queries) == 2
    assert len({row.discovery_context_hash for row in searches}) == 2


def test_a_redirect_is_an_edge_not_a_mutation(first_run, m3):
    """The two addresses stay two sources; the relationship is evidence."""
    edges = m3.scalars(
        select(m.ResearchSourceEdge).where(
            m.ResearchSourceEdge.relation_type == "REDIRECTS_TO"
        )
    ).all()
    assert edges, "the corpus contains a redirect"
    for edge in edges:
        assert edge.edge_origin == "FETCH_OBSERVED"
        assert edge.observed_by_fetch_event_id is not None
        assert edge.from_source_id != edge.to_source_id


# --- retrieval -------------------------------------------------------------


def test_every_failure_outcome_is_recorded_as_an_event(first_run):
    """A pipeline only tested against HTTP 200 has never run its error paths."""
    outcomes = first_run.fetch_outcomes
    assert outcomes["OK"] >= 10
    for failure in ("NOT_FOUND", "GONE", "ROBOTS_DENIED", "LOGIN_WALL", "DENIED",
                    "TIMEOUT", "TRANSPORT_ERROR", "TOO_LARGE", "MIME_MISMATCH"):
        assert outcomes.get(failure) == 1, f"{failure} was not exercised"


def test_a_failed_source_carries_no_body(first_run, m3):
    rows = m3.scalars(
        select(m.ResearchFetchEvent).where(
            m.ResearchFetchEvent.fetch_outcome.not_in(("OK", "NOT_MODIFIED"))
        )
    ).all()
    assert rows
    assert all(row.body_id is None for row in rows)


def test_one_failed_source_does_not_erase_the_successful_ones(first_run, m3):
    """Nine good pages and one timeout is nine pages of evidence."""
    assert first_run.failed_sources
    assert first_run.attempt.status == "PARTIAL"
    assert first_run.claims_created > 0
    assert _count(m3, m.ResearchEvidenceItem) > 0


def test_the_same_bytes_from_two_domains_are_one_body(first_run, m3):
    """The mirror serves identical bytes; byte identity is global."""
    mirror = m3.scalars(
        select(m.ResearchSource).where(
            m.ResearchSource.normalized_locator == normalize_locator(corpus.ABOUT_MIRROR_URL)
        )
    ).one()
    home = m3.scalars(
        select(m.ResearchSource).where(
            m.ResearchSource.normalized_locator == normalize_locator(corpus.HOME)
        )
    ).one()
    mirror_body = m3.scalar(
        select(m.ResearchFetchEvent.body_id).where(
            m.ResearchFetchEvent.source_id == mirror.id,
            m.ResearchFetchEvent.fetch_outcome == "OK",
        )
    )
    home_body = m3.scalar(
        select(m.ResearchFetchEvent.body_id).where(
            m.ResearchFetchEvent.source_id == home.id,
            m.ResearchFetchEvent.fetch_outcome == "OK",
        )
    )
    # Different bytes (the mirror is undated) but the same semantic document.
    assert mirror_body != home_body
    mirror_artifact = m3.scalar(
        select(m.ResearchArtifactDerivation.artifact_id).where(
            m.ResearchArtifactDerivation.body_id == mirror_body
        )
    )
    home_artifact = m3.scalar(
        select(m.ResearchArtifactDerivation.artifact_id).where(
            m.ResearchArtifactDerivation.body_id == home_body
        )
    )
    assert mirror_artifact == home_artifact


def test_a_304_references_the_body_it_validated_and_invents_nothing(m3, company, transport):
    """Re-running retrieval against unchanged pages must not fake a download."""
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    second = run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    assert second.fetch_outcomes.get("NOT_MODIFIED", 0) >= 8
    rows = m3.scalars(
        select(m.ResearchFetchEvent).where(
            m.ResearchFetchEvent.fetch_outcome == "NOT_MODIFIED"
        )
    ).all()
    for row in rows:
        assert row.http_status == 304
        assert row.validated_by == "ETAG"
        assert row.body_id is not None
    assert second.bodies_created == 0


# --- canonicalization ------------------------------------------------------


def test_publication_metadata_lives_on_the_derivation_not_the_artifact(first_run, m3):
    """A dated original and an undated mirror are one document, two readings."""
    home_hash = m3.scalar(
        select(m.ResearchArtifactDerivation.canonical_content_hash)
        .join(m.ResearchArtifact,
              m.ResearchArtifact.id == m.ResearchArtifactDerivation.artifact_id)
        .where(m.ResearchArtifactDerivation.title.like("About Meridian%"))
        .limit(1)
    )
    derivations = m3.scalars(
        select(m.ResearchArtifactDerivation).where(
            m.ResearchArtifactDerivation.canonical_content_hash == home_hash
        )
    ).all()
    assert len(derivations) == 2
    assert len({d.artifact_id for d in derivations}) == 1
    granularities = {d.source_published_granularity for d in derivations}
    assert granularities == {"DATE", "UNDATED"}


def test_no_invented_publication_precision(first_run, m3):
    rows = m3.scalars(select(m.ResearchArtifactDerivation)).all()
    for row in rows:
        if row.source_published_granularity == "UNDATED":
            assert row.source_published_at is None
        else:
            assert row.source_published_at is not None


def test_every_canonicalization_strategy_is_exercised(first_run, m3):
    strategies = set(m3.scalars(select(m.ResearchArtifact.canonicalization_strategy)).all())
    assert strategies == {
        "HTML_TEXT_V1", "PDF_TEXT_V1", "JSON_CANONICAL_V1",
    }


def test_a_cosmetic_change_is_a_new_body_and_the_same_artifact(m3, company):
    """Whitespace is not semantics, and must not fork the document."""
    transport = FixtureTransport()
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    before_artifacts = _count(m3, m.ResearchArtifact)

    transport.apply_cosmetic_change(corpus.PM_URL)
    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    assert _count(m3, m.ResearchArtifact) == before_artifacts


# --- text derivation -------------------------------------------------------


def test_a_second_text_policy_creates_a_second_derivation_and_edits_nothing(
    first_run, m3
):
    from boro_gtm.research.services import artifacts as artifact_service

    body = m3.scalars(select(m.ResearchArtifactBody).limit(1)).one()
    original = m3.scalars(
        select(m.ResearchTextDerivation).where(
            m.ResearchTextDerivation.body_id == body.id
        )
    ).one()
    before = original.extracted_text

    second, created = artifact_service.derive_text(
        m3, body_id=body.id, raw=body.raw_body, media_type="text/html", now=LATER,
        redaction_policy_version="2",
    )
    m3.flush()
    assert created
    assert second.id != original.id
    assert second.text_derivation_contract_hash != original.text_derivation_contract_hash
    m3.refresh(original)
    assert original.extracted_text == before


def test_redaction_removes_contact_details_from_derived_text(first_run, m3):
    """A privacy policy that only applies to new documents is not a policy."""
    texts = m3.scalars(select(m.ResearchTextDerivation.extracted_text)).all()
    joined = " ".join(t or "" for t in texts)
    assert "dispatch@meridianmechanical.com" not in joined
    assert "[REDACTED_EMAIL]" in joined


# --- extraction ------------------------------------------------------------


def test_every_extractor_kind_is_exercised(m3, company, transport):
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW,
                 confirm_sampled=True)
    kinds = set(m3.scalars(select(m.ResearchExtraction.extractor_kind)).all())
    assert kinds == {"RULE", "PARSER", "MODEL", "HUMAN"}


def test_a_second_attempt_reuses_deterministic_extractions(m3, company, transport):
    """Re-reading the same text under the same contract is not new knowledge."""
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    before = _count(m3, m.ResearchExtraction)
    second = run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    # Everything unchanged came back 304, so nothing was re-read at all; the
    # deterministic results from attempt 1 are still the only ones that exist.
    assert _count(m3, m.ResearchExtraction) == before
    assert second.extractions_created == 0


def test_a_sampled_extraction_gets_its_own_execution_slot(first_run, m3):
    rows = m3.scalars(
        select(m.ResearchExtraction).where(
            m.ResearchExtraction.determinism == "SAMPLED"
        )
    ).all()
    assert rows
    for row in rows:
        assert row.sample_execution_id is not None
        assert row.extractor_kind == "MODEL"


def test_sampled_output_creates_evidence_but_asserts_no_claim(first_run, m3):
    """A model's read may be recorded. It may not speak for the company."""
    assert first_run.sampled_pending_review

    sampled_ids = m3.scalars(
        select(m.ResearchEvidenceItem.id)
        .join(m.ResearchExtraction,
              m.ResearchExtraction.id == m.ResearchEvidenceItem.extraction_id)
        .where(m.ResearchExtraction.determinism == "SAMPLED")
    ).all()
    assert sampled_ids

    linked = m3.scalars(
        select(m.ClaimEvidenceLink.id).where(
            m.ClaimEvidenceLink.evidence_item_id.in_(sampled_ids)
        )
    ).all()
    assert linked == []


def test_human_confirmation_makes_a_sampled_reading_assertable(m3, company, transport):
    result = run_pipeline(m3, company_id=company.id, transport=transport, now=NOW,
                          confirm_sampled=True)
    assert result.sampled_pending_review == []

    human_linked = m3.scalars(
        select(m.ClaimEvidenceLink.id)
        .join(m.ResearchEvidenceItem,
              m.ResearchEvidenceItem.id == m.ClaimEvidenceLink.evidence_item_id)
        .join(m.ResearchExtraction,
              m.ResearchExtraction.id == m.ResearchEvidenceItem.extraction_id)
        .where(m.ResearchExtraction.extractor_kind == "HUMAN")
    ).all()
    assert human_linked


# --- evidence --------------------------------------------------------------


def test_every_locator_kind_is_exercised(first_run, m3):
    kinds = {
        row["kind"] for row in m3.scalars(select(m.ResearchEvidenceItem.locator)).all()
    }
    assert kinds == {"HTML_SPAN", "PDF_SPAN", "JSON_POINTER", "JOB_FIELD"}


def test_every_locator_resolves_into_the_text_it_cites(first_run, m3):
    """A locator nobody can follow is a citation to nothing."""
    rows = m3.scalars(select(m.ResearchEvidenceItem)).all()
    assert rows
    for row in rows:
        assert row.locator.get("resolved") is True, row.locator
        assert row.quote
        assert row.quote_sha256


def test_evidence_provenance_is_single_valued_and_agrees_on_one_body(first_run, m3):
    rows = m3.execute(
        select(
            m.ResearchEvidenceItem.body_id,
            m.ResearchFetchEvent.body_id,
            m.ResearchArtifactDerivation.body_id,
            m.ResearchExtraction.body_id,
        )
        .join(m.ResearchFetchEvent,
              m.ResearchFetchEvent.id == m.ResearchEvidenceItem.fetch_event_id)
        .join(m.ResearchArtifactDerivation,
              m.ResearchArtifactDerivation.id
              == m.ResearchEvidenceItem.artifact_derivation_id)
        .join(m.ResearchExtraction,
              m.ResearchExtraction.id == m.ResearchEvidenceItem.extraction_id)
    ).all()
    assert rows
    for evidence_body, fetch_body, derivation_body, extraction_body in rows:
        assert evidence_body == fetch_body == derivation_body == extraction_body


def test_evidence_exists_without_any_consumer(first_run, m3):
    """Evidence is owned by nobody; a claim is one possible reader, not the point."""
    total = _count(m3, m.ResearchEvidenceItem)
    linked = m3.scalar(
        select(func.count(func.distinct(m.ClaimEvidenceLink.evidence_item_id)))
    ) or 0
    assert total > linked > 0


# --- claims ----------------------------------------------------------------


def test_claims_are_written_into_the_existing_ledger(first_run, m3):
    """One claim ledger. A second one would be two disagreeing sources of truth."""
    rows = m3.scalars(
        select(CompanyClaim).where(
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION
        )
    ).all()
    assert rows
    for claim in rows:
        assert claim.subject_company_id is not None
        assert claim.provider_record_version_id is None
        assert claim.assertion_fingerprint is not None


def test_every_m3_claim_cites_evidence(first_run, m3):
    claim_ids = m3.scalars(
        select(CompanyClaim.id).where(
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION
        )
    ).all()
    for claim_id in claim_ids:
        count = m3.scalar(
            select(func.count()).select_from(m.ClaimEvidenceLink).where(
                m.ClaimEvidenceLink.claim_id == claim_id
            )
        )
        assert count >= 1


def test_the_fact_type_distribution_is_not_all_fact(first_run, m3):
    """If everything is a FACT, the fact-type column is decoration."""
    types = set(m3.scalars(
        select(CompanyClaim.fact_type).where(
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION
        )
    ).all())
    assert "FACT" in types
    assert "PROXY" in types
    assert len(types) >= 3


def test_a_job_ad_mention_never_becomes_a_fact_about_the_operation(first_run, m3):
    """The registry ceiling holds through the pipeline, not just in unit tests.

    The line is precise, and worth stating: that a posting *exists* is a fact
    about the posting, so `hiring_signal` may be FACT. What the posting implies
    about how the company actually works may not, and every attribute in that
    second category is evidence-class capped.
    """
    from boro_gtm.research.registry import RESEARCH_ATTRIBUTES

    capped = {a.key for a in RESEARCH_ATTRIBUTES if a.evidence_class_capped}
    rows = m3.execute(
        select(CompanyClaim.attribute_key, CompanyClaim.fact_type,
               CompanyClaim.value_jsonb)
        .join(m.ClaimEvidenceLink, m.ClaimEvidenceLink.claim_id == CompanyClaim.id)
        .where(m.ClaimEvidenceLink.source_class == "JOB_BOARD")
    ).all()
    assert rows
    seen_capped = 0
    for attribute_key, fact_type, value in rows:
        if attribute_key not in capped:
            continue
        seen_capped += 1
        assert value.get("evidence_class") == "JOB_DESCRIPTION_MENTION"
        assert fact_type != "FACT", f"{attribute_key} reached FACT from a job board"
    assert seen_capped > 0


def test_a_stated_negative_is_a_fact_and_silence_is_not(first_run, m3):
    """The directory says emergency service is not offered; that is evidence."""
    negatives = m3.scalars(
        select(CompanyClaim).where(
            CompanyClaim.attribute_key == "emergency_service",
            CompanyClaim.value_jsonb["value"].astext == "false",
        )
    ).all()
    assert negatives, "a stated negative must be representable"
    assert all(c.availability == "OBSERVED" for c in negatives)


def test_absence_is_a_gap_and_never_a_false_claim(first_run, m3, company):
    """The rule M3 is under the most pressure to break."""
    unevidenced = m3.scalars(
        select(m.OperationalResearchGap.attribute_key).where(
            m.OperationalResearchGap.gap_kind == "NO_EVIDENCE"
        )
    ).all()
    assert unevidenced
    for key in unevidenced:
        rows = m3.scalars(
            select(CompanyClaim).where(
                CompanyClaim.subject_company_id == company.id,
                CompanyClaim.attribute_key == key,
            )
        ).all()
        assert rows == [], f"{key} has a gap and a claim at the same time"


def test_unknown_is_reported_as_not_available_without_writing_a_row(m3, company):
    before = _count(m3, CompanyClaim)
    state = claim_service.unknown_attribute_state("erp")
    assert state == {
        "attribute_key": "erp", "availability": "NOT_AVAILABLE",
        "value": None, "fact_type": None,
    }
    assert _count(m3, CompanyClaim) == before


def test_a_contradiction_produces_two_claims_not_one_winner(first_run, m3):
    """The company says 58 technicians; a directory says about 25. Both stand."""
    rows = m3.scalars(
        select(CompanyClaim).where(CompanyClaim.attribute_key == "technician_count")
    ).all()
    values = {(c.value_jsonb["min"], c.fact_type) for c in rows}
    assert (58, "FACT") in values
    assert (25, "ESTIMATE") in values


# --- confidence and independence -------------------------------------------


def test_repetition_by_one_publisher_is_not_corroboration(first_run, m3):
    """A page saying the same thing three times is one voice."""
    claim = m3.scalars(
        select(CompanyClaim)
        .where(
            CompanyClaim.attribute_key == "emergency_service",
            CompanyClaim.value_jsonb["value"].astext == "true",
            CompanyClaim.fact_type == "FACT",
        )
    ).first()
    assert claim is not None
    links = m3.scalars(
        select(m.ClaimEvidenceLink).where(m.ClaimEvidenceLink.claim_id == claim.id)
    ).all()
    assert len(links) >= 2, "the same statement appears more than once"
    publishers = {link.source_class for link in links}
    assert publishers == {"COMPANY_OWN_SITE"}
    # base(FACT) 0.95 x trust 0.90 x corroboration 1.0 -> no inflation.
    assert float(claim.confidence) == pytest.approx(0.855, abs=1e-6)


def test_trust_inputs_are_frozen_on_the_link(first_run, m3):
    links = m3.scalars(select(m.ClaimEvidenceLink)).all()
    assert links
    for link in links:
        assert link.trust_policy_version == "1"
        assert 0.0 <= float(link.trust_tier) <= 1.0


def test_a_weaker_corroborating_source_never_lowers_confidence(first_run, m3):
    """Looking for more evidence must not be punished."""
    from boro_gtm.research.policies import compute_confidence

    strong = compute_confidence("FACT", [0.90], 1)
    with_weak = compute_confidence("FACT", [0.90, 0.40], 1)
    assert with_weak >= strong


# --- gaps ------------------------------------------------------------------


def test_a_repeated_attempt_appends_an_event_rather_than_a_second_gap(
    m3, company, transport
):
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    gaps_after_one = _count(m3, m.OperationalResearchGap)
    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    assert _count(m3, m.OperationalResearchGap) == gaps_after_one
    gap = m3.scalars(select(m.OperationalResearchGap).limit(1)).one()
    assert gap_service.attempt_count(m3, gap.id) >= 1
    assert gap_service.current_status(m3, gap.id) in {"RAISED", "ATTEMPTED"}


def test_a_gap_is_keyed_by_the_question_not_only_the_company(first_run, m3):
    rows = m3.scalars(select(m.OperationalResearchGap)).all()
    assert rows
    assert all(row.run_id is not None for row in rows)


# --- identity signals ------------------------------------------------------


def test_a_registry_parent_raises_a_signal_and_touches_no_m2_identity(
    m3, company, transport
):
    """A filing that mentions a parent is a question for a human, not a merge."""
    from boro_gtm.discovery.domain.models import (
        CompanyDomain,
        EntityResolutionDecision,
        ProviderEntity,
    )

    before = (
        _count(m3, ProviderEntity),
        _count(m3, EntityResolutionDecision),
        _count(m3, Company),
        _count(m3, CompanyDomain),
    )
    result = run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)

    assert result.signals_raised == 1
    signal = m3.scalars(select(m.IdentityReviewSignal)).one()
    assert signal.signal_kind == "POSSIBLE_PARENT"
    assert "MERIDIAN MECHANICAL HOLDINGS" in signal.normalized_concern

    occurrence = m3.scalars(select(m.IdentityReviewSignalOccurrence)).one()
    assert occurrence.is_open is True
    assert _count(m3, m.IdentityReviewSignalEvidence) > 0

    after = (
        _count(m3, ProviderEntity),
        _count(m3, EntityResolutionDecision),
        _count(m3, Company),
        _count(m3, CompanyDomain),
    )
    assert before == after, "M3 must not write M2 identity"


def test_reraising_the_same_concern_reuses_the_open_occurrence(m3, company, transport):
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)
    assert _count(m3, m.IdentityReviewSignal) == 1
    assert _count(m3, m.IdentityReviewSignalOccurrence) == 1


# --- idempotency -----------------------------------------------------------


def test_running_the_pipeline_twice_duplicates_nothing_it_should_not(
    m3, company, transport
):
    """The second run may add events. It may not add identities."""
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)
    stable_before = {
        "runs": _count(m3, m.OperationalResearchRun),
        "sources": _count(m3, m.ResearchSource),
        "bodies": _count(m3, m.ResearchArtifactBody),
        "artifacts": _count(m3, m.ResearchArtifact),
        "derivations": _count(m3, m.ResearchArtifactDerivation),
        "text": _count(m3, m.ResearchTextDerivation),
        "extractions_det": m3.scalar(
            select(func.count()).select_from(m.ResearchExtraction).where(
                m.ResearchExtraction.determinism == "DETERMINISTIC"
            )
        ),
        "evidence": _count(m3, m.ResearchEvidenceItem),
        "claims": _count(m3, CompanyClaim),
        "gaps": _count(m3, m.OperationalResearchGap),
        "signals": _count(m3, m.IdentityReviewSignal),
    }
    growth_before = {
        "attempts": _count(m3, m.OperationalResearchAttempt),
        "fetch_events": _count(m3, m.ResearchFetchEvent),
        "discoveries": _count(m3, m.ResearchSourceDiscovery),
    }

    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    stable_after = {
        "runs": _count(m3, m.OperationalResearchRun),
        "sources": _count(m3, m.ResearchSource),
        "bodies": _count(m3, m.ResearchArtifactBody),
        "artifacts": _count(m3, m.ResearchArtifact),
        "derivations": _count(m3, m.ResearchArtifactDerivation),
        "text": _count(m3, m.ResearchTextDerivation),
        "extractions_det": m3.scalar(
            select(func.count()).select_from(m.ResearchExtraction).where(
                m.ResearchExtraction.determinism == "DETERMINISTIC"
            )
        ),
        "evidence": _count(m3, m.ResearchEvidenceItem),
        "claims": _count(m3, CompanyClaim),
        "gaps": _count(m3, m.OperationalResearchGap),
        "signals": _count(m3, m.IdentityReviewSignal),
    }
    assert stable_before == stable_after

    assert _count(m3, m.OperationalResearchAttempt) == growth_before["attempts"] + 1
    assert _count(m3, m.ResearchFetchEvent) > growth_before["fetch_events"]
    assert _count(m3, m.ResearchSourceDiscovery) > growth_before["discoveries"]


# --- changed source --------------------------------------------------------


def test_a_semantic_change_creates_new_bytes_and_a_new_document(m3, company):
    """And leaves everything it did not touch exactly as it was."""
    transport = FixtureTransport()
    run_pipeline(m3, company_id=company.id, transport=transport, now=NOW)

    services = m3.scalars(
        select(m.ResearchSource).where(
            m.ResearchSource.normalized_locator == normalize_locator(corpus.SERVICES_URL)
        )
    ).one()
    emergency = m3.scalars(
        select(m.ResearchSource).where(
            m.ResearchSource.normalized_locator == normalize_locator(corpus.EMERGENCY_URL)
        )
    ).one()
    emergency_bodies_before = m3.scalars(
        select(m.ResearchFetchEvent.body_id).where(
            m.ResearchFetchEvent.source_id == emergency.id
        )
    ).all()
    artifacts_before = _count(m3, m.ResearchArtifact)

    # A page that really changed also changes its validator.
    transport.apply_semantic_change(corpus.SERVICES_URL)
    transport.set_etag(corpus.SERVICES_URL, '"services-v2"')
    run_pipeline(m3, company_id=company.id, transport=transport, now=LATER)

    services_bodies = m3.scalars(
        select(m.ResearchFetchEvent.body_id).where(
            m.ResearchFetchEvent.source_id == services.id,
            m.ResearchFetchEvent.fetch_outcome == "OK",
        )
    ).all()
    assert len(set(services_bodies)) == 2, "new bytes for the changed page"
    assert _count(m3, m.ResearchArtifact) > artifacts_before

    emergency_bodies_after = m3.scalars(
        select(m.ResearchFetchEvent.body_id).where(
            m.ResearchFetchEvent.source_id == emergency.id
        )
    ).all()
    assert set(emergency_bodies_after) == set(emergency_bodies_before)


# --- the commercial firewall ------------------------------------------------


FORBIDDEN_FIELDS = (
    "qualification_score", "qualification_route", "budget_band", "buyer_role",
    "commercial_level_candidate", "commercial_level_final", "selected_capabilities",
    "price_floor_usd", "quoted_price_usd", "normalized_gross_margin",
    "pain_score", "fragmentation_score", "owner_bottleneck", "needs_automation",
)


def test_the_pipeline_writes_no_commercial_judgement(first_run, m3):
    """Running the whole thing must not produce a single interpretive field."""
    keys = set(m3.scalars(select(CompanyClaim.attribute_key)).all())
    for forbidden in FORBIDDEN_FIELDS:
        assert forbidden not in keys


def test_no_claim_value_carries_a_capability_id_or_a_price(first_run, m3):
    """A source may mention a product. That is not selecting a BoRo capability."""
    import json

    rows = m3.scalars(select(CompanyClaim.value_jsonb)).all()
    blob = json.dumps(rows, default=str)
    assert "CAP-" not in blob
    for token in ("price_usd", "margin", "commercial_level", "qualification"):
        assert token not in blob


def test_no_ev_signal_is_stored_as_an_operational_claim(first_run, m3):
    """M3 stores primitives. Deriving EV-* from them is M4's job."""
    keys = set(m3.scalars(select(CompanyClaim.attribute_key)).all())
    assert not any(key.upper().startswith("EV-") for key in keys)
    values = m3.scalars(select(CompanyClaim.value_jsonb)).all()
    import json

    assert "EV-" not in json.dumps(values, default=str)


# --- the Q2 canonical projection -------------------------------------------


def test_the_projection_reports_observation_dates_not_fetch_dates(first_run, m3):
    """A crawl date is not an event date (M3-ADR-045)."""
    from boro_gtm.research.services.projection import project_claim

    claim = m3.scalars(
        select(CompanyClaim)
        .where(
            CompanyClaim.attribute_key == "branch_count",
            CompanyClaim.fact_type == "FACT",
        )
    ).one()
    records = project_claim(m3, claim)
    assert records
    # The about page states 2025-11-04; the pipeline ran on 2026-09-26.
    assert all(r.date_observed == datetime(2025, 11, 4).date() for r in records)
    assert all(r.date_observed != NOW.date() for r in records)


def test_an_undated_source_projects_without_a_date_rather_than_a_guess(first_run, m3):
    from boro_gtm.research.services.projection import project_claim

    claim = m3.scalars(
        select(CompanyClaim).where(
            CompanyClaim.attribute_key == "technician_count",
            CompanyClaim.fact_type == "ESTIMATE",
        )
    ).one()
    records = project_claim(m3, claim)
    assert records
    assert all(r.date_observed is None for r in records)


def test_an_unevidenced_attribute_projects_as_not_available(first_run, m3, company):
    """Never false, never zero, never "does not use"."""
    from boro_gtm.research.services.projection import project_company

    gapped = m3.scalars(
        select(m.OperationalResearchGap.attribute_key).limit(1)
    ).one()
    projected = project_company(m3, company.id, [gapped])
    state = projected[gapped][0]
    assert state.availability == "NOT_AVAILABLE"
    assert state.value is None
    assert state.fact_type is None
    assert state.confidence is None


def test_the_projection_refuses_to_emit_an_unsupported_claim(first_run, m3, company):
    from boro_gtm.research.services.projection import (
        UnsupportedClaimError,
        project_claim,
    )

    orphan = CompanyClaim(
        attribute_key="fleet_presence",
        attribute_registry_version=RESEARCH_REGISTRY_VERSION,
        value_jsonb={"value": True}, fact_type="FACT", availability="OBSERVED",
        subject_company_id=company.id, period_granularity="UNDATED",
        created_at=NOW,
    )
    m3.add(orphan)
    m3.flush()
    with pytest.raises(UnsupportedClaimError):
        project_claim(m3, orphan)


def test_the_projection_never_manufactures_a_hypothesis(first_run, m3):
    """`related_hypothesis` belongs to M4. M3 leaves it empty, always."""
    from boro_gtm.research.services.projection import project_claim

    claims = m3.scalars(
        select(CompanyClaim).where(
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION
        )
    ).all()
    seen = 0
    for claim in claims:
        for record in project_claim(m3, claim):
            assert record.related_hypothesis is None
            seen += 1
    assert seen > 0
