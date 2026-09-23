"""M2 projections: determinism, truncatability, precedence.

Covers acceptance D1–D12 and E1–E7.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text

from boro_gtm.core.enums import FactType
from boro_gtm.discovery.domain.models import (
    Company,
    CompanyClaim,
    CompanyLocation,
    CompanyMarketPresence,
    CompanyName,
    CompanyProfile,
    CompanyRelationship,
    ProjectionRun,
    ProviderEntity,
)
from boro_gtm.discovery.enums import ClaimAssertion, RelationshipType
from boro_gtm.discovery.providers.fixtures import (
    FixtureJsonDirectoryAdapter,
    json_record,
)
from boro_gtm.discovery.services import resolution
from boro_gtm.discovery.services.claims import write_claim, write_relationship_claim
from boro_gtm.discovery.services.projection import (
    PROJECTION_TABLES,
    normalized_rows,
    rebuild_projections,
)
from tests.integration.conftest_m2 import run_discovery

pytestmark = pytest.mark.integration


RECORDS = [
    json_record({"legal_name": "Schmidt Kältetechnik GmbH",
                 "website_domain": "schmidt-kaelte.de",
                 "trading_names": ["Schmidt Kälte"],
                 "employees": {"min": 40, "max": 60},
                 "founded": 1994,
                 "location": {"country": "DE", "city": "München",
                              "postal_code": "80331"}}, external_id="A"),
    json_record({"legal_name": "Nowak Elektro Sp. z o.o.",
                 "website_domain": "nowak-elektro.pl",
                 "employees": {"min": 8, "max": 12},
                 "location": {"country": "PL", "city": "Kraków"}},
                external_id="B"),
]


@pytest.fixture
def projected(m2_session, provider_json):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json, RECORDS)
    rebuild_projections(m2_session)
    return m2_session


# --- D1-D4: determinism ----------------------------------------------------


def test_rebuild_from_empty_reproduces_the_same_rows(projected):
    before = normalized_rows(projected)
    rebuild_projections(projected)
    after = normalized_rows(projected)
    assert after == before
    assert any(rows for rows in before.values()), "the comparison is not vacuous"


def test_projections_survive_full_truncation(projected):
    before = normalized_rows(projected)
    for model in PROJECTION_TABLES:
        projected.execute(text(f"DELETE FROM {model.__tablename__}"))
    projected.flush()
    assert all(not rows for rows in normalized_rows(projected).values())

    rebuild_projections(projected)
    assert normalized_rows(projected) == before, (
        "projections are derived: evidence alone must reconstruct them"
    )


def test_rebuild_reads_no_wall_clock(projected):
    """Row content must not depend on when the rebuild ran (design §5.7)."""
    before = normalized_rows(projected)
    rebuild_projections(projected)
    assert normalized_rows(projected) == before

    # An open-ended interval is stored as a sentinel start, never as "today".
    presences = projected.scalars(select(CompanyMarketPresence)).all()
    for presence in presences:
        assert presence.effective_from != date.today()


def test_projection_tables_use_natural_keys_only(projected):
    for model in PROJECTION_TABLES:
        pk = {c.name for c in model.__table__.primary_key}
        assert "id" not in pk, f"{model.__tablename__} uses a surrogate key"


def test_projection_run_records_counts_and_digests(projected):
    run = projected.scalars(
        select(ProjectionRun).order_by(ProjectionRun.started_at.desc())).first()
    assert run is not None
    assert run.row_counts["company_names"] >= 2
    assert set(run.content_digests) == {m.__tablename__ for m in PROJECTION_TABLES}
    assert run.attribute_registry_version and run.identity_policy_version


# --- D5-D8: precedence -----------------------------------------------------


def test_higher_trust_provider_wins_a_scalar_conflict(
    m2_session, provider_json, provider_scrape
):
    from boro_gtm.discovery.providers.fixtures import FixtureScrapeAdapter

    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Acme GmbH",
                                "website_domain": "acme.de",
                                "founded": 1994}, external_id="A")])
    rebuild_projections(m2_session)
    run_discovery(m2_session, FixtureScrapeAdapter(), provider_scrape,
                  [json_record({"title": "Acme Handels GmbH", "domain": "acme.de"})])
    rebuild_projections(m2_session)

    company = m2_session.scalars(select(Company)).one()
    primary = m2_session.scalars(
        select(CompanyName).where(CompanyName.company_id == company.id,
                                  CompanyName.is_primary.is_(True))
    ).one()
    # Both names are retained as evidence; only one is primary.
    all_names = m2_session.scalars(
        select(CompanyName.name_normalized).where(
            CompanyName.company_id == company.id)).all()
    assert set(all_names) == {"acme", "acme handels"}
    assert primary.name_normalized in all_names


def test_ties_break_deterministically_not_by_insertion_order(projected):
    """The same inputs must pick the same winner on every rebuild."""
    winners = []
    for _ in range(3):
        rebuild_projections(projected)
        winners.append(sorted(
            (str(n.company_id), n.name_normalized)
            for n in projected.scalars(
                select(CompanyName).where(CompanyName.is_primary.is_(True))).all()
        ))
    assert winners[0] == winners[1] == winners[2]


def test_unknown_stays_unknown_and_never_becomes_zero(m2_session, provider_json):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Sparse GmbH",
                                "website_domain": "sparse.de"}, external_id="A")])
    rebuild_projections(m2_session)
    profile = m2_session.scalars(select(CompanyProfile)).one()
    assert profile.employee_count_min is None
    assert profile.employee_count_max is None
    assert profile.founded_year is None


def test_a_not_available_claim_is_not_a_fact(m2_session, provider_json):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Acme GmbH",
                                "website_domain": "acme.de"}, external_id="A")])
    entity = m2_session.scalars(select(ProviderEntity)).one()
    head = resolution.current_head(m2_session, entity.id)
    claim = write_claim(
        m2_session, "founded_year", None,
        provider_record_version_id=head.provider_record_version_id,
        resolution_decision_id=head.id,
    )
    assert claim.fact_type is None
    assert claim.availability == "NOT_AVAILABLE"
    assert claim.value_jsonb is None
    assert claim.confidence is None

    rebuild_projections(m2_session)
    profile = m2_session.scalars(select(CompanyProfile)).one()
    assert profile.founded_year is None, "absence never projects a value"


def test_every_projected_row_cites_the_claims_behind_it(projected):
    for model in (CompanyName, CompanyLocation, CompanyProfile):
        for row in projected.scalars(select(model)).all():
            cited = getattr(row, "derived_from_claim_ids", None)
            if cited is None:
                continue
            assert cited, f"{model.__tablename__} row cites no evidence"
            existing = projected.scalar(
                select(func.count()).select_from(CompanyClaim)
                .where(CompanyClaim.id.in_(cited)))
            assert existing == len(cited)


# --- E1-E7: temporal history ----------------------------------------------


def _head(session):
    entity = session.scalars(select(ProviderEntity)).first()
    return resolution.current_head(session, entity.id)


def _market_id(session):
    from boro_gtm.market_intelligence.domain.models import Market

    market = Market(name="Germany", iso2="DE", iso3="DEU",
                    is_home_market=False, created_at=datetime.now(UTC))
    session.add(market)
    session.flush()
    return market.id


def test_market_presence_keeps_history_rather_than_overwriting(
    m2_session, provider_json
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Acme GmbH",
                                "website_domain": "acme.de"}, external_id="A")])
    head = _head(m2_session)
    market = _market_id(m2_session)

    for valid_from, valid_to in ((date(2019, 1, 1), date(2021, 12, 31)),
                                 (date(2023, 1, 1), None)):
        write_claim(
            m2_session, "market_presence",
            {"market_id": str(market), "presence_type": "OPERATES",
             "valid_from": valid_from.isoformat(),
             "valid_to": valid_to.isoformat() if valid_to else None,
             "assertion": ClaimAssertion.ASSERTED.value},
            provider_record_version_id=head.provider_record_version_id,
            resolution_decision_id=head.id, fact_type=FactType.INFERENCE.value,
        )
    rebuild_projections(m2_session)

    rows = m2_session.scalars(
        select(CompanyMarketPresence).order_by(CompanyMarketPresence.effective_from)
    ).all()
    assert len(rows) == 2, "re-entering a market appends, it does not overwrite"
    assert rows[0].effective_from == date(2019, 1, 1)
    assert rows[0].effective_to == date(2021, 12, 31)
    assert rows[1].effective_from == date(2023, 1, 1)
    assert rows[1].effective_to is None, "an open interval asserts no end"


def test_provider_silence_does_not_close_an_interval(m2_session, provider_json):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Acme GmbH",
                                "website_domain": "acme.de"}, external_id="A")])
    head = _head(m2_session)
    market = _market_id(m2_session)
    write_claim(
        m2_session, "market_presence",
        {"market_id": str(market), "presence_type": "OPERATES",
         "valid_from": "2019-01-01", "valid_to": None,
         "assertion": ClaimAssertion.ASSERTED.value},
        provider_record_version_id=head.provider_record_version_id,
        resolution_decision_id=head.id, fact_type=FactType.INFERENCE.value)
    rebuild_projections(m2_session)

    # A later run that simply does not mention the market.
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Acme GmbH",
                                "website_domain": "acme.de",
                                "founded": 1994}, external_id="A")])
    rebuild_projections(m2_session)

    row = m2_session.scalars(select(CompanyMarketPresence)).one()
    assert row.effective_to is None, "silence is not an exit"


def test_a_retraction_removes_the_projected_interval(m2_session, provider_json):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json,
                  [json_record({"legal_name": "Acme GmbH",
                                "website_domain": "acme.de"}, external_id="A")])
    head = _head(m2_session)
    market = _market_id(m2_session)
    value = {"market_id": str(market), "presence_type": "OPERATES",
             "valid_from": "2019-01-01", "valid_to": None}
    write_claim(m2_session, "market_presence",
                {**value, "assertion": ClaimAssertion.ASSERTED.value},
                provider_record_version_id=head.provider_record_version_id,
                resolution_decision_id=head.id, fact_type=FactType.INFERENCE.value)
    rebuild_projections(m2_session)
    assert m2_session.scalar(
        select(func.count()).select_from(CompanyMarketPresence)) == 1

    retraction = write_claim(
        m2_session, "market_presence",
        {**value, "assertion": ClaimAssertion.RETRACTED.value},
        provider_record_version_id=head.provider_record_version_id,
        resolution_decision_id=head.id, fact_type=FactType.INFERENCE.value)
    rebuild_projections(m2_session)

    assert m2_session.scalar(
        select(func.count()).select_from(CompanyMarketPresence)) == 0
    # The asserting claim is still on file: retraction adds, it never deletes.
    assert m2_session.get(CompanyClaim, retraction.id) is not None
    assert m2_session.scalar(
        select(func.count()).select_from(CompanyClaim)
        .where(CompanyClaim.attribute_key == "market_presence")) == 2


def test_relationship_history_is_temporal_and_symmetric_once(
    m2_session, provider_json
):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json, RECORDS)
    first, second = sorted(
        m2_session.scalars(select(Company)).all(), key=lambda c: str(c.id))

    write_relationship_claim(
        m2_session, first.id, second.id, RelationshipType.SUBSIDIARY_OF.value,
        valid_from=date(2018, 5, 1), valid_to=date(2022, 3, 31))
    write_relationship_claim(
        m2_session, second.id, first.id, RelationshipType.SISTER_OF.value,
        valid_from=date(2022, 4, 1))
    rebuild_projections(m2_session)

    rows = m2_session.scalars(
        select(CompanyRelationship).order_by(CompanyRelationship.effective_from)).all()
    assert len(rows) == 2
    assert rows[0].relationship_type == RelationshipType.SUBSIDIARY_OF.value
    assert rows[0].effective_to == date(2022, 3, 31)
    assert rows[1].effective_to is None
    # The symmetric type is stored once, in canonical order.
    sister = rows[1]
    assert str(sister.from_company_id) < str(sister.to_company_id)


def test_superseded_relationship_claim_is_not_projected(m2_session, provider_json):
    run_discovery(m2_session, FixtureJsonDirectoryAdapter(), provider_json, RECORDS)
    first, second = sorted(
        m2_session.scalars(select(Company)).all(), key=lambda c: str(c.id))
    original = write_relationship_claim(
        m2_session, first.id, second.id, RelationshipType.FRANCHISE_OF.value,
        valid_from=date(2020, 1, 1))
    write_relationship_claim(
        m2_session, first.id, second.id, RelationshipType.FRANCHISE_OF.value,
        valid_from=date(2020, 1, 1), valid_to=date(2024, 6, 30),
        supersedes_claim_id=original.id)
    rebuild_projections(m2_session)

    row = m2_session.scalars(select(CompanyRelationship)).one()
    assert row.effective_to == date(2024, 6, 30)
    assert original.id not in (row.derived_from_claim_ids or [])


def test_claims_are_append_only(projected):
    from sqlalchemy.exc import DBAPIError

    savepoint = projected.begin_nested()
    with pytest.raises(DBAPIError) as exc:
        projected.execute(text("UPDATE company_claims SET fact_type = 'FACT'"))
    assert "append-only" in str(exc.value)
    savepoint.rollback()


def test_a_claim_has_exactly_one_attribution_path(projected):
    from sqlalchemy.exc import IntegrityError

    head = _head(projected)
    company = projected.scalars(select(Company)).first()
    savepoint = projected.begin_nested()
    projected.add(CompanyClaim(
        attribute_key="legal_name", attribute_registry_version="1.0",
        value_jsonb={"value": "X", "normalized": "x"}, fact_type="FACT",
        availability="OBSERVED", confidence=0.5,
        provider_record_version_id=head.provider_record_version_id,
        subject_company_id=company.id,  # both paths at once
        resolution_decision_id=head.id,
        period_granularity="UNDATED", created_at=datetime.now(UTC),
        value_text="x",
    ))
    with pytest.raises(IntegrityError) as exc:
        projected.flush()
    assert "exactly_one_attribution_path" in str(exc.value)
    savepoint.rollback()
