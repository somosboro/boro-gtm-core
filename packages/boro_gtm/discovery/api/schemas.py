"""M2 API response schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ORM = ConfigDict(from_attributes=True)


class ProviderOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    provider_key: str
    name: str
    identity_capability: str
    canonicalization_strategy: str
    canonicalization_version: str
    media_type: str
    trust_tier: float
    is_active: bool
    #: TEST/FIXTURE adapters are flagged so they are never mistaken for
    #: production discovery sources.
    is_fixture: bool


class DiscoveryRunOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    provider_id: uuid.UUID
    status: str
    market_id: uuid.UUID | None = None
    vertical_id: uuid.UUID | None = None
    icp_id: uuid.UUID | None = None
    channel_id: uuid.UUID | None = None
    allow_partial_resolution: bool
    adapter_version: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    created_at: datetime


class DiscoveryQueryOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    page_number: int
    cursor: str | None = None
    parameters: dict[str, Any]
    result_count: int | None = None
    issued_at: datetime


class RecordVersionOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    provider_entity_id: uuid.UUID
    canonical_payload_hash: str
    canonicalization_strategy: str
    canonicalization_version: str
    retrieved_at: datetime
    body_count: int = 0
    sighting_count: int = 0


class ProviderEntityOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    provider_id: uuid.UUID
    provider_external_id: str
    external_id_kind: str
    key_algorithm_version: str | None = None
    first_seen_at: datetime


class CompanyOut(BaseModel):
    """Anchor plus current projection."""

    id: uuid.UUID
    lifecycle_status: str
    identity_policy_version: str
    merged_into_company_id: uuid.UUID | None = None
    created_at: datetime
    canonical_name: str | None = None
    primary_domain: str | None = None
    legal_form: str | None = None
    founded_year: int | None = None
    employee_count_min: int | None = None
    employee_count_max: int | None = None
    projection_conflict: bool = False


class ClaimOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    attribute_key: str
    attribute_registry_version: str
    value_jsonb: dict[str, Any] | None = None
    unit: str | None = None
    fact_type: str | None = None
    availability: str
    confidence: float | None = None
    provider_record_version_id: uuid.UUID | None = None
    subject_company_id: uuid.UUID | None = None
    resolution_decision_id: uuid.UUID | None = None
    observed_at: date | None = None
    period_granularity: str
    created_at: datetime


class LocationOut(BaseModel):
    model_config = ORM
    location_type: str
    address_normalized: str
    address_raw: str | None = None
    city: str | None = None
    postal_code: str | None = None
    market_id: uuid.UUID | None = None


class MarketPresenceOut(BaseModel):
    model_config = ORM
    market_id: uuid.UUID
    presence_type: str
    effective_from: date
    effective_to: date | None = None
    fact_type: str | None = None
    confidence: float | None = None


class VerticalOut(BaseModel):
    model_config = ORM
    vertical_id: uuid.UUID
    classification_method: str
    fact_type: str | None = None
    confidence: float | None = None
    is_primary: bool


class RelationshipOut(BaseModel):
    model_config = ORM
    from_company_id: uuid.UUID
    to_company_id: uuid.UUID
    relationship_type: str
    effective_from: date
    effective_to: date | None = None
    fact_type: str | None = None


class DecisionOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    provider_entity_id: uuid.UUID
    provider_record_version_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    merged_company_id: uuid.UUID | None = None
    decision: str
    method: str
    supersedes_decision_id: uuid.UUID | None = None
    identity_policy_version: str
    signals: dict[str, Any] | None = None
    rationale: str | None = None
    decided_by: str
    decided_at: datetime


class AttributeDefinitionOut(BaseModel):
    model_config = ORM
    attribute_key: str
    registry_version: str
    value_kind: str
    value_type: str
    cardinality: str
    projection_strategy: str
    conflict_strategy: str
    allowed_units: list[str] | None = None
    allowed_fact_types: list[str]
    shadow_column: str | None = None
    target_projection: str | None = None


class CreateRunRequest(BaseModel):
    provider_key: str
    market_iso2: str | None = None
    vertical_key: str | None = None
    icp_key: str | None = None
    channel_key: str | None = None
    allow_partial_resolution: bool = False
    query: dict[str, Any] = Field(default_factory=dict)


class HumanReviewRequest(BaseModel):
    """Record a human resolution decision, superseding the current head."""

    provider_entity_id: uuid.UUID
    decision: str
    company_id: uuid.UUID | None = None
    rationale: str
    decided_by: str


class ProjectionRunOut(BaseModel):
    model_config = ORM
    id: uuid.UUID
    started_at: datetime
    completed_at: datetime | None = None
    attribute_registry_version: str
    identity_policy_version: str
    row_counts: dict[str, Any] | None = None
    content_digests: dict[str, Any] | None = None
    triggered_by: str
