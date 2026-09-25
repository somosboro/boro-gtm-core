"""M3 Operational Research — 23 tables (design revision 4).

Three rules govern every table here, each learned the expensive way in an
earlier milestone:

1. **An append-only row may not contain a value that changes.** Anything that
   changes lives in an event log or a projection.
2. **A globally deduplicated identity row may not carry a fact belonging to one
   of the contexts that produced it**, and every provenance walk is
   single-valued.
3. **A derived value may not be keyed more narrowly than the context that
   determines it**, and evidence exists independently of whatever consumes it.

The enum vocabularies in :mod:`boro_gtm.research.enums` are mirrored by CHECK
constraints below; the pair is one contract stated twice.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from boro_gtm.core.db import Base, new_uuid

# M3 declares foreign keys into M1 (markets, verticals) and M2 (companies,
# company_claims). Importing them guarantees those tables are in the shared
# metadata whatever the import order.
from boro_gtm.discovery.domain import models as _m2_models  # noqa: F401
from boro_gtm.market_intelligence.domain import models as _m0_models  # noqa: F401
from boro_gtm.research import enums as e

#: Python ``None`` must become SQL ``NULL``, not the JSON value ``null``.
NULLABLE_JSONB = JSONB(none_as_null=True)


def _vocab(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    joined = ",".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({joined})", name=name)


# ---------------------------------------------------------------------------
# The question, and its executions
# ---------------------------------------------------------------------------


class OperationalResearchRun(Base):
    """A logical research *question*, not an execution (M3-ADR-019).

    Identity is ``research_plan_hash``, which covers every input that changes
    the question. Nothing execution-shaped lives here: no status, no stage
    timestamps, no error, no seeds.
    """

    __tablename__ = "operational_research_runs"
    __table_args__ = (
        UniqueConstraint("research_plan_hash", name="uq_run_plan_hash"),
        Index("ix_research_runs_company", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    research_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verticals.id", ondelete="RESTRICT"), nullable=True
    )
    target_attribute_keys: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), nullable=False
    )
    plan_inputs: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    research_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperationalResearchAttempt(Base):
    """One execution of a question. Terminal states are final."""

    __tablename__ = "operational_research_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_attempt_number"),
        # At most one live attempt per run: two workers cannot execute the same
        # question concurrently.
        Index(
            "uq_attempt_live", "run_id", unique=True,
            postgresql_where="status NOT IN ('COMPLETED','PARTIAL','FAILED')",
        ),
        _vocab("status", e._v(e.AttemptStatus), "ck_attempt_status_vocabulary"),
        Index("ix_attempts_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_runs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    allow_partial_assertion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cost_units: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    #: Frozen when execution begins; an attempt whose seeds changed mid-flight
    #: would not be reproducible, which is the only reason to record them.
    attempt_seed_inputs: Mapped[dict[str, Any] | None] = mapped_column(
        NULLABLE_JSONB, nullable=True
    )
    attempt_seed_inputs_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovery_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetch_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extraction_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Where we looked
# ---------------------------------------------------------------------------


class ResearchSource(Base):
    """A normalized locator. Carries no run-specific state (M3-ADR-015)."""

    __tablename__ = "research_sources"
    __table_args__ = (
        UniqueConstraint("normalized_locator", "locator_policy_version", name="uq_source_locator"),
        Index("ix_sources_registrable_domain", "registrable_domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    normalized_locator: Mapped[str] = mapped_column(Text, nullable=False)
    locator_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    registrable_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchSourceDiscovery(Base):
    """How we found a source, each time. Context is part of identity."""

    __tablename__ = "research_source_discoveries"
    __table_args__ = (
        Index(
            "uq_discovery_identity",
            "source_id", "attempt_id", "discovery_method",
            "discovered_from_source_id", "discovery_context_hash",
            unique=True, postgresql_nulls_not_distinct=True,
        ),
        _vocab("discovery_method", e._v(e.DiscoveryMethod), "ck_discovery_method_vocabulary"),
        Index("ix_discovery_attempt", "attempt_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    discovery_method: Mapped[str] = mapped_column(String(16), nullable=False)
    discovered_from_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=True
    )
    discovery_context: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    discovery_context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relevance_hint: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchSourceEdge(Base):
    """An observed relationship between locators. Every edge carries evidence."""

    __tablename__ = "research_source_edges"
    __table_args__ = (
        Index(
            "uq_source_edge",
            "from_source_id", "to_source_id", "relation_type",
            "observed_by_fetch_event_id",
            unique=True, postgresql_nulls_not_distinct=True,
        ),
        _vocab("relation_type", e._v(e.SourceRelation), "ck_edge_relation_vocabulary"),
        _vocab("edge_origin", e._v(e.EdgeOrigin), "ck_edge_origin_vocabulary"),
        # "Every edge has evidence" is true in the schema, not only in prose.
        CheckConstraint(
            "(edge_origin = 'FETCH_OBSERVED' AND observed_by_fetch_event_id IS NOT NULL "
            "   AND corroborating_fetch_event_id IS NULL) OR "
            "(edge_origin = 'DERIVED' AND observed_by_fetch_event_id IS NOT NULL "
            "   AND corroborating_fetch_event_id IS NOT NULL) OR "
            "(edge_origin = 'HUMAN_ASSERTED' AND asserted_by IS NOT NULL "
            "   AND rationale IS NOT NULL)",
            name="ck_edge_origin_requires_evidence",
        ),
        # A mirror is an inference over two observations; no single fetch
        # witnesses it.
        CheckConstraint(
            "relation_type <> 'MIRROR_CANDIDATE' OR edge_origin <> 'FETCH_OBSERVED'",
            name="ck_mirror_is_derived",
        ),
        Index("ix_edges_to_source", "to_source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    from_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=False
    )
    to_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    edge_origin: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_by_fetch_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_fetch_events.id", ondelete="RESTRICT"), nullable=True
    )
    corroborating_fetch_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_fetch_events.id", ondelete="RESTRICT"), nullable=True
    )
    asserted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Retrieval, bytes, semantics, text
# ---------------------------------------------------------------------------


class ResearchFetchEvent(Base):
    """Every retrieval, successful or not. Unique on nothing, by design."""

    __tablename__ = "research_fetch_events"
    __table_args__ = (
        # Composite-FK target: evidence binds an extraction and a retrieval to
        # one body, declaratively.
        UniqueConstraint("id", "body_id", name="uq_fetch_event_body"),
        _vocab("fetch_outcome", e._v(e.FetchOutcome), "ck_fetch_outcome_vocabulary"),
        # A 304 returns no bytes but asserts the cached body is still current,
        # so it references the body it validated (M3-ADR-037).
        CheckConstraint(
            "(fetch_outcome = 'OK' AND body_id IS NOT NULL) OR "
            "(fetch_outcome = 'NOT_MODIFIED' AND body_id IS NOT NULL "
            "   AND http_status = 304 AND validated_by IS NOT NULL) OR "
            "(fetch_outcome NOT IN ('OK','NOT_MODIFIED') AND body_id IS NULL)",
            name="ck_fetch_body_semantics",
        ),
        CheckConstraint(
            "validated_by IS NULL OR validated_by IN ('ETAG','LAST_MODIFIED')",
            name="ck_fetch_validator_vocabulary",
        ),
        Index("ix_fetch_source_time", "source_id", "retrieved_at"),
        Index("ix_fetch_attempt", "attempt_id"),
        Index("ix_fetch_body", "body_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    body_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_artifact_bodies.id", ondelete="RESTRICT"), nullable=True
    )
    fetch_outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Which validator proved a 304, so the claim is checkable not assumed.
    validated_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    validator_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_headers_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchArtifactBody(Base):
    """Byte identity. Global, and nothing contextual (M3-ADR-023)."""

    __tablename__ = "research_artifact_bodies"
    __table_args__ = (
        UniqueConstraint("raw_body_sha256", name="uq_body_sha256"),
        _vocab("body_retention", e._v(e.RetentionState), "ck_body_retention_vocabulary"),
        Index("ix_bodies_retained", "body_retention"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    raw_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_body: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    body_retention: Mapped[str] = mapped_column(String(16), nullable=False, default="RETAINED")
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchBodyClassification(Base):
    """Sniffed media type, under a named versioned classifier."""

    __tablename__ = "research_body_classifications"
    __table_args__ = (
        UniqueConstraint("body_id", "classifier_policy_version", name="uq_body_classification"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    body_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifact_bodies.id", ondelete="RESTRICT"), nullable=False
    )
    classifier_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    sniffed_media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    encoding: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchArtifact(Base):
    """Semantic identity. No observational metadata (M3-ADR-031)."""

    __tablename__ = "research_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "canonicalization_strategy", "canonicalization_version",
            "canonical_content_hash", name="uq_artifact_identity",
        ),
        _vocab(
            "canonicalization_strategy", e._v(e.CanonicalizationStrategy),
            "ck_artifact_strategy_vocabulary",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    canonicalization_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchArtifactDerivation(Base):
    """One body under one canonicalization contract.

    Carries the publication metadata observed in *this* body — deliberately
    not on the artifact, because two bodies canonicalizing to one artifact may
    differ in publication date, and folding that into the canonical hash would
    split mirrors and break independence detection (M3-ADR-031).
    """

    __tablename__ = "research_artifact_derivations"
    __table_args__ = (
        UniqueConstraint(
            "body_id", "canonicalization_strategy", "canonicalization_version",
            name="uq_derivation_contract",
        ),
        UniqueConstraint("id", "body_id", name="uq_derivation_body"),
        _vocab(
            "canonicalization_strategy", e._v(e.CanonicalizationStrategy),
            "ck_derivation_strategy_vocabulary",
        ),
        _vocab("derivation_status", e._v(e.DerivationStatus), "ck_derivation_status_vocabulary"),
        CheckConstraint(
            "source_published_granularity IN ('DATE','MONTH','YEAR','UNDATED')",
            name="ck_derivation_granularity_vocabulary",
        ),
        # No invented publication precision.
        CheckConstraint(
            "(source_published_at IS NOT NULL) = "
            "(source_published_granularity <> 'UNDATED')",
            name="ck_derivation_published_consistency",
        ),
        Index("ix_derivations_artifact", "artifact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    body_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifact_bodies.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    canonicalization_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    derivation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="OK")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_published_granularity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNDATED"
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    derived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchTextDerivation(Base):
    """Readable text, versioned separately from semantics (M3-ADR-017)."""

    __tablename__ = "research_text_derivations"
    __table_args__ = (
        UniqueConstraint(
            "body_id", "text_derivation_contract_hash", name="uq_text_derivation_contract"
        ),
        UniqueConstraint("id", "body_id", name="uq_text_derivation_body"),
        _vocab("text_retention", e._v(e.RetentionState), "ck_text_retention_vocabulary"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    body_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifact_bodies.id", ondelete="RESTRICT"), nullable=False
    )
    text_extraction_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    redaction_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    text_derivation_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_offsets: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    text_retention: Mapped[str] = mapped_column(String(16), nullable=False, default="RETAINED")
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OK")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    derived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchExtraction(Base):
    """An immutable extraction *result*. Owns no attempt (M3-ADR-021)."""

    __tablename__ = "research_extractions"
    __table_args__ = (
        Index(
            "uq_extraction_contract",
            "text_derivation_id", "extraction_contract_hash", "sample_execution_id",
            unique=True, postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("id", "body_id", name="uq_extraction_body"),
        ForeignKeyConstraint(
            ["text_derivation_id", "body_id"],
            ["research_text_derivations.id", "research_text_derivations.body_id"],
            name="fk_extraction_same_body",
        ),
        _vocab("extractor_kind", e._v(e.ExtractorKind), "ck_extractor_kind_vocabulary"),
        _vocab("determinism", e._v(e.Determinism), "ck_extraction_determinism_vocabulary"),
        # A sampled contract may produce different output per run, so it needs
        # its own execution slot; a UNIQUE key keeping the first sample is a
        # frozen race, not idempotency (M3-ADR-038).
        CheckConstraint(
            "determinism = 'DETERMINISTIC' OR sample_execution_id IS NOT NULL",
            name="ck_sampled_requires_execution_slot",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    text_derivation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_text_derivations.id", ondelete="RESTRICT"), nullable=False
    )
    body_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifact_bodies.id", ondelete="RESTRICT"), nullable=False
    )
    extractor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    extractor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    determinism: Mapped[str] = mapped_column(String(16), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    sample_execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extraction_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    observations: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OK")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchAttemptExtraction(Base):
    """Which execution created or reused which result."""

    __tablename__ = "research_attempt_extractions"
    __table_args__ = (
        UniqueConstraint("attempt_id", "extraction_id", name="uq_attempt_extraction"),
        _vocab("usage_role", e._v(e.UsageRole), "ck_usage_role_vocabulary"),
        Index("ix_attempt_extraction_extraction", "extraction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_extractions.id", ondelete="RESTRICT"), nullable=False
    )
    usage_role: Mapped[str] = mapped_column(String(16), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Evidence, owned by nobody
# ---------------------------------------------------------------------------


class ResearchEvidenceItem(Base):
    """One exact observation, independent of whatever consumes it.

    Three composite foreign keys force the extraction, the retrieval and the
    canonicalization derivation to name the same body, declaratively — no
    trigger (M3-ADR-031).
    """

    __tablename__ = "research_evidence_items"
    __table_args__ = (
        UniqueConstraint(
            "extraction_id", "fetch_event_id", "artifact_derivation_id", "locator_hash",
            name="uq_evidence_item_identity",
        ),
        ForeignKeyConstraint(
            ["extraction_id", "body_id"],
            ["research_extractions.id", "research_extractions.body_id"],
            name="fk_evidence_extraction_same_body",
        ),
        ForeignKeyConstraint(
            ["fetch_event_id", "body_id"],
            ["research_fetch_events.id", "research_fetch_events.body_id"],
            name="fk_evidence_fetch_same_body",
        ),
        ForeignKeyConstraint(
            ["artifact_derivation_id", "body_id"],
            ["research_artifact_derivations.id", "research_artifact_derivations.body_id"],
            name="fk_evidence_derivation_same_body",
        ),
        Index("ix_evidence_lineage", "source_id", "artifact_derivation_id"),
        Index("ix_evidence_fetch", "fetch_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_extractions.id", ondelete="RESTRICT"), nullable=False
    )
    fetch_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_fetch_events.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_derivation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifact_derivations.id", ondelete="RESTRICT"), nullable=False
    )
    body_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_artifact_bodies.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=False
    )
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    locator_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Frozen so independence stays reproducible after the mapping changes.
    publisher_key: Mapped[str] = mapped_column(String(255), nullable=False)
    publisher_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClaimEvidenceLink(Base):
    """Which evidence supports which assertion, with that assertion's trust."""

    __tablename__ = "claim_evidence_links"
    __table_args__ = (
        UniqueConstraint("claim_id", "evidence_item_id", name="uq_claim_evidence"),
        _vocab("support_kind", e._v(e.SupportKind), "ck_support_kind_vocabulary"),
        _vocab("source_class", e._v(e.SourceClass), "ck_source_class_vocabulary"),
        CheckConstraint(
            "trust_tier >= 0 AND trust_tier <= 1", name="ck_trust_tier_range"
        ),
        Index("ix_claim_evidence_item", "evidence_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company_claims.id", ondelete="RESTRICT"), nullable=False
    )
    evidence_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_evidence_items.id", ondelete="RESTRICT"), nullable=False
    )
    support_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The trust inputs in force when *this* assertion was made. Frozen, so a
    #: recalibration cannot silently re-explain a number already written.
    source_class: Mapped[str] = mapped_column(String(32), nullable=False)
    trust_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    trust_tier: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Gaps — plan-specific
# ---------------------------------------------------------------------------


class OperationalResearchGap(Base):
    """Identity only. Whether an attribute is required comes from the plan."""

    __tablename__ = "operational_research_gaps"
    __table_args__ = (
        UniqueConstraint("run_id", "attribute_key", "gap_kind", name="uq_gap_identity"),
        _vocab("gap_kind", e._v(e.GapKind), "ck_gap_kind_vocabulary"),
        Index("ix_gaps_company_kind", "company_id", "gap_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_runs.id", ondelete="RESTRICT"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    attribute_key: Mapped[str] = mapped_column(String(128), nullable=False)
    gap_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    first_raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperationalResearchGapEvent(Base):
    """ATTEMPTED names the retrieval that performed it (M3-ADR-024)."""

    __tablename__ = "operational_research_gap_events"
    __table_args__ = (
        Index(
            "uq_gap_event_identity",
            "gap_id", "event_kind", "attempt_id", "source_id", "fetch_event_id",
            unique=True, postgresql_nulls_not_distinct=True,
        ),
        _vocab("event_kind", e._v(e.GapEventKind), "ck_gap_event_vocabulary"),
        Index("ix_gap_events_gap_time", "gap_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    gap_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_gaps.id", ondelete="RESTRICT"), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_sources.id", ondelete="RESTRICT"), nullable=True
    )
    fetch_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_fetch_events.id", ondelete="RESTRICT"), nullable=True
    )
    resolved_by_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("company_claims.id", ondelete="RESTRICT"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Identity review — an M3-owned queue, not an M2 integration
# ---------------------------------------------------------------------------


class IdentityReviewSignal(Base):
    """One durable semantic concern (M3-ADR-034)."""

    __tablename__ = "identity_review_signals"
    __table_args__ = (
        UniqueConstraint("signal_fingerprint", name="uq_signal_fingerprint"),
        _vocab("signal_kind", e._v(e.SignalKind), "ck_signal_kind_vocabulary"),
        Index("ix_signals_company_kind", "company_id", "signal_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    related_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True
    )
    signal_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_concern: Mapped[str] = mapped_column(Text, nullable=False)
    signal_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityReviewSignalOccurrence(Base):
    """One review episode. The concern is durable; the episode is not."""

    __tablename__ = "identity_review_signal_occurrences"
    __table_args__ = (
        UniqueConstraint("signal_id", "occurrence_number", name="uq_occurrence_number"),
        Index(
            "uq_occurrence_open", "signal_id", unique=True,
            postgresql_where="is_open",
        ),
        Index("ix_occurrence_attempt", "raised_by_attempt_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_review_signals.id", ondelete="RESTRICT"), nullable=False
    )
    occurrence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raised_by_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    supersedes_occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("identity_review_signal_occurrences.id", ondelete="RESTRICT"), nullable=True
    )
    #: Maintained by the status trigger so the partial unique index can keep at
    #: most one open episode per concern without reading the event log.
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityReviewSignalEvidence(Base):
    """Points at evidence items, so a signal needs no claim to exist."""

    __tablename__ = "identity_review_signal_evidence"
    __table_args__ = (
        UniqueConstraint(
            "signal_occurrence_id", "evidence_item_id", name="uq_signal_evidence"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    signal_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_review_signal_occurrences.id", ondelete="RESTRICT"), nullable=False
    )
    evidence_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_evidence_items.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentityReviewSignalEvent(Base):
    __tablename__ = "identity_review_signal_events"
    __table_args__ = (
        UniqueConstraint("occurrence_id", "status", "occurred_at", name="uq_signal_event"),
        _vocab("status", e._v(e.SignalStatus), "ck_signal_status_vocabulary"),
        Index("ix_signal_events_occurrence", "occurrence_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity_review_signal_occurrences.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


class OperationalResearchProfile(Base):
    """Company-global operational knowledge. No plan-specific coverage."""

    __tablename__ = "operational_research_profiles"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True
    )
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contradictions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    corroborating_publisher_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    assertion_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    publisher_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)


class OperationalResearchPlanProfile(Base):
    """Coverage, per research question (M3-ADR-029)."""

    __tablename__ = "operational_research_plan_profiles"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operational_research_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    coverage: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    confidence_summary: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    contradiction_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    required_attribute_count: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_attribute_count: Mapped[int] = mapped_column(Integer, nullable=False)
    not_applicable_attribute_count: Mapped[int] = mapped_column(Integer, nullable=False)
    open_gap_count: Mapped[int] = mapped_column(Integer, nullable=False)
    assertion_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    publisher_policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
