"""M1 contextual scoring engine.

Composition (04 — Layer A/B/C), all weights configuration-driven:

* **Layer A** base market prior — the 0-100 attractiveness score of a chosen run.
* **Layer B** context evidence — market x vertical profiles, ICP availability,
  ticket compatibility, channel accessibility, strategic reuse.
* **Layer C** result — ``score``, ``confidence`` and ``coverage`` as three
  independent numbers, plus per-component explanations and research gaps.

The missing-data policy is the heart of this module. An unknown component is
removed from *both* the numerator and the denominator; the displayed score is
renormalized over covered weight. An unknown therefore moves ``coverage`` down
and leaves the score a statement about what is actually known — it never reads
as a zero-fit verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boro_gtm.market_intelligence.scoring.confidence import aggregate_confidence
from boro_gtm.market_intelligence.scoring.definitions import (
    CHANNEL_ACCESS_WEIGHTS,
    CONTEXTUAL_MINIMUM_RANK_COVERAGE,
    CONTEXTUAL_MODEL_COMPONENTS,
    ContextualComponent,
)


@dataclass(slots=True)
class ContextualRequest:
    """The evaluation context."""

    vertical_key: str | None = None
    icp_key: str | None = None
    offer_key: str | None = None
    channel_key: str | None = None
    ticket_usd: float | None = None
    allow_low_coverage: bool = False
    #: The model's ``minimum_rank_coverage``; callers may tighten it per request.
    min_coverage: float = CONTEXTUAL_MINIMUM_RANK_COVERAGE


@dataclass(slots=True)
class ContextualMarketInputs:
    """Everything known about one market in this context."""

    market_key: str
    name: str

    #: Layer A
    base_score: float | None = None

    #: Layer B — market x vertical profile
    has_vertical_profile: bool = False
    vertical_fit_score: float | None = None
    vertical_sam_min: float | None = None
    vertical_sam_max: float | None = None
    vertical_profile_confidence: float | None = None
    vertical_profile_coverage: float | None = None

    #: Layer B — market-level ICP availability (SAM firm counts)
    sam_firms_min: float | None = None
    sam_firms_max: float | None = None
    sam_fact_type: str | None = None

    #: Layer B — observed ticket range for this market
    ticket_min_usd: float | None = None
    ticket_max_usd: float | None = None

    #: Layer B — channel/compliance inputs
    metrics: dict[str, float | None] = field(default_factory=dict)

    #: Layer B — strategic reuse
    recommended_channel: str | None = None

    fact_types: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ContextualComponentResult:
    component_key: str
    weight: float
    normalized_value: float | None
    weighted_score: float | None
    coverage: float
    confidence: float
    explanation: str
    missing_metrics: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContextualMarketResult:
    market_key: str
    name: str
    score: float | None
    confidence: float
    coverage: float
    comparable: bool
    rank: int | None
    components: list[ContextualComponentResult]
    missing_metrics: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _sam_scale(value: float) -> float:
    """Map an ICP firm count to 0-1 on a log-ish ladder.

    Thresholds are configuration-flavoured heuristics for screening, stated
    openly rather than dressed up as a probability.
    """
    ladder = [(100, 0.1), (500, 0.25), (2_000, 0.45), (10_000, 0.65),
              (30_000, 0.85), (60_000, 0.95)]
    for threshold, score in ladder:
        if value <= threshold:
            return score
    return 1.0


def _ticket_overlap(
    requested: float | None, low: float | None, high: float | None
) -> float | None:
    """How well a requested ticket sits inside the market's observed range."""
    if low is None or high is None:
        return None
    if requested is None:
        return None
    if high < low:
        low, high = high, low
    if low <= requested <= high:
        return 1.0
    # Graceful decay outside the band rather than a cliff to zero.
    span = max(high - low, 1.0)
    distance = (low - requested) if requested < low else (requested - high)
    return max(0.0, 1.0 - distance / span)


def evaluate_market(
    inputs: ContextualMarketInputs,
    request: ContextualRequest,
    components: list[dict[str, Any]] | None = None,
    channel_weights: dict[str, dict[str, float]] | None = None,
) -> ContextualMarketResult:
    """Evaluate one market against the context."""
    specs = components or CONTEXTUAL_MODEL_COMPONENTS
    channels = channel_weights or CHANNEL_ACCESS_WEIGHTS

    results: list[ContextualComponentResult] = []
    missing_all: list[str] = []

    for spec in specs:
        key = spec["component_key"]
        weight = float(spec["weight"])
        handler = _HANDLERS.get(key)
        if handler is None:  # pragma: no cover - configuration guard
            results.append(
                ContextualComponentResult(
                    component_key=key,
                    weight=weight,
                    normalized_value=None,
                    weighted_score=None,
                    coverage=0.0,
                    confidence=0.0,
                    explanation=f"No evaluator registered for component {key!r}.",
                    missing_metrics=list(spec.get("required_metrics", [])),
                )
            )
            continue

        normalized, confidence, explanation, missing = handler(inputs, request, channels)
        if normalized is None:
            missing_all.extend(missing)
            results.append(
                ContextualComponentResult(
                    component_key=key,
                    weight=weight,
                    normalized_value=None,
                    weighted_score=None,
                    coverage=0.0,
                    confidence=0.0,
                    explanation=explanation,
                    missing_metrics=missing,
                )
            )
            continue

        normalized = min(1.0, max(0.0, normalized))
        # A component can be scored from partial evidence (e.g. a channel whose
        # inputs are only partly observed). Those unobserved inputs are still
        # genuine research gaps, so they are reported even though the component
        # counts as covered.
        missing_all.extend(missing)
        results.append(
            ContextualComponentResult(
                component_key=key,
                weight=weight,
                normalized_value=normalized,
                weighted_score=round(weight * normalized, 6),
                coverage=1.0,
                confidence=confidence,
                explanation=explanation,
                missing_metrics=missing,
            )
        )

    total_weight = sum(float(spec["weight"]) for spec in specs)
    covered_weight = sum(c.weight for c in results if c.coverage > 0)
    coverage = covered_weight / total_weight if total_weight else 0.0

    if covered_weight > 0:
        earned = sum(c.weighted_score or 0.0 for c in results)
        # Renormalize over covered weight: unknown is excluded, not zeroed.
        # Identical semantics to native base scoring (A-8).
        score = round(earned / covered_weight * 100.0, 4)
    else:
        score = None
    # One confidence algorithm, shared with the base engine (A-10).
    confidence = aggregate_confidence(results, total_weight)

    comparable = coverage >= request.min_coverage or request.allow_low_coverage
    notes: list[str] = []
    if not comparable:
        notes.append(
            f"Coverage {coverage:.2f} is below the minimum {request.min_coverage:.2f} "
            "required for rank comparison. Pass allow_low_coverage=true to include "
            "this market in the ranking anyway."
        )
    if score is not None and coverage < 1.0:
        notes.append(
            f"Score is renormalized over {coverage:.0%} of model weight. "
            "Missing components are excluded, not scored as zero."
        )

    return ContextualMarketResult(
        market_key=inputs.market_key,
        name=inputs.name,
        score=score,
        confidence=confidence,
        coverage=round(coverage, 4),
        comparable=comparable,
        rank=None,
        components=results,
        missing_metrics=sorted(set(missing_all)),
        notes=notes,
    )


def rank_results(
    results: list[ContextualMarketResult], allow_low_coverage: bool = False
) -> list[ContextualMarketResult]:
    """Assign ranks over comparable markets only."""
    rankable = [
        r for r in results
        if r.score is not None and (r.comparable or allow_low_coverage)
    ]
    rankable.sort(key=lambda r: (-(r.score or 0.0), r.market_key))
    for position, result in enumerate(rankable, start=1):
        result.rank = position
    results.sort(
        key=lambda r: (r.rank is None, r.rank or 0, -(r.score or 0.0), r.market_key)
    )
    return results


# ---------------------------------------------------------------------------
# Component evaluators
# ---------------------------------------------------------------------------
# Each returns (normalized_0_1 | None, confidence, explanation, missing_metrics)


def _base_market_prior(inputs, request, channels):
    if inputs.base_score is None:
        return (
            None,
            0.0,
            "No base market score available for this market in the selected run.",
            ["base_market_score"],
        )
    normalized = inputs.base_score / 100.0
    return (
        normalized,
        0.90,
        f"Base market prior = {inputs.base_score:.3f}/100 = {normalized:.4f}.",
        [],
    )


def _vertical_density_fit(inputs, request, channels):
    if request.vertical_key is None:
        return (
            None,
            0.0,
            "No vertical supplied in the request context.",
            ["context.vertical"],
        )
    if not inputs.has_vertical_profile:
        return (
            None,
            0.0,
            (
                f"No stored market x vertical profile for {request.vertical_key!r}. "
                "Vertical density is unknown, so this component is excluded from "
                "the score rather than treated as zero fit."
            ),
            ["market_vertical_profile.fit_score", "market_vertical_profile.sam"],
        )
    if inputs.vertical_fit_score is not None:
        value = inputs.vertical_fit_score / 100.0
        return (
            value,
            float(inputs.vertical_profile_confidence or 0.5),
            f"Stored vertical fit score {inputs.vertical_fit_score:.2f}/100.",
            [],
        )
    if inputs.vertical_sam_min is not None and inputs.vertical_sam_max is not None:
        midpoint = (inputs.vertical_sam_min + inputs.vertical_sam_max) / 2.0
        value = _sam_scale(midpoint)
        return (
            value,
            float(inputs.vertical_profile_confidence or 0.5),
            (
                f"Derived from vertical SAM range "
                f"[{inputs.vertical_sam_min:,.0f}, {inputs.vertical_sam_max:,.0f}] "
                f"-> midpoint {midpoint:,.0f} -> {value:.2f}."
            ),
            [],
        )
    # The profile exists as qualitative evidence but carries no numbers.
    return (
        None,
        0.0,
        (
            "A market x vertical profile exists (the research names this vertical "
            "a market priority) but supplies no fit score or vertical SAM. "
            "Density remains unquantified; component excluded from the score."
        ),
        ["market_vertical_profile.fit_score", "market_vertical_profile.sam"],
    )


def _icp_availability(inputs, request, channels):
    if inputs.sam_firms_min is None or inputs.sam_firms_max is None:
        return (
            None,
            0.0,
            (
                "No SAM ICP firm-count range for this market. ICP availability is "
                "unknown; component excluded from the score."
            ),
            ["market_size_estimate.sam_min", "market_size_estimate.sam_max"],
        )
    midpoint = (inputs.sam_firms_min + inputs.sam_firms_max) / 2.0
    value = _sam_scale(midpoint)
    confidence = 0.65 if (inputs.sam_fact_type or "").upper() == "ESTIMATE" else 0.5
    return (
        value,
        confidence,
        (
            f"SAM ICP firms [{inputs.sam_firms_min:,.0f}, {inputs.sam_firms_max:,.0f}] "
            f"-> midpoint {midpoint:,.0f} -> availability {value:.2f} "
            f"(fact type {inputs.sam_fact_type or 'UNKNOWN'})."
        ),
        [],
    )


def _ticket_compatibility(inputs, request, channels):
    if request.ticket_usd is None:
        return (
            None,
            0.0,
            "No ticket supplied in the request context.",
            ["context.ticket_usd"],
        )
    overlap = _ticket_overlap(request.ticket_usd, inputs.ticket_min_usd, inputs.ticket_max_usd)
    if overlap is None:
        return (
            None,
            0.0,
            (
                "No observed ticket range for this market; ticket compatibility "
                "cannot be assessed and is excluded from the score."
            ),
            ["market_size_estimate.ticket_min_usd", "market_size_estimate.ticket_max_usd"],
        )
    return (
        overlap,
        0.65,
        (
            f"Requested ticket ${request.ticket_usd:,.0f} vs observed market range "
            f"[${inputs.ticket_min_usd:,.0f}, ${inputs.ticket_max_usd:,.0f}] "
            f"-> compatibility {overlap:.2f}."
        ),
        [],
    )


def _channel_accessibility(inputs, request, channels):
    if request.channel_key is None:
        return (None, 0.0, "No channel supplied in the request context.", ["context.channel"])
    weights = channels.get(request.channel_key)
    if weights is None:
        return (
            None,
            0.0,
            f"No accessibility configuration for channel {request.channel_key!r}.",
            [f"channel_access_weights.{request.channel_key}"],
        )

    total = 0.0
    used = 0.0
    parts: list[str] = []
    missing: list[str] = []
    for metric, weight in weights.items():
        value = inputs.metrics.get(metric)
        if value is None:
            missing.append(metric)
            continue
        total += weight * float(value)
        used += weight
        parts.append(f"{weight:.2f}*{metric}({value})")

    if used == 0:
        return (
            None,
            0.0,
            (
                f"None of the inputs for channel {request.channel_key!r} are observed "
                f"({', '.join(weights)}); component excluded from the score."
            ),
            missing,
        )

    # Renormalize over the inputs actually observed.
    value = total / used
    confidence = 0.5 * (used / sum(weights.values()))
    explanation = (
        f"Channel {request.channel_key!r} accessibility = " + " + ".join(parts)
        + f" normalized over observed weight {used:.2f} -> {value:.3f}."
    )
    if missing:
        explanation += f" Unobserved inputs excluded: {', '.join(missing)}."
    return (value, confidence, explanation, missing)


def _strategic_reuse(inputs, request, channels):
    language = inputs.metrics.get("language_access")
    if language is None:
        return (
            None,
            0.0,
            "No language-access input; strategic reuse cannot be assessed.",
            ["language_access"],
        )
    parts = [f"language_access={language}"]
    value = float(language)
    confidence = 0.5

    if inputs.recommended_channel and request.channel_key:
        recommended = inputs.recommended_channel.lower()
        aligned = request.channel_key.lower() in recommended or (
            request.channel_key == "multichannel" and "+" in recommended
        )
        # Motion alignment nudges reuse without dominating it.
        value = 0.75 * value + 0.25 * (1.0 if aligned else 0.4)
        parts.append(
            f"recommended motion {inputs.recommended_channel!r} "
            f"{'aligns' if aligned else 'differs'} with requested channel"
        )
        confidence = 0.6

    return (value, confidence, "Strategic reuse: " + "; ".join(parts) + ".", [])


_HANDLERS = {
    ContextualComponent.BASE_MARKET_PRIOR: _base_market_prior,
    ContextualComponent.VERTICAL_DENSITY_FIT: _vertical_density_fit,
    ContextualComponent.ICP_AVAILABILITY: _icp_availability,
    ContextualComponent.TICKET_COMPATIBILITY: _ticket_compatibility,
    ContextualComponent.CHANNEL_ACCESSIBILITY: _channel_accessibility,
    ContextualComponent.STRATEGIC_REUSE: _strategic_reuse,
}
