"""M2 persistence model — discovery, resolution, claims and projections.

Four tiers with different mutability rules (design §3):

* **identity anchor** — ``companies``. Never truncated; UUIDs are minted.
* **evidence** — provider and decision and claim tables. Append-only.
* **derived projections** — rebuildable byte-for-byte from evidence. Natural
  keys only, no surrogate keys, no wall-clock columns.
* **configuration / operational** — the attribute registry and
  ``projection_runs``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
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
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from boro_gtm.core.db import Base, TimestampMixin, new_uuid

# M2 declares foreign keys into the M0/M1 registries (markets, verticals, icps,
# channels). Importing them here guarantees those tables are present in the
# shared metadata no matter which module is imported first — otherwise a
# resolution order that reaches M2 first raises NoReferencedTableError.
from boro_gtm.market_intelligence.domain import models as _m0_models  # noqa: F401
from boro_gtm.strategy.domain import models as _m1_models  # noqa: F401

#: Every nullable JSONB column uses this type: Python ``None`` must become SQL
#: ``NULL``, not the JSON value ``null``. Unknown is an absence, and ``IS NULL``
#: — in queries and in CHECK constraints — must be able to see it.
NULLABLE_JSONB = JSONB(none_as_null=True)

# ---------------------------------------------------------------------------
# Identity anchor
# ---------------------------------------------------------------------------


class Company(Base):
    """The durable canonical identity anchor (design §3.2).

    Holds only what preserves identity and lifecycle. Every business attribute
    is a claim-derived projection on :class:`CompanyProfile` or another derived
    table. There is deliberately no ``created_by_decision_id``: it would create
    a circular foreign key, so the creating decision is derived by query.
    """

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('ACTIVE','MERGED','DISSOLVED')",
            name="lifecycle_vocabulary",
        ),
        CheckConstraint(
            "(lifecycle_status = 'MERGED') = (merged_into_company_id IS NOT NULL)",
            name="merged_requires_survivor",
        ),
        CheckConstraint("id <> merged_into_company_id", name="no_self_merge"),
        Index("ix_companies_lifecycle", "lifecycle_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    identity_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    merged_into_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True
    )


# ---------------------------------------------------------------------------
# Provider layer — append-only evidence
# ---------------------------------------------------------------------------


class DiscoveryProvider(Base, TimestampMixin):
    """Provider registry and capabilities (configuration tier)."""

    __tablename__ = "discovery_providers"
    __table_args__ = (
        CheckConstraint(
            "identity_capability IN "
            "('NATIVE_EXTERNAL_ID','DERIVED_STABLE_KEY','CONTENT_ONLY')",
            name="identity_capability_vocabulary",
        ),
        CheckConstraint(
            "canonicalization_strategy IN ('JSON_CANONICAL_V1','CSV_ROW_V1')",
            name="canonicalization_strategy_vocabulary",
        ),
        CheckConstraint("trust_tier >= 0 AND trust_tier <= 1", name="trust_tier_range"),
        CheckConstraint(
            "identity_capability <> 'DERIVED_STABLE_KEY' OR "
            "(key_fields IS NOT NULL AND key_algorithm_version IS NOT NULL)",
            name="derived_key_requires_declaration",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    identity_capability: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Ordered normalized fields participating in a derived key. NULL unless
    #: identity_capability = DERIVED_STABLE_KEY.
    key_fields: Mapped[list[str] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    key_algorithm_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    canonicalization_strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Weight in candidate resolution. Versioned configuration, not a judgement
    #: baked into code.
    trust_tier: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0.5)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Test/fixture adapters are flagged so they can never be mistaken for a
    #: production discovery source.
    is_fixture: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DiscoveryRun(Base):
    """One execution against one provider in one M1 context (design §7)."""

    __tablename__ = "discovery_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','FETCHING','PARTIAL_FETCH','FETCHED',"
            "'NORMALIZING','RESOLVING','COMPLETED','FAILED')",
            name="status_vocabulary",
        ),
        Index("ix_discovery_runs_status", "status"),
        Index("ix_discovery_runs_context", "market_id", "vertical_id", "icp_id", "channel_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_providers.id", ondelete="RESTRICT"), nullable=False
    )
    # M1 context as real foreign keys (the ADR-014 pattern). M2 references M1
    # and never writes to it.
    market_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("markets.id", ondelete="RESTRICT"), nullable=True
    )
    vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verticals.id", ondelete="RESTRICT"), nullable=True
    )
    icp_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("icps.id", ondelete="RESTRICT"), nullable=True
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    adapter_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Opt-in: permits canonical writes from a run that never reached FETCHED.
    allow_partial_resolution: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set only when the fetch genuinely completed. This, not ``status``, is
    #: what permits canonical writes: later stages overwrite ``status``, and an
    #: incomplete fetch must not be launderable into a complete one.
    fetch_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_units: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    provider: Mapped[DiscoveryProvider] = relationship()


class DiscoveryQuery(Base):
    """The exact query issued. Reproducibility lives here."""

    __tablename__ = "discovery_queries"
    __table_args__ = (
        UniqueConstraint("discovery_run_id", "page_number", name="uq_query_run_page"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderEntity(Base):
    """Provider-side identity (design §4.2)."""

    __tablename__ = "provider_entities"
    __table_args__ = (
        UniqueConstraint(
            "provider_id", "provider_external_id", name="uq_provider_entity_external_id"
        ),
        CheckConstraint(
            "external_id_kind IN ('NATIVE','DERIVED','CONTENT')",
            name="external_id_kind_vocabulary",
        ),
        CheckConstraint(
            "external_id_kind <> 'DERIVED' OR key_algorithm_version IS NOT NULL",
            name="derived_kind_requires_algorithm_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_providers.id", ondelete="RESTRICT"), nullable=False
    )
    provider_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: NATIVE / DERIVED / CONTENT — a derived key is never presented as
    #: provider-issued.
    external_id_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    key_algorithm_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Set when two genuinely different organizations collapse onto one derived
    #: key. Such an entity may never auto-match (design §4.2).
    # NOTE: there is deliberately no ``identity_collision`` column. A
    # collision only becomes visible once a *second* version lands under the
    # entity, and this table is append-only — the flag could never be set.
    # It is a derived predicate: ``resolution.has_identity_collision``.
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderRecordVersion(Base):
    """An immutable *semantic* observation of a provider entity.

    Identity includes the canonicalization contract: a canonical hash is only
    meaningful relative to the algorithm that produced it (design §4.1).
    """

    __tablename__ = "provider_record_versions"
    __table_args__ = (
        UniqueConstraint(
            "provider_entity_id",
            "canonicalization_strategy",
            "canonicalization_version",
            "canonical_payload_hash",
            name="uq_version_identity",
        ),
        CheckConstraint(
            "canonicalization_strategy IN ('JSON_CANONICAL_V1','CSV_ROW_V1')",
            name="canonicalization_strategy_vocabulary",
        ),
        Index("ix_versions_entity_retrieved", "provider_entity_id", "retrieved_at"),
        Index("ix_versions_query", "discovery_query_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_entities.id", ondelete="RESTRICT"), nullable=False
    )
    canonical_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parsed_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovery_query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_queries.id", ondelete="RESTRICT"), nullable=True
    )


class ProviderRecordBody(Base):
    """A distinct byte representation of one semantic version (design §4.3).

    1:N under a version, because a provider reformatting its payload produces
    new bytes but the same semantics. Prunable by retention policy without
    touching the version.
    """

    __tablename__ = "provider_record_bodies"
    __table_args__ = (
        UniqueConstraint(
            "provider_record_version_id", "raw_body_sha256", name="uq_body_version_digest"
        ),
        Index("ix_bodies_version_retrieved", "provider_record_version_id", "retrieved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_record_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="CASCADE"), nullable=False
    )
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovery_query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_queries.id", ondelete="RESTRICT"), nullable=True
    )


class ProviderRecordNormalization(Base):
    """Normalizer output, keyed by version and normalizer version.

    Separate from the version so the raw table stays strictly immutable and a
    normalizer upgrade re-derives without destroying prior output.
    """

    __tablename__ = "provider_record_normalizations"
    __table_args__ = (
        UniqueConstraint(
            "provider_record_version_id", "normalizer_version", name="uq_normalization_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_record_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="CASCADE"), nullable=False
    )
    normalizer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_payload: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    #: Set when the payload could not be normalized. The record is retained.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderRecordSighting(Base):
    """"The provider re-confirmed this version, unchanged, on date Y"."""

    __tablename__ = "provider_record_sightings"
    __table_args__ = (
        UniqueConstraint(
            "provider_record_version_id", "discovery_query_id", name="uq_sighting_version_query"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_record_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="CASCADE"), nullable=False
    )
    discovery_query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_queries.id", ondelete="CASCADE"), nullable=False
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Resolution layer — append-only evidence
# ---------------------------------------------------------------------------


class EntityResolutionCandidate(Base):
    """A considered pairing, retained even when rejected."""

    __tablename__ = "entity_resolution_candidates"
    __table_args__ = (
        Index("ix_candidates_version", "provider_record_version_id"),
        Index("ix_candidates_company", "candidate_company_id"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_record_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="CASCADE"), nullable=False
    )
    candidate_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    match_signals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    score: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntityResolutionDecision(Base):
    """Append-only resolution outcome.

    Four constraints together give the full invariant (design §6.3):

    * one root per provider entity — ``uq_resolution_root``
    * one child per decision — ``uq_resolution_supersedes``
    * supersession confined to one entity — ``fk_supersedes_same_entity``
    * no self-reference — ``no_self_supersede``

    Therefore the decision graph per entity is a linear chain, and exactly one
    head exists by construction.
    """

    __tablename__ = "entity_resolution_decisions"
    __table_args__ = (
        # Target for the composite self-FK. Redundant against the PK, but
        # PostgreSQL requires a unique constraint covering referenced columns.
        UniqueConstraint("id", "provider_entity_id", name="uq_decision_entity"),
        ForeignKeyConstraint(
            ["supersedes_decision_id", "provider_entity_id"],
            ["entity_resolution_decisions.id", "entity_resolution_decisions.provider_entity_id"],
            name="fk_supersedes_same_entity",
        ),
        CheckConstraint("id <> supersedes_decision_id", name="no_self_supersede"),
        CheckConstraint(
            "decision IN ('MATCHED','CREATED_NEW','AMBIGUOUS','REJECTED','MERGED','SPLIT')",
            name="decision_vocabulary",
        ),
        CheckConstraint(
            "method IN ('DETERMINISTIC','CANDIDATE_AUTO','HUMAN_REVIEW')",
            name="method_vocabulary",
        ),
        # AMBIGUOUS and REJECTED resolve to no company; the others must.
        CheckConstraint(
            "(decision IN ('AMBIGUOUS','REJECTED') AND company_id IS NULL) OR "
            "(decision NOT IN ('AMBIGUOUS','REJECTED') AND company_id IS NOT NULL)",
            name="company_required_unless_unresolved",
        ),
        CheckConstraint(
            "(decision IN ('MERGED','SPLIT')) = (merged_company_id IS NOT NULL)",
            name="merged_company_only_for_merge_split",
        ),
        # At most one ROOT per provider entity. Without this, two concurrent
        # workers could each start a chain: NULLs do not collide in a plain
        # unique index.
        Index(
            "uq_resolution_root",
            "provider_entity_id",
            unique=True,
            postgresql_where=sa_text("supersedes_decision_id IS NULL"),
        ),
        # At most one CHILD per decision — no forked chain.
        Index(
            "uq_resolution_supersedes",
            "supersedes_decision_id",
            unique=True,
            postgresql_where=sa_text("supersedes_decision_id IS NOT NULL"),
        ),
        Index("ix_decisions_company", "company_id"),
        Index("ix_decisions_entity_decided", "provider_entity_id", "decided_at"),
        Index(
            "ix_decisions_ambiguous",
            "decided_at",
            postgresql_where=sa_text("decision = 'AMBIGUOUS'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    provider_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_entities.id", ondelete="RESTRICT"), nullable=False
    )
    provider_record_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="RESTRICT"), nullable=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True
    )
    #: The retiring identity, for MERGED / SPLIT.
    merged_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Points **backwards** to the decision this replaces. The old row is never
    #: touched.
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    identity_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    signals: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Claim layer — append-only evidence
# ---------------------------------------------------------------------------


class AttributeDefinition(Base):
    """The versioned attribute contract (design §5.2, configuration tier)."""

    __tablename__ = "attribute_definitions"
    __table_args__ = (
        UniqueConstraint("registry_version", "attribute_key", name="uq_attribute_definition"),
        CheckConstraint("value_kind IN ('SCALAR','RANGE','SET')", name="value_kind_vocabulary"),
        CheckConstraint(
            "value_type IN ('TEXT','INTEGER','NUMERIC','DATE','BOOLEAN',"
            "'REFERENCE','DOMAIN','STRUCTURED')",
            name="value_type_vocabulary",
        ),
        CheckConstraint("cardinality IN ('ONE','MANY')", name="cardinality_vocabulary"),
        CheckConstraint(
            "projection_strategy IN ('HIGHEST_PRECEDENCE','ENVELOPE','UNION','LATEST')",
            name="projection_strategy_vocabulary",
        ),
        CheckConstraint(
            "conflict_strategy IN ('PRECEDENCE','ENVELOPE','FLAG_AMBIGUOUS')",
            name="conflict_strategy_vocabulary",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    attribute_key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Which milestone owns this attribute. Added by M3 (design §5) so the
    #: M3 taxonomy is separable from M2's and the evidence trigger can tell
    #: them apart. Backfilled to 'M2' for the existing registry.
    owner_milestone: Mapped[str | None] = mapped_column(String(8), nullable=True)
    allowed_units: Mapped[list[str] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    allowed_fact_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(8), nullable=False)
    projection_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    conflict_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    shadow_column: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value_schema: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    target_projection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompanyClaim(Base):
    """One typed assertion against the attribute registry (design §5).

    Provider-sourced claims carry **no** ``company_id``: attribution runs
    through the effective resolution decision, so a corrected resolution never
    rewrites a claim (design §5.5).
    """

    __tablename__ = "company_claims"
    __table_args__ = (
        # Exactly one attribution path.
        CheckConstraint(
            "(provider_record_version_id IS NOT NULL) <> (subject_company_id IS NOT NULL)",
            name="exactly_one_attribution_path",
        ),
        # M0 availability semantics, reused verbatim.
        CheckConstraint(
            "(availability = 'OBSERVED' AND fact_type IS NOT NULL) OR "
            "(availability = 'NOT_AVAILABLE' AND fact_type IS NULL "
            " AND value_jsonb IS NULL AND value_numeric IS NULL "
            " AND value_text IS NULL AND value_ref_id IS NULL)",
            name="availability_consistency",
        ),
        CheckConstraint(
            "availability IN ('OBSERVED','NOT_AVAILABLE')", name="availability_vocabulary"
        ),
        CheckConstraint(
            "fact_type IS NULL OR fact_type IN "
            "('FACT','ESTIMATE','PROXY','INFERENCE','HYPOTHESIS')",
            name="fact_type_vocabulary",
        ),
        CheckConstraint(
            "period_granularity IN ('DATE','YEAR','SNAPSHOT','UNDATED')",
            name="period_granularity_vocabulary",
        ),
        CheckConstraint(
            "(period_granularity = 'DATE' AND observed_at IS NOT NULL) OR "
            "(period_granularity <> 'DATE' AND observed_at IS NULL)",
            name="observed_at_requires_date_granularity",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        # M3's assertion dedupe key (design §4.2). Partial, so M2 claims —
        # which leave it NULL — are wholly unaffected.
        Index(
            "uq_claim_assertion_fingerprint", "assertion_fingerprint", unique=True,
            postgresql_where=sa_text("assertion_fingerprint IS NOT NULL"),
        ),
        Index("ix_claims_version", "provider_record_version_id"),
        Index("ix_claims_subject", "subject_company_id"),
        Index("ix_claims_attribute", "attribute_key", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    attribute_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attribute_registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    value_jsonb: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    availability: Mapped[str] = mapped_column(String(16), nullable=False, default="OBSERVED")
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    #: Provider-sourced attribution path.
    provider_record_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="RESTRICT"), nullable=True
    )
    #: Direct attribution path (human or derived claims).
    subject_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True
    )
    #: Which decision was in force when written. History, never attribution.
    resolution_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity_resolution_decisions.id", ondelete="RESTRICT"), nullable=True
    )
    observed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_granularity: Mapped[str] = mapped_column(String(16), nullable=False, default="UNDATED")
    #: M3 only: the assertion identity that makes a duplicate claim
    #: unrepresentable. NULL on every M2 claim.
    assertion_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompanyRelationshipClaim(Base):
    """A time-bounded relationship assertion (design §12)."""

    __tablename__ = "company_relationship_claims"
    __table_args__ = (
        UniqueConstraint("supersedes_claim_id", name="uq_relationship_supersedes"),
        CheckConstraint("id <> supersedes_claim_id", name="no_self_supersede"),
        CheckConstraint(
            "relationship_type IN ('SUBSIDIARY_OF','FRANCHISE_OF','ACQUIRED_BY','SISTER_OF')",
            name="relationship_type_vocabulary",
        ),
        CheckConstraint("assertion IN ('ASSERTED','RETRACTED')", name="assertion_vocabulary"),
        CheckConstraint("from_company_id <> to_company_id", name="no_self_relationship"),
        # Symmetric types are stored once, in canonical order.
        CheckConstraint(
            "relationship_type <> 'SISTER_OF' OR from_company_id < to_company_id",
            name="symmetric_canonical_ordering",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="valid_interval",
        ),
        Index("ix_relationship_claims_from", "from_company_id", "relationship_type"),
        Index("ix_relationship_claims_to", "to_company_id", "relationship_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    from_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    to_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    assertion: Mapped[str] = mapped_column(String(16), nullable=False, default="ASSERTED")
    supersedes_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("company_relationship_claims.id", ondelete="RESTRICT"), nullable=True
    )
    provider_record_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_record_versions.id", ondelete="RESTRICT"), nullable=True
    )
    resolution_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entity_resolution_decisions.id", ondelete="RESTRICT"), nullable=True
    )
    fact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    observed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_granularity: Mapped[str] = mapped_column(String(16), nullable=False, default="UNDATED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# Derived projections
#
# Every table below is a pure function of evidence + registry version +
# identity policy version. Three rules make the rebuild contract enforceable:
#   * natural keys only — a surrogate UUID would differ on every rebuild
#   * no wall-clock columns — rebuild timing lives in projection_runs
#   * interval columns, never now()-filtered snapshots
# ---------------------------------------------------------------------------


class CompanyProfile(Base):
    """Canonical business attributes, derived from claims (design §3.3)."""

    __tablename__ = "company_profiles"
    __table_args__ = (
        CheckConstraint(
            "employee_count_min IS NULL OR employee_count_max IS NULL OR "
            "employee_count_min <= employee_count_max",
            name="employee_envelope_order",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    canonical_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_form: Mapped[str | None] = mapped_column(Text, nullable=True)
    founded_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employee_count_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employee_count_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    projection_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attribute_registry_version: Mapped[str] = mapped_column(String(32), nullable=False)


class CompanyName(Base):
    __tablename__ = "company_names"
    __table_args__ = (
        CheckConstraint(
            "name_type IN ('LEGAL','TRADING','FORMER','LOCALIZED','PROVIDER_DISPLAY')",
            name="name_type_vocabulary",
        ),
        Index("ix_company_names_trgm", "name_normalized"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    name_normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    name_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    name_raw: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )


class CompanyDomain(Base):
    """At most one company may claim a domain as its *identity* (design §8)."""

    __tablename__ = "company_domains"
    __table_args__ = (
        CheckConstraint(
            "domain_role IN ('IDENTITY','ALTERNATE','REDIRECT','COUNTRY_TLD','DEFUNCT','GROUP')",
            name="domain_role_vocabulary",
        ),
        Index(
            "uq_identity_domain",
            "domain_normalized",
            unique=True,
            postgresql_where=sa_text("domain_role = 'IDENTITY'"),
        ),
        Index("ix_company_domains_lookup", "domain_normalized"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    domain_normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    domain_role: Mapped[str] = mapped_column(String(16), nullable=False)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )


class CompanyLocation(Base):
    """Physical or registered places only. ``SERVICE_AREA`` is not a place."""

    __tablename__ = "company_locations"
    __table_args__ = (
        CheckConstraint(
            "location_type IN ('HEADQUARTERS','BRANCH','DEPOT','REGISTERED_OFFICE')",
            name="location_type_vocabulary",
        ),
        Index("ix_company_locations_market", "market_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    location_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    address_normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    market_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("markets.id", ondelete="RESTRICT"), nullable=True
    )
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )


class CompanyMarketPresence(Base):
    """Operating geography with **validity intervals** (design §5.7).

    ``effective_from`` is part of the key precisely so a company that leaves a
    market and later re-enters has two rows rather than one overwritten row.
    """

    __tablename__ = "company_market_presences"
    __table_args__ = (
        CheckConstraint(
            "presence_type IN ('HEADQUARTERED','BRANCH','OPERATES','SERVES_REMOTELY')",
            name="presence_type_vocabulary",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from < effective_to",
            name="presence_interval_order",
        ),
        Index("ix_presences_market", "market_id", "presence_type"),
        Index("ix_presences_interval", "effective_from", "effective_to"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="RESTRICT"), primary_key=True
    )
    presence_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: Interval start is in the key. A sentinel date is used rather than NULL
    #: so the primary key stays usable.
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    fact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )


class CompanyVertical(Base):
    __tablename__ = "company_verticals"
    __table_args__ = (
        CheckConstraint(
            "classification_method IN "
            "('PROVIDER_TAXONOMY','NAICS_MAPPING','KEYWORD_INFERENCE','HUMAN_REVIEW')",
            name="classification_method_vocabulary",
        ),
        CheckConstraint(
            "fact_type IS NULL OR fact_type IN "
            "('FACT','ESTIMATE','PROXY','INFERENCE','HYPOTHESIS')",
            name="fact_type_vocabulary",
        ),
        Index("ix_company_verticals_vertical", "vertical_id", "fact_type"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    vertical_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("verticals.id", ondelete="RESTRICT"), primary_key=True
    )
    classification_method: Mapped[str] = mapped_column(String(32), nullable=False)
    fact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )


class CompanyRelationship(Base):
    """Effective relationships with validity intervals (design §12.4)."""

    __tablename__ = "company_relationships"
    __table_args__ = (
        CheckConstraint(
            "relationship_type IN ('SUBSIDIARY_OF','FRANCHISE_OF','ACQUIRED_BY','SISTER_OF')",
            name="relationship_type_vocabulary",
        ),
        CheckConstraint("from_company_id <> to_company_id", name="no_self_relationship"),
        CheckConstraint(
            "relationship_type <> 'SISTER_OF' OR from_company_id < to_company_id",
            name="symmetric_canonical_ordering",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from < effective_to",
            name="relationship_interval_order",
        ),
        Index("ix_relationships_interval", "effective_from", "effective_to"),
    )

    from_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    to_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    relationship_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    fact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    derived_from_claim_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )


class EntityResolutionHead(Base):
    """O(1) cache of ``current_entity_resolutions``. Not the invariant."""

    __tablename__ = "entity_resolution_heads"

    provider_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_entities.id", ondelete="CASCADE"), primary_key=True
    )
    current_decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity_resolution_decisions.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=True
    )


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


class ProjectionRun(Base):
    """Rebuild telemetry. Deliberately outside every determinism claim, which
    is why no projection table carries a timestamp of its own."""

    __tablename__ = "projection_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attribute_registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    row_counts: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    #: SHA-256 over a canonical row serialization, per table. Telemetry only —
    #: the contract is row-set equality (M2-ADR-030).
    content_digests: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSONB, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_company_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )


class DiscoveryJob(Base):
    """PostgreSQL-backed job queue, claimed with FOR UPDATE SKIP LOCKED.

    Deliberately minimal: it is a queue, not a domain concept, and nothing in
    the schema graph depends on it (M2-ADR-010).
    """

    __tablename__ = "discovery_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','DONE','FAILED')", name="status_vocabulary"
        ),
        Index("ix_jobs_claimable", "status", "run_after"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
