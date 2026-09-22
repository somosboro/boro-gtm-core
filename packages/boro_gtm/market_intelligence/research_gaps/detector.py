"""Research-gap detection.

A gap is a deterministic, de-duplicated statement that a specific piece of
evidence is missing for a specific context. Identity is the SHA-256 of the
context tuple, which gives the guarantees in 06 acceptance criterion D:

* the same missing metric in the same context never produces a second row;
* a ``RESOLVED`` or ``DISMISSED`` gap is not silently reopened — changing the
  snapshot or model changes the fingerprint, so genuinely new context creates a
  genuinely new gap.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.core.db import utcnow
from boro_gtm.core.enums import ResearchGapPriority, ResearchGapStatus
from boro_gtm.strategy.domain.models import ResearchGap

logger = logging.getLogger(__name__)

#: Metrics whose absence materially blocks a decision.
_HIGH_PRIORITY_METRICS = {
    "population",
    "market_size_estimate.sam_min",
    "market_size_estimate.sam_max",
    "market_vertical_profile.fit_score",
    "market_vertical_profile.sam",
}


#: Which context dimensions genuinely affect a metric's identity.
#:
#: A missing market-level SAM is the same research task no matter which offer
#: or channel prompted the question, so scoping the fingerprint to the relevant
#: dimensions keeps one gap per real-world question instead of one per request
#: permutation. Anything not listed here falls back to the full context.
_METRIC_SCOPE: dict[str, frozenset[str]] = {
    "population": frozenset({"market", "snapshot"}),
    "market_size_estimate.sam_min": frozenset({"market", "snapshot"}),
    "market_size_estimate.sam_max": frozenset({"market", "snapshot"}),
    "market_size_estimate.ticket_min_usd": frozenset({"market", "snapshot"}),
    "market_size_estimate.ticket_max_usd": frozenset({"market", "snapshot"}),
    "market_vertical_profile.fit_score": frozenset({"market", "vertical", "snapshot"}),
    "market_vertical_profile.sam": frozenset({"market", "vertical", "snapshot"}),
    "base_market_score": frozenset({"market", "snapshot"}),
}

#: Raw observation metrics are market-level facts.
_MARKET_LEVEL_PREFIXES = (
    "gdp_", "real_gdp_", "innovation_", "manufacturing_", "software_",
    "language_", "timezone_", "b2b_", "technology_spending_",
)

_FULL_SCOPE = frozenset(
    {"market", "vertical", "icp", "offer", "channel", "snapshot", "model"}
)


def _scope_for(metric_key: str) -> frozenset[str]:
    if metric_key in _METRIC_SCOPE:
        return _METRIC_SCOPE[metric_key]
    if metric_key.startswith(_MARKET_LEVEL_PREFIXES):
        return frozenset({"market", "snapshot"})
    return _FULL_SCOPE


@dataclass(frozen=True, slots=True)
class GapContext:
    """The identity of a gap.

    ``fingerprint`` hashes only the dimensions the metric actually depends on
    (see :func:`_scope_for`), which is what guarantees one open gap per real
    research question rather than one per request permutation.
    """

    metric_key: str
    market_id: uuid.UUID | None = None
    vertical_id: uuid.UUID | None = None
    icp_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    channel_id: uuid.UUID | None = None
    snapshot_id: uuid.UUID | None = None
    model: str | None = None

    def fingerprint(self) -> str:
        scope = _scope_for(self.metric_key)
        dimensions = {
            "market": str(self.market_id or ""),
            "vertical": str(self.vertical_id or ""),
            "icp": str(self.icp_id or ""),
            "offer": str(self.offer_id or ""),
            "channel": str(self.channel_id or ""),
            "snapshot": str(self.snapshot_id or ""),
            "model": self.model or "",
        }
        parts = [self.metric_key] + [
            f"{name}={value if name in scope else ''}"
            for name, value in sorted(dimensions.items())
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def priority_for(metric_key: str) -> str:
    if metric_key in _HIGH_PRIORITY_METRICS:
        return ResearchGapPriority.HIGH.value
    if metric_key.startswith("context."):
        # The caller simply did not supply this; that is not a research task.
        return ResearchGapPriority.LOW.value
    return ResearchGapPriority.MEDIUM.value


def record_gap(session: Session, context: GapContext, reason: str) -> tuple[ResearchGap, bool]:
    """Create the gap, or return the existing row untouched.

    Returns:
        ``(gap, created)``. An existing gap in any status is returned as-is:
        a resolved gap is never resurrected by a later run over the same
        context.
    """
    fingerprint = context.fingerprint()
    existing = session.scalar(
        select(ResearchGap).where(ResearchGap.context_fingerprint == fingerprint)
    )
    if existing is not None:
        return existing, False

    # Persist only the dimensions that are part of this gap's identity, so a
    # market-level gap never appears to be scoped to whichever vertical or
    # offer happened to surface it first.
    scope = _scope_for(context.metric_key)
    gap = ResearchGap(
        market_id=context.market_id if "market" in scope else None,
        vertical_id=context.vertical_id if "vertical" in scope else None,
        icp_id=context.icp_id if "icp" in scope else None,
        offer_id=context.offer_id if "offer" in scope else None,
        channel_id=context.channel_id if "channel" in scope else None,
        snapshot_id=context.snapshot_id if "snapshot" in scope else None,
        metric_key=context.metric_key,
        priority=priority_for(context.metric_key),
        reason=reason,
        status=ResearchGapStatus.OPEN.value,
        context_fingerprint=fingerprint,
    )
    session.add(gap)
    session.flush()
    return gap, True


def record_gaps(
    session: Session, items: list[tuple[GapContext, str]]
) -> dict[str, int]:
    """Bulk-record gaps, reporting how many were new."""
    created = 0
    existing = 0
    for context, reason in items:
        _, was_created = record_gap(session, context, reason)
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "already_known": existing, "total": len(items)}


def set_status(session: Session, gap_id: uuid.UUID, status: str) -> ResearchGap | None:
    """Lifecycle management only — this milestone performs no web research."""
    gap = session.get(ResearchGap, gap_id)
    if gap is None:
        return None
    gap.status = status
    gap.updated_at = utcnow()
    session.flush()
    return gap
