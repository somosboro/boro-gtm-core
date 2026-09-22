"""M1 contextual engine: unknown data must reduce coverage, never zero fit."""

from __future__ import annotations

import pytest

from boro_gtm.market_intelligence.scoring.contextual_engine import (
    ContextualMarketInputs,
    ContextualRequest,
    evaluate_market,
    rank_results,
)


def _request(**kwargs) -> ContextualRequest:
    defaults = {
        "vertical_key": "commercial_hvac",
        "icp_key": "boro_field_service_midmarket_v1",
        "offer_key": "operations_os_core",
        "channel_key": "multichannel",
        "ticket_usd": 20000.0,
        "min_coverage": 0.5,
    }
    defaults.update(kwargs)
    return ContextualRequest(**defaults)


def _full_inputs(**overrides) -> ContextualMarketInputs:
    base = {
        "market_key": "US",
        "name": "United States",
        "base_score": 81.908,
        "has_vertical_profile": True,
        "vertical_fit_score": 80.0,
        "vertical_profile_confidence": 0.6,
        "sam_firms_min": 50000.0,
        "sam_firms_max": 90000.0,
        "sam_fact_type": "ESTIMATE",
        "ticket_min_usd": 20000.0,
        "ticket_max_usd": 60000.0,
        "metrics": {
            "language_access": 1.0,
            "timezone_overlap": 0.95,
            "b2b_email_legal_access": 1.0,
        },
        "recommended_channel": "Email + phone + LinkedIn + associations",
    }
    base.update(overrides)
    return ContextualMarketInputs(**base)


def test_full_evidence_yields_full_coverage() -> None:
    result = evaluate_market(_full_inputs(), _request())
    assert result.coverage == pytest.approx(1.0)
    assert result.score is not None
    assert result.comparable is True
    assert all(c.coverage == 1.0 for c in result.components)


def test_score_confidence_and_coverage_are_independent() -> None:
    result = evaluate_market(_full_inputs(), _request())
    assert 0.0 <= result.confidence <= 1.0
    assert 0.0 <= result.coverage <= 1.0
    assert result.score is not None and 0.0 <= result.score <= 100.0
    # A high score must not imply high confidence.
    assert result.score / 100.0 != pytest.approx(result.confidence)


def test_missing_vertical_profile_lowers_coverage_not_score() -> None:
    """The core M1 guarantee (06 — acceptance C)."""
    known = evaluate_market(_full_inputs(), _request())
    unknown = evaluate_market(
        _full_inputs(has_vertical_profile=False, vertical_fit_score=None), _request()
    )

    assert unknown.coverage < known.coverage
    assert unknown.coverage == pytest.approx(0.75)

    component = next(
        c for c in unknown.components if c.component_key == "vertical_density_fit"
    )
    assert component.coverage == 0.0
    assert component.weighted_score is None
    assert component.missing_metrics

    # The decisive assertion: the score is NOT dragged toward zero.
    zero_fit_score = sum(
        c.weighted_score or 0.0 for c in unknown.components
    )  # what a naive "unknown = 0" implementation would report
    assert unknown.score > zero_fit_score
    assert unknown.score > 50.0


def test_unknown_is_never_worse_than_a_measured_low_value() -> None:
    unknown = evaluate_market(_full_inputs(has_vertical_profile=False), _request())
    measured_low = evaluate_market(_full_inputs(vertical_fit_score=1.0), _request())
    assert unknown.score > measured_low.score
    assert unknown.coverage < measured_low.coverage


def test_total_absence_of_evidence_yields_no_score() -> None:
    barren = ContextualMarketInputs(market_key="ZZ", name="Nowhere")
    result = evaluate_market(barren, _request())
    assert result.score is None
    assert result.coverage == 0.0
    assert result.confidence == 0.0
    assert result.comparable is False


def test_low_coverage_blocks_comparability_unless_overridden() -> None:
    sparse = ContextualMarketInputs(
        market_key="ZZ", name="Nowhere", base_score=60.0, metrics={}
    )
    blocked = evaluate_market(sparse, _request(min_coverage=0.6))
    assert blocked.comparable is False
    assert any("below the minimum" in n for n in blocked.notes)

    allowed = evaluate_market(
        sparse, _request(min_coverage=0.6, allow_low_coverage=True)
    )
    assert allowed.comparable is True
    assert allowed.score == pytest.approx(blocked.score)


def test_low_coverage_markets_are_not_ranked_by_default() -> None:
    strong = evaluate_market(_full_inputs(), _request())
    sparse_inputs = ContextualMarketInputs(
        market_key="ZZ", name="Nowhere", base_score=99.0
    )
    sparse = evaluate_market(sparse_inputs, _request(min_coverage=0.6))
    rank_results([strong, sparse])
    assert strong.rank == 1
    # Despite a higher base prior, it cannot outrank on 35% coverage.
    assert sparse.rank is None

    reranked = rank_results([strong, sparse], allow_low_coverage=True)
    assert all(r.rank is not None for r in reranked)


def test_channel_accessibility_renormalizes_over_observed_inputs() -> None:
    partial = _full_inputs(metrics={"language_access": 1.0})
    result = evaluate_market(partial, _request(channel_key="multichannel"))
    component = next(
        c for c in result.components if c.component_key == "channel_accessibility"
    )
    assert component.coverage == 1.0
    assert component.normalized_value == pytest.approx(1.0)
    assert component.missing_metrics  # the unobserved inputs are still reported
    assert component.confidence < 0.5  # partial evidence lowers confidence


def test_unknown_channel_configuration_is_uncovered() -> None:
    result = evaluate_market(_full_inputs(), _request(channel_key="carrier_pigeon"))
    component = next(
        c for c in result.components if c.component_key == "channel_accessibility"
    )
    assert component.coverage == 0.0


def test_ticket_outside_range_decays_and_does_not_crash() -> None:
    inside = evaluate_market(_full_inputs(), _request(ticket_usd=30000))
    outside = evaluate_market(_full_inputs(), _request(ticket_usd=1000))
    inside_c = next(
        c for c in inside.components if c.component_key == "ticket_compatibility"
    )
    outside_c = next(
        c for c in outside.components if c.component_key == "ticket_compatibility"
    )
    assert inside_c.normalized_value == pytest.approx(1.0)
    assert 0.0 <= outside_c.normalized_value < 1.0
    # Still covered: we measured it, and it is a poor fit.
    assert outside_c.coverage == 1.0


def test_every_component_carries_an_explanation() -> None:
    result = evaluate_market(_full_inputs(has_vertical_profile=False), _request())
    for component in result.components:
        assert component.explanation
        assert len(component.explanation) > 20
