"""A-10 — one confidence algorithm, and every factor materially participates."""

from __future__ import annotations

import pytest

from boro_gtm.core.enums import FactType, PeriodGranularity
from boro_gtm.market_intelligence.scoring.confidence import (
    CONFIDENCE_LABEL_SCALE,
    FACT_TYPE_CONFIDENCE,
    RECENCY_FACTOR,
    aggregate_confidence,
    definition_block,
    evidence_confidence,
)


class _Component:
    def __init__(self, key: str, weight: float, coverage: float, confidence: float):
        self.component_key = key
        self.weight = weight
        self.coverage = coverage
        self.confidence = confidence


# --- every factor must change the result -----------------------------------


def test_fact_type_materially_participates() -> None:
    strong = evidence_confidence(FactType.FACT.value, "YEAR", "HIGH")
    weak = evidence_confidence(FactType.HYPOTHESIS.value, "YEAR", "HIGH")
    assert strong > weak
    assert weak > 0.0


def test_confidence_label_materially_participates() -> None:
    """The A-10 regression: the label scale is no longer decorative."""
    high = evidence_confidence(FactType.FACT.value, "YEAR", "HIGH")
    low = evidence_confidence(FactType.FACT.value, "YEAR", "LOW")
    assert high > low
    assert low == pytest.approx(high * CONFIDENCE_LABEL_SCALE["LOW"])


def test_absent_label_is_neutral_not_punitive() -> None:
    labelled = evidence_confidence(FactType.FACT.value, "YEAR", "HIGH")
    unlabelled = evidence_confidence(FactType.FACT.value, "YEAR", None)
    assert unlabelled == pytest.approx(labelled)


def test_unknown_label_is_neutral() -> None:
    assert evidence_confidence(
        FactType.FACT.value, "YEAR", "SOMETHING_NEW"
    ) == pytest.approx(evidence_confidence(FactType.FACT.value, "YEAR", None))


def test_missing_usable_date_degrades_confidence() -> None:
    """Required by the authoritative provenance semantics."""
    dated = evidence_confidence(FactType.FACT.value, PeriodGranularity.DATE.value)
    yearly = evidence_confidence(FactType.FACT.value, PeriodGranularity.YEAR.value)
    undated = evidence_confidence(FactType.FACT.value, PeriodGranularity.UNDATED.value)
    assert dated > yearly > undated
    assert undated > 0.0


def test_absent_granularity_is_treated_as_undated() -> None:
    assert evidence_confidence(FactType.FACT.value, None) == pytest.approx(
        evidence_confidence(FactType.FACT.value, PeriodGranularity.UNDATED.value)
    )


def test_no_value_scores_zero_evidence() -> None:
    assert evidence_confidence(None, "YEAR", "HIGH") == 0.0


def test_evidence_confidence_is_bounded() -> None:
    for fact_type in [None, *(f.value for f in FactType)]:
        for granularity in [None, *(g.value for g in PeriodGranularity)]:
            for label in [None, "HIGH", "LOW", "NONSENSE"]:
                value = evidence_confidence(fact_type, granularity, label)
                assert 0.0 <= value <= 1.0


# --- aggregation -----------------------------------------------------------


def test_aggregate_is_weighted_by_component_weight() -> None:
    components = [
        _Component("big", 80.0, 1.0, 1.0),
        _Component("small", 20.0, 1.0, 0.0),
    ]
    assert aggregate_confidence(components) == pytest.approx(0.8)


def test_aggregate_is_scaled_by_coverage() -> None:
    """Confident about a third of the model is not the same as confident."""
    full = [_Component("a", 50.0, 1.0, 1.0), _Component("b", 50.0, 1.0, 1.0)]
    partial = [_Component("a", 50.0, 1.0, 1.0), _Component("b", 50.0, 0.0, 0.0)]
    assert aggregate_confidence(full) == pytest.approx(1.0)
    assert aggregate_confidence(partial) == pytest.approx(0.5)


def test_aggregate_with_no_coverage_is_zero() -> None:
    assert aggregate_confidence([_Component("a", 50.0, 0.0, 0.0)]) == 0.0
    assert aggregate_confidence([]) == 0.0


def test_aggregate_respects_an_explicit_total_weight() -> None:
    components = [_Component("a", 25.0, 1.0, 1.0)]
    assert aggregate_confidence(components, total_weight=100.0) == pytest.approx(0.25)


# --- one algorithm, published ----------------------------------------------


def test_both_engines_use_the_same_aggregate() -> None:
    from boro_gtm.market_intelligence.scoring import base_engine, contextual_engine

    assert base_engine.aggregate_confidence is aggregate_confidence
    assert contextual_engine.aggregate_confidence is aggregate_confidence


def test_definition_block_publishes_every_factor() -> None:
    block = definition_block()
    assert block["fact_type_confidence"] == FACT_TYPE_CONFIDENCE
    assert block["confidence_label_scale"] == CONFIDENCE_LABEL_SCALE
    assert block["recency_factor"] == RECENCY_FACTOR
    assert "algorithm" in block


def test_no_duplicate_confidence_tables_remain() -> None:
    """There must be exactly one source of these numbers."""
    from pathlib import Path

    root = Path("packages/boro_gtm")
    definers = [
        path
        for path in root.rglob("*.py")
        if "CONFIDENCE_LABEL_SCALE: dict" in path.read_text(encoding="utf-8")
    ]
    assert [p.name for p in definers] == ["confidence.py"]


def test_fact_type_table_covers_exactly_the_stored_vocabulary() -> None:
    assert set(FACT_TYPE_CONFIDENCE) == {f.value for f in FactType}
