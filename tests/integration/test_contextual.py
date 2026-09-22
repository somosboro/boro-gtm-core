"""M1 integration: seeds, profiles, contextual runs and research gaps."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from boro_gtm.core.enums import ResearchGapStatus, ScoreRunKind
from boro_gtm.market_intelligence.research_gaps.detector import GapContext, record_gap
from boro_gtm.market_intelligence.services import contextual_service, scoring_service
from boro_gtm.strategy.domain.models import (
    ICP,
    Channel,
    MarketVerticalProfile,
    Offer,
    ResearchGap,
    Vertical,
)
from boro_gtm.strategy.seeds.loader import seed_all
from tests.fixtures.golden import INITIAL_PORTFOLIO

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"

REQUIRED_VERTICALS = {
    "commercial_hvac",
    "industrial_maintenance",
    "facilities_management",
    "mechanical_contractors",
}
REQUIRED_CHANNELS = {"email", "phone", "linkedin", "partner", "multichannel"}
REQUIRED_OFFERS = {"operations_architecture_sprint", "operations_os_core"}


@pytest.fixture
def seeded_session(imported_session):
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    seed_all(imported_session)
    imported_session.flush()
    return imported_session


def test_required_seeds_exist(seeded_session) -> None:
    verticals = {v.key for v in seeded_session.scalars(select(Vertical)).all()}
    assert REQUIRED_VERTICALS <= verticals
    assert len(verticals) == 9

    icps = {i.key for i in seeded_session.scalars(select(ICP)).all()}
    assert "boro_field_service_midmarket_v1" in icps

    offers = {o.key for o in seeded_session.scalars(select(Offer)).all()}
    assert REQUIRED_OFFERS <= offers

    channels = {c.key for c in seeded_session.scalars(select(Channel)).all()}
    assert REQUIRED_CHANNELS <= channels


def test_icp_definition_captures_the_profile(seeded_session) -> None:
    icp = seeded_session.scalar(
        select(ICP).where(ICP.key == "boro_field_service_midmarket_v1")
    )
    definition = icp.definition
    assert definition["business_model"] == "B2B"
    assert definition["employees"] == {"min": 20, "max": 150, "preferred": True}
    assert definition["field_workers"] == {"min": 10, "max": 75, "preferred": True}
    assert definition["positive_signals"]
    assert definition["negative_signals"]


def test_offer_prices_are_seed_configuration(seeded_session) -> None:
    offer = seeded_session.scalar(
        select(Offer).where(Offer.key == "operations_os_core")
    )
    assert float(offer.ticket_min) == 15000
    assert float(offer.ticket_max) == 30000
    assert offer.currency == "USD"


def test_seeding_is_idempotent(seeded_session) -> None:
    before = seeded_session.scalar(select(func.count()).select_from(MarketVerticalProfile))
    seed_all(seeded_session)
    seeded_session.flush()
    after = seeded_session.scalar(select(func.count()).select_from(MarketVerticalProfile))
    assert before == after
    assert seeded_session.scalar(select(func.count()).select_from(Vertical)) == 9


def test_profiles_are_derived_only_from_evidence(seeded_session) -> None:
    profiles = seeded_session.scalars(select(MarketVerticalProfile)).all()
    assert profiles
    for profile in profiles:
        assert profile.evidence["derived_from"] == "snapshot_deep_dive.priority_verticals"
        # Nothing quantitative is invented.
        assert profile.fit_score is None
        assert profile.sam_min is None
        assert profile.sam_max is None


def test_markets_without_deep_dive_have_no_profile(seeded_session) -> None:
    from boro_gtm.market_intelligence.domain.models import Market

    poland = seeded_session.scalar(select(Market).where(Market.iso2 == "PL"))
    profiles = seeded_session.scalars(
        select(MarketVerticalProfile).where(MarketVerticalProfile.market_id == poland.id)
    ).all()
    assert profiles == []


def test_contextual_ranking_over_initial_portfolio(seeded_session) -> None:
    run, results, gaps = contextual_service.run_contextual_ranking(
        seeded_session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        icp_key="boro_field_service_midmarket_v1",
        offer_key="operations_architecture_sprint",
        channel_key="multichannel",
        ticket_usd=3000,
        market_iso2=INITIAL_PORTFOLIO,
    )
    assert run is not None
    assert run.kind == ScoreRunKind.CONTEXTUAL.value
    assert len(results) == len(INITIAL_PORTFOLIO)

    for result in results:
        assert result.score is not None
        assert 0.0 <= result.score <= 100.0
        assert 0.0 <= result.confidence <= 1.0
        assert 0.0 <= result.coverage <= 1.0
        assert result.components

    ranks = [r.rank for r in results if r.rank is not None]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1


def test_contextual_run_does_not_mutate_base_scores(seeded_session) -> None:
    from boro_gtm.market_intelligence.domain.models import MarketScore

    base_run = seeded_session.scalar(
        select(contextual_service.ScoreRun).where(
            contextual_service.ScoreRun.kind
            == ScoreRunKind.REFERENCE_REPRODUCTION.value
        )
    )
    before = {
        r.market_id: float(r.score)
        for r in seeded_session.scalars(
            select(MarketScore).where(MarketScore.score_run_id == base_run.id)
        ).all()
    }
    contextual_service.run_contextual_ranking(
        seeded_session, snapshot_key=SNAPSHOT_KEY, vertical_key="commercial_hvac",
        channel_key="email", ticket_usd=20000, market_iso2=INITIAL_PORTFOLIO,
    )
    seeded_session.flush()
    after = {
        r.market_id: float(r.score)
        for r in seeded_session.scalars(
            select(MarketScore).where(MarketScore.score_run_id == base_run.id)
        ).all()
    }
    assert before == after


def test_missing_evidence_lowers_coverage_and_creates_gaps(seeded_session) -> None:
    """Poland has no deep dive and no TAM/SAM/SOM in this snapshot."""
    _, results, gaps = contextual_service.run_contextual_ranking(
        seeded_session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        channel_key="multichannel",
        ticket_usd=3000,
        market_iso2=["US", "PL"],
    )
    by_iso = {r.market_key: r for r in results}
    assert by_iso["PL"].coverage < by_iso["US"].coverage
    # Missing evidence must not read as a zero-fit verdict.
    assert by_iso["PL"].score is not None
    assert by_iso["PL"].score > 0.0
    assert by_iso["PL"].missing_metrics
    assert gaps["created"] > 0


def test_research_gaps_are_deduplicated(seeded_session) -> None:
    kwargs = dict(
        snapshot_key=SNAPSHOT_KEY, vertical_key="commercial_hvac",
        channel_key="multichannel", ticket_usd=3000, market_iso2=["PL"],
    )
    _, _, first = contextual_service.run_contextual_ranking(seeded_session, **kwargs)
    seeded_session.flush()
    _, _, second = contextual_service.run_contextual_ranking(seeded_session, **kwargs)
    seeded_session.flush()

    assert first["created"] > 0
    assert second["created"] == 0
    assert second["already_known"] == first["created"]

    total = seeded_session.scalar(select(func.count()).select_from(ResearchGap))
    assert total == first["created"]


def test_resolved_gap_is_not_recreated(seeded_session) -> None:
    from boro_gtm.market_intelligence.domain.models import Market

    market = seeded_session.scalar(select(Market).where(Market.iso2 == "PL"))
    context = GapContext(metric_key="market_size_estimate.sam_min", market_id=market.id)
    gap, created = record_gap(seeded_session, context, "initial")
    assert created is True

    gap.status = ResearchGapStatus.RESOLVED.value
    seeded_session.flush()

    same, created_again = record_gap(seeded_session, context, "seen again")
    assert created_again is False
    assert same.id == gap.id
    assert same.status == ResearchGapStatus.RESOLVED.value


def test_different_context_creates_a_distinct_gap(seeded_session) -> None:
    from boro_gtm.market_intelligence.domain.models import Market

    market = seeded_session.scalar(select(Market).where(Market.iso2 == "PL"))
    hvac = seeded_session.scalar(select(Vertical).where(Vertical.key == "commercial_hvac"))
    other = seeded_session.scalar(
        select(Vertical).where(Vertical.key == "refrigeration")
    )

    _, a = record_gap(
        seeded_session,
        GapContext(metric_key="m", market_id=market.id, vertical_id=hvac.id),
        "a",
    )
    _, b = record_gap(
        seeded_session,
        GapContext(metric_key="m", market_id=market.id, vertical_id=other.id),
        "b",
    )
    assert a is True and b is True


def test_low_coverage_markets_are_unranked_by_default(seeded_session) -> None:
    """A market with almost no contextual evidence must not be rank-comparable."""
    _, results, _ = contextual_service.run_contextual_ranking(
        seeded_session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        channel_key="multichannel",
        ticket_usd=3000,
        market_iso2=["US", "PL"],
        min_coverage=0.7,
    )
    by_iso = {r.market_key: r for r in results}
    assert by_iso["US"].comparable is True
    assert by_iso["PL"].comparable is False
    assert by_iso["PL"].rank is None
    # The score is still computed and returned for inspection.
    assert by_iso["PL"].score is not None


def test_allow_low_coverage_opts_back_in(seeded_session) -> None:
    _, results, _ = contextual_service.run_contextual_ranking(
        seeded_session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        channel_key="multichannel",
        ticket_usd=3000,
        market_iso2=["US", "PL"],
        min_coverage=0.7,
        allow_low_coverage=True,
    )
    assert all(r.rank is not None for r in results)


def test_gap_identity_ignores_irrelevant_context(seeded_session) -> None:
    """A market-level gap is one research question, not one per permutation.

    The same missing market SAM surfaced by two different offer/ICP contexts
    must collapse to a single open gap.
    """
    common = dict(
        snapshot_key=SNAPSHOT_KEY, vertical_key="commercial_hvac",
        channel_key="multichannel", market_iso2=["PL"],
    )
    contextual_service.run_contextual_ranking(
        seeded_session,
        icp_key="boro_field_service_midmarket_v1",
        offer_key="operations_os_core",
        ticket_usd=20000,
        **common,
    )
    seeded_session.flush()
    contextual_service.run_contextual_ranking(
        seeded_session, offer_key="operations_architecture_sprint", ticket_usd=3000,
        **common,
    )
    seeded_session.flush()

    sam_gaps = seeded_session.scalars(
        select(ResearchGap).where(
            ResearchGap.metric_key == "market_size_estimate.sam_min"
        )
    ).all()
    assert len(sam_gaps) == 1
    # A market-level gap records no vertical/offer/ICP scoping.
    assert sam_gaps[0].vertical_id is None
    assert sam_gaps[0].offer_id is None
    assert sam_gaps[0].icp_id is None


def test_vertical_scoped_gap_keeps_its_vertical(seeded_session) -> None:
    contextual_service.run_contextual_ranking(
        seeded_session, snapshot_key=SNAPSHOT_KEY, vertical_key="commercial_hvac",
        channel_key="multichannel", ticket_usd=3000, market_iso2=["US"],
    )
    seeded_session.flush()
    gaps = seeded_session.scalars(
        select(ResearchGap).where(
            ResearchGap.metric_key == "market_vertical_profile.fit_score"
        )
    ).all()
    assert gaps
    assert all(g.vertical_id is not None for g in gaps)
