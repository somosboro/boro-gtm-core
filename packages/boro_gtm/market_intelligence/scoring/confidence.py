"""The confidence algorithm.

There is exactly **one** confidence algorithm in this engine, defined here and
used by both the base and the contextual scoring engines (A-10). Every factor
it consumes is configuration carried in ``scoring_models.definition``, so a
change of weighting is a model version, not a code change.

Evidence confidence for a single metric
---------------------------------------

::

    evidence_confidence = fact_type_factor x recency_factor x label_factor

``fact_type_factor``
    How strong a kind of evidence this is (FACT 1.00 … HYPOTHESIS 0.30).
    A metric with no value at all contributes 0.0 — but note that an
    *uncovered* component is excluded from the aggregate entirely rather than
    dragging it down, so a 0.0 here only occurs for a metric that is present
    in a covered component.

``recency_factor``
    How well the observation is located in time. A year-only period is usable
    but coarse; an undated observation is weaker still. This is the mechanism
    by which a missing usable date degrades confidence rather than being
    silently treated as current.

``label_factor``
    The source's own HIGH / MEDIUM / LOW assessment, converted through the
    documented :data:`CONFIDENCE_LABEL_SCALE`. Absent label -> neutral 1.0,
    so a source that declines to self-assess is not punished.

Aggregate confidence for a market
---------------------------------

::

    covered           = components with coverage > 0
    covered_weight    = sum(c.weight for c in covered)
    weighted_mean     = sum(c.confidence * c.weight for c in covered) / covered_weight
    coverage          = covered_weight / total_model_weight
    market_confidence = weighted_mean * coverage

The trailing ``* coverage`` is deliberate: a component set that is individually
well-evidenced but covers only a third of the model must not report as fully
confident. Score, confidence and coverage remain three separate numbers.
"""

from __future__ import annotations

from typing import Protocol

from boro_gtm.core.enums import FactType, PeriodGranularity

#: Fact type -> strength of that kind of evidence.
#: Engine defaults, versioned in the model definition; not scientific truths.
FACT_TYPE_CONFIDENCE: dict[str, float] = {
    FactType.FACT.value: 1.00,
    FactType.PROXY.value: 0.75,
    FactType.ESTIMATE.value: 0.65,
    FactType.INFERENCE.value: 0.50,
    FactType.HYPOTHESIS.value: 0.30,
}

#: Source HIGH/MEDIUM/LOW labels -> multiplier. Applied explicitly, never
#: invisibly: the mapping is published in ``scoring_models.definition``.
CONFIDENCE_LABEL_SCALE: dict[str, float] = {
    "HIGH": 1.00,
    "HIGH_MEDIUM": 0.92,
    "MEDIUM_HIGH": 0.92,
    "MEDIUM": 0.85,
    "LOW_MEDIUM": 0.75,
    "MEDIUM_LOW": 0.75,
    "LOW_TO_MEDIUM": 0.75,
    "LOW": 0.60,
}

#: Period granularity -> how much the temporal precision is trusted.
#: An observation with no usable date is discounted (see 03 — Provenance).
RECENCY_FACTOR: dict[str, float] = {
    PeriodGranularity.DATE.value: 1.00,
    PeriodGranularity.YEAR.value: 0.90,
    PeriodGranularity.SNAPSHOT.value: 0.85,
    PeriodGranularity.UNDATED.value: 0.70,
}

#: Applied when an observation records no granularity at all.
UNKNOWN_RECENCY_FACTOR = 0.70

#: Applied when the source states no confidence label.
NEUTRAL_LABEL_FACTOR = 1.0


def fact_type_factor(fact_type: str | None) -> float:
    """Strength of a kind of evidence. ``None`` (no value) scores 0.0."""
    if fact_type is None:
        return 0.0
    return FACT_TYPE_CONFIDENCE.get(fact_type, 0.0)


def recency_factor(period_granularity: str | None) -> float:
    """Temporal-precision discount. Missing granularity degrades confidence."""
    if period_granularity is None:
        return UNKNOWN_RECENCY_FACTOR
    return RECENCY_FACTOR.get(period_granularity, UNKNOWN_RECENCY_FACTOR)


def label_factor(confidence_label: str | None) -> float:
    """Source self-assessment multiplier. Unknown or absent label is neutral."""
    if not confidence_label:
        return NEUTRAL_LABEL_FACTOR
    return CONFIDENCE_LABEL_SCALE.get(
        confidence_label.strip().upper(), NEUTRAL_LABEL_FACTOR
    )


def evidence_confidence(
    fact_type: str | None,
    period_granularity: str | None = None,
    confidence_label: str | None = None,
) -> float:
    """Confidence in one piece of evidence, in ``[0, 1]``."""
    value = (
        fact_type_factor(fact_type)
        * recency_factor(period_granularity)
        * label_factor(confidence_label)
    )
    return round(min(1.0, max(0.0, value)), 6)


class _WeightedComponent(Protocol):
    """Structural type shared by both engines' component results."""

    component_key: str
    weight: float
    coverage: float
    confidence: float


def aggregate_confidence(
    components: list[_WeightedComponent], total_weight: float | None = None
) -> float:
    """Weighted mean component confidence, scaled by coverage.

    Args:
        components: every component of the model, covered or not.
        total_weight: the model's full weight. Defaults to the sum of the
            supplied component weights.
    """
    covered = [c for c in components if c.coverage > 0]
    if not covered:
        return 0.0

    covered_weight = sum(c.weight for c in covered)
    if covered_weight <= 0:
        return 0.0

    full_weight = (
        total_weight if total_weight is not None else sum(c.weight for c in components)
    )
    if not full_weight:
        return 0.0

    weighted_mean = sum(c.confidence * c.weight for c in covered) / covered_weight
    coverage = covered_weight / full_weight
    return round(min(1.0, max(0.0, weighted_mean * coverage)), 4)


def definition_block() -> dict[str, object]:
    """The confidence configuration published in a model definition."""
    return {
        "algorithm": (
            "evidence_confidence = fact_type_factor * recency_factor * "
            "label_factor; market_confidence = weighted mean over covered "
            "components, scaled by coverage"
        ),
        "fact_type_confidence": FACT_TYPE_CONFIDENCE,
        "confidence_label_scale": CONFIDENCE_LABEL_SCALE,
        "recency_factor": RECENCY_FACTOR,
        "unknown_recency_factor": UNKNOWN_RECENCY_FACTOR,
        "neutral_label_factor": NEUTRAL_LABEL_FACTOR,
    }
