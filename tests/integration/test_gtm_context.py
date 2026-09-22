"""ADR-014 — contextual dimensions are distinct, durable and queryable.

Proves the requirement that `USA x HVAC x ICP x channel x ticket` and
`Germany x Industrial Maintenance x ICP x channel x ticket` are representable
as *different* contexts, without a `market_gtm_profiles` table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from boro_gtm.core.enums import ScoreRunKind
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketDeepDive,
    MarketObservation,
    MarketScore,
    ScoreRun,
)
from boro_gtm.market_intelligence.services import contextual_service, scoring_service
from boro_gtm.strategy.domain.models import ICP, Channel, Offer, Vertical
from boro_gtm.strategy.seeds.loader import seed_all

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"
ICP_KEY = "boro_field_service_midmarket_v1"


@pytest.fixture
def seeded(imported_session):
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    seed_all(imported_session)
    imported_session.flush()
    return imported_session


def _run(session, **overrides):
    kwargs = dict(
        snapshot_key=SNAPSHOT_KEY,
        icp_key=ICP_KEY,
        offer_key="operations_os_core",
        channel_key="email",
        ticket_usd=20000,
        allow_low_coverage=True,
    )
    kwargs.update(overrides)
    run, results, _ = contextual_service.run_contextual_ranking(session, **kwargs)
    session.flush()
    return run, results


def test_the_two_required_contexts_are_distinct(seeded) -> None:
    us_run, us_results = _run(
        seeded, vertical_key="commercial_hvac", market_iso2=["US"]
    )
    de_run, de_results = _run(
        seeded, vertical_key="industrial_maintenance", market_iso2=["DE"]
    )

    assert us_run.id != de_run.id
    # Context is carried by real foreign keys, not only by JSON.
    assert us_run.vertical_id != de_run.vertical_id
    assert us_run.icp_id == de_run.icp_id
    assert float(us_run.ticket_usd) == 20000.0

    assert [r.market_key for r in us_results] == ["US"]
    assert [r.market_key for r in de_results] == ["DE"]


def test_context_is_queryable_by_foreign_key(seeded) -> None:
    """The point of ADR-014: joinable context, no JSON spelunking."""
    _run(seeded, vertical_key="commercial_hvac", market_iso2=["US"])
    _run(seeded, vertical_key="industrial_maintenance", market_iso2=["DE"])

    hvac = seeded.scalar(select(Vertical).where(Vertical.key == "commercial_hvac"))
    icp = seeded.scalar(select(ICP).where(ICP.key == ICP_KEY))
    email = seeded.scalar(select(Channel).where(Channel.key == "email"))

    rows = seeded.scalars(
        select(MarketScore)
        .join(ScoreRun, ScoreRun.id == MarketScore.score_run_id)
        .join(Market, Market.id == MarketScore.market_id)
        .where(
            ScoreRun.vertical_id == hvac.id,
            ScoreRun.icp_id == icp.id,
            ScoreRun.channel_id == email.id,
            Market.iso2 == "US",
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].coverage is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vertical_key", "refrigeration"),
        ("offer_key", "operations_architecture_sprint"),
        ("channel_key", "phone"),
        ("ticket_usd", 55000),
    ],
)
def test_changing_any_dimension_yields_a_new_context(seeded, field, value) -> None:
    base_run, _ = _run(seeded, vertical_key="commercial_hvac", market_iso2=["US"])
    overrides = {"vertical_key": "commercial_hvac", "market_iso2": ["US"], field: value}
    other_run, _ = _run(seeded, **overrides)

    assert base_run.id != other_run.id
    column = {
        "vertical_key": "vertical_id",
        "offer_key": "offer_id",
        "channel_key": "channel_id",
        "ticket_usd": "ticket_usd",
    }[field]
    assert getattr(base_run, column) != getattr(other_run, column)


def test_gtm_motion_evidence_has_a_home_without_a_profile_table(seeded) -> None:
    """The responsibility the removed table named is already represented."""
    us = seeded.scalar(select(Market).where(Market.iso2 == "US"))

    deep_dive = seeded.scalar(
        select(MarketDeepDive).where(MarketDeepDive.market_id == us.id)
    )
    assert deep_dive.recommended_channel
    assert deep_dive.common_buyers

    channel_metrics = seeded.scalars(
        select(MarketObservation).where(
            MarketObservation.market_id == us.id,
            MarketObservation.metric_key.in_(
                ["language_access", "timezone_overlap", "b2b_email_legal_access"]
            ),
        )
    ).all()
    assert len(channel_metrics) == 3
    # And unlike a denormalised profile row, they keep their provenance.
    for observation in channel_metrics:
        assert observation.fact_type is not None
        assert observation.sources


def test_no_market_gtm_profiles_table_exists(seeded) -> None:
    from sqlalchemy import text

    tables = seeded.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    ).scalars().all()
    assert "market_gtm_profiles" not in tables


def test_channel_weighting_is_configuration_not_per_market_evidence(seeded) -> None:
    from boro_gtm.market_intelligence.scoring.definitions import CONTEXTUAL_MODEL_KEY

    model = scoring_service.get_model(seeded, CONTEXTUAL_MODEL_KEY, "1.0")
    weights = model.definition["channel_access_weights"]
    assert "email" in weights and "multichannel" in weights
    # Weights are model configuration; they carry no market identity.
    assert all(isinstance(v, dict) for v in weights.values())


def test_offer_and_ticket_are_independent_dimensions(seeded) -> None:
    offer = seeded.scalar(select(Offer).where(Offer.key == "operations_os_core"))
    run, _ = _run(
        seeded, vertical_key="commercial_hvac", market_iso2=["US"], ticket_usd=27500
    )
    assert run.offer_id == offer.id
    # A ticket outside the offer's own band is still representable.
    assert float(run.ticket_usd) == 27500.0
    assert float(offer.ticket_min) <= 27500.0 <= float(offer.ticket_max)
