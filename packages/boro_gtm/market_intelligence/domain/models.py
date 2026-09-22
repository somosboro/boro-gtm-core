"""M0 persistence model — snapshots, markets, observations, provenance, scoring.

Design constraints enforced here (see 07_ADRS.md):

* ADR-002 — ``markets`` carries **no** score column. Scores only ever exist as
  rows of :class:`MarketScore` belonging to a :class:`ScoreRun`.
* ADR-003 — ``market_snapshots`` is immutable and content-hashed.
* ADR-004 — nullable numeric columns mean *unknown*, never zero.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from boro_gtm.core.db import Base, TimestampMixin, new_uuid


class MarketSnapshot(Base, TimestampMixin):
    """An immutable imported intelligence artifact.

    The raw payload is retained verbatim: parsed rows elsewhere are
    *projections* of this record, never a replacement for it.
    """

    __tablename__ = "market_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    generated_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    score_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    universe_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    snapshot_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    observations: Mapped[list[MarketObservation]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )
    score_runs: Mapped[list[ScoreRun]] = relationship(back_populates="snapshot")


class Market(Base, TimestampMixin):
    """Canonical market registry. Deliberately free of any score attribute."""

    __tablename__ = "markets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    iso2: Mapped[str | None] = mapped_column(String(2), unique=True, nullable=True)
    iso3: Mapped[str | None] = mapped_column(String(3), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_home_market: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    observations: Mapped[list[MarketObservation]] = relationship(back_populates="market")


class Source(Base, TimestampMixin):
    """Source catalog entry."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_for: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketObservation(Base, TimestampMixin):
    """A single normalized metric value for a market within a snapshot.

    ``value_numeric`` NULL means *unknown*. A row is still written for a known
    unknown so that provenance, fact type and methodology survive the import.
    """

    __tablename__ = "market_observations"
    __table_args__ = (
        UniqueConstraint(
            "market_id",
            "snapshot_id",
            "metric_key",
            "period_label",
            name="uq_market_observations_identity",
        ),
        Index("ix_market_observations_metric", "metric_key"),
        Index("ix_market_observations_snapshot_market", "snapshot_id", "market_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    metric_key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_label: Mapped[str] = mapped_column(String(64), nullable=False, default="snapshot")
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    methodology: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    market: Mapped[Market] = relationship(back_populates="observations")
    snapshot: Mapped[MarketSnapshot] = relationship(back_populates="observations")
    sources: Mapped[list[ObservationSource]] = relationship(
        back_populates="observation", cascade="all, delete-orphan"
    )


class ObservationSource(Base):
    """Observation → source provenance link.

    ``attribution`` records *how* the link was established so that a
    market-level source reference is never mistaken for an exact per-metric
    citation (see 03 — Provenance rules).
    """

    __tablename__ = "observation_sources"

    observation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_observations.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    attribution: Mapped[str] = mapped_column(String(32), nullable=False, default="MARKET_LEVEL")

    observation: Mapped[MarketObservation] = relationship(back_populates="sources")
    source: Mapped[Source] = relationship()


class MarketCategoryRef(Base):
    """Stable lookup of the market-category vocabulary."""

    __tablename__ = "market_categories"

    category_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketSnapshotCategory(Base):
    """Category assignment of a market within a snapshot."""

    __tablename__ = "market_snapshot_categories"

    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), primary_key=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    category_key: Mapped[str] = mapped_column(
        ForeignKey("market_categories.category_key", ondelete="RESTRICT"), primary_key=True
    )


class MarketCompetitionAssessment(Base, TimestampMixin):
    """Competition level. Explicitly *not* folded into the score."""

    __tablename__ = "market_competition_assessments"
    __table_args__ = (
        UniqueConstraint("market_id", "snapshot_id", name="uq_competition_market_snapshot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    included_in_score: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MarketSizeEstimate(Base, TimestampMixin):
    """TAM/SAM/SOM ranges. Only created where the source supplies them."""

    __tablename__ = "market_size_estimates"
    __table_args__ = (
        UniqueConstraint("market_id", "snapshot_id", name="uq_market_size_market_snapshot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_level: Mapped[str | None] = mapped_column(String(32), nullable=True)

    tam_min: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    tam_max: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_min: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_max: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    som_accounts_min: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    som_accounts_max: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    ticket_min_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    ticket_max_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_value_min_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_value_max_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    som_pool_value_min_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    som_pool_value_max_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketDeepDive(Base, TimestampMixin):
    """Structured deep-dive metadata.

    Semicolon-separated source strings are parsed into arrays *and* the
    original raw string is retained in ``raw_values`` (03 — Deep-dive parsing).
    """

    __tablename__ = "market_deep_dives"
    __table_args__ = (
        UniqueConstraint("market_id", "snapshot_id", name="uq_deep_dive_market_snapshot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    priority_verticals: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    priority_geographies: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    recommended_channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    common_buyers: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    common_problem_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class ScoringModel(Base, TimestampMixin):
    """A versioned, configuration-driven scoring definition."""

    __tablename__ = "scoring_models"
    __table_args__ = (UniqueConstraint("key", "version", name="uq_scoring_models_key_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalization_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    components: Mapped[list[ScoringModelComponent]] = relationship(
        back_populates="scoring_model",
        cascade="all, delete-orphan",
        order_by="ScoringModelComponent.ordinal",
    )


class ScoringModelComponent(Base):
    """One weighted component of a scoring model."""

    __tablename__ = "scoring_model_components"
    __table_args__ = (
        UniqueConstraint("scoring_model_id", "component_key", name="uq_model_component_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scoring_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scoring_models.id", ondelete="CASCADE"), nullable=False
    )
    component_key: Mapped[str] = mapped_column(String(128), nullable=False)
    weight: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_metrics: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    scoring_model: Mapped[ScoringModel] = relationship(back_populates="components")


class ScoreRun(Base):
    """An immutable execution of a scoring model against a snapshot + context."""

    __tablename__ = "score_runs"
    __table_args__ = (Index("ix_score_runs_lookup", "snapshot_id", "scoring_model_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    scoring_model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scoring_models.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    universe_definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    snapshot: Mapped[MarketSnapshot] = relationship(back_populates="score_runs")
    scoring_model: Mapped[ScoringModel] = relationship()
    scores: Mapped[list[MarketScore]] = relationship(
        back_populates="score_run", cascade="all, delete-orphan"
    )


class MarketScore(Base):
    """A market's score inside one run. The only place a score may live."""

    __tablename__ = "market_scores"
    __table_args__ = (
        UniqueConstraint("score_run_id", "market_id", name="uq_market_scores_run_market"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
                        name="confidence_range"),
        CheckConstraint("coverage IS NULL OR (coverage >= 0 AND coverage <= 1)",
                        name="coverage_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    score_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("score_runs.id", ondelete="CASCADE"), nullable=False
    )
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: NULL when coverage is too low for the score to be meaningful.
    score: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    coverage: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    score_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    score_run: Mapped[ScoreRun] = relationship(back_populates="scores")
    market: Mapped[Market] = relationship()
    components: Mapped[list[MarketScoreComponent]] = relationship(
        back_populates="market_score", cascade="all, delete-orphan"
    )


class MarketScoreComponent(Base):
    """Per-component breakdown supporting full explainability."""

    __tablename__ = "market_score_components"
    __table_args__ = (
        UniqueConstraint(
            "market_score_id", "component_key", name="uq_score_component_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_score_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_scores.id", ondelete="CASCADE"), nullable=False
    )
    component_key: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_value: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    normalized_value: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)
    weighted_score: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    coverage: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    component_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    market_score: Mapped[MarketScore] = relationship(back_populates="components")
