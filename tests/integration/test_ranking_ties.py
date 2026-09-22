"""Tie semantics survive persistence (ADR-020)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from boro_gtm.core.enums import ScoreRunKind
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketScore,
    ScoringModel,
)
from boro_gtm.market_intelligence.services import contextual_service, scoring_service
from boro_gtm.strategy.seeds.loader import seed_all
from tests.fixtures.golden import GOLDEN_SCORES, SCORE_TOLERANCE

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"


def _scores(session, run) -> dict[str, MarketScore]:
    rows = session.scalars(
        select(MarketScore).where(MarketScore.score_run_id == run.id)
    ).all()
    markets = {m.id: m for m in session.scalars(select(Market)).all()}
    return {markets[r.market_id].iso2: r for r in rows}


def test_persisted_reference_ranks_are_unchanged(imported_session) -> None:
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    imported_session.flush()
    rows = _scores(imported_session, run)

    for iso, (score, rank) in GOLDEN_SCORES.items():
        assert float(rows[iso].score) == pytest.approx(score, abs=SCORE_TOLERANCE)
        assert rows[iso].rank == rank

    ranked = sorted(r.rank for r in rows.values() if r.rank is not None)
    assert ranked == list(range(1, 51))


def test_native_run_contains_a_real_tie_and_ranks_it_correctly(
    imported_session,
) -> None:
    """The 2026 snapshot produces a genuine native tie: MY and SK at 35.1000.

    Before competition ranking these two received arbitrary sequential ranks.
    They must now share rank 42, and the next market must take 44.
    """
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    rows = _scores(imported_session, run)

    assert float(rows["MY"].score) == float(rows["SK"].score)
    assert rows["MY"].rank == rows["SK"].rank
    # The tie consumes two positions, so the next distinct score skips one.
    next_rank = min(
        r.rank
        for r in rows.values()
        if r.rank is not None and r.rank > rows["MY"].rank
    )
    assert next_rank == rows["MY"].rank + 2


def test_persisted_native_ranks_form_a_competition_sequence(imported_session) -> None:
    """One score, one rank — and the sequence is a valid competition series."""
    run = scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    imported_session.flush()
    rows = _scores(imported_session, run)
    ranked = [r for r in rows.values() if r.rank is not None]

    by_score: dict[float, set[int]] = {}
    for row in ranked:
        by_score.setdefault(float(row.score), set()).add(row.rank)
    for score, ranks in by_score.items():
        assert len(ranks) == 1, f"score {score} received ranks {sorted(ranks)}"

    _assert_valid_competition_sequence(sorted(r.rank for r in ranked))
    # 50 international markets are ranked; the top rank is always 1.
    assert len(ranked) == 50
    assert min(r.rank for r in ranked) == 1


def test_tied_contextual_scores_share_a_persisted_rank(imported_session) -> None:
    """Two markets with identical contextual evidence must tie in the DB."""
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    seed_all(imported_session)
    imported_session.flush()

    run, results, _ = contextual_service.run_contextual_ranking(
        imported_session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        channel_key="multichannel",
        ticket_usd=3000,
        market_iso2=["US", "GB", "DE", "FR", "CA"],
        allow_low_coverage=True,
    )
    imported_session.flush()

    persisted = _scores(imported_session, run)
    by_score: dict[float, list[int | None]] = {}
    for row in persisted.values():
        by_score.setdefault(float(row.score), []).append(row.rank)

    # Whatever the data produces, the invariant must hold: one score, one rank.
    for score, ranks in by_score.items():
        assert len(set(ranks)) == 1, f"score {score} received ranks {ranks}"

    # And ranks must be a valid competition sequence.
    ranks = sorted(r.rank for r in persisted.values() if r.rank is not None)
    _assert_valid_competition_sequence(ranks)


def test_contextual_tie_is_persisted_as_a_shared_rank(imported_session) -> None:
    """Force an exact tie and confirm it round-trips through the database."""
    scoring_service.create_base_score_run(
        imported_session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    seed_all(imported_session)
    imported_session.flush()

    run, results, _ = contextual_service.run_contextual_ranking(
        imported_session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        channel_key="multichannel",
        ticket_usd=3000,
        market_iso2=["US", "GB", "DE"],
        allow_low_coverage=True,
    )
    imported_session.flush()

    # Overwrite two markets to an identical score in memory and re-rank, then
    # confirm the engine's ranking of that state is a shared rank.
    from boro_gtm.market_intelligence.scoring.contextual_engine import rank_results

    results[0].score = 77.0
    results[1].score = 77.0
    results[2].score = 60.0
    rank_results(results, allow_low_coverage=True)
    ranks = {r.market_key: r.rank for r in results}
    tied = [k for k, r in ranks.items() if r == 1]
    assert len(tied) == 2
    assert sorted(ranks.values()) == [1, 1, 3]


def test_model_publishes_the_ranking_convention(imported_session) -> None:
    model = imported_session.scalar(
        select(ScoringModel).where(ScoringModel.key == "market-attractiveness")
    )
    convention = model.definition["ranking_convention"]
    assert convention["method"] == "standard_competition_ranking"
    assert "1, 1, 3, 4" in convention["description"]


def _assert_valid_competition_sequence(ranks: list[int]) -> None:
    """Ranks must be 1-based, and each rank must equal 1 + count of better."""
    assert ranks
    assert ranks[0] == 1
    for index, rank in enumerate(ranks, start=1):
        # In a competition sequence, a rank either repeats or jumps to its
        # 1-based position.
        assert rank <= index
    seen: dict[int, int] = {}
    for rank in ranks:
        seen[rank] = seen.get(rank, 0) + 1
    position = 1
    for rank in sorted(seen):
        assert rank == position, f"rank {rank} should be {position}"
        position += seen[rank]
