"""Score-run integration: golden reproduction through the database."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from boro_gtm.core.enums import ScoreRunKind
from boro_gtm.core.errors import ModelNotFoundError, SnapshotNotFoundError, ValidationError
from boro_gtm.market_intelligence.domain.models import Market, MarketScore, ScoreRun
from boro_gtm.market_intelligence.services import scoring_service
from tests.fixtures.golden import (
    GOLDEN_HOME_BENCHMARK,
    GOLDEN_SCORES,
    SCORE_TOLERANCE,
)

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"


@pytest.fixture
def reference_run(imported_session):
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    imported_session.flush()
    return run


def _scores(session, run) -> dict[str, MarketScore]:
    rows = session.scalars(
        select(MarketScore).where(MarketScore.score_run_id == run.id)
    ).all()
    markets = {m.id: m for m in session.scalars(select(Market)).all()}
    return {markets[r.market_id].iso2: r for r in rows}


@pytest.mark.parametrize(("iso2", "expected"), sorted(GOLDEN_SCORES.items()))
def test_golden_scores_reproduce_from_database(
    imported_session, reference_run, iso2, expected
) -> None:
    scores = _scores(imported_session, reference_run)
    score, rank = expected
    assert float(scores[iso2].score) == pytest.approx(score, abs=SCORE_TOLERANCE)
    assert scores[iso2].rank == rank


def test_home_benchmark_scored_but_unranked(imported_session, reference_run) -> None:
    scores = _scores(imported_session, reference_run)
    iso, expected = GOLDEN_HOME_BENCHMARK
    assert float(scores[iso].score) == pytest.approx(expected, abs=SCORE_TOLERANCE)
    assert scores[iso].rank is None


def test_reference_reproduction_matches_imported_reference(
    imported_session, reference_run
) -> None:
    imported = imported_session.scalar(
        select(ScoreRun).where(ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value)
    )
    comparison = scoring_service.compare_runs(
        imported_session, imported.id, reference_run.id
    )
    assert comparison["max_score_delta"] <= SCORE_TOLERANCE
    assert comparison["rank_mismatches"] == []
    assert comparison["compared_markets"] == 51


def test_every_top50_rank_matches(imported_session, reference_run, source_payload) -> None:
    from boro_gtm.market_intelligence.importers.countries import resolve_country

    scores = _scores(imported_session, reference_run)
    for entry in source_payload["markets"]:
        iso = resolve_country(entry["country"]).iso2
        assert scores[iso].rank == entry["rank"], iso


def test_ranks_are_contiguous_1_to_50(imported_session, reference_run) -> None:
    ranks = sorted(
        r.rank for r in _scores(imported_session, reference_run).values() if r.rank
    )
    assert ranks == list(range(1, 51))


def test_native_run_is_honest_about_coverage(imported_session) -> None:
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    scores = _scores(imported_session, run)

    # icp_density_proxy_20 is 20 of 100 weight and has no raw prerequisite.
    assert all(float(s.coverage) == pytest.approx(0.8) for s in scores.values())
    assert run.universe_definition["method"] == "percentile_over_observed_subuniverse"

    # Native scores legitimately differ; the engine never fakes parity.
    imported = imported_session.scalar(
        select(ScoreRun).where(ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value)
    )
    comparison = scoring_service.compare_runs(imported_session, imported.id, run.id)
    assert comparison["max_score_delta"] > SCORE_TOLERANCE


def test_native_run_covers_only_markets_with_raw_data(imported_session) -> None:
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    assert len(_scores(imported_session, run)) == 51


def test_score_components_are_persisted_with_explanations(
    imported_session, reference_run
) -> None:
    scores = _scores(imported_session, reference_run)
    components = scores["US"].components
    assert len(components) == 7
    assert sum(float(c.weighted_score) for c in components) == pytest.approx(
        81.908, abs=SCORE_TOLERANCE
    )
    for component in components:
        assert component.explanation


def test_runs_are_additive_not_destructive(imported_session, reference_run) -> None:
    """A new run must never mutate an earlier one (ADR-002/ADR-003)."""
    before = {k: float(v.score) for k, v in _scores(imported_session, reference_run).items()}
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    after = {k: float(v.score) for k, v in _scores(imported_session, reference_run).items()}
    assert before == after


def test_unknown_snapshot_and_model_raise(imported_session) -> None:
    with pytest.raises(SnapshotNotFoundError):
        scoring_service.create_base_score_run(
            imported_session, "MI-1999-01-01-V1",
            ScoreRunKind.REFERENCE_REPRODUCTION.value,
        )
    with pytest.raises(ModelNotFoundError):
        scoring_service.create_base_score_run(
            imported_session, SNAPSHOT_KEY,
            ScoreRunKind.REFERENCE_REPRODUCTION.value, "nope", "9.9",
        )


def test_unsupported_mode_rejected(imported_session) -> None:
    with pytest.raises(ValidationError):
        scoring_service.create_base_score_run(imported_session, SNAPSHOT_KEY, "vibes")
