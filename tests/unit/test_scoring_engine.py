"""Base scoring engine behaviour, independent of the database."""

from __future__ import annotations

import pytest

from boro_gtm.market_intelligence.importers.countries import resolve_country
from boro_gtm.market_intelligence.importers.mappings import RAW_METRIC_MAP
from boro_gtm.market_intelligence.scoring.base_engine import (
    MarketInputs,
    run_native_recalculation,
    run_reference_reproduction,
)
from boro_gtm.market_intelligence.scoring.definitions import (
    BASE_MODEL_COMPONENTS,
    CONTEXTUAL_MODEL_COMPONENTS,
)
from tests.fixtures.golden import (
    GOLDEN_HOME_BENCHMARK,
    GOLDEN_SCORES,
    SCORE_TOLERANCE,
)


def _inputs(source_payload: dict) -> list[MarketInputs]:
    rows = [(m, False) for m in source_payload["markets"]]
    rows.append((source_payload["home_market_benchmark"], True))
    out = []
    for entry, is_home in rows:
        metrics = {
            spec.metric_key: entry["raw"].get(field)
            for field, spec in RAW_METRIC_MAP.items()
        }
        out.append(
            MarketInputs(
                market_key=resolve_country(entry["country"]).iso2,
                name=entry["country"],
                metrics=metrics,
                imported_components=dict(entry["subscores"]),
                imported_total=entry["market_score"],
                imported_rank=entry.get("rank"),
                fact_types={
                    spec.metric_key: spec.default_fact_type
                    for spec in RAW_METRIC_MAP.values()
                },
                confidence_label=entry.get("confidence_level"),
                is_home_market=is_home,
            )
        )
    return out


def test_model_weights_sum_to_100() -> None:
    assert sum(c["weight"] for c in BASE_MODEL_COMPONENTS) == 100.0
    assert sum(c["weight"] for c in CONTEXTUAL_MODEL_COMPONENTS) == 100.0


@pytest.mark.parametrize(("iso2", "expected"), sorted(GOLDEN_SCORES.items()))
def test_reference_reproduction_matches_golden_scores(
    source_payload: dict, iso2: str, expected: tuple[float, int]
) -> None:
    result = run_reference_reproduction(_inputs(source_payload))
    by_iso = {r.market_key: r for r in result.results}
    score, rank = expected
    assert by_iso[iso2].score == pytest.approx(score, abs=SCORE_TOLERANCE)
    assert by_iso[iso2].rank == rank


def test_reference_reproduction_matches_every_published_score(
    source_payload: dict,
) -> None:
    result = run_reference_reproduction(_inputs(source_payload))
    by_iso = {r.market_key: r for r in result.results}
    rows = [*source_payload["markets"], source_payload["home_market_benchmark"]]
    for entry in rows:
        iso = resolve_country(entry["country"]).iso2
        assert by_iso[iso].score == pytest.approx(
            entry["market_score"], abs=SCORE_TOLERANCE
        )


def test_reference_reproduction_matches_every_published_rank(
    source_payload: dict,
) -> None:
    result = run_reference_reproduction(_inputs(source_payload))
    by_iso = {r.market_key: r for r in result.results}
    for entry in source_payload["markets"]:
        iso = resolve_country(entry["country"]).iso2
        assert by_iso[iso].rank == entry["rank"], iso


def test_home_benchmark_is_scored_but_unranked(source_payload: dict) -> None:
    result = run_reference_reproduction(_inputs(source_payload))
    by_iso = {r.market_key: r for r in result.results}
    iso, score = GOLDEN_HOME_BENCHMARK
    assert by_iso[iso].score == pytest.approx(score, abs=SCORE_TOLERANCE)
    assert by_iso[iso].rank is None


def test_reference_run_is_deterministic(source_payload: dict) -> None:
    first = run_reference_reproduction(_inputs(source_payload))
    second = run_reference_reproduction(_inputs(source_payload))
    assert [(r.market_key, r.score, r.rank) for r in first.results] == [
        (r.market_key, r.score, r.rank) for r in second.results
    ]


def test_gtm_ease_is_natively_exact(source_payload: dict) -> None:
    """The one component with no percentile must reproduce exactly."""
    result = run_native_recalculation(_inputs(source_payload))
    by_iso = {r.market_key: r for r in result.results}
    rows = [*source_payload["markets"], source_payload["home_market_benchmark"]]
    for entry in rows:
        iso = resolve_country(entry["country"]).iso2
        component = next(
            c for c in by_iso[iso].components if c.component_key == "gtm_ease_10"
        )
        assert component.weighted_score == pytest.approx(
            entry["subscores"]["gtm_ease_10"], abs=1e-9
        )


def test_native_mode_marks_icp_density_uncovered(source_payload: dict) -> None:
    """Population is absent, so the component must be uncovered, not zero."""
    result = run_native_recalculation(_inputs(source_payload))
    for market in result.results:
        component = next(
            c for c in market.components if c.component_key == "icp_density_proxy_20"
        )
        assert component.coverage == 0.0
        assert component.weighted_score is None
        assert component.metadata.get("native_blocked") is True
    # 80 of 100 weight remains computable.
    assert all(m.coverage == pytest.approx(0.8) for m in result.results)


def test_native_mode_emits_population_research_gaps(source_payload: dict) -> None:
    result = run_native_recalculation(_inputs(source_payload))
    metrics = {metric for _, metric, _ in result.gaps}
    assert metrics == {"population"}
    assert len(result.gaps) == len(result.results)


def test_native_mode_declares_its_universe(source_payload: dict) -> None:
    result = run_native_recalculation(_inputs(source_payload))
    universe = result.universe_definition
    assert universe["method"] == "percentile_over_observed_subuniverse"
    # Software spending is unknown for 21 markets and must not be silently
    # counted as observed.
    assert universe["observed_value_counts"]["gdp_per_capita_usd"] == 51


def test_missing_component_is_excluded_not_zeroed() -> None:
    """A market missing one component keeps a score over covered weight."""
    full = MarketInputs(
        market_key="AA", name="Alpha",
        imported_components={c["component_key"]: c["weight"] for c in BASE_MODEL_COMPONENTS},
    )
    partial = MarketInputs(
        market_key="BB", name="Beta",
        imported_components={
            c["component_key"]: c["weight"]
            for c in BASE_MODEL_COMPONENTS
            if c["component_key"] != "outlook_5"
        },
    )
    result = run_reference_reproduction([full, partial])
    by_key = {r.market_key: r for r in result.results}

    assert by_key["AA"].score == pytest.approx(100.0)
    assert by_key["AA"].coverage == pytest.approx(1.0)
    # 95 of 100 weight, and coverage reports the shortfall honestly.
    assert by_key["BB"].score == pytest.approx(95.0)
    assert by_key["BB"].coverage == pytest.approx(0.95)
    outlook = next(
        c for c in by_key["BB"].components if c.component_key == "outlook_5"
    )
    assert outlook.weighted_score is None
    assert outlook.coverage == 0.0
