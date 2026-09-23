"""Shared vocabularies.

Two classes of vocabulary live here:

**Closed** vocabularies describe a domain that cannot grow without a code
change (fact types, run kinds, lifecycle states). These are mirrored by
database CHECK constraints — see :data:`CLOSED_VOCABULARIES` and migration
``0002``.

**Open** vocabularies (metric keys, component keys, market regions,
competition levels) stay unconstrained text: the research taxonomy may grow
between snapshots and a database enum would force a migration for each value.
"""

from __future__ import annotations

from enum import StrEnum


class FactType(StrEnum):
    """Evidence taxonomy for a value that *exists*.

    ``N/D`` is deliberately absent. "No data" is not a kind of evidence: it is
    the absence of evidence, recorded as :class:`Availability.NOT_AVAILABLE`
    with a NULL ``fact_type`` and a NULL value.
    """

    FACT = "FACT"
    ESTIMATE = "ESTIMATE"
    PROXY = "PROXY"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"


#: The token the source artifact uses for "not available / not used".
#: Accepted on input, never stored as a ``fact_type``.
SOURCE_NOT_AVAILABLE_TOKEN = "N/D"

#: Fact-type tokens a source document may legally carry.
SOURCE_FACT_TYPE_TOKENS = frozenset({*FactType, SOURCE_NOT_AVAILABLE_TOKEN})


class Availability(StrEnum):
    """Whether an observation carries a value at all."""

    #: A value was supplied; ``fact_type`` states what kind of evidence it is.
    OBSERVED = "OBSERVED"
    #: No value was supplied. ``value_numeric`` and ``fact_type`` are both NULL.
    NOT_AVAILABLE = "NOT_AVAILABLE"


class PeriodGranularity(StrEnum):
    """How precisely an observation is located in time.

    Recorded explicitly so that a year-only period is never mistaken for — or
    silently widened into — an exact date.
    """

    #: ``observed_at`` holds a real, source-stated calendar date.
    DATE = "DATE"
    #: ``period_label`` is a calendar year such as "2025".
    YEAR = "YEAR"
    #: The value is a property of the snapshot, not of a dated period.
    SNAPSHOT = "SNAPSHOT"
    #: The source says only "latest" or similar; no usable date exists.
    UNDATED = "UNDATED"


class ObservationAttribution(StrEnum):
    """How an observation-to-source link was established."""

    #: The source file names this source for this exact metric.
    EXPLICIT = "EXPLICIT"
    #: Catalogued as supporting this metric type, and listed by this market.
    METRIC_HINT = "METRIC_HINT"
    #: Listed by this market, with no per-metric attribution in the source.
    MARKET_LEVEL = "MARKET_LEVEL"


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
#: Open by design: new snapshots may introduce new metrics.
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


def _values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_cls)


#: Closed vocabularies mirrored by database CHECK constraints (A-2).
#: ``table.column -> allowed values``. Consumed by migration 0002 and asserted
#: by ``tests/integration/test_constraints.py`` so code and schema cannot drift.
CLOSED_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "market_observations.fact_type": _values(FactType),
    "market_observations.availability": _values(Availability),
    "market_observations.period_granularity": _values(PeriodGranularity),
    "market_competition_assessments.fact_type": _values(FactType),
    "market_size_estimates.fact_type": _values(FactType),
    "observation_sources.attribution": _values(ObservationAttribution),
    "score_runs.kind": _values(ScoreRunKind),
    "score_runs.status": _values(ScoreRunStatus),
    "research_gaps.status": _values(ResearchGapStatus),
    "research_gaps.priority": _values(ResearchGapPriority),
    "market_snapshot_categories.category_key": _values(MarketCategory),
}

#: Deliberately left as open text (documented for the audit trail).
OPEN_VOCABULARIES: tuple[str, ...] = (
    "market_observations.metric_key",
    "market_observations.unit",
    "market_observations.period_label",
    "market_observations.confidence",
    "market_score_components.component_key",
    "scoring_model_components.component_key",
    "scoring_models.key",
    "scoring_models.version",
    "markets.region",
    "market_competition_assessments.level",
    "market_size_estimates.confidence_level",
)


def coerce_vocabulary(value: str | None, vocabulary: type[StrEnum], field: str) -> str | None:
    """Validate a closed-vocabulary filter value, or refuse it by name.

    A filter value outside its vocabulary used to return an empty list, so a
    typo was indistinguishable from "nothing matches". On a review queue that
    reads as "nothing to review", which is the wrong answer to give quietly.

    Returns the vocabulary's own spelling of the value, or ``None`` when no
    filter was supplied.
    """
    from boro_gtm.core.errors import ValidationError

    if value is None or value == "":
        return None
    # Match case-insensitively but return the vocabulary's own spelling: these
    # enums are not uniformly upper-case (ScoreRunKind is lower), so upper-casing
    # the caller's input would reject a value the vocabulary actually contains.
    by_fold = {member.value.casefold(): member.value for member in vocabulary}
    canonical = by_fold.get(value.strip().casefold())
    if canonical is None:
        raise ValidationError(
            f"{field}={value!r} is not a valid value",
            details={"field": field, "valid": sorted(by_fold.values())},
        )
    return canonical
