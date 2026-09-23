"""M2 entity resolution: matching policy and chain integrity.

Covers acceptance B1–B14 and C1–C9.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from boro_gtm.discovery.domain.models import (
    Company,
    CompanyClaim,
    CompanyDomain,
    EntityResolutionDecision,
    EntityResolutionHead,
    ProviderEntity,
)
from boro_gtm.discovery.enums import ResolutionDecision
from boro_gtm.discovery.providers.fixtures import (
    FixtureCsvExportAdapter,
    FixtureJsonDirectoryAdapter,
    csv_record,
    json_record,
)
from boro_gtm.discovery.services import resolution
from boro_gtm.discovery.services.projection import rebuild_projections
from tests.integration.conftest_m2 import run_discovery

pytestmark = pytest.mark.integration


def _json(name, domain=None, city="Berlin", country="DE", external_id=None):
    payload = {"legal_name": name, "location": {"country": country, "city": city}}
    if domain:
        payload["website_domain"] = domain
    return json_record(payload, external_id=external_id or name)


def _csv(name, domain="", city="Berlin", country="DE"):
    return csv_record({"company": name, "domain": domain, "country": country,
                       "city": city, "zip": "", "staff_min": "", "staff_max": ""})


def _new_company(session) -> Company:
    company = Company(created_at=datetime.now(UTC), identity_policy_version="1.0",
                      lifecycle_status="ACTIVE")
    session.add(company)
    session.flush()
    return company


def _company_count(session) -> int:
    return session.scalar(select(func.count()).select_from(Company))


# --- B1-B4: cross-provider identity ---------------------------------------


def test_same_identity_domain_across_providers_is_one_company(
    m2_session, provider_json, provider_csv
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Schmidt Kältetechnik GmbH", "schmidt-kaelte.de")])
    rebuild_projections(m2_session)
    assert _company_count(m2_session) == 1

    _, _, stats = run_discovery(
        m2_session, FixtureCsvExportAdapter(), provider_csv,
        [_csv("Schmidt Kaeltetechnik", domain="www.schmidt-kaelte.de")])

    assert stats["matched"] == 1
    assert stats["created"] == 0
    assert _company_count(m2_session) == 1
    # Two provider entities, one company: identity is ours, not the provider's.
    assert m2_session.scalar(select(func.count()).select_from(ProviderEntity)) == 2


def test_subdomain_and_scheme_do_not_create_a_second_company(
    m2_session, provider_json
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Acme GmbH", "acme.de", external_id="A")])
    rebuild_projections(m2_session)
    _, _, stats = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json,
        [_json("Acme GmbH", "https://www.acme.de/impressum", external_id="B")])
    assert stats["matched"] == 1
    assert _company_count(m2_session) == 1


# --- B5-B8: false merges --------------------------------------------------


def test_shared_platform_domain_does_not_merge_distinct_companies(
    m2_session, provider_json
):
    """wixsite.com and friends are hosting, not identity (design §8)."""
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Bäckerei Krause", "krause.wixsite.com", external_id="A")])
    rebuild_projections(m2_session)
    _, _, stats = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json,
        [_json("Elektro Nowak", "nowak.wixsite.com", city="Hamburg",
               external_id="B")])
    assert stats["created"] == 1
    assert _company_count(m2_session) == 2
    # The blocklisted domain is never projected as IDENTITY, for either
    # company — the policy is applied where the projection is written.
    rebuild_projections(m2_session)
    rows = m2_session.execute(
        select(CompanyDomain.domain_normalized, CompanyDomain.domain_role)).all()
    assert rows, "the domains were still recorded, just not as identity"
    assert {role for _, role in rows} == {"GROUP"}
    assert {domain for domain, _ in rows} == {"wixsite.com"}


def test_similar_names_without_shared_identity_do_not_merge(
    m2_session, provider_json
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Müller Bau GmbH", external_id="A")])
    rebuild_projections(m2_session)
    _, _, stats = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json,
        [_json("Mueller Bau GmbH", city="Hamburg", external_id="B")])
    assert stats["matched"] == 0
    assert _company_count(m2_session) == 2


def test_exact_name_alone_never_auto_merges(m2_session, provider_json):
    """An exact normalized name scores below the auto-match threshold."""
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Nordwind Logistik GmbH", external_id="A")])
    rebuild_projections(m2_session)
    _, _, stats = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json,
        [_json("Nordwind Logistik GmbH", city="Kiel", external_id="B")])

    assert stats["matched"] == 0, "name alone must not merge"
    assert stats["ambiguous"] == 1, "it is parked, not silently split"
    assert _company_count(m2_session) == 1, "and nothing was created either"


def test_ambiguous_decision_writes_no_claims_and_no_company(
    m2_session, provider_json
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Nordwind Logistik GmbH", external_id="A")])
    rebuild_projections(m2_session)
    before = m2_session.scalar(select(func.count()).select_from(CompanyClaim))
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Nordwind Logistik GmbH", city="Kiel", external_id="B")])
    after = m2_session.scalar(select(func.count()).select_from(CompanyClaim))
    assert after == before, "an ambiguous record contributes no attributed claims"

    ambiguous = m2_session.scalars(
        select(EntityResolutionDecision).where(
            EntityResolutionDecision.decision == ResolutionDecision.AMBIGUOUS.value
        )
    ).all()
    assert len(ambiguous) == 1
    assert ambiguous[0].company_id is None


def test_fuzzy_similarity_cannot_reach_the_auto_match_threshold():
    from boro_gtm.discovery.services.resolution import (
        AUTO_MATCH_THRESHOLD,
        SIGNAL_WEIGHTS,
    )

    assert SIGNAL_WEIGHTS["fuzzy_name"] < AUTO_MATCH_THRESHOLD
    # Even every non-deterministic signal a fuzzy retrieval can raise at once
    # stays short of an automatic merge without a corroborating identity.
    weak = SIGNAL_WEIGHTS["fuzzy_name"] + SIGNAL_WEIGHTS["same_country"] \
        + SIGNAL_WEIGHTS["shared_group_domain"]
    assert weak < AUTO_MATCH_THRESHOLD


# --- C1-C6: chain integrity -----------------------------------------------


def _one_entity(session, provider):
    run_discovery(session, FixtureJsonDirectoryAdapter(), provider,
                  [_json("Acme GmbH", "acme.de", external_id="A")])
    return session.scalars(select(ProviderEntity)).one()


def test_exactly_one_root_per_entity(m2_session, provider_json):
    entity = _one_entity(m2_session, provider_json)
    roots = m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision).where(
            EntityResolutionDecision.provider_entity_id == entity.id,
            EntityResolutionDecision.supersedes_decision_id.is_(None),
        )
    )
    assert roots == 1


def test_a_second_root_is_rejected_by_the_database(m2_session, provider_json):
    entity = _one_entity(m2_session, provider_json)
    head = resolution.current_head(m2_session, entity.id)
    savepoint = m2_session.begin_nested()
    m2_session.add(EntityResolutionDecision(
        provider_entity_id=entity.id,
        provider_record_version_id=head.provider_record_version_id,
        company_id=head.company_id, decision="MATCHED", method="HUMAN_REVIEW",
        supersedes_decision_id=None, identity_policy_version="1.0",
        signals={}, decided_by="test", decided_at=datetime.now(UTC),
    ))
    with pytest.raises(IntegrityError) as exc:
        m2_session.flush()
    assert "uq_resolution_root" in str(exc.value)
    savepoint.rollback()


def test_two_decisions_cannot_supersede_the_same_head(m2_session, provider_json):
    """Chains stay linear: no forks (design §6.3)."""
    entity = _one_entity(m2_session, provider_json)
    head = resolution.current_head(m2_session, entity.id)
    other = _new_company(m2_session)

    resolution.append_human_decision(
        m2_session, entity, decision="MATCHED", company_id=other.id,
        decided_by="reviewer:a")

    savepoint = m2_session.begin_nested()
    m2_session.add(EntityResolutionDecision(
        provider_entity_id=entity.id, company_id=other.id,
        decision="MATCHED", method="HUMAN_REVIEW",
        supersedes_decision_id=head.id, identity_policy_version="1.0",
        signals={}, decided_by="reviewer:b", decided_at=datetime.now(UTC),
    ))
    with pytest.raises(IntegrityError) as exc:
        m2_session.flush()
    assert "uq_resolution_supersedes" in str(exc.value)
    savepoint.rollback()


def test_a_decision_cannot_supersede_another_entitys_decision(
    m2_session, provider_json
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [_json("Acme GmbH", "acme.de", external_id="A"),
                   _json("Beta GmbH", "beta.de", external_id="B")])
    first, second = m2_session.scalars(
        select(ProviderEntity).order_by(ProviderEntity.provider_external_id)).all()
    foreign_head = resolution.current_head(m2_session, first.id)

    savepoint = m2_session.begin_nested()
    m2_session.add(EntityResolutionDecision(
        provider_entity_id=second.id, company_id=None,
        decision="AMBIGUOUS", method="HUMAN_REVIEW",
        supersedes_decision_id=foreign_head.id, identity_policy_version="1.0",
        signals={}, decided_by="test", decided_at=datetime.now(UTC),
    ))
    with pytest.raises(IntegrityError) as exc:
        m2_session.flush()
    assert "fk_supersedes_same_entity" in str(exc.value)
    savepoint.rollback()


def test_a_decision_cannot_supersede_itself(m2_session, provider_json):
    entity = _one_entity(m2_session, provider_json)
    decision_id = uuid.uuid4()
    savepoint = m2_session.begin_nested()
    with pytest.raises(IntegrityError) as exc:
        m2_session.execute(text(
            "INSERT INTO entity_resolution_decisions "
            "(id, provider_entity_id, company_id, decision, method, "
            " supersedes_decision_id, identity_policy_version, signals, "
            " decided_by, decided_at) "
            "VALUES (:i, :e, NULL, 'AMBIGUOUS', 'HUMAN_REVIEW', :i, '1.0', "
            "'{}'::jsonb, 'test', now())"
        ), {"i": decision_id, "e": entity.id})
    assert "no_self_supersede" in str(exc.value)
    savepoint.rollback()


def test_decisions_are_append_only(m2_session, provider_json):
    entity = _one_entity(m2_session, provider_json)
    head = resolution.current_head(m2_session, entity.id)
    savepoint = m2_session.begin_nested()
    with pytest.raises(DBAPIError) as exc:
        m2_session.execute(text(
            "UPDATE entity_resolution_decisions SET decision = 'MATCHED' "
            "WHERE id = :i"), {"i": head.id})
    assert "append-only" in str(exc.value)
    savepoint.rollback()


# --- C7-C9: reassignment --------------------------------------------------


def test_reassignment_supersedes_without_mutating_claims(m2_session, provider_json):
    entity = _one_entity(m2_session, provider_json)
    original_head = resolution.current_head(m2_session, entity.id)
    claims_before = {
        (c.id, c.attribute_key, c.resolution_decision_id)
        for c in m2_session.scalars(select(CompanyClaim)).all()
    }
    assert claims_before

    target = _new_company(m2_session)
    resolution.append_human_decision(
        m2_session, entity, decision="MATCHED", company_id=target.id,
        decided_by="reviewer:a", rationale="Same firm under a new legal entity")

    claims_after = {
        (c.id, c.attribute_key, c.resolution_decision_id)
        for c in m2_session.scalars(select(CompanyClaim)).all()
    }
    assert claims_after == claims_before, "claims are evidence, never rewritten"

    # The original decision survives verbatim.
    m2_session.expire_all()
    still = m2_session.get(EntityResolutionDecision, original_head.id)
    assert still.company_id == original_head.company_id
    assert still.decision == original_head.decision

    # Attribution nonetheless follows the new head.
    rebuild_projections(m2_session)
    owners = set(m2_session.scalars(select(CompanyDomain.company_id)).all())
    assert owners == {target.id}


def test_head_cache_is_rebuildable_from_decisions_alone(m2_session, provider_json):
    entity = _one_entity(m2_session, provider_json)
    target = _new_company(m2_session)
    resolution.append_human_decision(
        m2_session, entity, decision="MATCHED", company_id=target.id,
        decided_by="reviewer:a")
    expected = resolution.current_head(m2_session, entity.id)

    m2_session.execute(text("DELETE FROM entity_resolution_heads"))
    m2_session.flush()
    resolution.rebuild_heads(m2_session)

    head = m2_session.scalars(select(EntityResolutionHead)).one()
    assert head.current_decision_id == expected.id
    assert head.company_id == target.id


def test_resolving_an_already_resolved_entity_is_idempotent(
    m2_session, provider_json
):
    records = [_json("Acme GmbH", "acme.de", external_id="A")]
    _, _, first = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json, records)
    decisions_before = m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision))

    _, _, second = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json, records)
    decisions_after = m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision))

    assert first["entities"] == 1
    # The second run created no new version, but it still *saw* the record, so
    # it must resolve it — a run that silently resolves nothing would hide a
    # first run that died before resolution.
    assert second["entities"] == 1, (
        "a re-run must attribute versions through sightings, not through the "
        "query that first created them"
    )
    assert second["created"] == 0
    assert decisions_after == decisions_before
    assert _company_count(m2_session) == 1


# --- A10: derived-key collisions ------------------------------------------


def test_a_colliding_derived_key_blocks_auto_matching(m2_session, provider_csv):
    """Two firms on one corporate domain derive one key. That key has been
    shown not to identify a single object, so it may never auto-match."""
    from boro_gtm.discovery.services.resolution import has_identity_collision

    run_discovery(m2_session, FixtureCsvExportAdapter(), provider_csv, [
        _csv("Schmidt Kältetechnik GmbH", domain="schmidt-gruppe.de"),
        _csv("Schmidt Anlagenbau GmbH", domain="schmidt-gruppe.de",
             city="Hamburg"),
    ])

    entity = m2_session.scalars(select(ProviderEntity)).one()
    assert has_identity_collision(m2_session, entity) is True

    head = resolution.current_head(m2_session, entity.id)
    assert head.decision == ResolutionDecision.AMBIGUOUS.value
    assert head.company_id is None
    assert head.signals.get("reason") == "identity_collision"
    assert _company_count(m2_session) == 0, (
        "a colliding key is weaker evidence than no key at all"
    )


def test_one_derived_entity_with_a_renamed_record_is_not_a_collision(
    m2_session, provider_csv
):
    """Only *disagreeing* names collide. A single name seen twice does not."""
    from boro_gtm.discovery.services.resolution import has_identity_collision

    run_discovery(m2_session, FixtureCsvExportAdapter(), provider_csv, [
        _csv("Acme GmbH", domain="acme.de"),
        _csv("Acme GmbH", domain="acme.de", city="Hamburg"),
    ])
    entity = m2_session.scalars(select(ProviderEntity)).one()
    assert has_identity_collision(m2_session, entity) is False
    assert _company_count(m2_session) == 1


def test_a_native_id_entity_cannot_collide_this_way(m2_session, provider_json):
    """A provider-issued id is the provider's own assertion of identity."""
    from boro_gtm.discovery.services.resolution import has_identity_collision

    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json, [
        _json("Acme GmbH", "acme.de", external_id="P-1"),
        _json("Acme Holding GmbH", "acme.de", external_id="P-1"),
    ])
    entity = m2_session.scalars(select(ProviderEntity)).one()
    assert has_identity_collision(m2_session, entity) is False


def test_re_resolving_an_ambiguous_entity_does_not_grow_the_chain(
    m2_session, provider_csv
):
    records = [
        _csv("Schmidt Kältetechnik GmbH", domain="schmidt-gruppe.de"),
        _csv("Schmidt Anlagenbau GmbH", domain="schmidt-gruppe.de",
             city="Hamburg"),
    ]
    run_discovery(m2_session, FixtureCsvExportAdapter(), provider_csv, records)
    before = m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision))
    run_discovery(m2_session, FixtureCsvExportAdapter(), provider_csv, records)
    after = m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision))
    assert after == before, "an unchanged re-resolution is a no-op"
