"""M0 read API plus the base score-run trigger."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from boro_gtm.core.db import get_db
from boro_gtm.core.enums import FactType, ScoreRunKind, coerce_vocabulary
from boro_gtm.core.errors import MarketNotFoundError, NotFoundError
from boro_gtm.market_intelligence.api import schemas as s
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketCompetitionAssessment,
    MarketDeepDive,
    MarketObservation,
    MarketScore,
    MarketSizeEstimate,
    MarketSnapshot,
    MarketSnapshotCategory,
    ObservationSource,
    ScoreRun,
    ScoringModel,
    Source,
)
from boro_gtm.market_intelligence.services import scoring_service

router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _market_by_iso(session: Session, iso2: str) -> Market:
    market = session.scalar(select(Market).where(Market.iso2 == iso2.upper()))
    if market is None:
        raise MarketNotFoundError(
            f"No market with ISO-3166-1 alpha-2 code {iso2.upper()!r}",
            details={"iso2": iso2.upper()},
        )
    return market


def _market_dict(market: Market) -> dict[str, Any]:
    return {
        "id": str(market.id),
        "iso2": market.iso2,
        "iso3": market.iso3,
        "name": market.name,
        "region": market.region,
        "is_home_market": market.is_home_market,
    }


def _latest_run(session: Session, kind: str | None = None) -> ScoreRun | None:
    stmt = select(ScoreRun).order_by(ScoreRun.completed_at.desc().nullslast())
    if kind:
        stmt = stmt.where(ScoreRun.kind == kind)
    return session.scalars(stmt.limit(1)).first()


def _resolve_run(session: Session, score_run_id: uuid.UUID) -> ScoreRun:
    run = session.get(ScoreRun, score_run_id)
    if run is None:
        raise NotFoundError(
            f"No score run {score_run_id}", details={"score_run_id": str(score_run_id)}
        )
    return run


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@router.get("/market-intelligence/snapshots", response_model=list[s.SnapshotOut],
            tags=["snapshots"])
def list_snapshots(db: Session = Depends(get_db)) -> Any:
    return db.scalars(
        select(MarketSnapshot).order_by(MarketSnapshot.created_at.desc())
    ).all()


@router.get(
    "/market-intelligence/snapshots/{snapshot_key}",
    response_model=s.SnapshotDetail,
    tags=["snapshots"],
)
def get_snapshot(snapshot_key: str, db: Session = Depends(get_db)) -> Any:
    snapshot = scoring_service.get_snapshot(db, snapshot_key)
    counts = {
        "observations": db.scalar(
            select(func.count()).select_from(MarketObservation)
            .where(MarketObservation.snapshot_id == snapshot.id)
        ),
        "market_size_estimates": db.scalar(
            select(func.count()).select_from(MarketSizeEstimate)
            .where(MarketSizeEstimate.snapshot_id == snapshot.id)
        ),
        "deep_dives": db.scalar(
            select(func.count()).select_from(MarketDeepDive)
            .where(MarketDeepDive.snapshot_id == snapshot.id)
        ),
        "competition_assessments": db.scalar(
            select(func.count()).select_from(MarketCompetitionAssessment)
            .where(MarketCompetitionAssessment.snapshot_id == snapshot.id)
        ),
        "markets": db.scalar(select(func.count()).select_from(Market)),
        "sources": db.scalar(select(func.count()).select_from(Source)),
    }
    runs = db.scalars(
        select(ScoreRun).where(ScoreRun.snapshot_id == snapshot.id)
        .order_by(ScoreRun.started_at)
    ).all()
    payload = s.SnapshotDetail.model_validate(snapshot)
    payload.counts = {k: int(v or 0) for k, v in counts.items()}
    payload.score_runs = [s.ScoreRunSummary.model_validate(r) for r in runs]
    payload.metadata = snapshot.snapshot_metadata
    return payload


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------


@router.get("/markets", response_model=s.PaginatedMarkets, tags=["markets"])
def list_markets(
    db: Session = Depends(get_db),
    region: str | None = Query(None),
    category: str | None = Query(None),
    home_market: bool | None = Query(None),
    min_score: float | None = Query(None, description="Requires score_run"),
    score_run: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Any:
    stmt = select(Market)
    if region:
        stmt = stmt.where(Market.region == region)
    if home_market is not None:
        stmt = stmt.where(Market.is_home_market.is_(home_market))
    if category:
        stmt = stmt.where(
            Market.id.in_(
                select(MarketSnapshotCategory.market_id)
                .where(MarketSnapshotCategory.category_key == category.upper())
            )
        )

    run: ScoreRun | None = None
    if score_run is not None:
        run = _resolve_run(db, score_run)
    elif min_score is not None:
        run = _latest_run(db, ScoreRunKind.REFERENCE_REPRODUCTION.value)

    if min_score is not None:
        if run is None:
            raise NotFoundError(
                "min_score requires a score run, and none is available",
                details={"hint": "pass score_run, or create one via POST /score-runs/base"},
            )
        stmt = stmt.where(
            Market.id.in_(
                select(MarketScore.market_id).where(
                    MarketScore.score_run_id == run.id,
                    MarketScore.score >= min_score,
                )
            )
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # Ordering is by name; ranking semantics live on the ranking endpoint so
    # pagination here can never reorder a ranking.
    markets = db.scalars(stmt.order_by(Market.name).limit(limit).offset(offset)).all()

    scores: dict[uuid.UUID, MarketScore] = {}
    if run is not None:
        for row in db.scalars(
            select(MarketScore).where(
                MarketScore.score_run_id == run.id,
                MarketScore.market_id.in_([m.id for m in markets]) if markets else False,
            )
        ).all():
            scores[row.market_id] = row

    results = []
    for market in markets:
        detail = s.MarketDetail.model_validate(market)
        detail.categories = list(
            db.scalars(
                select(MarketSnapshotCategory.category_key)
                .where(MarketSnapshotCategory.market_id == market.id)
            ).all()
        )
        row = scores.get(market.id)
        if row is not None and run is not None:
            detail.latest_score = s.MarketScoreProjection(
                score_run_id=run.id,
                kind=run.kind,
                score=float(row.score) if row.score is not None else None,
                rank=row.rank,
                confidence=float(row.confidence) if row.confidence is not None else None,
                coverage=float(row.coverage) if row.coverage is not None else None,
            )
        results.append(detail)

    return s.PaginatedMarkets(total=total, limit=limit, offset=offset, results=results)


@router.get("/markets/{iso2}", response_model=s.MarketDetail, tags=["markets"])
def get_market(
    iso2: str,
    db: Session = Depends(get_db),
    score_run: uuid.UUID | None = Query(None),
) -> Any:
    market = _market_by_iso(db, iso2)
    detail = s.MarketDetail.model_validate(market)
    detail.categories = list(
        db.scalars(
            select(MarketSnapshotCategory.category_key)
            .where(MarketSnapshotCategory.market_id == market.id)
        ).all()
    )
    competition = db.scalar(
        select(MarketCompetitionAssessment)
        .where(MarketCompetitionAssessment.market_id == market.id)
    )
    if competition is not None:
        detail.competition = {
            "level": competition.level,
            "fact_type": competition.fact_type,
            "included_in_score": competition.included_in_score,
        }

    run = _resolve_run(db, score_run) if score_run else _latest_run(
        db, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    if run is not None:
        row = db.scalar(
            select(MarketScore).where(
                MarketScore.score_run_id == run.id, MarketScore.market_id == market.id
            )
        )
        if row is not None:
            detail.latest_score = s.MarketScoreProjection(
                score_run_id=run.id,
                kind=run.kind,
                score=float(row.score) if row.score is not None else None,
                rank=row.rank,
                confidence=float(row.confidence) if row.confidence is not None else None,
                coverage=float(row.coverage) if row.coverage is not None else None,
            )
        snapshot = db.get(MarketSnapshot, run.snapshot_id)
        detail.snapshot_key = snapshot.key if snapshot else None
    return detail


@router.get(
    "/markets/{iso2}/observations", response_model=list[s.ObservationOut], tags=["markets"]
)
def get_observations(
    iso2: str,
    db: Session = Depends(get_db),
    snapshot: str | None = Query(None, description="Snapshot key"),
    metric_key: str | None = Query(None),
    fact_type: str | None = Query(None),
) -> Any:
    # Query parameters are validated before the path resource is resolved, so
    # a malformed request is refused on its own merits — the same order
    # FastAPI already uses for a malformed UUID.
    wanted_fact = coerce_vocabulary(fact_type, FactType, "fact_type")
    market = _market_by_iso(db, iso2)
    stmt = (
        select(MarketObservation)
        .options(selectinload(MarketObservation.sources).selectinload(ObservationSource.source))
        .where(MarketObservation.market_id == market.id)
    )
    if snapshot:
        snap = scoring_service.get_snapshot(db, snapshot)
        stmt = stmt.where(MarketObservation.snapshot_id == snap.id)
    if metric_key:
        stmt = stmt.where(MarketObservation.metric_key == metric_key)
    if wanted_fact:
        stmt = stmt.where(MarketObservation.fact_type == wanted_fact)

    out = []
    for obs in db.scalars(stmt.order_by(MarketObservation.metric_key)).all():
        payload = s.ObservationOut.model_validate(obs)
        payload.sources = [
            {"source_key": link.source.source_key, "attribution": link.attribution}
            for link in obs.sources
        ]
        out.append(payload)
    return out


@router.get("/markets/{iso2}/sources", tags=["markets"])
def get_market_sources(iso2: str, db: Session = Depends(get_db)) -> Any:
    """Provenance view: which sources back which metrics, and how."""
    market = _market_by_iso(db, iso2)
    observations = db.scalars(
        select(MarketObservation)
        .options(selectinload(MarketObservation.sources).selectinload(ObservationSource.source))
        .where(MarketObservation.market_id == market.id)
    ).all()

    by_source: dict[str, dict[str, Any]] = {}
    for obs in observations:
        for link in obs.sources:
            entry = by_source.setdefault(
                link.source.source_key,
                {
                    "source_key": link.source.source_key,
                    "title": link.source.title,
                    "url": link.source.url,
                    "used_for": link.source.used_for,
                    "metrics": [],
                },
            )
            entry["metrics"].append(
                {
                    "metric_key": obs.metric_key,
                    "period_label": obs.period_label,
                    "attribution": link.attribution,
                }
            )
    return {
        "market": _market_dict(market),
        "sources": sorted(by_source.values(), key=lambda e: e["source_key"]),
        "attribution_legend": {
            "EXPLICIT": "The source file names this source for this exact metric.",
            "METRIC_HINT": "Catalogued as supporting this metric type, and listed "
                           "by this market.",
            "MARKET_LEVEL": "Listed by this market, without per-metric attribution "
                            "in the source.",
        },
    }


@router.get(
    "/markets/{iso2}/market-size", response_model=list[s.MarketSizeOut], tags=["markets"]
)
def get_market_size(
    iso2: str, db: Session = Depends(get_db), snapshot: str | None = Query(None)
) -> Any:
    market = _market_by_iso(db, iso2)
    stmt = select(MarketSizeEstimate).where(MarketSizeEstimate.market_id == market.id)
    if snapshot:
        stmt = stmt.where(
            MarketSizeEstimate.snapshot_id == scoring_service.get_snapshot(db, snapshot).id
        )
    return db.scalars(stmt).all()


@router.get("/markets/{iso2}/deep-dive", response_model=list[s.DeepDiveOut], tags=["markets"])
def get_deep_dive(
    iso2: str, db: Session = Depends(get_db), snapshot: str | None = Query(None)
) -> Any:
    market = _market_by_iso(db, iso2)
    stmt = select(MarketDeepDive).where(MarketDeepDive.market_id == market.id)
    if snapshot:
        stmt = stmt.where(
            MarketDeepDive.snapshot_id == scoring_service.get_snapshot(db, snapshot).id
        )
    return db.scalars(stmt).all()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=list[s.SourceOut], tags=["sources"])
def list_sources(db: Session = Depends(get_db)) -> Any:
    return db.scalars(select(Source).order_by(Source.source_key)).all()


@router.get("/sources/{source_key}", response_model=s.SourceOut, tags=["sources"])
def get_source(source_key: str, db: Session = Depends(get_db)) -> Any:
    source = db.scalar(select(Source).where(Source.source_key == source_key))
    if source is None:
        raise NotFoundError(f"No source {source_key!r}", details={"source_key": source_key})
    return source


# ---------------------------------------------------------------------------
# Scoring models
# ---------------------------------------------------------------------------


@router.get("/scoring-models", response_model=list[s.ScoringModelOut], tags=["scoring"])
def list_scoring_models(db: Session = Depends(get_db)) -> Any:
    return db.scalars(
        select(ScoringModel)
        .options(selectinload(ScoringModel.components))
        .order_by(ScoringModel.key, ScoringModel.version)
    ).all()


@router.get(
    "/scoring-models/{key}/{version}", response_model=s.ScoringModelDetail, tags=["scoring"]
)
def get_scoring_model(key: str, version: str, db: Session = Depends(get_db)) -> Any:
    return scoring_service.get_model(db, key, version)


# ---------------------------------------------------------------------------
# Score runs
# ---------------------------------------------------------------------------


@router.get("/score-runs", response_model=list[s.ScoreRunOut], tags=["scoring"])
def list_score_runs(
    db: Session = Depends(get_db),
    snapshot: str | None = Query(None),
    model: str | None = Query(None, description="key:version"),
    kind: str | None = Query(None),
) -> Any:
    wanted_kind = coerce_vocabulary(kind, ScoreRunKind, "kind")
    stmt = select(ScoreRun).order_by(ScoreRun.started_at.desc().nullslast())
    if snapshot:
        stmt = stmt.where(ScoreRun.snapshot_id == scoring_service.get_snapshot(db, snapshot).id)
    if wanted_kind:
        stmt = stmt.where(ScoreRun.kind == wanted_kind)
    if model:
        model_key, _, model_version = model.partition(":")
        stmt = stmt.where(
            ScoreRun.scoring_model_id
            == scoring_service.get_model(db, model_key, model_version or "1.0").id
        )
    return db.scalars(stmt).all()


@router.get("/score-runs/{score_run_id}", response_model=s.ScoreRunOut, tags=["scoring"])
def get_score_run(score_run_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    return _resolve_run(db, score_run_id)


@router.get(
    "/score-runs/{score_run_id}/ranking", response_model=s.RankingResponse, tags=["scoring"]
)
def get_ranking(
    score_run_id: uuid.UUID,
    db: Session = Depends(get_db),
    include_unranked: bool = Query(
        False, description="Include home-market benchmarks and universe-only markets."
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Any:
    run = _resolve_run(db, score_run_id)
    stmt = (
        select(MarketScore)
        .options(selectinload(MarketScore.market))
        .where(MarketScore.score_run_id == run.id)
    )
    if not include_unranked:
        stmt = stmt.where(MarketScore.rank.is_not(None))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # Rank ascending keeps ranking semantics intact under pagination.
    rows = db.scalars(
        stmt.order_by(MarketScore.rank.asc().nullslast(), MarketScore.score.desc().nullslast())
        .limit(limit)
        .offset(offset)
    ).all()

    snapshot = db.get(MarketSnapshot, run.snapshot_id)
    model = db.get(ScoringModel, run.scoring_model_id)
    return s.RankingResponse(
        score_run_id=run.id,
        kind=run.kind,
        snapshot_key=snapshot.key if snapshot else "",
        model=f"{model.key}:{model.version}" if model else "",
        total=total,
        limit=limit,
        offset=offset,
        results=[
            s.RankingEntry(
                rank=row.rank,
                market=_market_dict(row.market),
                score=float(row.score) if row.score is not None else None,
                confidence=float(row.confidence) if row.confidence is not None else None,
                coverage=float(row.coverage) if row.coverage is not None else None,
            )
            for row in rows
        ],
    )


@router.get(
    "/score-runs/{score_run_id}/markets/{iso2}",
    response_model=s.MarketScoreDetail,
    tags=["scoring"],
)
def get_market_score(
    score_run_id: uuid.UUID, iso2: str, db: Session = Depends(get_db)
) -> Any:
    run = _resolve_run(db, score_run_id)
    market = _market_by_iso(db, iso2)
    row = db.scalar(
        select(MarketScore)
        .options(selectinload(MarketScore.components))
        .where(MarketScore.score_run_id == run.id, MarketScore.market_id == market.id)
    )
    if row is None:
        raise NotFoundError(
            f"Market {market.iso2} has no score in run {run.id}",
            details={"score_run_id": str(run.id), "iso2": market.iso2},
        )
    snapshot = db.get(MarketSnapshot, run.snapshot_id)
    model = db.get(ScoringModel, run.scoring_model_id)

    from boro_gtm.strategy.domain.models import ResearchGap

    gaps = db.scalars(
        select(ResearchGap).where(
            ResearchGap.market_id == market.id, ResearchGap.status == "OPEN"
        )
    ).all()

    return s.MarketScoreDetail(
        score_run_id=run.id,
        kind=run.kind,
        model=f"{model.key}:{model.version}" if model else "",
        snapshot_key=snapshot.key if snapshot else "",
        market=_market_dict(market),
        score=float(row.score) if row.score is not None else None,
        rank=row.rank,
        confidence=float(row.confidence) if row.confidence is not None else None,
        coverage=float(row.coverage) if row.coverage is not None else None,
        metadata=row.score_metadata,
        components=[s.ScoreComponentOut.model_validate(c) for c in row.components],
        research_gaps=[
            {
                "id": str(g.id),
                "metric_key": g.metric_key,
                "priority": g.priority,
                "reason": g.reason,
                "status": g.status,
            }
            for g in gaps
        ],
    )


@router.post("/score-runs/base", response_model=s.ScoreRunOut, status_code=201, tags=["scoring"])
def create_base_run(payload: s.BaseScoreRunRequest, db: Session = Depends(get_db)) -> Any:
    run = scoring_service.create_base_score_run(
        db, payload.snapshot_key, payload.mode, payload.model_key, payload.model_version
    )
    db.commit()
    db.refresh(run)
    return run
