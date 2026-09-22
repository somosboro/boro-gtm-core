"""Base market-attractiveness scoring engine.

Two modes, per ADR-007:

``reference_reproduction``
    Recompute each market's total from the component values imported with the
    snapshot. This is the deterministic parity path and must reproduce the
    published scores and ranks exactly.

``native_recalculation``
    Recompute components from raw observations, and only where every required
    raw metric is present. Nothing is reverse-engineered. Components that
    cannot be rebuilt are reported with ``coverage = 0`` and excluded from the
    total, which lowers the run's coverage and emits research gaps.

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

from boro_gtm.core.enums import FactType
from boro_gtm.market_intelligence.scoring.definitions import (
    BASE_MODEL_COMPONENTS,
    FACT_TYPE_CONFIDENCE,
    BaseComponent,
)
from boro_gtm.market_intelligence.scoring.percentile import (
    DEFAULT_METHOD_KEY,
    get_method,
    log_transform,
)

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
    #: metric_key -> fact type, used for confidence
    fact_types: dict[str, str] = field(default_factory=dict)
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
    """Average fact-type confidence across the metrics a component consumes."""
    if not metrics:
        return 0.0
    values = [
        FACT_TYPE_CONFIDENCE.get(
            inputs.fact_types.get(m, FactType.UNKNOWN.value), 0.0
        )
        for m in metrics
    ]
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Reference reproduction
# ---------------------------------------------------------------------------


def run_reference_reproduction(
    markets: list[MarketInputs],
    components: list[dict[str, Any]] | None = None,
) -> ScoreRunResult:
    """Sum the imported component values into a total, deterministically."""
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
        confidence = _aggregate_confidence(comp_results, market.confidence_label)
        results.append(
            MarketResult(
                market_key=market.market_key,
                score=round(total, 6),
                confidence=confidence,
                coverage=coverage,
                components=comp_results,
                is_home_market=market.is_home_market,
                metadata={"mode": "reference_reproduction"},
            )
        )

    _assign_ranks(results)
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
        results.append(
            MarketResult(
                market_key=market.market_key,
                # Reported on the covered weight only: unknown is not zero.
                score=round(total, 6) if covered_weight else None,
                confidence=_aggregate_confidence(comp_results, market.confidence_label),
                coverage=coverage,
                components=comp_results,
                is_home_market=market.is_home_market,
                metadata={
                    "mode": "native_recalculation",
                    "covered_weight": covered_weight,
                    "total_weight": total_weight,
                    "score_renormalized_to_100": (
                        round(total / covered_weight * 100.0, 6) if covered_weight else None
                    ),
                },
            )
        )

    _assign_ranks(results)
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


def _aggregate_confidence(
    components: list[ComponentResult], confidence_label: str | None
) -> float:
    """Weight component confidence by component weight, over covered weight."""
    covered = [c for c in components if c.coverage > 0]
    if not covered:
        return 0.0
    weight_sum = sum(c.weight for c in covered)
    if weight_sum == 0:
        return 0.0
    weighted = sum(c.confidence * c.weight for c in covered) / weight_sum
    # Scale by the share of total weight that is actually covered, so a
    # confidently-computed 30% of the model never reads as fully confident.
    total_weight = sum(c.weight for c in components)
    coverage_factor = weight_sum / total_weight if total_weight else 0.0
    return round(weighted * coverage_factor, 4)


def _assign_ranks(results: list[MarketResult]) -> None:
    """Ordinal ranks over the international set, highest score first.

    Home-market benchmarks stay in the normalization universe but are left
    unranked, matching the source artifact's
    ``international_top50_excludes_home_market``. Markets without a comparable
    score are also unranked.
    """
    scored = [r for r in results if r.score is not None and not r.is_home_market]
    scored.sort(key=lambda r: (-r.score, r.market_key))
    for position, result in enumerate(scored, start=1):
        result.rank = position


__all__ = [
    "MarketInputs",
    "ComponentResult",
    "MarketResult",
    "ScoreRunResult",
    "run_reference_reproduction",
    "run_native_recalculation",
    BaseComponent.__name__,
]
