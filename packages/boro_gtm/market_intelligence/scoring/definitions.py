"""Versioned scoring-model definitions.

Model definitions are *data*: they are persisted into ``scoring_models`` /
``scoring_model_components`` at import time and read back by the engine. The
dictionaries below are seed configuration, not engine constants, so a new model
version never requires an engine change.
"""

from __future__ import annotations

from typing import Any

from boro_gtm.core.enums import MetricKey
from boro_gtm.market_intelligence.scoring import confidence as confidence_cfg
from boro_gtm.market_intelligence.scoring.ranking import RANKING_CONVENTION

ENGINE_VERSION = "0.2.0"

BASE_MODEL_KEY = "market-attractiveness"
BASE_MODEL_VERSION = "1.0"

CONTEXTUAL_MODEL_KEY = "contextual-market-fit"
CONTEXTUAL_MODEL_VERSION = "1.0"

#: Minimum coverage a result must reach before it is rank-comparable.
#: Explicit per model (A-8). A result below this keeps its score and coverage
#: but is returned unranked, so missing evidence never reads as "bad market".
BASE_MINIMUM_RANK_COVERAGE = 0.5
CONTEXTUAL_MINIMUM_RANK_COVERAGE = 0.5

#: Re-exported from the single confidence algorithm so that model definitions
#: and the engines cannot drift apart (A-10).
FACT_TYPE_CONFIDENCE = confidence_cfg.FACT_TYPE_CONFIDENCE
CONFIDENCE_LABEL_SCALE = confidence_cfg.CONFIDENCE_LABEL_SCALE


class BaseComponent:
    """Component keys of ``market-attractiveness:1.0``."""

    ECONOMIC_STRENGTH = "economic_strength_20"
    TECHNOLOGY_INVESTMENT_READINESS = "technology_investment_readiness_20"
    DIGITALIZATION_OPPORTUNITY = "digitalization_opportunity_15"
    ICP_DENSITY_PROXY = "icp_density_proxy_20"
    ABILITY_TO_PAY = "ability_to_pay_10"
    GTM_EASE = "gtm_ease_10"
    OUTLOOK = "outlook_5"


#: Ordered component spec. ``required_metrics`` drives native-mode coverage:
#: a component is natively computable only when every listed metric exists.
BASE_MODEL_COMPONENTS: list[dict[str, Any]] = [
    {
        "component_key": BaseComponent.ECONOMIC_STRENGTH,
        "weight": 20.0,
        "formula": "12*P(log GDP nominal) + 8*P(log GDP per capita)",
        "required_metrics": [
            MetricKey.GDP_NOMINAL_USD_BN.value,
            MetricKey.GDP_PER_CAPITA_USD.value,
        ],
        "ordinal": 1,
        "terms": [
            {"weight": 12.0, "metric": MetricKey.GDP_NOMINAL_USD_BN.value, "transform": "log"},
            {"weight": 8.0, "metric": MetricKey.GDP_PER_CAPITA_USD.value, "transform": "log"},
        ],
        "natively_computable": True,
    },
    {
        "component_key": BaseComponent.TECHNOLOGY_INVESTMENT_READINESS,
        "weight": 20.0,
        "formula": "20*P(WIPO GII 2025 score or documented equivalent proxy)",
        "required_metrics": [MetricKey.INNOVATION_DIGITAL_PROXY.value],
        "ordinal": 2,
        "terms": [
            {
                "weight": 20.0,
                "metric": MetricKey.INNOVATION_DIGITAL_PROXY.value,
                "transform": "identity",
            }
        ],
        "natively_computable": True,
    },
    {
        "component_key": BaseComponent.DIGITALIZATION_OPPORTUNITY,
        "weight": 15.0,
        "formula": "15*P(P(log GDP per capita) * (1-P(digital/innovation maturity)))",
        "required_metrics": [
            MetricKey.GDP_PER_CAPITA_USD.value,
            MetricKey.INNOVATION_DIGITAL_PROXY.value,
        ],
        "ordinal": 3,
        "kind": "nested_percentile_product",
        "terms": [
            {"metric": MetricKey.GDP_PER_CAPITA_USD.value, "transform": "log", "invert": False},
            {
                "metric": MetricKey.INNOVATION_DIGITAL_PROXY.value,
                "transform": "identity",
                "invert": True,
            },
        ],
        "natively_computable": True,
    },
    {
        "component_key": BaseComponent.ICP_DENSITY_PROXY,
        "weight": 20.0,
        "formula": (
            "9*P(log implied population) + 7*P(log manufacturing value added) "
            "+ 4*P(log GDP nominal)"
        ),
        "required_metrics": [
            MetricKey.POPULATION.value,
            MetricKey.MANUFACTURING_VALUE_ADDED_USD_BN.value,
            MetricKey.GDP_NOMINAL_USD_BN.value,
        ],
        "ordinal": 4,
        "terms": [
            {"weight": 9.0, "metric": MetricKey.POPULATION.value, "transform": "log"},
            {
                "weight": 7.0,
                "metric": MetricKey.MANUFACTURING_VALUE_ADDED_USD_BN.value,
                "transform": "log",
            },
            {"weight": 4.0, "metric": MetricKey.GDP_NOMINAL_USD_BN.value, "transform": "log"},
        ],
        # The snapshot exposes no standalone population metric, so this
        # component cannot be rebuilt from raw inputs. ADR-007 forbids
        # reverse-engineering it; native runs mark it uncovered instead.
        "natively_computable": False,
        "native_blocker": (
            "Required metric 'population' is not supplied by the snapshot. "
            "Reference reproduction uses the imported component value; native "
            "recalculation reports this component as uncovered."
        ),
    },
    {
        "component_key": BaseComponent.ABILITY_TO_PAY,
        "weight": 10.0,
        "formula": "10*P(log GDP per capita)",
        "required_metrics": [MetricKey.GDP_PER_CAPITA_USD.value],
        "ordinal": 5,
        "terms": [
            {"weight": 10.0, "metric": MetricKey.GDP_PER_CAPITA_USD.value, "transform": "log"}
        ],
        "natively_computable": True,
    },
    {
        "component_key": BaseComponent.GTM_EASE,
        "weight": 10.0,
        "formula": (
            "4*language access + 3*time-zone overlap with Santiago "
            "+ 3*B2B-email legal access"
        ),
        "required_metrics": [
            MetricKey.LANGUAGE_ACCESS.value,
            MetricKey.TIMEZONE_OVERLAP.value,
            MetricKey.B2B_EMAIL_LEGAL_ACCESS.value,
        ],
        "ordinal": 6,
        # No percentile: these are already 0-1 scoring inputs, which makes this
        # the one component whose native value is universe-independent.
        "kind": "linear",
        "terms": [
            {"weight": 4.0, "metric": MetricKey.LANGUAGE_ACCESS.value},
            {"weight": 3.0, "metric": MetricKey.TIMEZONE_OVERLAP.value},
            {"weight": 3.0, "metric": MetricKey.B2B_EMAIL_LEGAL_ACCESS.value},
        ],
        "natively_computable": True,
    },
    {
        "component_key": BaseComponent.OUTLOOK,
        "weight": 5.0,
        "formula": "5*P(IMF real GDP growth 2026)",
        "required_metrics": [MetricKey.REAL_GDP_GROWTH_PCT.value],
        "ordinal": 7,
        "terms": [
            {
                "weight": 5.0,
                "metric": MetricKey.REAL_GDP_GROWTH_PCT.value,
                "transform": "identity",
            }
        ],
        "natively_computable": True,
    },
]


def base_model_definition(metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the ``definition`` JSONB for ``market-attractiveness:1.0``."""
    return {
        "engine_version": ENGINE_VERSION,
        "total_weight": sum(c["weight"] for c in BASE_MODEL_COMPONENTS),
        "percentile_method": "midrank_over_n_minus_1",
        "percentile_definition": (
            "P(x) = (count(v < x) + (count(v == x) - 1)/2) / (N - 1), "
            "N = normalization universe size"
        ),
        "percentile_note": (
            "Percentile rank is ordinal and therefore invariant under the log "
            "transform named in the published formula; the transform is kept "
            "for fidelity and affects recorded raw values only."
        ),
        "confidence": confidence_cfg.definition_block(),
        "minimum_rank_coverage": BASE_MINIMUM_RANK_COVERAGE,
        "scoring_semantics": {
            "reference_reproduction": (
                "score = sum of imported component values. Exact reproduction "
                "of the published artifact; no renormalization."
            ),
            "native_recalculation": (
                "score = earned / covered_weight * 100. Uncovered components "
                "are excluded from both numerator and denominator."
            ),
            "coverage": "covered_weight / total_model_weight",
            "ranking": (
                "a result is ranked only when coverage >= minimum_rank_coverage; "
                "below that it keeps score and coverage but stays unranked."
            ),
        },
        "ranking_convention": RANKING_CONVENTION,
        "components": BASE_MODEL_COMPONENTS,
        "source_metadata": metadata or {},
    }


#: ----------------------------------------------------------------------
#: M1 contextual model — seed structure from 04. Editable and versioned.
#: ----------------------------------------------------------------------


class ContextualComponent:
    BASE_MARKET_PRIOR = "base_market_prior"
    VERTICAL_DENSITY_FIT = "vertical_density_fit"
    ICP_AVAILABILITY = "icp_availability"
    TICKET_COMPATIBILITY = "ticket_compatibility"
    CHANNEL_ACCESSIBILITY = "channel_accessibility"
    STRATEGIC_REUSE = "strategic_reuse_localization"


CONTEXTUAL_MODEL_COMPONENTS: list[dict[str, Any]] = [
    {
        "component_key": ContextualComponent.BASE_MARKET_PRIOR,
        "weight": 35.0,
        "formula": "35 * (base_market_score / 100)",
        "required_metrics": ["base_market_score"],
        "ordinal": 1,
    },
    {
        "component_key": ContextualComponent.VERTICAL_DENSITY_FIT,
        "weight": 25.0,
        "formula": "25 * normalized(market x vertical density evidence)",
        "required_metrics": ["market_vertical_profile.fit_score", "market_vertical_profile.sam"],
        "ordinal": 2,
    },
    {
        "component_key": ContextualComponent.ICP_AVAILABILITY,
        "weight": 15.0,
        "formula": "15 * normalized(SAM ICP firm count within market)",
        "required_metrics": ["market_size_estimate.sam_min", "market_size_estimate.sam_max"],
        "ordinal": 3,
    },
    {
        "component_key": ContextualComponent.TICKET_COMPATIBILITY,
        "weight": 10.0,
        "formula": "10 * overlap(requested ticket, observed market ticket range)",
        "required_metrics": [
            "market_size_estimate.ticket_min_usd",
            "market_size_estimate.ticket_max_usd",
        ],
        "ordinal": 4,
    },
    {
        "component_key": ContextualComponent.CHANNEL_ACCESSIBILITY,
        "weight": 10.0,
        "formula": "10 * channel_accessibility(channel, market legal/language inputs)",
        "required_metrics": [
            MetricKey.B2B_EMAIL_LEGAL_ACCESS.value,
            MetricKey.LANGUAGE_ACCESS.value,
            MetricKey.TIMEZONE_OVERLAP.value,
        ],
        "ordinal": 5,
    },
    {
        "component_key": ContextualComponent.STRATEGIC_REUSE,
        "weight": 5.0,
        "formula": "5 * (language reuse + recommended-motion reuse)",
        "required_metrics": [MetricKey.LANGUAGE_ACCESS.value],
        "ordinal": 6,
    },
]

#: Channel -> which market inputs gate access, and how strongly.
#: Seed configuration; compliance posture is a business decision, not a law.
CHANNEL_ACCESS_WEIGHTS: dict[str, dict[str, float]] = {
    "email": {
        MetricKey.B2B_EMAIL_LEGAL_ACCESS.value: 0.70,
        MetricKey.LANGUAGE_ACCESS.value: 0.30,
    },
    "phone": {
        MetricKey.TIMEZONE_OVERLAP.value: 0.60,
        MetricKey.LANGUAGE_ACCESS.value: 0.40,
    },
    "linkedin": {
        MetricKey.LANGUAGE_ACCESS.value: 0.70,
        MetricKey.TIMEZONE_OVERLAP.value: 0.30,
    },
    "partner": {
        MetricKey.LANGUAGE_ACCESS.value: 0.50,
        MetricKey.TIMEZONE_OVERLAP.value: 0.50,
    },
    "event": {
        MetricKey.LANGUAGE_ACCESS.value: 0.50,
        MetricKey.TIMEZONE_OVERLAP.value: 0.50,
    },
    "multichannel": {
        MetricKey.B2B_EMAIL_LEGAL_ACCESS.value: 0.40,
        MetricKey.LANGUAGE_ACCESS.value: 0.35,
        MetricKey.TIMEZONE_OVERLAP.value: 0.25,
    },
}


def contextual_model_definition() -> dict[str, Any]:
    """Build the ``definition`` JSONB for ``contextual-market-fit:1.0``."""
    return {
        "engine_version": ENGINE_VERSION,
        "total_weight": sum(c["weight"] for c in CONTEXTUAL_MODEL_COMPONENTS),
        "components": CONTEXTUAL_MODEL_COMPONENTS,
        "channel_access_weights": CHANNEL_ACCESS_WEIGHTS,
        "confidence": confidence_cfg.definition_block(),
        "minimum_rank_coverage": CONTEXTUAL_MINIMUM_RANK_COVERAGE,
        "ranking_convention": RANKING_CONVENTION,
        "missing_data_policy": {
            "mode": "renormalize_to_covered_weight",
            "description": (
                "Unknown components are excluded from both numerator and "
                "denominator; the displayed score is renormalized over covered "
                "weight only. Unknown never contributes zero."
            ),
            "min_comparable_coverage": CONTEXTUAL_MINIMUM_RANK_COVERAGE,
        },
    }
