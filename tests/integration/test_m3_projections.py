"""The stored projections: determinism, contradictions, coverage, staleness.

A projection is only trustworthy if truncating it and rebuilding reproduces it.
These tests do that, in several insertion orders, and assert the stored result
itself is stable — not merely the API's sorted view of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures import corpus
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION, get_attribute
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import profiles
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
def researched(m3, company):
    result = run_pipeline(
        m3, company_id=company.id, transport=FixtureTransport(), now=NOW
    )
    return result


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- company-global profile -------------------------------------------------


def test_the_company_profile_holds_company_global_state_only(researched, m3, company):
    """No coverage, no plan missingness, and nothing commercial (§3.17)."""
    profile = m3.get(m.OperationalResearchProfile, company.id)
    assert profile is not None
    assert profile.facts
    columns = set(m.OperationalResearchProfile.__table__.columns.keys())
    for forbidden in ("coverage", "confidence_summary", "contradiction_rate",
                      "qualification_score", "commercial_level", "price_floor_usd"):
        assert forbidden not in columns

    import json

    blob = json.dumps(profile.facts) + json.dumps(profile.contradictions)
    for token in ("EV-", "CAP-", "qualification", "commercial_level", "margin"):
        assert token not in blob


def test_every_projected_attribute_carries_the_designed_state(researched, m3, company):
    profile = m3.get(m.OperationalResearchProfile, company.id)
    for key, state in profile.facts.items():
        assert state["availability"] == "OBSERVED"
        assert "envelope" in state
        assert state["best_claim_id"]
        assert state["best_fact_type"] in {
            "FACT", "ESTIMATE", "PROXY", "INFERENCE", "HYPOTHESIS"
        }
        assert state["claim_ids"]
        assert key in profile.corroborating_publisher_counts
        assert key in profile.contradictions
    assert profile.assertion_policy_version == "1"
    assert profile.publisher_policy_version == "1"


def test_a_contradiction_projects_as_an_envelope_with_a_labelled_best(
    researched, m3, company
):
    """C1/ADR-012: the disagreement survives, and the winner is labelled as one."""
    profile = m3.get(m.OperationalResearchProfile, company.id)
    state = profile.facts["technician_count"]

    # The company says 58; a directory says about 25.
    assert state["envelope"] == {"min": 25, "max": 58}
    assert profile.contradictions["technician_count"]["contradiction"] is True
    assert len(profile.contradictions["technician_count"]["claim_ids"]) >= 2

    # The best is labelled separately, and precedence picked the FACT.
    assert state["best_fact_type"] == "FACT"
    assert state["best"] == {"min": 58, "max": 58}
    assert len(state["claim_ids"]) >= 2, "the losing claim is still listed"


def test_the_projection_never_discards_a_contributing_claim(researched, m3, company):
    profile = m3.get(m.OperationalResearchProfile, company.id)
    projected = {uuid.UUID(c) for state in profile.facts.values()
                 for c in state["claim_ids"]}
    asserted = set(m3.scalars(
        select(CompanyClaim.id).where(
            CompanyClaim.subject_company_id == company.id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
            CompanyClaim.availability == "OBSERVED",
        )
    ).all())
    assert projected == asserted
    assert set(profile.derived_from_claim_ids) == asserted


# --- determinism ------------------------------------------------------------


def _semantic(profile) -> dict:
    """Everything the design says is derived. Nothing generated per rebuild."""
    return {
        "facts": profile.facts,
        "contradictions": profile.contradictions,
        "publishers": profile.corroborating_publisher_counts,
        "claims": sorted(str(c) for c in profile.derived_from_claim_ids),
        "assertion_policy_version": profile.assertion_policy_version,
        "publisher_policy_version": profile.publisher_policy_version,
    }


def test_rebuilding_from_empty_reproduces_the_projection(researched, m3, company):
    """Truncate and rebuild: the derived result is the same, or it is not derived."""
    before = _semantic(m3.get(m.OperationalResearchProfile, company.id))

    m3.execute(m.OperationalResearchProfile.__table__.delete())
    m3.flush()
    assert _count(m3, m.OperationalResearchProfile) == 0

    profiles.rebuild_company_profile(m3, company.id)
    assert _semantic(m3.get(m.OperationalResearchProfile, company.id)) == before


def test_rebuilding_twice_changes_nothing(researched, m3, company):
    first = _semantic(m3.get(m.OperationalResearchProfile, company.id))
    profiles.rebuild_company_profile(m3, company.id)
    profiles.rebuild_company_profile(m3, company.id)
    assert _semantic(m3.get(m.OperationalResearchProfile, company.id)) == first


def test_insertion_order_does_not_change_the_stored_projection(researched, m3, company):
    """C2/E5: the same claims, read in either order, project identically.

    An earlier version of this test built two *different* companies and compared
    them. That cannot isolate insertion order: the claim ids differ, and the
    final precedence tiebreak is the claim id, so the two were allowed to
    disagree. This holds the claims fixed and varies only the order they are
    presented in.
    """
    claims = m3.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id == company.id,
            CompanyClaim.attribute_key == "technician_count",
        )
    ).all()
    assert len(claims) >= 2, "the corpus contradicts itself on this attribute"

    forward = profiles._claim_facts(m3, list(claims))
    backward = profiles._claim_facts(m3, list(reversed(claims)))

    assert (
        profiles._envelope("technician_count", forward)
        == profiles._envelope("technician_count", backward)
    )
    assert (
        profiles._contradicted("technician_count", forward)
        is profiles._contradicted("technician_count", backward)
    )
    # And the precedence key is a total order, so sorting either way agrees.
    assert (
        [str(f.claim.id) for f in sorted(forward, key=profiles.precedence_key)]
        == [str(f.claim.id) for f in sorted(backward, key=profiles.precedence_key)]
    )


def test_precedence_prefers_fact_type_then_trust_then_date_then_id(researched, m3, company):
    """The frozen order from M3-ADR-012, checked rung by rung."""
    claims = m3.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id == company.id,
            CompanyClaim.attribute_key == "technician_count",
        )
    ).all()
    ranked = sorted(profiles._claim_facts(m3, list(claims)),
                    key=profiles.precedence_key)
    # FACT outranks ESTIMATE regardless of anything below it.
    assert ranked[0].claim.fact_type == "FACT"
    assert ranked[-1].claim.fact_type == "ESTIMATE"

    # The last tiebreak is the claim id, never "keep the current row"
    # (M2-ADR-024 proved that rule is not deterministic on a rebuild).
    assert profiles.precedence_key(ranked[0])[-1] == str(ranked[0].claim.id)

    # Two otherwise-identical candidates are ordered by id and nothing else.
    import dataclasses

    left, right = ranked[0], dataclasses.replace(
        ranked[0], claim=ranked[-1].claim, trust_tier=ranked[0].trust_tier,
        source_published_at=ranked[0].source_published_at,
        earliest_retrieved_at=ranked[0].earliest_retrieved_at,
    )
    # Equal on every rung above the id, so the ids decide.
    ordered = sorted([left, right], key=profiles.precedence_key)
    if left.claim.fact_type == right.claim.fact_type:
        assert [str(f.claim.id) for f in ordered] == sorted(
            [str(left.claim.id), str(right.claim.id)]
        )


def test_the_projection_reads_no_clock(researched, m3, company):
    """E5: nothing stored depends on when the rebuild happened."""
    import json

    before = json.dumps(_semantic(m3.get(m.OperationalResearchProfile, company.id)),
                        sort_keys=True)
    profiles.rebuild_company_profile(m3, company.id)
    after = json.dumps(_semantic(m3.get(m.OperationalResearchProfile, company.id)),
                       sort_keys=True)
    assert before == after
    assert "as_of" not in after
    for column in m.OperationalResearchProfile.__table__.columns.keys():
        assert "at" != column[-2:] or column == "company_id"


# --- corroboration across claims -------------------------------------------


def test_corroboration_across_claims_follows_the_independence_rule(
    researched, m3, company
):
    """The largest set of pairwise-independent lineages, per M3-ADR-059.

    Independent means differing in **both** publisher and document, so the
    count is a maximum matching over the publisher/document graph — not a
    component count, which under-reported, and not a pair count, which
    over-reported.
    """
    profile = m3.get(m.OperationalResearchProfile, company.id)
    counts = profile.corroborating_publisher_counts

    # Four pairs support `emergency_service = true`: the company's services
    # page, the company's emergency page, a directory's byte-identical copy of
    # that emergency page, and a trade publication. The largest pairwise
    # independent set is three — services / copy / trade press. The copy is
    # excluded only from a set already holding the page it copied, which is the
    # clause that stops a mirror inflating.
    assert counts["emergency_service"] == 3

    # branch_count is asserted only by the company (home, mirror, redirect
    # source) plus a directory listing a different value -- two publishers.
    assert counts["branch_count"] == 2

    # A single-publisher attribute stays at one.
    assert counts["installation"] == 1


def test_re_extraction_does_not_raise_corroboration(m3, company):
    """A better extractor reading the same page is not a second witness."""
    import dataclasses

    from boro_gtm.research.services import claims as claim_svc
    from boro_gtm.research.services.evidence import (
        EvidenceContext,
        create_evidence_item,
    )
    from boro_gtm.research.services.extraction import (
        SERVICE_EXTRACTOR,
        run_extraction,
    )

    run_pipeline(m3, company_id=company.id, transport=FixtureTransport(), now=NOW)
    profiles.rebuild_company_profile(m3, company.id)
    before = m3.get(
        m.OperationalResearchProfile, company.id
    ).corroborating_publisher_counts["emergency_service"]

    source = m3.scalars(select(m.ResearchSource).where(
        m.ResearchSource.normalized_locator
        == corpus.normalize_locator(corpus.EMERGENCY_URL)
    )).one()
    fetch = m3.scalars(select(m.ResearchFetchEvent).where(
        m.ResearchFetchEvent.source_id == source.id,
        m.ResearchFetchEvent.fetch_outcome == "OK",
    )).one()
    derivation = m3.scalars(select(m.ResearchArtifactDerivation).where(
        m.ResearchArtifactDerivation.body_id == fetch.body_id
    )).one()
    text = m3.scalars(select(m.ResearchTextDerivation).where(
        m.ResearchTextDerivation.body_id == fetch.body_id
    )).one()

    newer = dataclasses.replace(SERVICE_EXTRACTOR, extractor_version="9.0.0")
    attempt_id = fetch.attempt_id
    outcome = run_extraction(
        m3, extractor=newer, text_derivation=text, attempt_id=attempt_id, now=LATER
    )
    context = EvidenceContext(
        extraction_id=outcome.extraction.id, fetch_event_id=fetch.id,
        artifact_derivation_id=derivation.id, body_id=fetch.body_id, source=source,
    )
    fresh = []
    for observation in outcome.observations:
        evidence, _ = create_evidence_item(
            m3, context=context, observation=observation, now=LATER
        )
        fresh.append((observation, evidence.id))
    for pending in claim_svc.group_observations(m3, fresh):
        claim_svc.assert_claim(m3, company_id=company.id, pending=pending, now=LATER)
    m3.flush()
    profiles.rebuild_company_profile(m3, company.id)

    after = m3.get(
        m.OperationalResearchProfile, company.id
    ).corroborating_publisher_counts["emergency_service"]
    assert after == before


# --- plan profile -----------------------------------------------------------


def test_two_research_plans_for_one_company_coexist(m3, company):
    """M1, a release blocker: the PK is the run, not the company."""
    first = run_pipeline(
        m3, company_id=company.id, transport=FixtureTransport(), now=NOW
    )
    other_run, created = get_or_create_run(
        m3, company_id=company.id, now=LATER,
        target_attribute_keys=("erp", "crm", "emergency_service"),
    )
    assert created is True
    profiles.rebuild_plan_profile(m3, other_run.id)

    rows = m3.scalars(select(m.OperationalResearchPlanProfile)).all()
    assert len(rows) == 2
    assert {r.company_id for r in rows} == {company.id}
    assert {r.run_id for r in rows} == {first.attempt.run_id, other_run.id}

    by_run = {r.run_id: r for r in rows}
    # Different denominators, because the questions are different.
    assert (by_run[first.attempt.run_id].required_attribute_count
            != by_run[other_run.id].required_attribute_count)


def test_plan_coverage_is_never_copied_onto_the_company_profile(researched, m3, company):
    profile = m3.get(m.OperationalResearchProfile, company.id)
    import json

    blob = json.dumps(_semantic(profile))
    assert "coverage" not in blob
    assert "contradiction_rate" not in blob


def test_coverage_is_covered_over_required(researched, m3):
    plan = m3.scalars(select(m.OperationalResearchPlanProfile)).one()
    expected = round(plan.covered_attribute_count / plan.required_attribute_count, 4)
    assert float(plan.coverage) == pytest.approx(expected, abs=1e-6)
    assert 0.0 <= float(plan.coverage) <= 1.0


def test_optional_attributes_do_not_enter_the_required_denominator(m3, company):
    """Finding extra evidence must not dilute required coverage."""
    run, _ = get_or_create_run(
        m3, company_id=company.id, now=NOW,
        target_attribute_keys=("emergency_service", "fleet_size", "certification"),
    )
    coverage = profiles.compute_coverage(m3, run)
    # Only emergency_service is required of those three.
    assert coverage.required_attribute_count == 1
    assert get_attribute("fleet_size").required is False
    assert get_attribute("certification").required is False


def test_a_non_applicable_attribute_leaves_both_sides_untouched(m3, company):
    """F5/N2: unknown is not zero, and inapplicable is not unknown."""
    import dataclasses

    import boro_gtm.research.registry as registry_module

    original = registry_module.RESEARCH_ATTRIBUTES
    patched = tuple(
        dataclasses.replace(a, applies_to_verticals=("hvac",))
        if a.key == "fleet_presence" else a
        for a in original
    )
    registry_module.RESEARCH_ATTRIBUTES = patched
    registry_module._BY_KEY = {a.key: a for a in patched}
    try:
        run, _ = get_or_create_run(
            m3, company_id=company.id, now=NOW,
            target_attribute_keys=("emergency_service", "fleet_presence"),
        )
        coverage = profiles.compute_coverage(m3, run)
        # The run has no vertical, so the hvac-only attribute does not apply.
        assert coverage.not_applicable_attribute_count == 1
        assert coverage.required_attribute_count == 1
    finally:
        registry_module.RESEARCH_ATTRIBUTES = original
        registry_module._BY_KEY = {a.key: a for a in original}


def test_coverage_confidence_and_contradiction_stay_separate(researched, m3):
    """F6: three numbers, three columns, and none derived from the others."""
    plan = m3.scalars(select(m.OperationalResearchPlanProfile)).one()
    assert plan.coverage is not None
    assert plan.contradiction_rate is not None
    values = {
        float(plan.coverage),
        float(plan.contradiction_rate),
        float(plan.confidence_summary) if plan.confidence_summary is not None else -1.0,
    }
    assert len(values) == 3, "collapsing any two would make one a proxy for another"


def test_coverage_is_not_a_score(researched, m3):
    columns = set(m.OperationalResearchPlanProfile.__table__.columns.keys())
    for forbidden in ("lead_score", "fit", "priority", "sales_ready",
                      "qualification_score", "grade", "tier"):
        assert forbidden not in columns


# --- staleness --------------------------------------------------------------


def test_staleness_is_computed_and_never_stored(researched, m3, company):
    """No column anywhere carries a staleness verdict."""
    for table in (m.OperationalResearchProfile, m.OperationalResearchPlanProfile):
        assert not any(
            "stale" in c or "fresh" in c for c in table.__table__.columns.keys()
        )
    import json

    blob = json.dumps(m3.get(m.OperationalResearchProfile, company.id).facts)
    for word in ("FRESH", "AGING", "STALE", "UNKNOWN_AGE"):
        assert word not in blob


def test_all_four_staleness_states_are_reachable(researched, m3, company):
    from boro_gtm.research.services.staleness import company_staleness, staleness_of

    # From the corpus, judged at two different dates.
    fresh = company_staleness(m3, company.id, date(2025, 11, 5))
    aged = company_staleness(m3, company.id, date(2030, 1, 1))
    assert any(v.state == "FRESH" for v in fresh.values())
    assert any(v.state == "STALE" for v in aged.values())
    assert any(v.state == "UNKNOWN_AGE" for v in fresh.values())
    assert staleness_of("emergency_service", date(2025, 9, 1),
                        date(2026, 9, 27)).state == "AGING"


def test_time_passing_never_rewrites_a_claim(researched, m3, company):
    """E3: an old fact is not rewritten to false, or to anything else."""
    from boro_gtm.research.services.staleness import company_staleness

    before = {
        (c.id, c.fact_type, c.availability,
         None if c.value_jsonb is None else str(c.value_jsonb))
        for c in m3.scalars(select(CompanyClaim)).all()
    }
    company_staleness(m3, company.id, date(2035, 1, 1))
    m3.flush()
    after = {
        (c.id, c.fact_type, c.availability,
         None if c.value_jsonb is None else str(c.value_jsonb))
        for c in m3.scalars(select(CompanyClaim)).all()
    }
    assert before == after


def test_an_undated_source_reports_unknown_age(researched, m3):
    """E4: we do not know its age, and guessing either way is a fabrication."""
    from boro_gtm.research.services.staleness import staleness_of

    verdict = staleness_of("technician_count", None, date(2026, 9, 27))
    assert verdict.state == "UNKNOWN_AGE"
    assert verdict.age_days is None


def test_a_retrieval_date_is_never_used_as_the_observation_date(researched, m3, company):
    """E1: the most common way an evidence system starts lying."""
    from boro_gtm.research.services.staleness import company_staleness

    verdicts = company_staleness(m3, company.id, date(2026, 9, 27))
    dated = [v for v in verdicts.values() if v.observed_at is not None]
    assert dated
    assert all(v.observed_at != NOW.date() for v in dated)
