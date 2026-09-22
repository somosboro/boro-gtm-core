"""Contextual ranking service — loads evidence, scores, records gaps."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.core.enums import ScoreRunKind, ScoreRunStatus
from boro_gtm.core.errors import NotFoundError, ValidationError
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketDeepDive,
    MarketObservation,
    MarketScore,
    MarketScoreComponent,
    MarketSizeEstimate,
    ScoreRun,
)
from boro_gtm.market_intelligence.research_gaps.detector import GapContext, record_gaps
from boro_gtm.market_intelligence.scoring.contextual_engine import (
    ContextualMarketInputs,
    ContextualMarketResult,
    ContextualRequest,
    evaluate_market,
    rank_results,
)
from boro_gtm.market_intelligence.scoring.definitions import (
    CONTEXTUAL_MODEL_KEY,
    CONTEXTUAL_MODEL_VERSION,
    ENGINE_VERSION,
)
from boro_gtm.market_intelligence.services import scoring_service
from boro_gtm.strategy.domain.models import ICP, Channel, MarketVerticalProfile, Offer, Vertical

logger = logging.getLogger(__name__)


def _lookup(session: Session, model, key: str | None, label: str):
    if key is None:
        return None
    row = session.scalar(select(model).where(model.key == key))
    if row is None:
        raise NotFoundError(f"No {label} with key {key!r}", details={label: key})
    return row


def run_contextual_ranking(
    session: Session,
    snapshot_key: str,
    vertical_key: str | None = None,
    icp_key: str | None = None,
    offer_key: str | None = None,
    channel_key: str | None = None,
    ticket_usd: float | None = None,
    market_iso2: list[str] | None = None,
    allow_low_coverage: bool = False,
    min_coverage: float = 0.5,
    base_run_id: uuid.UUID | None = None,
    model_key: str = CONTEXTUAL_MODEL_KEY,
    model_version: str = CONTEXTUAL_MODEL_VERSION,
    persist: bool = True,
) -> tuple[ScoreRun | None, list[ContextualMarketResult], dict[str, int]]:
    """Score a set of markets in context and persist the run."""
    snapshot = scoring_service.get_snapshot(session, snapshot_key)
    model = scoring_service.get_model(session, model_key, model_version)

    vertical = _lookup(session, Vertical, vertical_key, "vertical")
    icp = _lookup(session, ICP, icp_key, "icp")
    offer = _lookup(session, Offer, offer_key, "offer")
    channel = _lookup(session, Channel, channel_key, "channel")

    # A requested ticket defaults to the offer's midpoint when omitted.
    if ticket_usd is None and offer is not None:
        if offer.ticket_min is not None and offer.ticket_max is not None:
            ticket_usd = (float(offer.ticket_min) + float(offer.ticket_max)) / 2.0

    base_run = (
        session.get(ScoreRun, base_run_id)
        if base_run_id
        else session.scalar(
            select(ScoreRun)
            .where(
                ScoreRun.snapshot_id == snapshot.id,
                ScoreRun.kind == ScoreRunKind.REFERENCE_REPRODUCTION.value,
            )
            .order_by(ScoreRun.completed_at.desc().nullslast())
            .limit(1)
        )
    )
    if base_run is None:
        raise ValidationError(
            "No base score run available to supply the market prior. "
            "Create one via POST /api/v1/score-runs/base first.",
            details={"snapshot_key": snapshot_key},
        )

    stmt = select(Market)
    if market_iso2:
        stmt = stmt.where(Market.iso2.in_([c.upper() for c in market_iso2]))
    markets = session.scalars(stmt.order_by(Market.iso2)).all()
    if not markets:
        raise NotFoundError(
            "No markets matched the request",
            details={"market_iso2": market_iso2 or []},
        )

    request = ContextualRequest(
        vertical_key=vertical_key,
        icp_key=icp_key,
        offer_key=offer_key,
        channel_key=channel_key,
        ticket_usd=ticket_usd,
        allow_low_coverage=allow_low_coverage,
        min_coverage=min_coverage,
    )

    results: list[ContextualMarketResult] = []
    gap_items: list[tuple[GapContext, str]] = []

    for market in markets:
        inputs = _load_inputs(session, market, snapshot.id, base_run.id, vertical)
        result = evaluate_market(inputs, request)
        results.append(result)

        for component in result.components:
            for metric in component.missing_metrics:
                if metric.startswith("context."):
                    # Caller omitted an input; not a research task.
                    continue
                gap_items.append(
                    (
                        GapContext(
                            metric_key=metric,
                            market_id=market.id,
                            vertical_id=vertical.id if vertical else None,
                            icp_id=icp.id if icp else None,
                            offer_id=offer.id if offer else None,
                            channel_id=channel.id if channel else None,
                            snapshot_id=snapshot.id,
                            model=f"{model.key}:{model.version}",
                        ),
                        (
                            f"Component {component.component_key} could not be "
                            f"evaluated for {market.iso2}: {component.explanation}"
                        ),
                    )
                )

    rank_results(results, allow_low_coverage)
    gap_summary = record_gaps(session, gap_items) if gap_items else {
        "created": 0, "already_known": 0, "total": 0
    }

    run: ScoreRun | None = None
    if persist:
        now = datetime.now(UTC)
        run = ScoreRun(
            scoring_model_id=model.id,
            snapshot_id=snapshot.id,
            kind=ScoreRunKind.CONTEXTUAL.value,
            # First-class, queryable context (ADR-014). The JSONB below keeps
            # the full request; these columns make the context joinable.
            vertical_id=vertical.id if vertical else None,
            icp_id=icp.id if icp else None,
            offer_id=offer.id if offer else None,
            channel_id=channel.id if channel else None,
            ticket_usd=ticket_usd,
            context={
                "kind": "contextual",
                "snapshot_key": snapshot.key,
                "vertical": vertical_key,
                "icp": icp_key,
                "offer": offer_key,
                "channel": channel_key,
                "ticket_usd": ticket_usd,
                "markets": [m.iso2 for m in markets],
                "allow_low_coverage": allow_low_coverage,
                "min_coverage": min_coverage,
                "base_score_run_id": str(base_run.id),
            },
            universe_definition={
                "method": "explicit_market_list",
                "member_count": len(markets),
                "members": [m.iso2 for m in markets],
                "base_run_kind": base_run.kind,
            },
            status=ScoreRunStatus.COMPLETED.value,
            started_at=now,
            completed_at=datetime.now(UTC),
            engine_version=ENGINE_VERSION,
        )
        session.add(run)
        session.flush()

        by_iso = {m.iso2: m for m in markets}
        for result in results:
            market = by_iso[result.market_key]
            score_row = MarketScore(
                score_run_id=run.id,
                market_id=market.id,
                rank=result.rank,
                # NULL when not comparable: the engine refuses to publish a
                # rankable number it cannot stand behind.
                score=result.score,
                confidence=result.confidence,
                coverage=result.coverage,
                score_metadata={
                    "comparable": result.comparable,
                    "notes": result.notes,
                    "missing_metrics": result.missing_metrics,
                },
            )
            session.add(score_row)
            session.flush()
            for component in result.components:
                session.add(
                    MarketScoreComponent(
                        market_score_id=score_row.id,
                        component_key=component.component_key,
                        raw_value=None,
                        normalized_value=component.normalized_value,
                        weighted_score=component.weighted_score,
                        confidence=component.confidence,
                        coverage=component.coverage,
                        explanation=component.explanation,
                        component_metadata={
                            "weight": component.weight,
                            "missing_metrics": component.missing_metrics,
                        },
                    )
                )
        session.flush()

    logger.info(
        "Contextual ranking completed",
        extra={
            "score_run_id": str(run.id) if run else None,
            "markets": len(results),
            "gaps_created": gap_summary["created"],
        },
    )
    return run, results, gap_summary


def _load_inputs(
    session: Session,
    market: Market,
    snapshot_id: uuid.UUID,
    base_run_id: uuid.UUID,
    vertical: Vertical | None,
) -> ContextualMarketInputs:
    """Gather every stored piece of evidence for one market."""
    base = session.scalar(
        select(MarketScore).where(
            MarketScore.score_run_id == base_run_id, MarketScore.market_id == market.id
        )
    )
    size = session.scalar(
        select(MarketSizeEstimate).where(
            MarketSizeEstimate.market_id == market.id,
            MarketSizeEstimate.snapshot_id == snapshot_id,
        )
    )
    deep_dive = session.scalar(
        select(MarketDeepDive).where(
            MarketDeepDive.market_id == market.id,
            MarketDeepDive.snapshot_id == snapshot_id,
        )
    )
    profile = None
    if vertical is not None:
        profile = session.scalar(
            select(MarketVerticalProfile).where(
                MarketVerticalProfile.market_id == market.id,
                MarketVerticalProfile.vertical_id == vertical.id,
                MarketVerticalProfile.snapshot_id == snapshot_id,
            )
        )

    observations = session.scalars(
        select(MarketObservation).where(
            MarketObservation.market_id == market.id,
            MarketObservation.snapshot_id == snapshot_id,
        )
    ).all()
    metrics = {
        o.metric_key: (float(o.value_numeric) if o.value_numeric is not None else None)
        for o in observations
    }
    fact_types = {o.metric_key: o.fact_type for o in observations}

    return ContextualMarketInputs(
        market_key=market.iso2,
        name=market.name,
        base_score=float(base.score) if base and base.score is not None else None,
        has_vertical_profile=profile is not None,
        vertical_fit_score=(
            float(profile.fit_score) if profile and profile.fit_score is not None else None
        ),
        vertical_sam_min=(
            float(profile.sam_min) if profile and profile.sam_min is not None else None
        ),
        vertical_sam_max=(
            float(profile.sam_max) if profile and profile.sam_max is not None else None
        ),
        vertical_profile_confidence=(
            float(profile.confidence) if profile and profile.confidence is not None else None
        ),
        vertical_profile_coverage=(
            float(profile.coverage) if profile and profile.coverage is not None else None
        ),
        sam_firms_min=float(size.sam_min) if size and size.sam_min is not None else None,
        sam_firms_max=float(size.sam_max) if size and size.sam_max is not None else None,
        sam_fact_type=size.fact_type if size else None,
        ticket_min_usd=(
            float(size.ticket_min_usd) if size and size.ticket_min_usd is not None else None
        ),
        ticket_max_usd=(
            float(size.ticket_max_usd) if size and size.ticket_max_usd is not None else None
        ),
        metrics=metrics,
        recommended_channel=deep_dive.recommended_channel if deep_dive else None,
        fact_types=fact_types,
    )
