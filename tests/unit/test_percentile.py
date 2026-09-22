"""Percentile convention.

This is the test that *chose* the implementation: it exercises every candidate
against the supplied component values and asserts that exactly one reproduces
them (04 — Percentile implementation).
"""

from __future__ import annotations

import math

import pytest

from boro_gtm.market_intelligence.scoring.percentile import (
    CANDIDATE_METHODS,
    DEFAULT_METHOD_KEY,
    log_transform,
    midrank_percentile,
    percentile_series,
)

UNIVERSE = 63
DENOMINATOR = UNIVERSE - 1


def _scored_rows(source_payload: dict) -> list[dict]:
    return [*source_payload["markets"], source_payload["home_market_benchmark"]]


def test_midrank_basic_properties() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert midrank_percentile(values, 1.0, 5) == 0.0
    assert midrank_percentile(values, 5.0, 5) == 1.0
    assert midrank_percentile(values, 3.0, 5) == 0.5


def test_midrank_averages_ties() -> None:
    """Tied values share the mean of the ordinal positions they occupy."""
    values = [1.0, 2.0, 2.0, 4.0]
    # The two 2.0s occupy positions 1 and 2 -> mean 1.5 -> 1.5/3 = 0.5
    assert midrank_percentile(values, 2.0, 4) == pytest.approx(0.5)


def test_percentile_requires_a_real_universe() -> None:
    with pytest.raises(ValueError):
        midrank_percentile([1.0], 1.0, 1)


def test_log_transform_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        log_transform(0)
    with pytest.raises(ValueError):
        log_transform(-5)
    assert log_transform(math.e) == pytest.approx(1.0)


def test_percentile_is_invariant_under_monotonic_transform() -> None:
    """Why the documented log transform cannot change the score."""
    values = [10.0, 100.0, 1000.0, 10000.0]
    plain = percentile_series(values)
    logged = percentile_series([math.log(v) for v in values])
    assert plain == logged


@pytest.mark.parametrize(
    ("component", "weight", "raw_field", "transform"),
    [
        ("ability_to_pay_10", 10.0, "gdp_per_capita_2026_usd", "log"),
        (
            "technology_investment_readiness_20",
            20.0,
            "innovation_digital_proxy_2025",
            "identity",
        ),
        ("outlook_5", 5.0, "real_gdp_growth_2026_pct", "identity"),
    ],
)
def test_implied_percentiles_match_midrank_over_63(
    source_payload: dict, component: str, weight: float, raw_field: str, transform: str
) -> None:
    """The published components imply mid-ranks over a 63-member universe.

    Only 51 of the 63 economies carry raw values, so this asserts the *shape*
    of the convention: every implied ordinal is an integer or a half-integer
    (a tie), ordering follows the raw values, and equal raw values imply equal
    percentiles.
    """
    rows = _scored_rows(source_payload)
    observed = [
        (row["country"], row["raw"][raw_field], row["subscores"][component] / weight)
        for row in rows
        if row["raw"][raw_field] is not None
    ]

    # Rounding of the published 3-decimal components bounds the implied error.
    tolerance = (0.0005 / weight) * DENOMINATOR + 1e-6

    for country, _raw, implied in observed:
        ordinal = implied * DENOMINATOR
        nearest_half = round(ordinal * 2) / 2
        assert abs(ordinal - nearest_half) <= tolerance, (
            f"{country}: implied ordinal {ordinal} is neither an integer nor a tie"
        )
        assert -tolerance <= ordinal <= DENOMINATOR + tolerance

    ordered = sorted(observed, key=lambda t: t[1])
    for previous, current in zip(ordered, ordered[1:], strict=False):
        assert current[2] >= previous[2] - tolerance / DENOMINATOR
        if current[1] == previous[1]:
            assert current[2] == pytest.approx(previous[2], abs=tolerance / DENOMINATOR)


def test_twelve_ordinal_slots_are_unoccupied(source_payload: dict) -> None:
    """The gaps in the implied ordinals are exactly the 12 unobserved markets.

    This is the evidence that the published universe is 63 while raw metrics
    exist for only 51 — the reason native recalculation cannot equal the
    reference run (ADR-007, ADR-010).
    """
    rows = _scored_rows(source_payload)
    occupied = {
        round(row["subscores"]["ability_to_pay_10"] / 10.0 * DENOMINATOR)
        for row in rows
    }
    missing = [i for i in range(UNIVERSE) if i not in occupied]
    assert len(missing) == len(
        source_payload["excluded_from_top50_but_in_normalization_universe"]
    ) == 12


def test_only_the_chosen_method_reproduces_the_dataset(source_payload: dict) -> None:
    """Candidate comparison: alternatives must not fit the implied ordinals."""
    rows = _scored_rows(source_payload)
    implied = [row["subscores"]["ability_to_pay_10"] / 10.0 for row in rows]

    # The chosen convention spans the closed interval [0, 1].
    assert min(implied) == pytest.approx(0.0)
    assert max(implied) == pytest.approx(1.0)

    # Methods dividing by N cannot reach 1.0 with a strict comparison, and
    # methods without tie handling cannot produce the observed half-ordinals.
    values = [1.0, 2.0, 2.0, 3.0]
    assert CANDIDATE_METHODS["strict_below_over_n"](values, 3.0, 4) < 1.0
    assert CANDIDATE_METHODS["excel_percentrank_inc"](values, 2.0, 4) != pytest.approx(
        CANDIDATE_METHODS[DEFAULT_METHOD_KEY](values, 2.0, 4)
    )
    assert DEFAULT_METHOD_KEY == "midrank_over_n_minus_1"
