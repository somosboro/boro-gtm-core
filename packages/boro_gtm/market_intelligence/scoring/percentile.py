"""Centralised, deterministic percentile implementations.

04 requires that candidate percentile conventions be implemented explicitly and
tested against the supplied component values rather than tuned per country.

**Result of that exercise** (see ``tests/unit/test_percentile.py`` and
ADR-009): the snapshot uses :func:`midrank_percentile` — the mean 0-indexed
ordinal position among ties, divided by ``N - 1`` — over a universe of
``N = 63`` economies::

    P(x) = ( count(v < x) + (count(v == x) - 1) / 2 ) / (N - 1)

This reproduces every single-metric component exactly (``ability_to_pay_10``,
``technology_investment_readiness_20``, ``outlook_5``), the implied
``P(log GDP)`` inside ``economic_strength_20``, and the nested percentile in
``digitalization_opportunity_15``.

Two consequences worth stating plainly:

* The lowest-valued market scores ``0.0`` and the highest scores ``1.0``.
* Because the convention is purely **ordinal**, the ``log`` transform named in
  the source formula has *no effect* on the result — percentile rank is
  invariant under any strictly monotonic transform. The log is retained in the
  formula strings for fidelity to the published methodology, and
  :func:`log_transform` is applied so that the recorded ``raw_value`` matches
  what the formula describes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

Percentile = Callable[[Sequence[float], float, int], float]


def _check(universe_size: int) -> None:
    if universe_size < 2:
        raise ValueError("Percentile requires a universe of at least 2 members")


def midrank_percentile(values: Sequence[float], x: float, universe_size: int) -> float:
    """Mean ordinal rank among ties, scaled to ``[0, 1]`` over ``N - 1``.

    This is the convention used by the 2026 snapshot.

    Args:
        values: observed values in the normalization universe.
        x: the value to rank.
        universe_size: ``N``. May exceed ``len(values)`` when part of the
            universe is unobserved; the caller is then responsible for
            declaring the reduced coverage.
    """
    _check(universe_size)
    below = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return (below + (equal - 1) / 2.0) / (universe_size - 1)


def strict_below_percentile(values: Sequence[float], x: float, universe_size: int) -> float:
    """``count(v < x) / N`` — classic 'percent below'."""
    _check(universe_size)
    return sum(1 for v in values if v < x) / universe_size


def weak_below_percentile(values: Sequence[float], x: float, universe_size: int) -> float:
    """``count(v <= x) / N``."""
    _check(universe_size)
    return sum(1 for v in values if v <= x) / universe_size


def midpoint_over_n_percentile(values: Sequence[float], x: float, universe_size: int) -> float:
    """``(count(<x) + 0.5 * count(==x)) / N`` — Hazen-like midpoint."""
    _check(universe_size)
    below = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return (below + 0.5 * equal) / universe_size


def excel_percentrank_inc(values: Sequence[float], x: float, universe_size: int) -> float:
    """``count(v < x) / (N - 1)`` — Excel ``PERCENTRANK.INC`` without ties handling."""
    _check(universe_size)
    return sum(1 for v in values if v < x) / (universe_size - 1)


#: Every candidate considered, so the choice stays auditable.
CANDIDATE_METHODS: dict[str, Percentile] = {
    "midrank_over_n_minus_1": midrank_percentile,
    "strict_below_over_n": strict_below_percentile,
    "weak_below_over_n": weak_below_percentile,
    "midpoint_over_n": midpoint_over_n_percentile,
    "excel_percentrank_inc": excel_percentrank_inc,
}

#: The convention proven to reproduce the snapshot.
DEFAULT_METHOD_KEY = "midrank_over_n_minus_1"
DEFAULT_METHOD: Percentile = midrank_percentile


def get_method(key: str = DEFAULT_METHOD_KEY) -> Percentile:
    try:
        return CANDIDATE_METHODS[key]
    except KeyError as exc:
        raise ValueError(
            f"Unknown percentile method {key!r}; known: {sorted(CANDIDATE_METHODS)}"
        ) from exc


def log_transform(value: float) -> float:
    """Natural log used by the published formula for scale-heavy metrics.

    Raises:
        ValueError: for non-positive input, which would be a data error rather
            than something to silently clamp.
    """
    if value <= 0:
        raise ValueError(f"log transform requires a positive value, got {value}")
    return math.log(value)


def percentile_series(
    values: Sequence[float],
    universe_size: int | None = None,
    method_key: str = DEFAULT_METHOD_KEY,
) -> list[float]:
    """Percentile-rank an entire series in one pass."""
    method = get_method(method_key)
    n = universe_size if universe_size is not None else len(values)
    return [method(values, v, n) for v in values]
