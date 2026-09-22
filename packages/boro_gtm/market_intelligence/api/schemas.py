"""Response schemas for the market-intelligence API.

Kept separate from the ORM models so persistence shape and wire shape can
evolve independently (06 — Quality gates).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ORM = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    app: str


class SourceOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    source_key: str
    title: str
    url: str | None = None
    publisher: str | None = None
    used_for: list[str] | None = None
    note: str | None = None
    #: Only when the catalog states one; never parsed from a free-text title.
    published_at: date | None = None


class ScoreRunSummary(BaseModel):
    model_config = ORM
    id: uuid.UUID
    kind: str
    status: str
    engine_version: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    context: dict[str, Any] | None = None


class SnapshotOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    key: str
    title: str
    generated_date: date | None = None
    score_version: str | None = None
    universe_size: int | None = None
    source_filename: str | None = None
    #: Canonical semantic identity of the document.
    sha256: str
    #: Digest of the literal bytes imported, when a file was used.
    source_file_sha256: str | None = None
    created_at: datetime


class SnapshotDetail(SnapshotOut):
    # These are assembled by the route. The validation aliases keep Pydantic
    # from reaching for same-named ORM attributes (``MarketSnapshot.metadata``
    # is SQLAlchemy's MetaData, not the snapshot's metadata column).
    counts: dict[str, int] = Field(default_factory=dict, validation_alias="api_counts")
    score_runs: list[ScoreRunSummary] = Field(
        default_factory=list, validation_alias="api_score_runs"
    )
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="api_metadata")


class MarketOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    iso2: str | None = None
    iso3: str | None = None
    name: str
    region: str | None = None
    is_home_market: bool


class MarketScoreProjection(BaseModel):
    score_run_id: uuid.UUID
    kind: str
    score: float | None = None
    rank: int | None = None
    confidence: float | None = None
    coverage: float | None = None


class MarketDetail(MarketOut):
    categories: list[str] = Field(default_factory=list)
    competition: dict[str, Any] | None = None
    latest_score: MarketScoreProjection | None = None
    snapshot_key: str | None = None


class ObservationOut(BaseModel):
    """A normalized observation, with its evidence and temporal provenance.

    ``fact_type`` is null exactly when ``availability`` is ``NOT_AVAILABLE``:
    the absence of evidence is reported as absence, not as a kind of evidence.
    """

    model_config = ORM
    id: uuid.UUID
    metric_key: str
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    #: OBSERVED | NOT_AVAILABLE
    availability: str
    period_label: str
    #: DATE | YEAR | SNAPSHOT | UNDATED
    period_granularity: str
    #: An exact as-of date, present only when the source states one.
    observed_at: date | None = None
    fact_type: str | None = None
    confidence: str | None = None
    methodology: str | None = None
    metadata: dict[str, Any] | None = Field(
        default=None, validation_alias="observation_metadata"
    )
    # Populated by the route; aliased away from the ORM relationship of the
    # same name, which holds association objects rather than dicts.
    sources: list[dict[str, str]] = Field(
        default_factory=list, validation_alias="api_sources"
    )


class MarketSizeOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    fact_type: str
    confidence_level: str | None = None
    tam_min: float | None = None
    tam_max: float | None = None
    sam_min: float | None = None
    sam_max: float | None = None
    som_accounts_min: float | None = None
    som_accounts_max: float | None = None
    ticket_min_usd: float | None = None
    ticket_max_usd: float | None = None
    sam_value_min_usd: float | None = None
    sam_value_max_usd: float | None = None
    som_pool_value_min_usd: float | None = None
    som_pool_value_max_usd: float | None = None
    warning: str | None = None


class DeepDiveOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    priority_verticals: list[str] | None = None
    priority_geographies: list[str] | None = None
    recommended_channel: str | None = None
    common_buyers: list[str] | None = None
    common_problem_pattern: str | None = None
    raw_values: dict[str, Any] | None = None


class ScoringComponentOut(BaseModel):
    model_config = ORM
    component_key: str
    weight: float
    formula: str | None = None
    required_metrics: list[str] | None = None
    ordinal: int


class ScoringModelOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    key: str
    version: str
    name: str
    description: str | None = None
    normalization_method: str | None = None
    #: Coverage a result must reach to be rank-comparable under this model.
    minimum_rank_coverage: float
    active: bool
    components: list[ScoringComponentOut] = Field(default_factory=list)


class ScoringModelDetail(ScoringModelOut):
    definition: dict[str, Any]


class ScoreRunOut(ScoreRunSummary):
    model_config = ORM
    snapshot_id: uuid.UUID
    scoring_model_id: uuid.UUID
    universe_definition: dict[str, Any] | None = None
    #: First-class contextual dimensions (ADR-014); null for base runs.
    vertical_id: uuid.UUID | None = None
    icp_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    channel_id: uuid.UUID | None = None
    ticket_usd: float | None = None


class RankingEntry(BaseModel):
    #: Null when the result is not rank-comparable (home benchmark, no score,
    #: or coverage below the model's minimum_rank_coverage).
    rank: int | None = None
    market: dict[str, Any]
    score: float | None = None
    confidence: float | None = None
    coverage: float | None = None


class RankingResponse(BaseModel):
    score_run_id: uuid.UUID
    kind: str
    snapshot_key: str
    model: str
    total: int
    limit: int
    offset: int
    results: list[RankingEntry]


class ScoreComponentOut(BaseModel):
    model_config = ORM
    component_key: str
    raw_value: float | None = None
    normalized_value: float | None = None
    weighted_score: float | None = None
    confidence: float | None = None
    coverage: float | None = None
    explanation: str | None = None
    metadata: dict[str, Any] | None = Field(
        default=None, validation_alias="component_metadata"
    )


class MarketScoreDetail(BaseModel):
    score_run_id: uuid.UUID
    kind: str
    model: str
    snapshot_key: str
    market: dict[str, Any]
    score: float | None = None
    rank: int | None = None
    confidence: float | None = None
    coverage: float | None = None
    metadata: dict[str, Any] | None = None
    components: list[ScoreComponentOut] = Field(default_factory=list)
    research_gaps: list[dict[str, Any]] = Field(default_factory=list)


class BaseScoreRunRequest(BaseModel):
    snapshot_key: str
    model_key: str = "market-attractiveness"
    model_version: str = "1.0"
    mode: str = "reference_reproduction"


class PaginatedMarkets(BaseModel):
    total: int
    limit: int
    offset: int
    results: list[MarketDetail]
