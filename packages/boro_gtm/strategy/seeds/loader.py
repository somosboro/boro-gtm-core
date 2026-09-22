"""Idempotent strategy seeding.

Market x vertical profiles are derived *only* from evidence already present in
a snapshot's deep-dive records. Where the snapshot says nothing about a
market/vertical pair, no profile row is created — an absent profile is honest
missing evidence and shows up as reduced coverage, never as a zero fit.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.core.db import utcnow
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketDeepDive,
    MarketSizeEstimate,
    MarketSnapshot,
    ScoringModel,
    ScoringModelComponent,
)
from boro_gtm.market_intelligence.scoring.definitions import (
    CONTEXTUAL_MODEL_COMPONENTS,
    CONTEXTUAL_MODEL_KEY,
    CONTEXTUAL_MODEL_VERSION,
    contextual_model_definition,
)
from boro_gtm.strategy.domain.models import ICP, Channel, MarketVerticalProfile, Offer, Vertical
from boro_gtm.strategy.seeds.boro_seeds import (
    CHANNELS,
    DEEP_DIVE_VERTICAL_ALIASES,
    ICPS,
    OFFERS,
    VERTICALS,
)

logger = logging.getLogger(__name__)


def seed_all(session: Session) -> dict[str, Any]:
    """Seed registries, the contextual model and derivable profiles."""
    result = {
        "verticals": _seed_verticals(session),
        "icps": _seed_icps(session),
        "offers": _seed_offers(session),
        "channels": _seed_channels(session),
        "contextual_model": _seed_contextual_model(session),
    }
    session.flush()
    result["market_vertical_profiles"] = _derive_profiles(session)
    session.flush()
    return result


def _seed_verticals(session: Session) -> int:
    created = 0
    for spec in VERTICALS:
        row = session.scalar(select(Vertical).where(Vertical.key == spec["key"]))
        if row is None:
            session.add(Vertical(**spec))
            created += 1
        else:
            row.name = spec["name"]
            row.description = spec["description"]
            row.taxonomy_codes = spec["taxonomy_codes"]
    return created


def _seed_icps(session: Session) -> int:
    created = 0
    for spec in ICPS:
        row = session.scalar(select(ICP).where(ICP.key == spec["key"]))
        if row is None:
            session.add(ICP(**spec))
            created += 1
        else:
            row.definition = spec["definition"]
            row.updated_at = utcnow()
    return created


def _seed_offers(session: Session) -> int:
    created = 0
    for spec in OFFERS:
        row = session.scalar(select(Offer).where(Offer.key == spec["key"]))
        if row is None:
            session.add(Offer(**spec))
            created += 1
        else:
            row.ticket_min = spec["ticket_min"]
            row.ticket_max = spec["ticket_max"]
            row.definition = spec["definition"]
    return created


def _seed_channels(session: Session) -> int:
    created = 0
    for spec in CHANNELS:
        row = session.scalar(select(Channel).where(Channel.key == spec["key"]))
        if row is None:
            session.add(Channel(**spec))
            created += 1
        else:
            row.definition = spec["definition"]
    return created


def _seed_contextual_model(session: Session) -> bool:
    model = session.scalar(
        select(ScoringModel).where(
            ScoringModel.key == CONTEXTUAL_MODEL_KEY,
            ScoringModel.version == CONTEXTUAL_MODEL_VERSION,
        )
    )
    if model is not None:
        return False

    model = ScoringModel(
        key=CONTEXTUAL_MODEL_KEY,
        version=CONTEXTUAL_MODEL_VERSION,
        name="Contextual market fit",
        description=(
            "Market x vertical x ICP x offer x channel fit. Composition seed "
            "from 04; weights are configuration and are expected to change as "
            "commercial evidence accumulates."
        ),
        normalization_method="weighted components renormalized over covered weight",
        definition=contextual_model_definition(),
        active=True,
    )
    session.add(model)
    session.flush()
    for spec in CONTEXTUAL_MODEL_COMPONENTS:
        session.add(
            ScoringModelComponent(
                scoring_model_id=model.id,
                component_key=spec["component_key"],
                weight=spec["weight"],
                formula=spec["formula"],
                required_metrics=spec["required_metrics"],
                ordinal=spec["ordinal"],
            )
        )
    return True


def _derive_profiles(session: Session) -> dict[str, int]:
    """Create market x vertical profiles from snapshot deep-dive evidence."""
    verticals = {v.key: v for v in session.scalars(select(Vertical)).all()}
    created = 0
    skipped_unmapped: set[str] = set()
    # Several deep-dive labels can resolve to the same vertical ("industrial
    # service" and "industrial technical service"). Track pairs staged in this
    # transaction, since a pending row is not yet visible to a SELECT.
    seen: set[tuple] = set()

    snapshots = session.scalars(select(MarketSnapshot)).all()
    for snapshot in snapshots:
        deep_dives = session.scalars(
            select(MarketDeepDive).where(MarketDeepDive.snapshot_id == snapshot.id)
        ).all()
        for dd in deep_dives:
            market = session.get(Market, dd.market_id)
            if market is None:  # pragma: no cover
                continue
            size = session.scalar(
                select(MarketSizeEstimate).where(
                    MarketSizeEstimate.market_id == market.id,
                    MarketSizeEstimate.snapshot_id == snapshot.id,
                )
            )
            for raw_label in dd.priority_verticals or []:
                key = _match_vertical(raw_label)
                if key is None:
                    skipped_unmapped.add(raw_label)
                    continue
                vertical = verticals.get(key)
                if vertical is None:
                    continue

                pair = (market.id, vertical.id, snapshot.id)
                if pair in seen:
                    continue
                exists = session.scalar(
                    select(MarketVerticalProfile).where(
                        MarketVerticalProfile.market_id == market.id,
                        MarketVerticalProfile.vertical_id == vertical.id,
                        MarketVerticalProfile.snapshot_id == snapshot.id,
                    )
                )
                if exists is not None:
                    seen.add(pair)
                    continue
                seen.add(pair)

                # The snapshot names this vertical as a market priority. That is
                # qualitative evidence of fit, not a measured firm count, so the
                # profile records presence + provenance and leaves the
                # quantitative fields NULL where the market-level TAM/SAM is not
                # vertical-specific.
                session.add(
                    MarketVerticalProfile(
                        market_id=market.id,
                        vertical_id=vertical.id,
                        snapshot_id=snapshot.id,
                        fit_score=None,
                        confidence=0.50,
                        coverage=0.40,
                        tam_min=None,
                        tam_max=None,
                        sam_min=None,
                        sam_max=None,
                        ticket_min_usd=(
                            float(size.ticket_min_usd)
                            if size and size.ticket_min_usd is not None else None
                        ),
                        ticket_max_usd=(
                            float(size.ticket_max_usd)
                            if size and size.ticket_max_usd is not None else None
                        ),
                        priority_geographies=dd.priority_geographies,
                        recommended_motion={
                            "recommended_channel": dd.recommended_channel,
                            "common_buyers": dd.common_buyers,
                        },
                        problem_patterns=(
                            [dd.common_problem_pattern] if dd.common_problem_pattern else None
                        ),
                        evidence={
                            "derived_from": "snapshot_deep_dive.priority_verticals",
                            "source_label": raw_label,
                            "snapshot_key": snapshot.key,
                            "ticket_range_scope": (
                                "market-level TAM/SAM/SOM, not vertical-specific"
                                if size else None
                            ),
                            "quantitative_fields_absent": [
                                "fit_score", "tam_min", "tam_max", "sam_min", "sam_max",
                            ],
                            "note": (
                                "Presence indicates the research named this vertical a "
                                "market priority. Vertical-level firm counts are not "
                                "supplied by this snapshot."
                            ),
                        },
                    )
                )
                created += 1

    if skipped_unmapped:
        logger.info(
            "Deep-dive vertical labels without a seeded mapping: %s",
            sorted(skipped_unmapped),
        )
    return {"created": created, "unmapped_labels": sorted(skipped_unmapped)}


def _match_vertical(label: str) -> str | None:
    """Deterministic label matching. No fuzzy guessing."""
    normalized = label.strip().lower()
    if normalized in DEEP_DIVE_VERTICAL_ALIASES:
        return DEEP_DIVE_VERTICAL_ALIASES[normalized]
    # Longest alias contained in the label wins, so "commercial HVAC/mechanical"
    # does not accidentally match the shorter "mechanical".
    matches = [
        (alias, key)
        for alias, key in DEEP_DIVE_VERTICAL_ALIASES.items()
        if alias in normalized
    ]
    if not matches:
        return None
    return max(matches, key=lambda pair: len(pair[0]))[1]
