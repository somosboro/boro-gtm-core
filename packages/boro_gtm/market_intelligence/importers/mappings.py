"""Source-field to normalized-metric mappings (03 — Metric mapping)."""

from __future__ import annotations

from dataclasses import dataclass

from boro_gtm.core.enums import FactType, MetricKey, PeriodGranularity


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """How one raw source field becomes a normalized observation."""

    metric_key: str
    period_label: str
    unit: str | None
    #: Fact type to record when a value is present. Never applied to an
    #: absent value: that becomes ``availability = NOT_AVAILABLE``.
    default_fact_type: str
    methodology: str | None = None
    #: How precisely this metric is located in time. The 2026 snapshot dates
    #: nothing to the day, so no metric here claims DATE granularity.
    period_granularity: str = PeriodGranularity.YEAR.value


#: raw source field -> normalized metric specification.
RAW_METRIC_MAP: dict[str, MetricSpec] = {
    "gdp_nominal_2025_usd_bn": MetricSpec(
        MetricKey.GDP_NOMINAL_USD_BN.value, "2025", "USD_BN", FactType.FACT.value,
        "IMF WEO nominal GDP vintage frozen for cross-country comparability.",
        PeriodGranularity.YEAR.value,
    ),
    "gdp_per_capita_2026_usd": MetricSpec(
        MetricKey.GDP_PER_CAPITA_USD.value, "2026", "USD", FactType.FACT.value,
        "IMF WEO GDP per capita projection.",
        PeriodGranularity.YEAR.value,
    ),
    "real_gdp_growth_2026_pct": MetricSpec(
        MetricKey.REAL_GDP_GROWTH_PCT.value, "2026", "PCT", FactType.FACT.value,
        "IMF WEO real GDP growth projection.",
        PeriodGranularity.YEAR.value,
    ),
    "innovation_digital_proxy_2025": MetricSpec(
        MetricKey.INNOVATION_DIGITAL_PROXY.value, "2025", "INDEX", FactType.PROXY.value,
        "WIPO GII 2025 score or documented equivalent proxy; surrogate for "
        "technology investment readiness, not literal IT spend.",
        PeriodGranularity.YEAR.value,
    ),
    "manufacturing_value_added_proxy_usd_bn": MetricSpec(
        MetricKey.MANUFACTURING_VALUE_ADDED_USD_BN.value, "latest", "USD_BN",
        FactType.PROXY.value,
        "World Bank manufacturing value added; source-dependent latest year.",
        # The source says only "latest": no usable year, so no usable date.
        PeriodGranularity.UNDATED.value,
    ),
    "software_spending_2024_pct_gdp": MetricSpec(
        MetricKey.SOFTWARE_SPENDING_PCT_GDP.value, "2024", "PCT_GDP",
        FactType.FACT.value,
        "Observed where available. Deliberately NOT an input to the score "
        "because country coverage is uneven.",
        PeriodGranularity.YEAR.value,
    ),
    "language_access_0_1": MetricSpec(
        MetricKey.LANGUAGE_ACCESS.value, "snapshot", "RATIO_0_1",
        FactType.INFERENCE.value,
        "Simplified GTM scoring input, not legal or linguistic advice.",
        PeriodGranularity.SNAPSHOT.value,
    ),
    "timezone_overlap_0_1": MetricSpec(
        MetricKey.TIMEZONE_OVERLAP.value, "snapshot", "RATIO_0_1",
        FactType.INFERENCE.value,
        "Working-hours overlap with Santiago, expressed 0-1.",
        PeriodGranularity.SNAPSHOT.value,
    ),
    "b2b_email_legal_access_0_1": MetricSpec(
        MetricKey.B2B_EMAIL_LEGAL_ACCESS.value, "snapshot", "RATIO_0_1",
        FactType.INFERENCE.value,
        "Simplified compliance scoring input. Campaigns still require "
        "country-specific legal review.",
        PeriodGranularity.SNAPSHOT.value,
    ),
}

#: Normalized metric key -> the raw field it came from (reverse index).
METRIC_TO_RAW_FIELD: dict[str, str] = {
    spec.metric_key: field for field, spec in RAW_METRIC_MAP.items()
}

#: Source-catalog keys that plausibly support a given metric.
#: Used only to attach *market-level* references; never presented as an exact
#: per-metric citation (03 — Provenance rules).
METRIC_SOURCE_HINTS: dict[str, tuple[str, ...]] = {
    MetricKey.GDP_NOMINAL_USD_BN.value: ("IMF_WEO_2026_04",),
    MetricKey.GDP_PER_CAPITA_USD.value: ("IMF_WEO_2026_04",),
    MetricKey.REAL_GDP_GROWTH_PCT.value: ("IMF_WEO_2026_04",),
    MetricKey.INNOVATION_DIGITAL_PROXY.value: ("WIPO_GII_2025",),
    MetricKey.MANUFACTURING_VALUE_ADDED_USD_BN.value: ("WORLD_BANK_MVA",),
    MetricKey.SOFTWARE_SPENDING_PCT_GDP.value: ("WIPO_SOFTWARE_2025",),
}

#: Category vocabulary seeded into the ``market_categories`` lookup.
MARKET_CATEGORY_LABELS: dict[str, str] = {
    "GIANT_MARKET": "Giant market",
    "HIGH_VALUE_NICHE": "High-value niche",
    "DIGITALIZATION_GAP": "Digitalization gap",
    "EMERGING_SCALE": "Emerging scale",
    "BALANCED_REGIONAL": "Balanced regional",
}


def split_semicolon_list(value: str | list[str] | None) -> list[str] | None:
    """Parse a semicolon-separated deep-dive string into a list.

    The caller retains the original string in ``raw_values`` so the parse is
    additive, never lossy (03 — Deep-dive parsing).
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    parts = [p.strip() for p in value.split(";")]
    return [p for p in parts if p]
