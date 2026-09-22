"""M1 API — strategy registries, profiles, contextual rankings, research gaps."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.core.config import get_settings
from boro_gtm.core.db import get_db
from boro_gtm.core.enums import ResearchGapStatus
from boro_gtm.core.errors import InsufficientCoverageError, NotFoundError, ValidationError
from boro_gtm.market_intelligence.domain.models import Market, MarketSnapshot
from boro_gtm.market_intelligence.research_gaps import detector
from boro_gtm.market_intelligence.services import contextual_service, scoring_service
from boro_gtm.strategy.api import schemas as s
from boro_gtm.strategy.domain.models import (
    ICP,
    Channel,
    MarketVerticalProfile,
    Offer,
    ResearchGap,
    Vertical,
)

router = APIRouter()


def _market_by_iso(session: Session, iso2: str) -> Market:
    from boro_gtm.core.errors import MarketNotFoundError

    market = session.scalar(select(Market).where(Market.iso2 == iso2.upper()))
    if market is None:
        raise MarketNotFoundError(
            f"No market with ISO code {iso2.upper()!r}", details={"iso2": iso2.upper()}
        )
    return market


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


@router.get("/verticals", response_model=list[s.VerticalOut], tags=["strategy"])
def list_verticals(db: Session = Depends(get_db)) -> Any:
    return db.scalars(select(Vertical).order_by(Vertical.key)).all()


@router.get("/icps", response_model=list[s.ICPOut], tags=["strategy"])
def list_icps(db: Session = Depends(get_db)) -> Any:
    return db.scalars(select(ICP).order_by(ICP.key)).all()


@router.get("/offers", response_model=list[s.OfferOut], tags=["strategy"])
def list_offers(db: Session = Depends(get_db)) -> Any:
    return db.scalars(select(Offer).order_by(Offer.key)).all()


@router.get("/channels", response_model=list[s.ChannelOut], tags=["strategy"])
def list_channels(db: Session = Depends(get_db)) -> Any:
    return db.scalars(select(Channel).order_by(Channel.key)).all()


# ---------------------------------------------------------------------------
# Market x vertical profiles
# ---------------------------------------------------------------------------


def _profile_payload(
    db: Session, profile: MarketVerticalProfile
) -> s.MarketVerticalProfileOut:
    vertical = db.get(Vertical, profile.vertical_id)
    snapshot = db.get(MarketSnapshot, profile.snapshot_id)
    payload = s.MarketVerticalProfileOut(
        id=profile.id,
        vertical_key=vertical.key if vertical else "",
        vertical_name=vertical.name if vertical else "",
        snapshot_key=snapshot.key if snapshot else "",
        fit_score=float(profile.fit_score) if profile.fit_score is not None else None,
        confidence=float(profile.confidence) if profile.confidence is not None else None,
        coverage=float(profile.coverage) if profile.coverage is not None else None,
        tam_min=float(profile.tam_min) if profile.tam_min is not None else None,
        tam_max=float(profile.tam_max) if profile.tam_max is not None else None,
        sam_min=float(profile.sam_min) if profile.sam_min is not None else None,
        sam_max=float(profile.sam_max) if profile.sam_max is not None else None,
        ticket_min_usd=(
            float(profile.ticket_min_usd) if profile.ticket_min_usd is not None else None
        ),
        ticket_max_usd=(
            float(profile.ticket_max_usd) if profile.ticket_max_usd is not None else None
        ),
        priority_geographies=profile.priority_geographies,
        recommended_motion=profile.recommended_motion,
        problem_patterns=profile.problem_patterns,
        evidence=profile.evidence,
    )
    gaps = db.scalars(
        select(ResearchGap).where(
            ResearchGap.market_id == profile.market_id,
            ResearchGap.vertical_id == profile.vertical_id,
            ResearchGap.status == ResearchGapStatus.OPEN.value,
        )
    ).all()
    payload.research_gaps = [
        {"metric_key": g.metric_key, "priority": g.priority, "reason": g.reason}
        for g in gaps
    ]
    return payload


@router.get(
    "/markets/{iso2}/verticals",
    response_model=list[s.MarketVerticalProfileOut],
    tags=["strategy"],
)
def list_market_verticals(
    iso2: str, db: Session = Depends(get_db), snapshot: str | None = Query(None)
) -> Any:
    market = _market_by_iso(db, iso2)
    stmt = select(MarketVerticalProfile).where(MarketVerticalProfile.market_id == market.id)
    if snapshot:
        stmt = stmt.where(
            MarketVerticalProfile.snapshot_id
            == scoring_service.get_snapshot(db, snapshot).id
        )
    return [_profile_payload(db, p) for p in db.scalars(stmt).all()]


@router.get(
    "/markets/{iso2}/verticals/{vertical_key}",
    response_model=s.MarketVerticalProfileOut,
    tags=["strategy"],
)
def get_market_vertical(
    iso2: str,
    vertical_key: str,
    db: Session = Depends(get_db),
    snapshot: str | None = Query(None),
) -> Any:
    market = _market_by_iso(db, iso2)
    vertical = db.scalar(select(Vertical).where(Vertical.key == vertical_key))
    if vertical is None:
        raise NotFoundError(
            f"No vertical {vertical_key!r}", details={"vertical_key": vertical_key}
        )
    stmt = select(MarketVerticalProfile).where(
        MarketVerticalProfile.market_id == market.id,
        MarketVerticalProfile.vertical_id == vertical.id,
    )
    if snapshot:
        stmt = stmt.where(
            MarketVerticalProfile.snapshot_id
            == scoring_service.get_snapshot(db, snapshot).id
        )
    profile = db.scalar(stmt)
    if profile is None:
        raise NotFoundError(
            (
                f"No stored profile for {market.iso2} x {vertical_key}. "
                "Absence means the snapshot supplies no evidence for this pair, "
                "not that the fit is poor."
            ),
            details={"iso2": market.iso2, "vertical_key": vertical_key},
        )
    return _profile_payload(db, profile)


# ---------------------------------------------------------------------------
# Contextual rankings
# ---------------------------------------------------------------------------


@router.post(
    "/contextual-rankings",
    response_model=s.ContextualRankingResponse,
    status_code=201,
    tags=["contextual"],
)
def create_contextual_ranking(
    payload: s.ContextualRankingRequest, db: Session = Depends(get_db)
) -> Any:
    settings = get_settings()
    min_coverage = (
        payload.min_coverage
        if payload.min_coverage is not None
        else settings.min_contextual_coverage
    )
    if not 0.0 <= min_coverage <= 1.0:
        raise ValidationError(
            "min_coverage must be between 0 and 1", details={"min_coverage": min_coverage}
        )

    run, results, gap_summary = contextual_service.run_contextual_ranking(
        db,
        snapshot_key=payload.snapshot_key,
        vertical_key=payload.vertical_key,
        icp_key=payload.icp_key,
        offer_key=payload.offer_key,
        channel_key=payload.channel_key,
        ticket_usd=payload.ticket_usd,
        market_iso2=payload.market_iso2,
        allow_low_coverage=payload.allow_low_coverage,
        min_coverage=min_coverage,
        base_run_id=payload.base_score_run_id,
        model_key=payload.model_key,
        model_version=payload.model_version,
    )
    db.commit()

    if not payload.allow_low_coverage and results and not any(r.comparable for r in results):
        raise InsufficientCoverageError(
            "No market in this context reached the minimum coverage threshold, so "
            "no comparable ranking can be produced. Re-send with "
            "allow_low_coverage=true to inspect the partial evidence anyway.",
            details={
                "min_coverage": min_coverage,
                "observed_coverage": {r.market_key: r.coverage for r in results},
            },
        )

    markets = {
        m.iso2: m
        for m in db.scalars(
            select(Market).where(Market.iso2.in_([r.market_key for r in results]))
        ).all()
    }
    open_gaps = {
        (g.market_id, g.metric_key): g
        for g in db.scalars(
            select(ResearchGap).where(ResearchGap.status == ResearchGapStatus.OPEN.value)
        ).all()
    }

    out_results = []
    for result in results:
        market = markets.get(result.market_key)
        gaps = []
        if market is not None:
            for metric in result.missing_metrics:
                gap = open_gaps.get((market.id, metric))
                if gap is not None:
                    gaps.append(
                        {
                            "id": str(gap.id),
                            "metric_key": gap.metric_key,
                            "priority": gap.priority,
                            "status": gap.status,
                        }
                    )
        out_results.append(
            s.ContextualResultOut(
                market={
                    "iso2": result.market_key,
                    "name": result.name,
                    "region": market.region if market else None,
                },
                score=result.score,
                confidence=result.confidence,
                coverage=result.coverage,
                comparable=result.comparable,
                rank=result.rank,
                components=[
                    s.ContextualComponentOut(
                        component_key=c.component_key,
                        weight=c.weight,
                        normalized_value=c.normalized_value,
                        weighted_score=c.weighted_score,
                        coverage=c.coverage,
                        confidence=c.confidence,
                        explanation=c.explanation,
                        missing_metrics=c.missing_metrics,
                    )
                    for c in result.components
                ],
                research_gaps=gaps,
                notes=result.notes,
            )
        )

    return s.ContextualRankingResponse(
        score_run_id=run.id if run else None,
        model=f"{payload.model_key}:{payload.model_version}",
        context={
            "snapshot_key": payload.snapshot_key,
            "vertical_key": payload.vertical_key,
            "icp_key": payload.icp_key,
            "offer_key": payload.offer_key,
            "channel_key": payload.channel_key,
            "ticket_usd": payload.ticket_usd,
            "market_iso2": payload.market_iso2,
        },
        coverage_policy={
            "min_comparable_coverage": min_coverage,
            "allow_low_coverage": payload.allow_low_coverage,
            "missing_data_policy": "renormalize_to_covered_weight",
            "note": (
                "Unknown components are excluded from numerator and denominator. "
                "Missing evidence lowers coverage; it never scores as zero fit."
            ),
        },
        research_gap_summary=gap_summary,
        results=out_results,
    )


# ---------------------------------------------------------------------------
# Research gaps
# ---------------------------------------------------------------------------


@router.get("/research-gaps", response_model=list[s.ResearchGapOut], tags=["research-gaps"])
def list_research_gaps(
    db: Session = Depends(get_db),
    market: str | None = Query(None, description="ISO alpha-2"),
    vertical: str | None = Query(None, description="Vertical key"),
    status: str | None = Query(None),
    priority: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Any:
    stmt = select(ResearchGap)
    if market:
        stmt = stmt.where(ResearchGap.market_id == _market_by_iso(db, market).id)
    if vertical:
        row = db.scalar(select(Vertical).where(Vertical.key == vertical))
        if row is None:
            raise NotFoundError(f"No vertical {vertical!r}")
        stmt = stmt.where(ResearchGap.vertical_id == row.id)
    if status:
        stmt = stmt.where(ResearchGap.status == status.upper())
    if priority:
        stmt = stmt.where(ResearchGap.priority == priority.upper())

    rows = db.scalars(
        stmt.order_by(ResearchGap.priority, ResearchGap.metric_key).limit(limit).offset(offset)
    ).all()

    out = []
    for gap in rows:
        market_row = db.get(Market, gap.market_id) if gap.market_id else None
        vertical_row = db.get(Vertical, gap.vertical_id) if gap.vertical_id else None
        payload = s.ResearchGapOut.model_validate(gap)
        payload.market_iso2 = market_row.iso2 if market_row else None
        payload.vertical_key = vertical_row.key if vertical_row else None
        out.append(payload)
    return out


@router.post(
    "/research-gaps/{gap_id}/status", response_model=s.ResearchGapOut, tags=["research-gaps"]
)
def update_research_gap_status(
    gap_id: uuid.UUID, payload: s.ResearchGapStatusRequest, db: Session = Depends(get_db)
) -> Any:
    """Lifecycle only. This milestone performs no web research."""
    valid = {s.value for s in ResearchGapStatus}
    if payload.status.upper() not in valid:
        raise ValidationError(
            f"Invalid status {payload.status!r}", details={"valid": sorted(valid)}
        )
    gap = detector.set_status(db, gap_id, payload.status.upper())
    if gap is None:
        raise NotFoundError(f"No research gap {gap_id}")
    db.commit()
    market_row = db.get(Market, gap.market_id) if gap.market_id else None
    vertical_row = db.get(Vertical, gap.vertical_id) if gap.vertical_id else None
    out = s.ResearchGapOut.model_validate(gap)
    out.market_iso2 = market_row.iso2 if market_row else None
    out.vertical_key = vertical_row.key if vertical_row else None
    return out
