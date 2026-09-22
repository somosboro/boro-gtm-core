"""M1 API schemas."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ORM = ConfigDict(from_attributes=True)


class VerticalOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    key: str
    name: str
    description: str | None = None
    taxonomy_codes: dict[str, Any] | None = None


class ICPOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    key: str
    name: str
    description: str | None = None
    version: str
    definition: dict[str, Any]


class OfferOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    key: str
    name: str
    description: str | None = None
    currency: str
    ticket_min: float | None = None
    ticket_max: float | None = None
    definition: dict[str, Any] | None = None


class ChannelOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    key: str
    name: str
    definition: dict[str, Any] | None = None


class MarketVerticalProfileOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    vertical_key: str
    vertical_name: str
    snapshot_key: str
    fit_score: float | None = None
    confidence: float | None = None
    coverage: float | None = None
    tam_min: float | None = None
    tam_max: float | None = None
    sam_min: float | None = None
    sam_max: float | None = None
    ticket_min_usd: float | None = None
    ticket_max_usd: float | None = None
    priority_geographies: list[str] | None = None
    recommended_motion: dict[str, Any] | None = None
    problem_patterns: list[str] | None = None
    evidence: dict[str, Any] | None = None
    research_gaps: list[dict[str, Any]] = Field(default_factory=list)


class ResearchGapOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    metric_key: str
    priority: str
    reason: str
    status: str
    market_iso2: str | None = None
    vertical_key: str | None = None


class ResearchGapStatusRequest(BaseModel):
    status: str


class ContextualRankingRequest(BaseModel):
    snapshot_key: str
    model_key: str = "contextual-market-fit"
    model_version: str = "1.0"
    vertical_key: str | None = None
    icp_key: str | None = None
    offer_key: str | None = None
    channel_key: str | None = None
    ticket_usd: float | None = None
    market_iso2: list[str] | None = None
    allow_low_coverage: bool = False
    min_coverage: float | None = None
    base_score_run_id: uuid.UUID | None = None


class ContextualComponentOut(BaseModel):
    component_key: str
    weight: float
    normalized_value: float | None = None
    weighted_score: float | None = None
    coverage: float
    confidence: float
    explanation: str
    missing_metrics: list[str] = Field(default_factory=list)


class ContextualResultOut(BaseModel):
    market: dict[str, Any]
    score: float | None = None
    confidence: float
    coverage: float
    comparable: bool
    rank: int | None = None
    components: list[ContextualComponentOut] = Field(default_factory=list)
    research_gaps: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ContextualRankingResponse(BaseModel):
    score_run_id: uuid.UUID | None = None
    model: str
    context: dict[str, Any]
    coverage_policy: dict[str, Any]
    research_gap_summary: dict[str, int]
    results: list[ContextualResultOut]
