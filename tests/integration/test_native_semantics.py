"""A-8 / A-9 / A-11 — native scoring semantics, gap persistence, fail-closed."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from boro_gtm.core.enums import ScoreRunKind, ScoreRunStatus
from boro_gtm.core.errors import ScoreReproductionFailedError
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketScore,
    MarketScoreComponent,
    ScoreRun,
    ScoringModel,
)
from boro_gtm.market_intelligence.services import scoring_service
from boro_gtm.strategy.domain.models import ResearchGap
from tests.fixtures.golden import GOLDEN_SCORES, SCORE_TOLERANCE

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"


def _scores(session, run) -> dict[str, MarketScore]:
    rows = session.scalars(
        select(MarketScore).where(MarketScore.score_run_id == run.id)
    ).all()
    markets = {m.id: m for m in session.scalars(select(Market)).all()}
    return {markets[r.market_id].iso2: r for r in rows}


# ---------------------------------------------------------------------------
# A-8 — covered-weight renormalization
# ---------------------------------------------------------------------------


def test_native_score_is_renormalized_over_covered_weight(imported_session) -> None:
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()

    for iso, row in _scores(imported_session, run).items():
        components = imported_session.scalars(
            select(MarketScoreComponent).where(
                MarketScoreComponent.market_score_id == row.id
            )
        ).all()
        earned = sum(
            float(c.weighted_score) for c in components if c.weighted_score is not None
        )
        covered_weight = sum(
            float(c.component_metadata.get("weight", 0) or 0)
            if c.component_metadata
            else 0.0
            for c in components
            if c.coverage and float(c.coverage) > 0
        ) or float(row.coverage) * 100.0

        expected = earned / covered_weight * 100.0
        assert float(row.score) == pytest.approx(expected, abs=1e-4), iso
        # The raw sum is retained for audit, and is NOT the score.
        assert float(row.score_metadata["earned_points"]) == pytest.approx(earned, abs=1e-4)
        assert row.score_metadata["renormalized"] is True


def test_native_coverage_and_score_are_separate(imported_session) -> None:
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    rows = _scores(imported_session, run)
    # 80 of 100 weight is computable; the score is on a full 0-100 scale.
    assert all(float(r.coverage) == pytest.approx(0.8) for r in rows.values())
    assert max(float(r.score) for r in rows.values()) > 70.0


def test_unequal_coverage_does_not_penalise_missing_evidence(imported_session) -> None:
    """The core A-8 regression, with deliberately unequal coverage.

    Two markets earn the *same proportion* of the weight they can be scored
    on, but one can be scored on far less of the model. Their scores must be
    equal; only coverage may differ.
    """
    from boro_gtm.market_intelligence.scoring.base_engine import (
        MarketInputs,
        run_native_recalculation,
    )

    specs = [
        {
            "component_key": "full",
            "weight": 80.0,
            "required_metrics": ["a"],
            "kind": "linear",
            "terms": [{"weight": 80.0, "metric": "a"}],
            "natively_computable": True,
        },
        {
            "component_key": "partial",
            "weight": 20.0,
            "required_metrics": ["b"],
            "kind": "linear",
            "terms": [{"weight": 20.0, "metric": "b"}],
            "natively_computable": True,
        },
    ]
    well_evidenced = MarketInputs(
        market_key="AA", name="Alpha", metrics={"a": 0.5, "b": 0.5}
    )
    under_evidenced = MarketInputs(
        market_key="BB", name="Beta", metrics={"a": 0.5, "b": None}
    )

    result = run_native_recalculation(
        [well_evidenced, under_evidenced], specs, minimum_rank_coverage=0.0
    )
    by_key = {r.market_key: r for r in result.results}

    # Both earn 50% of what they can be scored on.
    assert by_key["AA"].score == pytest.approx(50.0)
    assert by_key["BB"].score == pytest.approx(50.0)
    # Only coverage distinguishes them.
    assert by_key["AA"].coverage == pytest.approx(1.0)
    assert by_key["BB"].coverage == pytest.approx(0.8)
    # Missing evidence must not push the market down the ranking. Identical
    # scores mean identical standing, so they share a rank (ADR-020) rather
    # than being separated by an arbitrary sequential position.
    assert by_key["AA"].rank == by_key["BB"].rank == 1


def test_low_coverage_keeps_score_but_loses_rank(imported_session) -> None:
    """A result may hold a score while remaining unranked (A-8)."""
    from boro_gtm.market_intelligence.scoring.base_engine import (
        MarketInputs,
        run_native_recalculation,
    )

    specs = [
        {
            "component_key": "big",
            "weight": 70.0,
            "required_metrics": ["a"],
            "kind": "linear",
            "terms": [{"weight": 70.0, "metric": "a"}],
            "natively_computable": True,
        },
        {
            "component_key": "small",
            "weight": 30.0,
            "required_metrics": ["b"],
            "kind": "linear",
            "terms": [{"weight": 30.0, "metric": "b"}],
            "natively_computable": True,
        },
    ]
    rich = MarketInputs(market_key="AA", name="Alpha", metrics={"a": 0.9, "b": 0.9})
    sparse = MarketInputs(market_key="BB", name="Beta", metrics={"a": None, "b": 1.0})

    result = run_native_recalculation(
        [rich, sparse], specs, minimum_rank_coverage=0.5
    )
    by_key = {r.market_key: r for r in result.results}

    assert by_key["AA"].rank == 1
    # BB scores a perfect 100 on the 30% it can be measured on...
    assert by_key["BB"].score == pytest.approx(100.0)
    assert by_key["BB"].coverage == pytest.approx(0.3)
    # ...but 30% coverage is not rank-comparable.
    assert by_key["BB"].rank is None
    assert by_key["BB"].metadata["unranked_reason"] == "coverage_below_minimum"


def test_model_carries_an_explicit_rank_threshold(imported_session) -> None:
    model = imported_session.scalar(
        select(ScoringModel).where(ScoringModel.key == "market-attractiveness")
    )
    assert model.minimum_rank_coverage is not None
    assert 0.0 <= float(model.minimum_rank_coverage) <= 1.0
    assert model.definition["minimum_rank_coverage"] == float(model.minimum_rank_coverage)


def test_reference_reproduction_is_still_exact(imported_session) -> None:
    """Renormalization must not touch the golden path."""
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    imported_session.flush()
    rows = _scores(imported_session, run)
    for iso, (score, rank) in GOLDEN_SCORES.items():
        assert float(rows[iso].score) == pytest.approx(score, abs=SCORE_TOLERANCE)
        assert rows[iso].rank == rank
    assert rows["US"].score_metadata["renormalized"] is False


# ---------------------------------------------------------------------------
# A-9 — native gaps reach the database
# ---------------------------------------------------------------------------


def test_native_run_persists_population_gaps(imported_session) -> None:
    assert imported_session.scalar(select(func.count()).select_from(ResearchGap)) == 0

    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()

    gaps = imported_session.scalars(
        select(ResearchGap).where(ResearchGap.metric_key == "population")
    ).all()
    assert len(gaps) == 51, "one population gap per market with raw data"
    assert {g.status for g in gaps} == {"OPEN"}
    assert {g.priority for g in gaps} == {"HIGH"}
    # population is a market-level fact: no vertical/offer scoping.
    assert all(g.vertical_id is None and g.offer_id is None for g in gaps)
    assert all(g.market_id is not None for g in gaps)


def test_native_gap_persistence_is_idempotent(imported_session) -> None:
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    first = imported_session.scalar(select(func.count()).select_from(ResearchGap))

    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    second = imported_session.scalar(select(func.count()).select_from(ResearchGap))

    assert first == second == 51


def test_reference_run_emits_no_gaps(imported_session) -> None:
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    imported_session.flush()
    assert imported_session.scalar(select(func.count()).select_from(ResearchGap)) == 0


# ---------------------------------------------------------------------------
# A-11 — fail closed
# ---------------------------------------------------------------------------


def test_reference_reproduction_fails_closed_without_its_input(
    imported_session,
) -> None:
    """Deleting the imported reference must produce an error, not an empty run."""
    reference = imported_session.scalar(
        select(ScoreRun).where(ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value)
    )
    imported_session.delete(reference)
    imported_session.flush()

    runs_before = imported_session.scalar(select(func.count()).select_from(ScoreRun))

    with pytest.raises(ScoreReproductionFailedError) as exc:
        scoring_service.create_base_score_run(
            imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
        )

    assert exc.value.code == "SCORE_REPRODUCTION_FAILED"
    assert "imported_reference" in str(exc.value.details["missing"])
    # No run row was created at all — not even a FAILED one with zero results.
    imported_session.flush()
    assert imported_session.scalar(select(func.count()).select_from(ScoreRun)) == runs_before


def test_reference_reproduction_fails_when_components_are_missing(
    imported_session,
) -> None:
    reference = imported_session.scalar(
        select(ScoreRun).where(ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value)
    )
    for score in imported_session.scalars(
        select(MarketScore).where(MarketScore.score_run_id == reference.id)
    ).all():
        for component in score.components:
            imported_session.delete(component)
    imported_session.flush()

    with pytest.raises(ScoreReproductionFailedError) as exc:
        scoring_service.create_base_score_run(
            imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
        )
    assert exc.value.details["missing"] == "market_score_components"


def test_no_completed_zero_result_run_is_ever_created(imported_session) -> None:
    reference = imported_session.scalar(
        select(ScoreRun).where(ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value)
    )
    imported_session.delete(reference)
    imported_session.flush()

    with pytest.raises(ScoreReproductionFailedError):
        scoring_service.create_base_score_run(
            imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
        )
    imported_session.flush()

    empty_completed = [
        run
        for run in imported_session.scalars(
            select(ScoreRun).where(ScoreRun.status == ScoreRunStatus.COMPLETED.value)
        ).all()
        if not run.scores
    ]
    assert empty_completed == []
