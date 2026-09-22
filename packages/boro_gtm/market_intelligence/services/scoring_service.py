"""Persisting score runs from the snapshot projection in the database."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from boro_gtm.core.enums import ScoreRunKind, ScoreRunStatus
from boro_gtm.core.errors import ModelNotFoundError, SnapshotNotFoundError, ValidationError
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketObservation,
    MarketScore,
    MarketScoreComponent,
    MarketSnapshot,
    ScoreRun,
    ScoringModel,
)
from boro_gtm.market_intelligence.scoring.base_engine import (
    MarketInputs,
    ScoreRunResult,
    run_native_recalculation,
    run_reference_reproduction,
)
from boro_gtm.market_intelligence.scoring.definitions import (
    BASE_MODEL_KEY,
    BASE_MODEL_VERSION,
    ENGINE_VERSION,
)

logger = logging.getLogger(__name__)

SUPPORTED_MODES = {
    ScoreRunKind.REFERENCE_REPRODUCTION.value,
    ScoreRunKind.NATIVE_RECALCULATION.value,
}


def get_snapshot(session: Session, snapshot_key: str) -> MarketSnapshot:
    snapshot = session.scalar(select(MarketSnapshot).where(MarketSnapshot.key == snapshot_key))
    if snapshot is None:
        raise SnapshotNotFoundError(
            f"No snapshot with key {snapshot_key!r}", details={"snapshot_key": snapshot_key}
        )
    return snapshot


def get_model(session: Session, key: str, version: str) -> ScoringModel:
    model = session.scalar(
        select(ScoringModel)
        .options(selectinload(ScoringModel.components))
        .where(ScoringModel.key == key, ScoringModel.version == version)
    )
    if model is None:
        raise ModelNotFoundError(
            f"No scoring model {key}:{version}",
            details={"model_key": key, "model_version": version},
        )
    return model


def build_market_inputs(session: Session, snapshot: MarketSnapshot) -> list[MarketInputs]:
    """Project the stored snapshot back into engine inputs.

    Reads observations rather than the raw payload, which is what makes the
    recalculation a genuine round-trip through the database.
    """
    reference_run = session.scalar(
        select(ScoreRun).where(
            ScoreRun.snapshot_id == snapshot.id,
            ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value,
        )
    )
    imported: dict[str, dict[str, float]] = {}
    imported_totals: dict[str, float] = {}
    imported_ranks: dict[str, int | None] = {}
    if reference_run is not None:
        scores = session.scalars(
            select(MarketScore)
            .options(selectinload(MarketScore.components), selectinload(MarketScore.market))
            .where(MarketScore.score_run_id == reference_run.id)
        ).all()
        for score in scores:
            iso = score.market.iso2
            imported[iso] = {
                c.component_key: float(c.weighted_score)
                for c in score.components
                if c.weighted_score is not None
            }
            if score.score is not None:
                imported_totals[iso] = float(score.score)
            imported_ranks[iso] = score.rank

    observations = session.scalars(
        select(MarketObservation)
        .options(selectinload(MarketObservation.market))
        .where(MarketObservation.snapshot_id == snapshot.id)
    ).all()

    metrics: dict[str, dict[str, float | None]] = {}
    fact_types: dict[str, dict[str, str]] = {}
    confidence_labels: dict[str, str | None] = {}
    for obs in observations:
        iso = obs.market.iso2
        metrics.setdefault(iso, {})[obs.metric_key] = (
            float(obs.value_numeric) if obs.value_numeric is not None else None
        )
        fact_types.setdefault(iso, {})[obs.metric_key] = obs.fact_type
        confidence_labels.setdefault(iso, obs.confidence)

    markets = session.scalars(select(Market)).all()
    inputs: list[MarketInputs] = []
    for market in markets:
        iso = market.iso2
        inputs.append(
            MarketInputs(
                market_key=iso,
                name=market.name,
                metrics=metrics.get(iso, {}),
                imported_components=imported.get(iso, {}),
                imported_total=imported_totals.get(iso),
                imported_rank=imported_ranks.get(iso),
                fact_types=fact_types.get(iso, {}),
                confidence_label=confidence_labels.get(iso),
                is_home_market=market.is_home_market,
            )
        )
    inputs.sort(key=lambda i: i.market_key)
    return inputs


def create_base_score_run(
    session: Session,
    snapshot_key: str,
    mode: str,
    model_key: str = BASE_MODEL_KEY,
    model_version: str = BASE_MODEL_VERSION,
) -> ScoreRun:
    """Execute and persist a base score run."""
    if mode not in SUPPORTED_MODES:
        raise ValidationError(
            f"Unsupported score mode {mode!r}", details={"supported": sorted(SUPPORTED_MODES)}
        )

    snapshot = get_snapshot(session, snapshot_key)
    model = get_model(session, model_key, model_version)
    inputs = build_market_inputs(session, snapshot)

    component_specs = model.definition.get("components")
    started = datetime.now(UTC)

    if mode == ScoreRunKind.REFERENCE_REPRODUCTION.value:
        result = run_reference_reproduction(
            [i for i in inputs if i.imported_components], component_specs
        )
    else:
        # The universe is every market holding at least one raw observation.
        universe = [i for i in inputs if any(v is not None for v in i.metrics.values())]
        result = run_native_recalculation(universe, component_specs)

    run = ScoreRun(
        scoring_model_id=model.id,
        snapshot_id=snapshot.id,
        kind=mode,
        context={"kind": "base_market", "snapshot_key": snapshot.key, "mode": mode},
        universe_definition=result.universe_definition,
        status=ScoreRunStatus.COMPLETED.value,
        started_at=started,
        completed_at=datetime.now(UTC),
        engine_version=ENGINE_VERSION,
    )
    session.add(run)
    session.flush()

    market_by_iso = {m.iso2: m for m in session.scalars(select(Market)).all()}
    _persist_results(session, run, result, market_by_iso)
    session.flush()

    logger.info(
        "Base score run completed",
        extra={"score_run_id": str(run.id), "mode": mode, "markets": len(result.results)},
    )
    return run


def _persist_results(
    session: Session,
    run: ScoreRun,
    result: ScoreRunResult,
    market_by_iso: dict[str, Market],
) -> None:
    for market_result in result.results:
        market = market_by_iso.get(market_result.market_key)
        if market is None:  # pragma: no cover - registry is authoritative
            continue
        score = MarketScore(
            score_run_id=run.id,
            market_id=market.id,
            rank=market_result.rank,
            score=market_result.score,
            confidence=market_result.confidence,
            coverage=market_result.coverage,
            score_metadata=market_result.metadata,
        )
        session.add(score)
        session.flush()
        for component in market_result.components:
            session.add(
                MarketScoreComponent(
                    market_score_id=score.id,
                    component_key=component.component_key,
                    raw_value=component.raw_value,
                    normalized_value=component.normalized_value,
                    weighted_score=component.weighted_score,
                    confidence=component.confidence,
                    coverage=component.coverage,
                    explanation=component.explanation,
                    component_metadata=component.metadata,
                )
            )


def compare_runs(
    session: Session, reference_run_id, candidate_run_id
) -> dict[str, float | int | list]:
    """Compare two runs market-by-market; used by acceptance checks."""
    def load(run_id):
        rows = session.scalars(
            select(MarketScore)
            .options(selectinload(MarketScore.market))
            .where(MarketScore.score_run_id == run_id)
        ).all()
        return {r.market.iso2: r for r in rows}

    ref, cand = load(reference_run_id), load(candidate_run_id)
    deltas: list[dict] = []
    max_delta = 0.0
    rank_mismatches: list[dict] = []

    for iso, ref_score in ref.items():
        other = cand.get(iso)
        if other is None or ref_score.score is None or other.score is None:
            continue
        delta = abs(float(ref_score.score) - float(other.score))
        max_delta = max(max_delta, delta)
        deltas.append({"iso2": iso, "delta": delta})
        if ref_score.rank != other.rank:
            rank_mismatches.append(
                {"iso2": iso, "reference_rank": ref_score.rank, "candidate_rank": other.rank}
            )

    return {
        "compared_markets": len(deltas),
        "max_score_delta": max_delta,
        "rank_mismatches": rank_mismatches,
        "deltas": sorted(deltas, key=lambda d: -d["delta"])[:10],
    }
