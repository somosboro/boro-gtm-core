"""M1 persistence model — verticals, ICPs, offers, channels, profiles, gaps.

Per ADR-006 the *shape* of these tables is product-generic; the BoRo rows that
populate them are seed data under ``app/strategy/seeds``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from boro_gtm.core.db import Base, TimestampMixin, new_uuid


class Vertical(Base, TimestampMixin):
    """An industry vertical the engine can evaluate a market against."""

    __tablename__ = "verticals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    taxonomy_codes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class ICP(Base, TimestampMixin):
    """A versioned ideal-customer-profile definition."""

    __tablename__ = "icps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Offer(Base, TimestampMixin):
    """A commercial offer with a ticket range. Prices are seed configuration."""

    __tablename__ = "offers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    ticket_min: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    ticket_max: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Channel(Base, TimestampMixin):
    """An outbound/GTM channel."""

    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class MarketVerticalProfile(Base, TimestampMixin):
    """Stored market x vertical evidence.

    Every numeric column is nullable on purpose: an absent value is evidence we
    do not have, and must surface as reduced coverage rather than a zero.
    """

    __tablename__ = "market_vertical_profiles"
    __table_args__ = (
        UniqueConstraint(
            "market_id", "vertical_id", "snapshot_id", name="uq_mvp_market_vertical_snapshot"
        ),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
                        name="confidence_range"),
        CheckConstraint("coverage IS NULL OR (coverage >= 0 AND coverage <= 1)",
                        name="coverage_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    vertical_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("verticals.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )

    fit_score: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    coverage: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)

    tam_min: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    tam_max: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_min: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    sam_max: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    ticket_min_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    ticket_max_usd: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)

    priority_geographies: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    recommended_motion: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    problem_patterns: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    vertical: Mapped[Vertical] = relationship()


class ResearchGap(Base, TimestampMixin):
    """A deterministic record of missing evidence.

    ``context_fingerprint`` is a stable hash of (market, vertical, icp, offer,
    channel, metric_key, model) used to guarantee no duplicate OPEN gap exists
    for the same context — see 06 acceptance criterion D.
    """

    __tablename__ = "research_gaps"
    __table_args__ = (
        UniqueConstraint("context_fingerprint", name="uq_research_gaps_fingerprint"),
        Index("ix_research_gaps_status", "status", "priority"),
        Index("ix_research_gaps_market", "market_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=True
    )
    vertical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verticals.id", ondelete="CASCADE"), nullable=True
    )
    icp_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("icps.id", ondelete="CASCADE"), nullable=True
    )
    offer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("offers.id", ondelete="CASCADE"), nullable=True
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=True
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=True
    )

    metric_key: Mapped[str] = mapped_column(String(128), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    market: Mapped[Any] = relationship("Market")
    vertical: Mapped[Vertical | None] = relationship()
