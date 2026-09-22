"""Base market-attractiveness scoring engine.

Two modes, per ADR-007:

``reference_reproduction``
    Recompute each market's total from the component values imported with the
    snapshot. This is the deterministic parity path and must reproduce the
    published scores and ranks exactly.

``native_recalculation``
    Recompute components from raw observations, and only where every required
    raw metric is present. Nothing is reverse-engineered. Components that
    cannot be rebuilt are excluded from *both* the numerator and the
    denominator::

        covered_weight = sum(weight of scoreable components)
        score          = earned_points / covered_weight * 100
        coverage       = covered_weight / total_model_weight

    Missing evidence therefore lowers coverage without depressing the score,
    so an under-evidenced market is never ranked below a measured-poor one
    merely for being under-evidenced (A-8).

Ranking in both modes is gated on ``minimum_rank_coverage``: a result below the
model's threshold keeps its score and coverage but is returned unranked.

Known, deliberate limitation of the native mode against this snapshot: the
published percentiles were taken over a 63-economy universe, but raw metrics
exist for only 51 of those economies. A native run therefore percentile-ranks
over the *observed* sub-universe and records that fact in
``universe_definition``. Native scores are consequently not expected to equal
reference scores, and the engine never pretends otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boro_gtm.market_intelligence.scoring.confidence import (
    aggregate_confidence,
    evidence_confidence,
)
from boro_gtm.market_intelligence.scoring.definitions import (
    BASE_MINIMUM_RANK_COVERAGE,
    BASE_MODEL_COMPONENTS,
    BaseComponent,
)
from boro_gtm.market_intelligence.scoring.percentile import (
    DEFAULT_METHOD_KEY,
    get_method,
    log_transform,
)
from boro_gtm.market_intelligence.scoring.ranking import assign_competition_ranks

_TRANSFORMS = {
    "log": log_transform,
    "identity": lambda v: v,
}


@dataclass(slots=True)
class MarketInputs:
    """Everything the engine needs about one market, already normalized."""

    market_key: str
    name: str
    #: metric_key -> value (``None`` means observed-as-unknown)
    metrics: dict[str, float | None] = field(default_factory=dict)
    #: component_key -> value supplied by the snapshot
    imported_components: dict[str, float] = field(default_factory=dict)
    imported_total: float | None = None
    imported_rank: int | None = None
    #: metric_key -> fact type (None when the metric is unavailable)
    fact_types: dict[str, str | None] = field(default_factory=dict)
    #: metric_key -> period granularity, feeding the recency factor
    period_granularity: dict[str, str | None] = field(default_factory=dict)
    #: The source's own HIGH/MEDIUM/LOW assessment for this market
    confidence_label: str | None = None
    #: Home-market benchmarks contribute to the normalization universe but are
    #: excluded from the international ranking (``metadata
    #: .international_top50_excludes_home_market``).
    is_home_market: bool = False

    def metric(self, key: str) -> float | None:
        return self.metrics.get(key)


@dataclass(slots=True)
class ComponentResult:
    component_key: str
    weight: float
    raw_value: float | None
    normalized_value: float | None
    weighted_score: float | None
    coverage: float
    confidence: float
    explanation: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketResult:
    market_key: str
    score: float | None
    confidence: float
    coverage: float
    components: list[ComponentResult]
    rank: int | None = None
    is_home_market: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoreRunResult:
    mode: str
    results: list[MarketResult]
    universe_definition: dict[str, Any]
    #: (market_key, metric_key, reason) triples for the research-gap detector.
    gaps: list[tuple[str, str, str]] = field(default_factory=list)


def _confidence_for(inputs: MarketInputs, metrics: list[str]) -> float:
    """Mean evidence confidence across the metrics a component consumes.

    Delegates to the single confidence algorithm, so fact type, temporal
    precision and the source's own label all participate (A-10).
    """
    if not metrics:
        return 0.0
    values = [
        evidence_confidence(
            inputs.fact_types.get(metric),
            inputs.period_granularity.get(metric),
            inputs.confidence_label,
        )
        for metric in metrics
    ]
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Reference reproduction
# ---------------------------------------------------------------------------


def run_reference_reproduction(
    markets: list[MarketInputs],
    components: list[dict[str, Any]] | None = None,
    minimum_rank_coverage: float = BASE_MINIMUM_RANK_COVERAGE,
) -> ScoreRunResult:
    """Sum the imported component values into a total, deterministically.

    Reference reproduction is **not** renormalized: the published total is the
    sum of the published components, and reproducing it exactly is the whole
    point of this mode. Coverage is still reported, and ranking is still gated
    on it.
    """
    specs = components or BASE_MODEL_COMPONENTS
    results: list[MarketResult] = []

    for market in markets:
        comp_results: list[ComponentResult] = []
        total = 0.0
        covered_weight = 0.0
        total_weight = 0.0

        for spec in specs:
            key = spec["component_key"]
            weight = float(spec["weight"])
            total_weight += weight
            value = market.imported_components.get(key)
            if value is None:
                comp_results.append(
                    ComponentResult(
                        component_key=key,
                        weight=weight,
                        raw_value=None,
                        normalized_value=None,
                        weighted_score=None,
                        coverage=0.0,
                        confidence=0.0,
                        explanation=(
                            f"Snapshot supplied no value for {key}; excluded from the total."
                        ),
                    )
                )
                continue

            total += value
            covered_weight += weight
            comp_results.append(
                ComponentResult(
                    component_key=key,
                    weight=weight,
                    raw_value=value,
                    normalized_value=value / weight if weight else None,
                    weighted_score=value,
                    coverage=1.0,
                    confidence=_confidence_for(market, spec.get("required_metrics", [])),
                    explanation=(
                        f"Imported from snapshot: {key} = {value} "
                        f"(max {weight}). Formula of record: {spec.get('formula')}"
                    ),
                    metadata={"source": "snapshot_subscore"},
                )
            )

        coverage = covered_weight / total_weight if total_weight else 0.0
        confidence = aggregate_confidence(comp_results)
        results.append(
            MarketResult(
                market_key=market.market_key,
                score=round(total, 6),
                confidence=confidence,
                coverage=coverage,
                components=comp_results,
                is_home_market=market.is_home_market,
                metadata={
                    "mode": "reference_reproduction",
                    "covered_weight": covered_weight,
                    "total_weight": total_weight,
                    "renormalized": False,
                    "score_definition": "sum of imported component values",
                },
            )
        )

    _assign_ranks(results, minimum_rank_coverage)
    return ScoreRunResult(
        mode="reference_reproduction",
        results=results,
        universe_definition={
            "method": "imported_component_sum",
            "member_count": len(markets),
            "members": sorted(m.market_key for m in markets),
            "note": (
                "Totals are the arithmetic sum of snapshot-supplied component "
                "values. No percentile normalization is performed in this mode."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Native recalculation
# ---------------------------------------------------------------------------


def run_native_recalculation(
    markets: list[MarketInputs],
    components: list[dict[str, Any]] | None = None,
    percentile_method_key: str = DEFAULT_METHOD_KEY,
    universe_size: int | None = None,
    minimum_rank_coverage: float = BASE_MINIMUM_RANK_COVERAGE,
) -> ScoreRunResult:
    """Recompute components from raw metrics only.

    Args:
        markets: every market considered part of the normalization universe.
        universe_size: ``N`` for the percentile denominator. Defaults to the
            number of markets that actually have an observed value for the
            metric being ranked — i.e. the honest observed sub-universe.
    """
    specs = components or BASE_MODEL_COMPONENTS
    method = get_method(percentile_method_key)
    gaps: list[tuple[str, str, str]] = []

    # Pre-compute the observed value series for each metric once.
    series: dict[str, list[float]] = {}
    for spec in specs:
        for term in spec.get("terms", []):
            metric = term["metric"]
            if metric in series:
                continue
            transform = _TRANSFORMS[term.get("transform", "identity")]
            observed = [
                transform(m.metrics[metric])
                for m in markets
                if m.metrics.get(metric) is not None
            ]
            series[metric] = observed

    # digitalization_opportunity needs a percentile *of a percentile*, so the
    # inner percentiles must exist for the whole universe before ranking.
    nested_products: dict[str, dict[str, float]] = {}
    for spec in specs:
        if spec.get("kind") != "nested_percentile_product":
            continue
        products: dict[str, float] = {}
        for market in markets:
            factors: list[float] = []
            usable = True
            for term in spec["terms"]:
                metric = term["metric"]
                value = market.metrics.get(metric)
                if value is None:
                    usable = False
                    break
                transform = _TRANSFORMS[term.get("transform", "identity")]
                n = universe_size or len(series[metric])
                p = method(series[metric], transform(value), n)
                factors.append(1.0 - p if term.get("invert") else p)
            if usable:
                product = 1.0
                for f in factors:
                    product *= f
                products[market.market_key] = product
        nested_products[spec["component_key"]] = products

    results: list[MarketResult] = []
    for market in markets:
        comp_results: list[ComponentResult] = []
        total = 0.0
        covered_weight = 0.0
        total_weight = 0.0

        for spec in specs:
            key = spec["component_key"]
            weight = float(spec["weight"])
            total_weight += weight
            required = spec.get("required_metrics", [])

            # Components whose prerequisites the dataset never supplies.
            if not spec.get("natively_computable", True):
                blocker = spec.get("native_blocker", "Raw prerequisites unavailable.")
                missing = [m for m in required if market.metrics.get(m) is None]
                for metric in missing:
                    gaps.append((market.market_key, metric, blocker))
                comp_results.append(
                    ComponentResult(
                        component_key=key,
                        weight=weight,
                        raw_value=None,
                        normalized_value=None,
                        weighted_score=None,
                        coverage=0.0,
                        confidence=0.0,
                        explanation=blocker,
                        metadata={"missing_metrics": missing, "native_blocked": True},
                    )
                )
                continue

            missing = [m for m in required if market.metrics.get(m) is None]
            if missing:
                for metric in missing:
                    gaps.append(
                        (
                            market.market_key,
                            metric,
                            f"Required by component {key}; no observed value in snapshot.",
                        )
                    )
                comp_results.append(
                    ComponentResult(
                        component_key=key,
                        weight=weight,
                        raw_value=None,
                        normalized_value=None,
                        weighted_score=None,
                        coverage=0.0,
                        confidence=0.0,
                        explanation=(
                            f"Cannot compute {key}: missing {', '.join(missing)}. "
                            "Excluded from the total rather than scored as zero."
                        ),
                        metadata={"missing_metrics": missing},
                    )
                )
                continue

            value, normalized, explanation = _evaluate_component(
                spec, market, series, method, universe_size, nested_products
            )
            total += value
            covered_weight += weight
            comp_results.append(
                ComponentResult(
                    component_key=key,
                    weight=weight,
                    raw_value=value,
                    normalized_value=normalized,
                    weighted_score=value,
                    coverage=1.0,
                    confidence=_confidence_for(market, required),
                    explanation=explanation,
                    metadata={"source": "native_recalculation"},
                )
            )

        coverage = covered_weight / total_weight if total_weight else 0.0
        # Renormalize over covered weight: an uncovered component leaves both
        # the numerator and the denominator, so unknown never reads as zero.
        score = round(total / covered_weight * 100.0, 6) if covered_weight else None
        results.append(
            MarketResult(
                market_key=market.market_key,
                score=score,
                confidence=aggregate_confidence(comp_results),
                coverage=coverage,
                components=comp_results,
                is_home_market=market.is_home_market,
                metadata={
                    "mode": "native_recalculation",
                    "covered_weight": covered_weight,
                    "total_weight": total_weight,
                    "renormalized": True,
                    "earned_points": round(total, 6),
                    "score_definition": "earned_points / covered_weight * 100",
                },
            )
        )

    _assign_ranks(results, minimum_rank_coverage)
    observed_counts = {metric: len(vals) for metric, vals in series.items()}
    return ScoreRunResult(
        mode="native_recalculation",
        results=results,
        universe_definition={
            "method": "percentile_over_observed_subuniverse",
            "percentile_method": percentile_method_key,
            "member_count": len(markets),
            "members": sorted(m.market_key for m in markets),
            "observed_value_counts": observed_counts,
            "declared_universe_size": universe_size,
            "note": (
                "Percentiles are ranked over markets with an observed value for "
                "each metric. Where the snapshot's published universe is larger "
                "than the observed set, native scores legitimately differ from "
                "reference scores; no hidden inputs are reconstructed."
            ),
        },
        gaps=gaps,
    )


def _evaluate_component(
    spec: dict[str, Any],
    market: MarketInputs,
    series: dict[str, list[float]],
    method,
    universe_size: int | None,
    nested_products: dict[str, dict[str, float]],
) -> tuple[float, float, str]:
    """Return ``(weighted_value, normalized_0_1, explanation)``."""
    key = spec["component_key"]
    weight = float(spec["weight"])
    kind = spec.get("kind", "weighted_percentile_sum")

    if kind == "linear":
        # Already 0-1 inputs; no normalization universe involved.
        total = 0.0
        parts: list[str] = []
        for term in spec["terms"]:
            value = market.metrics[term["metric"]]
            contribution = float(term["weight"]) * float(value)
            total += contribution
            parts.append(f"{term['weight']}*{term['metric']}({value})={contribution:.4f}")
        return total, total / weight, f"{key} = " + " + ".join(parts)

    if kind == "nested_percentile_product":
        products = nested_products[key]
        product_values = sorted(products.values())
        n = universe_size or len(product_values)
        p = method(product_values, products[market.market_key], n)
        value = weight * p
        return (
            value,
            p,
            (
                f"{key} = {weight}*P(inner product={products[market.market_key]:.6f}) "
                f"= {weight}*{p:.6f} = {value:.6f}"
            ),
        )

    # Default: weighted sum of percentiles.
    total = 0.0
    parts = []
    for term in spec["terms"]:
        metric = term["metric"]
        transform = _TRANSFORMS[term.get("transform", "identity")]
        raw = market.metrics[metric]
        n = universe_size or len(series[metric])
        p = method(series[metric], transform(raw), n)
        contribution = float(term["weight"]) * p
        total += contribution
        parts.append(f"{term['weight']}*P({metric}={raw})={contribution:.6f}")
    return total, total / weight, f"{key} = " + " + ".join(parts)


def _assign_ranks(
    results: list[MarketResult], minimum_rank_coverage: float = BASE_MINIMUM_RANK_COVERAGE
) -> None:
    """Ordinal ranks over the international set, highest score first.

    Ranks follow the standard competition convention (1, 1, 3, 4): markets the
    model scores identically share a rank, because they have identical standing
    (ADR-020). Excluded markets are filtered out *before* ranking, so an
    unranked market never consumes a rank position.

    Three things leave a result unranked, and none of them is "a low score":

    * it is a home-market benchmark (``international_top50_excludes_home_market``);
    * it has no comparable score at all;
    * its coverage is below the model's ``minimum_rank_coverage``, in which
      case it keeps score and coverage but is not rank-comparable (A-8).
    """
    for result in results:
        if (
            result.score is not None
            and not result.is_home_market
            and result.coverage < minimum_rank_coverage
        ):
            result.metadata = {
                **result.metadata,
                "unranked_reason": "coverage_below_minimum",
                "minimum_rank_coverage": minimum_rank_coverage,
            }

    scored = [
        r
        for r in results
        if r.score is not None
        and not r.is_home_market
        and r.coverage >= minimum_rank_coverage
    ]
    # Standard competition ranking: equal scores share a rank (see ranking.py).
    assign_competition_ranks(scored)


__all__ = [
    "MarketInputs",
    "ComponentResult",
    "MarketResult",
    "ScoreRunResult",
    "run_reference_reproduction",
    "run_native_recalculation",
    BaseComponent.__name__,
]
