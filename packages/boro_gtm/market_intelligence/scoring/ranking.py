"""The ranking convention.

There is exactly **one** ranking algorithm in this engine, defined here and
used by both the base and the contextual scoring engines.

Standard competition ranking ("1224")
-------------------------------------

Equal scores share a rank, and the next distinct score skips the ranks the tie
consumed::

    80.0 -> 1
    80.0 -> 1
    72.0 -> 3
    65.0 -> 4

Why competition ranking rather than sequential positions: a rank is a claim
about *relative standing*. Two markets the model scores identically have
identical standing, and handing one of them "rank 1" and the other "rank 2"
invents a distinction the evidence does not support — the same class of error
as letting unknown data read as a low score.

Determinism
-----------

A result's **rank is a function of its score alone**. Nothing about input
order, ISO code or query order can change it, which is what makes the same
inputs rank identically no matter how they arrive.

Market key is used only as a stable secondary sort for the *listing order* of
tied results, so that output is reproducible; it never affects the rank value.

Ties are decided by exact equality of the score each engine has already
rounded (6dp for base, 4dp for contextual), so "equal" means equal at the
precision the engine publishes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class Rankable(Protocol):
    """Structural type shared by both engines' market results."""

    market_key: str
    score: float | None
    rank: int | None


def sort_key(result: Rankable) -> tuple[float, str]:
    """Deterministic listing order: best score first, then market key.

    The market key breaks presentation ties only. :func:`assign_competition_ranks`
    gives tied scores the same rank regardless of this ordering.
    """
    return (-(result.score if result.score is not None else 0.0), result.market_key)


def assign_competition_ranks(results: Iterable[Rankable]) -> list[Rankable]:
    """Assign standard competition ranks in place, best score first.

    Args:
        results: the rank-eligible results. Callers filter out anything that
            must stay unranked (home-market benchmarks, results with no score,
            results below ``minimum_rank_coverage``) *before* calling this, so
            an excluded market never consumes a rank position.

    Returns:
        The results in ranked order, for callers that want the sorted list.
    """
    ordered = sorted(results, key=sort_key)

    previous_score: float | None = None
    previous_rank = 0

    for position, result in enumerate(ordered, start=1):
        if previous_score is not None and result.score == previous_score:
            # Same standing as the market above: share its rank, and let the
            # position this tie consumes be skipped by the next distinct score.
            result.rank = previous_rank
        else:
            result.rank = position
            previous_rank = position
            previous_score = result.score

    return ordered


#: Published in ``scoring_models.definition`` so the convention travels with
#: the model rather than living only in code.
RANKING_CONVENTION = {
    "method": "standard_competition_ranking",
    "description": (
        "Equal scores receive the same rank; the next distinct score skips the "
        "positions the tie consumed (1, 1, 3, 4)."
    ),
    "rank_determinant": "score only",
    "tie_equality": "exact equality of the engine-rounded score",
    "listing_order_tiebreak": (
        "market key, for reproducible output order only; never affects rank"
    ),
    "unranked_reasons": [
        "home_market_benchmark",
        "no_comparable_score",
        "coverage_below_minimum",
    ],
}
