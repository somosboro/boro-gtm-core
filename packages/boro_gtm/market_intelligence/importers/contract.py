"""Source-file data contract.

Two validation layers run before anything touches the database:

1. **Structural** — the supplied ``market_intelligence_v1.schema.json``.
2. **Semantic** — the invariants in 03 (rank uniqueness, universe arithmetic,
   score bounds, component-vs-weight bounds, fact-type vocabulary, source-key
   referential integrity).

Layer 2 distinguishes *fatal* problems from *warnings*: a missing TAM/SAM/SOM
block is expected and must not fail an import, whereas a duplicate rank means
the artifact is not trustworthy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from boro_gtm.core.config import REPO_ROOT
from boro_gtm.core.enums import FactType
from boro_gtm.core.errors import ValidationError

SCHEMA_PATH = REPO_ROOT / "data" / "market_intelligence_v1.schema.json"

#: Component key -> maximum attainable weight, from metadata.score_formula.
COMPONENT_WEIGHTS: dict[str, float] = {
    "economic_strength_20": 20.0,
    "technology_investment_readiness_20": 20.0,
    "digitalization_opportunity_15": 15.0,
    "icp_density_proxy_20": 20.0,
    "ability_to_pay_10": 10.0,
    "gtm_ease_10": 10.0,
    "outlook_5": 5.0,
}

_WEIGHT_TOLERANCE = 1e-6
_VALID_FACT_TYPES = {f.value for f in FactType}


# ---------------------------------------------------------------------------
# Pydantic projection of the source file
# ---------------------------------------------------------------------------


class RawMetrics(BaseModel):
    """Raw per-market inputs. ``None`` always means unknown."""

    model_config = ConfigDict(extra="allow")

    gdp_nominal_2025_usd_bn: float | None = None
    gdp_per_capita_2026_usd: float | None = None
    real_gdp_growth_2026_pct: float | None = None
    innovation_digital_proxy_2025: float | None = None
    manufacturing_value_added_proxy_usd_bn: float | None = None
    software_spending_2024_pct_gdp: float | None = None
    language_access_0_1: float | None = None
    timezone_overlap_0_1: float | None = None
    b2b_email_legal_access_0_1: float | None = None


class Subscores(BaseModel):
    model_config = ConfigDict(extra="allow")

    economic_strength_20: float
    technology_investment_readiness_20: float
    digitalization_opportunity_15: float
    icp_density_proxy_20: float
    ability_to_pay_10: float
    gtm_ease_10: float
    outlook_5: float

    def as_dict(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.model_dump().items() if isinstance(v, (int, float))}


class Competition(BaseModel):
    model_config = ConfigDict(extra="allow")

    level: str
    fact_type: str = FactType.UNKNOWN.value
    included_in_score: bool = False


class TechnologySpendingGrowth(BaseModel):
    model_config = ConfigDict(extra="allow")

    value_pct: float | None = None
    year: int | None = None
    period: str | None = None
    scope: str | None = None
    source: str | None = None
    fact_type: str = FactType.UNKNOWN.value


class Range(BaseModel):
    model_config = ConfigDict(extra="allow")
    min: float | None = None
    max: float | None = None


class TamSamSom(BaseModel):
    model_config = ConfigDict(extra="allow")

    fact_type: str = FactType.UNKNOWN.value
    confidence_level: str | None = None
    tam_relevant_employer_firms: Range | None = None
    sam_boro_icp_firms: Range | None = None
    som_addressable_accounts_12_24m: Range | None = None
    ticket_usd: Range | None = None
    sam_economic_value_usd: Range | None = None
    som_account_pool_value_usd: Range | None = None
    warning: str | None = None


class DeepDive(BaseModel):
    model_config = ConfigDict(extra="allow")

    priority_verticals: str | list[str] | None = None
    priority_geographies: str | list[str] | None = None
    recommended_channel: str | None = None
    common_buyers: list[str] | str | None = None
    common_problem_pattern: str | None = None


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    url: str | None = None
    publisher: str | None = None
    used_for: list[str] | None = None
    note: str | None = None


class MarketEntry(BaseModel):
    """A ranked market or the home-market benchmark."""

    model_config = ConfigDict(extra="allow")

    country: str
    region: str
    raw: RawMetrics
    subscores: Subscores
    market_score: float
    rank: int | None = None
    market_categories: list[str] = Field(default_factory=list)
    competition: Competition
    why_it_matters: str | None = None
    confidence_level: str | None = None
    sources: list[str] = Field(default_factory=list)
    technology_spending_growth: TechnologySpendingGrowth | None = None
    tam_sam_som: TamSamSom | None = None
    deep_dive: DeepDive | None = None


class UniverseEntry(BaseModel):
    """A normalization-universe market excluded from the ranked top 50."""

    model_config = ConfigDict(extra="allow")

    country: str
    market_score: float


class SnapshotMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    generated_date: str
    currency: str | None = None
    universe_size: int
    normalization: str
    score_version: str
    score_formula: dict[str, str]
    important_method_notes: list[str] = Field(default_factory=list)
    fact_type_legend: dict[str, str] = Field(default_factory=dict)
    international_top50_excludes_home_market: str | None = None


class MarketIntelligenceDocument(BaseModel):
    """The validated source document."""

    model_config = ConfigDict(extra="allow")

    metadata: SnapshotMetadata
    source_catalog: dict[str, SourceEntry]
    markets: list[MarketEntry]
    home_market_benchmark: MarketEntry
    excluded_from_top50_but_in_normalization_universe: list[UniverseEntry]

    @property
    def all_scored_markets(self) -> list[MarketEntry]:
        """Ranked markets plus the home benchmark (everything with raw data)."""
        return [*self.markets, self.home_market_benchmark]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_schema(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or SCHEMA_PATH).read_text(encoding="utf-8"))


def validate_structure(payload: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
    """Validate against the JSON Schema. Raises on the first violation."""
    try:
        jsonschema.validate(payload, schema or load_schema())
    except jsonschema.ValidationError as exc:  # pragma: no cover - message passthrough
        raise ValidationError(
            f"Source document failed JSON Schema validation: {exc.message}",
            details={"path": list(exc.absolute_path)},
        ) from exc


def validate_semantics(doc: MarketIntelligenceDocument) -> ValidationReport:
    """Apply the semantic invariants from 03 — Validation rules."""
    report = ValidationReport()

    # --- ranked top 50 -----------------------------------------------------
    if len(doc.markets) != 50:
        report.errors.append(f"Expected exactly 50 ranked markets, found {len(doc.markets)}")

    ranks = [m.rank for m in doc.markets]
    if any(r is None for r in ranks):
        report.errors.append("Every ranked market must declare a rank")
    else:
        duplicates = {r for r in ranks if ranks.count(r) > 1}
        if duplicates:
            report.errors.append(f"Duplicate ranks in ranked top50: {sorted(duplicates)}")

    # --- duplicate names within each section -------------------------------
    for label, names in (
        ("markets", [m.country for m in doc.markets]),
        (
            "excluded_from_top50_but_in_normalization_universe",
            [m.country for m in doc.excluded_from_top50_but_in_normalization_universe],
        ),
    ):
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            report.errors.append(f"Duplicate market names in {label}: {dupes}")

    # --- universe arithmetic ----------------------------------------------
    computed = (
        len(doc.markets) + 1 + len(doc.excluded_from_top50_but_in_normalization_universe)
    )
    if computed != doc.metadata.universe_size:
        report.errors.append(
            f"metadata.universe_size={doc.metadata.universe_size} is inconsistent with "
            f"50 ranked + 1 benchmark + "
            f"{len(doc.excluded_from_top50_but_in_normalization_universe)} excluded = {computed}"
        )

    # --- score bounds and component-vs-weight bounds ----------------------
    for entry in doc.all_scored_markets:
        if not 0.0 <= entry.market_score <= 100.0:
            report.errors.append(
                f"{entry.country}: market_score {entry.market_score} outside 0-100"
            )
        for key, value in entry.subscores.as_dict().items():
            ceiling = COMPONENT_WEIGHTS.get(key)
            if ceiling is None:
                report.warnings.append(f"{entry.country}: unknown subscore component {key!r}")
                continue
            if value < -_WEIGHT_TOLERANCE or value > ceiling + _WEIGHT_TOLERANCE:
                report.errors.append(
                    f"{entry.country}: component {key}={value} outside [0, {ceiling}]"
                )

    for entry in doc.excluded_from_top50_but_in_normalization_universe:
        if not 0.0 <= entry.market_score <= 100.0:
            report.errors.append(
                f"{entry.country}: market_score {entry.market_score} outside 0-100"
            )

    # --- fact-type vocabulary ---------------------------------------------
    for entry in doc.all_scored_markets:
        candidates = [("competition", entry.competition.fact_type)]
        if entry.technology_spending_growth is not None:
            candidates.append(
                ("technology_spending_growth", entry.technology_spending_growth.fact_type)
            )
        if entry.tam_sam_som is not None:
            candidates.append(("tam_sam_som", entry.tam_sam_som.fact_type))
        for where, value in candidates:
            if value not in _VALID_FACT_TYPES:
                report.errors.append(
                    f"{entry.country}: invalid fact_type {value!r} in {where}"
                )

    # --- source referential integrity -------------------------------------
    catalog = set(doc.source_catalog)
    for entry in doc.all_scored_markets:
        unknown = sorted(set(entry.sources) - catalog)
        if unknown:
            report.errors.append(f"{entry.country}: references unknown source keys {unknown}")
        if entry.technology_spending_growth and entry.technology_spending_growth.source:
            if entry.technology_spending_growth.source not in catalog:
                report.errors.append(
                    f"{entry.country}: technology_spending_growth.source "
                    f"{entry.technology_spending_growth.source!r} is not in the catalog"
                )

    # --- warnings (never fatal) -------------------------------------------
    for entry in doc.markets:
        if entry.tam_sam_som is None:
            report.warnings.append(f"{entry.country}: no TAM/SAM/SOM supplied")
        if entry.technology_spending_growth is None or (
            entry.technology_spending_growth.value_pct is None
        ):
            report.warnings.append(f"{entry.country}: no country technology-spending growth")
        if entry.raw.software_spending_2024_pct_gdp is None:
            report.warnings.append(f"{entry.country}: no software spending % of GDP")
        if entry.confidence_level and entry.confidence_level.upper().startswith("LOW"):
            report.warnings.append(
                f"{entry.country}: low-confidence market assessment "
                f"({entry.confidence_level})"
            )

    unused = sorted(catalog - {s for m in doc.all_scored_markets for s in m.sources})
    for key in unused:
        report.warnings.append(f"Source catalog entry {key!r} is not referenced by any market")

    return report


def parse_document(payload: dict[str, Any]) -> MarketIntelligenceDocument:
    """Structural validation followed by the Pydantic projection."""
    validate_structure(payload)
    return MarketIntelligenceDocument.model_validate(payload)
