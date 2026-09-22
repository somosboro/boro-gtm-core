"""Shared vocabularies.

These are stored as plain text columns with CHECK-free validation at the
application boundary: the source taxonomy may grow between snapshots and a
database enum would require a migration for every new value.
"""

from __future__ import annotations

from enum import StrEnum


class FactType(StrEnum):
    """Source-supplied evidence taxonomy (``metadata.fact_type_legend``)."""

    FACT = "FACT"
    ESTIMATE = "ESTIMATE"
    PROXY = "PROXY"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"
    ND = "N/D"
    #: Used when the source omits an explicit fact type. Never upgraded to FACT.
    UNKNOWN = "UNKNOWN"


class MarketCategory(StrEnum):
    GIANT_MARKET = "GIANT_MARKET"
    HIGH_VALUE_NICHE = "HIGH_VALUE_NICHE"
    DIGITALIZATION_GAP = "DIGITALIZATION_GAP"
    EMERGING_SCALE = "EMERGING_SCALE"
    BALANCED_REGIONAL = "BALANCED_REGIONAL"


class ScoreRunKind(StrEnum):
    #: Supplied scores/ranks copied verbatim from the snapshot.
    IMPORTED_REFERENCE = "imported_reference"
    #: Recomputed by summing imported component values (must match the source).
    REFERENCE_REPRODUCTION = "reference_reproduction"
    #: Recomputed from raw observations only, where prerequisites exist.
    NATIVE_RECALCULATION = "native_recalculation"
    #: M1 market x vertical x ICP x offer x channel run.
    CONTEXTUAL = "contextual"


class ScoreRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ResearchGapStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ResearchGapPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Normalized metric vocabulary (see 03_IMPORTER_AND_DATA_CONTRACT.md).
class MetricKey(StrEnum):
    GDP_NOMINAL_USD_BN = "gdp_nominal_usd_bn"
    GDP_PER_CAPITA_USD = "gdp_per_capita_usd"
    REAL_GDP_GROWTH_PCT = "real_gdp_growth_pct"
    INNOVATION_DIGITAL_PROXY = "innovation_digital_proxy"
    MANUFACTURING_VALUE_ADDED_USD_BN = "manufacturing_value_added_usd_bn"
    SOFTWARE_SPENDING_PCT_GDP = "software_spending_pct_gdp"
    LANGUAGE_ACCESS = "language_access"
    TIMEZONE_OVERLAP = "timezone_overlap"
    B2B_EMAIL_LEGAL_ACCESS = "b2b_email_legal_access"
    TECHNOLOGY_SPENDING_GROWTH_PCT = "technology_spending_growth_pct"
    #: Required by the icp_density_proxy formula but absent from the dataset.
    POPULATION = "population"
