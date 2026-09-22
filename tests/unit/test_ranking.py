"""Tie semantics — standard competition ranking (ADR-020).

Equal scores must share a rank, ranking must depend on score alone, and the
reference reproduction path must be untouched by any of it.
"""

from __future__ import annotations

import itertools

import pytest

from boro_gtm.market_intelligence.importers.countries import resolve_country
from boro_gtm.market_intelligence.importers.mappings import RAW_METRIC_MAP
from boro_gtm.market_intelligence.scoring.base_engine import (
    MarketInputs,
    run_native_recalculation,
    run_reference_reproduction,
)
from boro_gtm.market_intelligence.scoring.contextual_engine import (
    ContextualMarketResult,
    rank_results,
)
from boro_gtm.market_intelligence.scoring.definitions import BASE_MODEL_COMPONENTS
from boro_gtm.market_intelligence.scoring.ranking import (
    RANKING_CONVENTION,
    assign_competition_ranks,
)
from tests.fixtures.golden import GOLDEN_SCORES, SCORE_TOLERANCE


class _Row:
    """Minimal structural stand-in for a rankable result."""

    def __init__(self, market_key: str, score: float | None):
        self.market_key = market_key
        self.score = score
        self.rank: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Row({self.market_key}, {self.score}, rank={self.rank})"


def _ranks(rows: list[_Row]) -> dict[str, int | None]:
    assign_competition_ranks(rows)
    return {r.market_key: r.rank for r in rows}


# ---------------------------------------------------------------------------
# The convention itself
# ---------------------------------------------------------------------------


def test_the_documented_example() -> None:
    """80, 80, 72, 65 -> 1, 1, 3, 4."""
    rows = [_Row("A", 80.0), _Row("B", 80.0), _Row("C", 72.0), _Row("D", 65.0)]
    assert _ranks(rows) == {"A": 1, "B": 1, "C": 3, "D": 4}


def test_two_way_tie() -> None:
    rows = [_Row("A", 90.0), _Row("B", 50.0), _Row("C", 50.0)]
    assert _ranks(rows) == {"A": 1, "B": 2, "C": 2}


def test_three_way_tie() -> None:
    rows = [_Row("A", 50.0), _Row("B", 50.0), _Row("C", 50.0)]
    assert _ranks(rows) == {"A": 1, "B": 1, "C": 1}


def test_tie_is_followed_by_the_next_competition_rank() -> None:
    """A three-way tie at the top consumes 1-3; the next score is rank 4."""
    rows = [
        _Row("A", 50.0),
        _Row("B", 50.0),
        _Row("C", 50.0),
        _Row("D", 40.0),
        _Row("E", 30.0),
    ]
    assert _ranks(rows) == {"A": 1, "B": 1, "C": 1, "D": 4, "E": 5}


def test_multiple_separate_ties() -> None:
    rows = [
        _Row("A", 90.0),
        _Row("B", 80.0),
        _Row("C", 80.0),
        _Row("D", 70.0),
        _Row("E", 70.0),
        _Row("F", 60.0),
    ]
    assert _ranks(rows) == {"A": 1, "B": 2, "C": 2, "D": 4, "E": 4, "F": 6}


def test_tie_at_the_bottom() -> None:
    rows = [_Row("A", 90.0), _Row("B", 10.0), _Row("C", 10.0)]
    assert _ranks(rows) == {"A": 1, "B": 2, "C": 2}


def test_no_ties_is_plain_sequential() -> None:
    rows = [_Row("A", 3.0), _Row("B", 2.0), _Row("C", 1.0)]
    assert _ranks(rows) == {"A": 1, "B": 2, "C": 3}


def test_single_result() -> None:
    rows = [_Row("A", 42.0)]
    assert _ranks(rows) == {"A": 1}


def test_empty_input_is_safe() -> None:
    assert assign_competition_ranks([]) == []


# ---------------------------------------------------------------------------
# Rank depends on score, not on input order
# ---------------------------------------------------------------------------


def test_input_order_cannot_change_any_rank() -> None:
    """Every permutation of tied markets must yield identical ranks."""
    scores = {"AA": 80.0, "BB": 80.0, "CC": 72.0, "DD": 65.0}
    expected = {"AA": 1, "BB": 1, "CC": 3, "DD": 4}

    for permutation in itertools.permutations(scores):
        rows = [_Row(key, scores[key]) for key in permutation]
        assert _ranks(rows) == expected, permutation


def test_market_key_does_not_determine_rank_among_ties() -> None:
    """Alphabetical order must not decide who is 'first' in a tie."""
    rows = [_Row("ZZ", 50.0), _Row("AA", 50.0)]
    ranks = _ranks(rows)
    assert ranks["ZZ"] == ranks["AA"] == 1


def test_listing_order_is_still_deterministic() -> None:
    """Rank is order-independent; the returned listing order is stable."""
    first = assign_competition_ranks([_Row("B", 50.0), _Row("A", 50.0)])
    second = assign_competition_ranks([_Row("A", 50.0), _Row("B", 50.0)])
    assert [r.market_key for r in first] == [r.market_key for r in second] == ["A", "B"]


def test_near_ties_are_not_ties() -> None:
    """Only exact equality shares a rank."""
    rows = [_Row("A", 50.000001), _Row("B", 50.0)]
    assert _ranks(rows) == {"A": 1, "B": 2}


# ---------------------------------------------------------------------------
# Native base scoring
# ---------------------------------------------------------------------------


def _linear_specs() -> list[dict]:
    return [
        {
            "component_key": "big",
            "weight": 70.0,
            "required_metrics": ["a"],
            "kind": "linear",
            "terms": [{"weight": 70.0, "metric": "a"}],
            "natively_computable": True,
        },
        {
            "component_key": "mid",
            "weight": 20.0,
            "required_metrics": ["b"],
            "kind": "linear",
            "terms": [{"weight": 20.0, "metric": "b"}],
            "natively_computable": True,
        },
        {
            "component_key": "small",
            "weight": 10.0,
            "required_metrics": ["c"],
            "kind": "linear",
            "terms": [{"weight": 10.0, "metric": "c"}],
            "natively_computable": True,
        },
    ]


def test_native_unequal_coverage_tie_shares_a_rank() -> None:
    """The exact case from the verification evidence: 50/50/50 -> 1/1/1."""
    markets = [
        MarketInputs(market_key="AA", name="full", metrics={"a": 0.5, "b": 0.5, "c": 0.5}),
        MarketInputs(market_key="BB", name="-10%", metrics={"a": 0.5, "b": 0.5, "c": None}),
        MarketInputs(market_key="CC", name="-30%", metrics={"a": 0.5, "b": None, "c": None}),
        MarketInputs(market_key="EE", name="poor", metrics={"a": 0.1, "b": 0.1, "c": 0.1}),
    ]
    result = run_native_recalculation(
        markets, _linear_specs(), minimum_rank_coverage=0.5
    )
    by_key = {r.market_key: r for r in result.results}

    assert by_key["AA"].score == by_key["BB"].score == by_key["CC"].score == 50.0
    # Identical scores, identical standing.
    assert by_key["AA"].rank == by_key["BB"].rank == by_key["CC"].rank == 1
    # ...and the three-way tie consumes ranks 1-3, so the next score is 4.
    assert by_key["EE"].rank == 4
    # Coverage still distinguishes them.
    assert by_key["AA"].coverage != by_key["CC"].coverage


def test_native_tie_with_one_market_below_minimum_coverage() -> None:
    """An unranked market must neither receive nor consume a rank."""
    markets = [
        MarketInputs(market_key="AA", name="full", metrics={"a": 0.5, "b": 0.5, "c": 0.5}),
        MarketInputs(market_key="BB", name="full", metrics={"a": 0.5, "b": 0.5, "c": 0.5}),
        # Same 50.0 score, but only 10% coverage.
        MarketInputs(market_key="DD", name="sparse", metrics={"a": None, "b": None, "c": 0.5}),
        MarketInputs(market_key="EE", name="poor", metrics={"a": 0.1, "b": 0.1, "c": 0.1}),
    ]
    result = run_native_recalculation(
        markets, _linear_specs(), minimum_rank_coverage=0.5
    )
    by_key = {r.market_key: r for r in result.results}

    assert by_key["DD"].score == pytest.approx(50.0)
    assert by_key["DD"].coverage == pytest.approx(0.1)
    assert by_key["DD"].rank is None
    assert by_key["DD"].metadata["unranked_reason"] == "coverage_below_minimum"

    # The two covered ties share rank 1; EE takes 3, not 4 — the excluded
    # market consumed nothing.
    assert by_key["AA"].rank == by_key["BB"].rank == 1
    assert by_key["EE"].rank == 3


def test_native_tie_is_order_independent() -> None:
    specs = _linear_specs()
    base = {
        "AA": {"a": 0.5, "b": 0.5, "c": 0.5},
        "BB": {"a": 0.5, "b": 0.5, "c": None},
        "EE": {"a": 0.1, "b": 0.1, "c": 0.1},
    }
    expected = None
    for permutation in itertools.permutations(base):
        markets = [
            MarketInputs(market_key=k, name=k, metrics=base[k]) for k in permutation
        ]
        result = run_native_recalculation(markets, specs, minimum_rank_coverage=0.5)
        ranks = {r.market_key: r.rank for r in result.results}
        expected = expected or ranks
        assert ranks == expected, permutation
    assert expected == {"AA": 1, "BB": 1, "EE": 3}


def test_home_benchmark_tie_does_not_consume_a_rank() -> None:
    markets = [
        MarketInputs(market_key="AA", name="a", metrics={"a": 0.5, "b": 0.5, "c": 0.5}),
        MarketInputs(
            market_key="CL", name="home", metrics={"a": 0.5, "b": 0.5, "c": 0.5},
            is_home_market=True,
        ),
        MarketInputs(market_key="EE", name="e", metrics={"a": 0.1, "b": 0.1, "c": 0.1}),
    ]
    result = run_native_recalculation(
        markets, _linear_specs(), minimum_rank_coverage=0.5
    )
    by_key = {r.market_key: r for r in result.results}
    assert by_key["CL"].rank is None
    assert by_key["AA"].rank == 1
    assert by_key["EE"].rank == 2


# ---------------------------------------------------------------------------
# Contextual scoring
# ---------------------------------------------------------------------------


def _contextual(market_key: str, score: float | None, coverage: float = 1.0,
                comparable: bool = True) -> ContextualMarketResult:
    return ContextualMarketResult(
        market_key=market_key,
        name=market_key,
        score=score,
        confidence=0.5,
        coverage=coverage,
        comparable=comparable,
        rank=None,
        components=[],
    )


def test_contextual_two_way_tie() -> None:
    results = rank_results(
        [_contextual("AA", 80.0), _contextual("BB", 80.0), _contextual("CC", 72.0)]
    )
    by_key = {r.market_key: r.rank for r in results}
    assert by_key == {"AA": 1, "BB": 1, "CC": 3}


def test_contextual_three_way_tie_then_next_rank() -> None:
    results = rank_results(
        [
            _contextual("AA", 60.0),
            _contextual("BB", 60.0),
            _contextual("CC", 60.0),
            _contextual("DD", 55.0),
        ]
    )
    assert {r.market_key: r.rank for r in results} == {
        "AA": 1, "BB": 1, "CC": 1, "DD": 4
    }


def test_contextual_tie_is_order_independent() -> None:
    scores = {"AA": 80.0, "BB": 80.0, "CC": 72.0}
    expected = {"AA": 1, "BB": 1, "CC": 3}
    for permutation in itertools.permutations(scores):
        results = rank_results([_contextual(k, scores[k]) for k in permutation])
        assert {r.market_key: r.rank for r in results} == expected, permutation


def test_contextual_tie_with_one_below_coverage() -> None:
    results = rank_results(
        [
            _contextual("AA", 80.0),
            _contextual("BB", 80.0),
            _contextual("ZZ", 80.0, coverage=0.2, comparable=False),
            _contextual("CC", 70.0),
        ]
    )
    by_key = {r.market_key: r.rank for r in results}
    assert by_key["AA"] == by_key["BB"] == 1
    assert by_key["ZZ"] is None
    # The uncomparable market consumed nothing: CC is 3, not 4.
    assert by_key["CC"] == 3


def test_contextual_allow_low_coverage_includes_the_tie() -> None:
    results = rank_results(
        [
            _contextual("AA", 80.0),
            _contextual("ZZ", 80.0, coverage=0.2, comparable=False),
            _contextual("CC", 70.0),
        ],
        allow_low_coverage=True,
    )
    by_key = {r.market_key: r.rank for r in results}
    assert by_key["AA"] == by_key["ZZ"] == 1
    assert by_key["CC"] == 3


def test_contextual_listing_order_places_unranked_last() -> None:
    results = rank_results(
        [
            _contextual("ZZ", 80.0, coverage=0.2, comparable=False),
            _contextual("CC", 70.0),
            _contextual("AA", 80.0),
        ]
    )
    assert [r.market_key for r in results] == ["AA", "CC", "ZZ"]


# ---------------------------------------------------------------------------
# Reference reproduction must be untouched
# ---------------------------------------------------------------------------


def _reference_inputs(source_payload: dict) -> list[MarketInputs]:
    rows = [(m, False) for m in source_payload["markets"]]
    rows.append((source_payload["home_market_benchmark"], True))
    return [
        MarketInputs(
            market_key=resolve_country(entry["country"]).iso2,
            name=entry["country"],
            metrics={
                spec.metric_key: entry["raw"].get(field)
                for field, spec in RAW_METRIC_MAP.items()
            },
            imported_components=dict(entry["subscores"]),
            imported_total=entry["market_score"],
            imported_rank=entry.get("rank"),
            is_home_market=is_home,
        )
        for entry, is_home in rows
    ]


def test_reference_reproduction_ranks_are_unchanged(source_payload: dict) -> None:
    """The published ranking must survive the tie-semantics change exactly."""
    result = run_reference_reproduction(_reference_inputs(source_payload))
    by_iso = {r.market_key: r for r in result.results}

    for entry in source_payload["markets"]:
        iso = resolve_country(entry["country"]).iso2
        assert by_iso[iso].rank == entry["rank"], iso
        assert by_iso[iso].score == pytest.approx(
            entry["market_score"], abs=SCORE_TOLERANCE
        )

    for iso, (score, rank) in GOLDEN_SCORES.items():
        assert by_iso[iso].score == pytest.approx(score, abs=SCORE_TOLERANCE)
        assert by_iso[iso].rank == rank


def test_reference_ranks_remain_contiguous_one_to_fifty(source_payload: dict) -> None:
    """No published score is tied, so competition ranking changes nothing."""
    result = run_reference_reproduction(_reference_inputs(source_payload))
    ranks = sorted(r.rank for r in result.results if r.rank is not None)
    assert ranks == list(range(1, 51))

    scores = [r.score for r in result.results]
    assert len(set(scores)) == len(scores), "published data has no tied scores"


def test_reference_reproduction_would_still_tie_correctly() -> None:
    """The mode is not exempt from the convention — the data simply has no ties."""
    specs = BASE_MODEL_COMPONENTS
    full = {c["component_key"]: c["weight"] for c in specs}
    result = run_reference_reproduction(
        [
            MarketInputs(market_key="AA", name="a", imported_components=full),
            MarketInputs(market_key="BB", name="b", imported_components=full),
        ]
    )
    assert {r.market_key: r.rank for r in result.results} == {"AA": 1, "BB": 1}


# ---------------------------------------------------------------------------
# Convention is published
# ---------------------------------------------------------------------------


def test_convention_is_published_in_both_model_definitions() -> None:
    from boro_gtm.market_intelligence.scoring.definitions import (
        base_model_definition,
        contextual_model_definition,
    )

    for definition in (base_model_definition(), contextual_model_definition()):
        convention = definition["ranking_convention"]
        assert convention["method"] == "standard_competition_ranking"
        assert convention["rank_determinant"] == "score only"


def test_one_ranking_algorithm_only() -> None:
    """Both engines must call the same function."""
    from boro_gtm.market_intelligence.scoring import base_engine, contextual_engine

    assert base_engine.assign_competition_ranks is assign_competition_ranks
    assert contextual_engine.assign_competition_ranks is assign_competition_ranks
    assert RANKING_CONVENTION["method"] == "standard_competition_ranking"
