"""M3 API schemas.

Three exclusions are structural rather than editorial, and each is enforced by
a test at the serialization boundary:

* **a run carries no status.** A question does not have one; its executions do.
* **no M4 field appears anywhere** — no qualification, no commercial level, no
  capability, no price, no hypothesis.
* **no raw body and no raw model output by default.** A body is retention-
  limited internal bytes; what a client needs is the hash, the length and the
  quoted span, all of which survive pruning.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class RunOut(BaseModel):
    """A research question. No execution state, by design."""

    id: uuid.UUID
    company_id: uuid.UUID
    research_policy_version: str
    vertical_id: uuid.UUID | None = None
    target_attribute_keys: list[str]
    research_plan_hash: str
    plan_inputs: dict[str, Any] | None = None
    created_by: str
    created_at: datetime
    attempt_count: int


class RunCreate(BaseModel):
    company_id: uuid.UUID
    vertical_id: uuid.UUID | None = None
    target_attribute_keys: list[str] | None = None
    plan_inputs: dict[str, Any] | None = None


class AttemptOut(BaseModel):
    """One execution, with the stage timestamps that gate it."""

    id: uuid.UUID
    run_id: uuid.UUID
    attempt_number: int
    status: str
    error: str | None = None
    failure_stage: str | None = None
    allow_partial_assertion: bool
    attempt_seed_inputs: dict[str, Any] | None = None
    attempt_seed_inputs_hash: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    discovery_completed_at: datetime | None = None
    fetch_completed_at: datetime | None = None
    extraction_completed_at: datetime | None = None
    completed_at: datetime | None = None


class AttemptCreate(BaseModel):
    """Fixture execution is explicit, so nothing implies a live crawler."""

    confirm_sampled: bool = False
    conditional: bool = True


# --- evidence ---------------------------------------------------------------


class SourceOut(BaseModel):
    id: uuid.UUID
    normalized_locator: str
    locator_policy_version: str
    host: str
    registrable_domain: str
    first_seen_at: datetime


class FetchEventOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    attempt_id: uuid.UUID
    body_id: uuid.UUID | None = None
    fetch_outcome: str
    http_status: int | None = None
    final_url: str | None = None
    declared_content_type: str | None = None
    content_length: int | None = None
    etag: str | None = None
    validated_by: str | None = None
    error_class: str | None = None
    retrieved_at: datetime


class DiscoveryOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    attempt_id: uuid.UUID
    discovery_method: str
    discovered_from_source_id: uuid.UUID | None = None
    discovery_context: dict[str, Any] | None = None
    discovery_context_hash: str | None = None
    relevance_hint: float | None = None
    discovered_at: datetime


class SourceEdgeOut(BaseModel):
    id: uuid.UUID
    from_source_id: uuid.UUID
    to_source_id: uuid.UUID
    relation_type: str
    edge_origin: str
    observed_by_fetch_event_id: uuid.UUID | None = None
    corroborating_fetch_event_id: uuid.UUID | None = None
    observed_at: datetime


class ArtifactOut(BaseModel):
    id: uuid.UUID
    canonicalization_strategy: str
    canonicalization_version: str
    canonical_content_hash: str
    first_seen_at: datetime


class DerivationOut(BaseModel):
    id: uuid.UUID
    body_id: uuid.UUID
    artifact_id: uuid.UUID
    canonicalization_strategy: str
    canonicalization_version: str
    canonical_content_hash: str
    derivation_status: str
    source_published_at: date | None = None
    source_published_granularity: str
    language: str | None = None
    title: str | None = None
    derived_at: datetime


class BodyOut(BaseModel):
    """Hash, length and retention state. **Never the bytes.**"""

    id: uuid.UUID
    raw_body_sha256: str
    byte_length: int
    body_retention: str
    pruned_at: datetime | None = None
    first_seen_at: datetime


class ExtractionOut(BaseModel):
    """Provenance of a reading. `raw_output` is excluded by default."""

    id: uuid.UUID
    text_derivation_id: uuid.UUID
    body_id: uuid.UUID
    extractor_kind: str
    extractor_id: str
    extractor_version: str
    model_provider: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    prompt_template_version: str | None = None
    output_schema_version: str
    determinism: str
    temperature: float | None = None
    sample_execution_id: str | None = None
    extraction_contract_hash: str
    extractor_confidence: float | None = None
    raw_output_sha256: str | None = None
    raw_output_retention: str
    status: str
    created_at: datetime


class EvidenceItemOut(BaseModel):
    """Enough to walk the provenance without reconstructing joins by hand."""

    id: uuid.UUID
    source: SourceOut
    fetch_event: FetchEventOut
    artifact_derivation: DerivationOut
    artifact: ArtifactOut
    body: BodyOut
    extraction: ExtractionOut
    locator: dict[str, Any]
    locator_hash: str
    quote: str | None = None
    quote_sha256: str | None = None
    publisher_key: str
    publisher_policy_version: str
    created_at: datetime


# --- company-global research ------------------------------------------------


class AttributeStateOut(BaseModel):
    attribute_key: str
    availability: str
    envelope: dict[str, Any] | None = None
    best: dict[str, Any] | None = None
    best_claim_id: uuid.UUID | None = None
    best_fact_type: str | None = None
    unit: str | None = None
    confidence: float | None = None
    observed_at: date | None = None
    observed_granularity: str | None = None
    contradiction: bool = False
    contradicting_claim_ids: list[uuid.UUID] = Field(default_factory=list)
    corroborating_publisher_count: int = 0
    claim_ids: list[uuid.UUID] = Field(default_factory=list)
    staleness: str | None = None


class CompanyResearchOut(BaseModel):
    """Company-global knowledge. **No coverage** — that belongs to a plan."""

    company_id: uuid.UUID
    attributes: list[AttributeStateOut]
    assertion_policy_version: str
    publisher_policy_version: str
    derived_from_claim_ids: list[uuid.UUID]
    as_of: date


class ResearchClaimOut(BaseModel):
    id: uuid.UUID
    attribute_key: str
    attribute_registry_version: str
    value: dict[str, Any] | None = None
    unit: str | None = None
    fact_type: str | None = None
    availability: str
    confidence: float | None = None
    observed_at: date | None = None
    period_granularity: str
    assertion_fingerprint: str | None = None
    evidence_item_ids: list[uuid.UUID]
    created_at: datetime


class Q2EvidenceOut(BaseModel):
    """The canonical seven-field commercial evidence record.

    `related_hypothesis` is always null: M4 owns hypotheses, and manufacturing
    one here would be M3 doing interpretation under another name.
    """

    source: str
    source_type: str
    date_observed: date | None = None
    fact: str
    confidence: float | None = None
    inference_allowed: bool
    related_hypothesis: None = None


# --- plan-scoped ------------------------------------------------------------


class CoverageOut(BaseModel):
    """Three numbers, never collapsed into one that would read as a score."""

    run_id: uuid.UUID
    company_id: uuid.UUID
    coverage: float
    confidence_summary: float | None = None
    contradiction_rate: float
    required_attribute_count: int
    covered_attribute_count: int
    not_applicable_attribute_count: int
    open_gap_count: int
    assertion_policy_version: str
    publisher_policy_version: str


class GapOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    company_id: uuid.UUID
    attribute_key: str
    gap_kind: str
    current_status: str | None = None
    attempt_count: int
    first_raised_at: datetime


# --- identity review --------------------------------------------------------


class SignalOccurrenceOut(BaseModel):
    id: uuid.UUID
    occurrence_number: int
    raised_by_attempt_id: uuid.UUID
    supersedes_occurrence_id: uuid.UUID | None = None
    is_open: bool
    current_status: str | None = None
    raised_at: datetime


class IdentitySignalOut(BaseModel):
    """M3-owned end to end. No provider entity, no M2 review request."""

    id: uuid.UUID
    company_id: uuid.UUID
    related_company_id: uuid.UUID | None = None
    signal_kind: str
    normalized_concern: str
    signal_policy_version: str
    signal_fingerprint: str
    first_raised_at: datetime
    occurrences: list[SignalOccurrenceOut] = Field(default_factory=list)


class SignalStatusIn(BaseModel):
    status: str
    actor: str
    note: str | None = None


class SignalEventOut(BaseModel):
    id: uuid.UUID
    occurrence_id: uuid.UUID
    status: str
    actor: str
    note: str | None = None
    occurred_at: datetime


# --- human review -----------------------------------------------------------


class EvidenceReviewIn(BaseModel):
    decision: str            # CONFIRM | REJECT
    actor: str
    note: str | None = None
    #: Required to assert on confirmation; a rejection needs no company.
    company_id: uuid.UUID | None = None


class EvidenceReviewOut(BaseModel):
    """A durable review record. Both decisions persist one.

    Keyed on the evidence item, which exists from the moment a sampled reading
    is recorded — before any claim does, and therefore before the point at
    which a claim-keyed route could have addressed it.
    """

    review_id: uuid.UUID
    evidence_item_id: uuid.UUID
    decision: str
    actor: str
    note: str | None = None
    reviewed_at: datetime
    human_extraction_id: uuid.UUID | None = None
    resulting_claim_id: uuid.UUID | None = None
    created_evidence_item_ids: list[uuid.UUID] = Field(default_factory=list)
    model_extraction_unchanged: bool


class PendingReviewOut(BaseModel):
    """A sampled reading awaiting a human. It supports no claim yet."""

    evidence_item_id: uuid.UUID
    extraction_id: uuid.UUID
    source: str
    quote: str | None = None
    locator: dict[str, Any]
    created_at: datetime


# --- retention --------------------------------------------------------------


class RetentionPlanOut(BaseModel):
    """Counts before execution, so a dry run is the default shape."""

    as_of: datetime
    dry_run: bool
    bodies: int
    text_derivations: int
    model_outputs: int
