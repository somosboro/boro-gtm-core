"""Immutable snapshot importer.

Guarantees:

* **Transactional** — the whole import runs in one transaction. A failure
  leaves no partial rows.
* **Idempotent** — the SHA-256 of the source bytes is the snapshot identity.
  Re-importing identical content returns the existing snapshot untouched.
* **Lossless on unknowns** — ``null`` in the source becomes SQL ``NULL``; an
  observation row is still written so fact type and provenance survive.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.core.enums import (
    FactType,
    MetricKey,
    ScoreRunKind,
    ScoreRunStatus,
)
from boro_gtm.core.errors import ImportConflictError, ValidationError
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketCategoryRef,
    MarketCompetitionAssessment,
    MarketDeepDive,
    MarketObservation,
    MarketScore,
    MarketScoreComponent,
    MarketSizeEstimate,
    MarketSnapshot,
    MarketSnapshotCategory,
    ObservationSource,
    ScoreRun,
    ScoringModel,
    ScoringModelComponent,
    Source,
)
from boro_gtm.market_intelligence.importers.contract import (
    MarketEntry,
    MarketIntelligenceDocument,
    parse_document,
    validate_semantics,
)
from boro_gtm.market_intelligence.importers.countries import resolve_country
from boro_gtm.market_intelligence.importers.mappings import (
    MARKET_CATEGORY_LABELS,
    METRIC_SOURCE_HINTS,
    RAW_METRIC_MAP,
    split_semicolon_list,
)
from boro_gtm.market_intelligence.scoring.definitions import (
    BASE_MODEL_COMPONENTS,
    BASE_MODEL_KEY,
    BASE_MODEL_VERSION,
    ENGINE_VERSION,
    base_model_definition,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ImportSummary:
    """Structured result of an import run (03 — Import result)."""

    snapshot_key: str
    snapshot_id: str
    created: bool
    markets: int = 0
    ranked_markets: int = 0
    home_market_benchmarks: int = 0
    universe_only_markets: int = 0
    sources: int = 0
    observations: int = 0
    categories: int = 0
    competition_assessments: int = 0
    market_size_estimates: int = 0
    deep_dives: int = 0
    warnings: list[str] = field(default_factory=list)
    reference_score_run_id: str | None = None
    max_score_delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_key": self.snapshot_key,
            "snapshot_id": self.snapshot_id,
            "created": self.created,
            "markets": self.markets,
            "ranked_markets": self.ranked_markets,
            "home_market_benchmarks": self.home_market_benchmarks,
            "universe_only_markets": self.universe_only_markets,
            "sources": self.sources,
            "observations": self.observations,
            "categories": self.categories,
            "competition_assessments": self.competition_assessments,
            "market_size_estimates": self.market_size_estimates,
            "deep_dives": self.deep_dives,
            "warnings": self.warnings,
            "reference_score_run_id": self.reference_score_run_id,
            "max_score_delta": self.max_score_delta,
        }


def compute_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_snapshot_key(doc: MarketIntelligenceDocument) -> str:
    """Derive the stable snapshot key, e.g. ``MI-2026-09-21-V1``."""
    generated = doc.metadata.generated_date
    version = doc.metadata.score_version.replace(".", "_")
    return f"MI-{generated}-V{version.split('_')[0]}"


class SnapshotImporter:
    """Imports one source document into one transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._market_cache: dict[str, Market] = {}
        self._source_cache: dict[str, Source] = {}

    # -- entry points ----------------------------------------------------

    def import_file(self, path: Path) -> ImportSummary:
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
        return self.import_payload(payload, sha256=compute_sha256(raw_bytes),
                                   source_filename=path.name)

    def import_payload(
        self,
        payload: dict[str, Any],
        sha256: str | None = None,
        source_filename: str | None = None,
    ) -> ImportSummary:
        digest = sha256 or compute_sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

        doc = parse_document(payload)
        report = validate_semantics(doc)
        if not report.ok:
            raise ValidationError(
                "Source document failed semantic validation",
                details={"errors": report.errors, "warnings": report.warnings},
            )

        snapshot_key = build_snapshot_key(doc)

        existing = self.session.scalar(
            select(MarketSnapshot).where(MarketSnapshot.sha256 == digest)
        )
        if existing is not None:
            logger.info(
                "Snapshot already imported; returning existing record",
                extra={"snapshot_key": existing.key, "sha256": digest},
            )
            return self._summarize_existing(existing, report.warnings)

        # Same key, different bytes -> a genuine conflict the caller must resolve.
        clashing = self.session.scalar(
            select(MarketSnapshot).where(MarketSnapshot.key == snapshot_key)
        )
        if clashing is not None:
            raise ImportConflictError(
                f"Snapshot key {snapshot_key!r} already exists with a different "
                "content hash. Snapshots are immutable: publish a new version "
                "instead of overwriting.",
                details={
                    "snapshot_key": snapshot_key,
                    "existing_sha256": clashing.sha256,
                    "incoming_sha256": digest,
                },
            )

        return self._do_import(doc, payload, digest, snapshot_key, source_filename,
                               report.warnings)

    # -- import steps ----------------------------------------------------

    def _do_import(
        self,
        doc: MarketIntelligenceDocument,
        payload: dict[str, Any],
        digest: str,
        snapshot_key: str,
        source_filename: str | None,
        warnings: list[str],
    ) -> ImportSummary:
        snapshot = MarketSnapshot(
            key=snapshot_key,
            title=doc.metadata.title,
            generated_date=_parse_date(doc.metadata.generated_date),
            score_version=doc.metadata.score_version,
            universe_size=doc.metadata.universe_size,
            source_filename=source_filename,
            sha256=digest,
            raw_payload=payload,
            snapshot_metadata=doc.metadata.model_dump(),
        )
        self.session.add(snapshot)
        self.session.flush()

        summary = ImportSummary(
            snapshot_key=snapshot_key,
            snapshot_id=str(snapshot.id),
            created=True,
            warnings=warnings,
        )

        self._ensure_category_lookup()
        summary.sources = self._import_sources(doc)

        # Ranked markets + home benchmark carry full evidence.
        for entry in doc.markets:
            self._import_market_entry(snapshot, entry, is_home_market=False, summary=summary)
            summary.ranked_markets += 1

        self._import_market_entry(
            snapshot, doc.home_market_benchmark, is_home_market=True, summary=summary
        )
        summary.home_market_benchmarks = 1

        # Universe-only markets: registry presence plus their published score.
        for entry in doc.excluded_from_top50_but_in_normalization_universe:
            self._resolve_market(entry.country, region=None, is_home_market=False)
            summary.universe_only_markets += 1

        summary.markets = len(self._market_cache)

        model = self._ensure_base_scoring_model(doc)
        run_id, max_delta = self._create_imported_reference_run(snapshot, model, doc)
        summary.reference_score_run_id = run_id
        summary.max_score_delta = max_delta

        self.session.flush()
        logger.info(
            "Snapshot imported",
            extra={"snapshot_key": snapshot_key, "markets": summary.markets},
        )
        return summary

    def _ensure_category_lookup(self) -> None:
        existing = {
            c.category_key for c in self.session.scalars(select(MarketCategoryRef)).all()
        }
        for key, label in MARKET_CATEGORY_LABELS.items():
            if key not in existing:
                self.session.add(MarketCategoryRef(category_key=key, name=label))
        self.session.flush()

    def _import_sources(self, doc: MarketIntelligenceDocument) -> int:
        count = 0
        for source_key, entry in doc.source_catalog.items():
            source = self.session.scalar(
                select(Source).where(Source.source_key == source_key)
            )
            if source is None:
                source = Source(
                    source_key=source_key,
                    title=entry.title,
                    url=entry.url,
                    publisher=entry.publisher,
                    used_for=entry.used_for,
                    note=entry.note,
                )
                self.session.add(source)
                count += 1
            self._source_cache[source_key] = source
        self.session.flush()
        return count

    def _resolve_market(
        self, country: str, region: str | None, is_home_market: bool
    ) -> Market:
        identity = resolve_country(country)
        if identity.iso2 in self._market_cache:
            market = self._market_cache[identity.iso2]
            if region and not market.region:
                market.region = region
            return market

        market = self.session.scalar(select(Market).where(Market.iso2 == identity.iso2))
        if market is None:
            market = Market(
                iso2=identity.iso2,
                iso3=identity.iso3,
                name=identity.canonical_name,
                region=region,
                is_home_market=is_home_market,
            )
            self.session.add(market)
            self.session.flush()
        else:
            if region and not market.region:
                market.region = region
            if is_home_market:
                market.is_home_market = True

        self._market_cache[identity.iso2] = market
        return market

    def _import_market_entry(
        self,
        snapshot: MarketSnapshot,
        entry: MarketEntry,
        is_home_market: bool,
        summary: ImportSummary,
    ) -> None:
        market = self._resolve_market(entry.country, entry.region, is_home_market)
        market_sources = [
            self._source_cache[k] for k in entry.sources if k in self._source_cache
        ]

        # --- raw metric observations -----------------------------------
        raw_values = entry.raw.model_dump()
        for field_name, spec in RAW_METRIC_MAP.items():
            if field_name not in raw_values:
                continue
            value = raw_values[field_name]
            observation = MarketObservation(
                market_id=market.id,
                snapshot_id=snapshot.id,
                metric_key=spec.metric_key,
                # NULL stays NULL: unknown is not zero (ADR-004).
                value_numeric=value,
                unit=spec.unit,
                period_label=spec.period_label,
                fact_type=spec.default_fact_type if value is not None else FactType.ND.value,
                confidence=entry.confidence_level,
                methodology=spec.methodology,
                observation_metadata={
                    "source_field": field_name,
                    "market_confidence_level": entry.confidence_level,
                    "value_supplied": value is not None,
                },
            )
            self.session.add(observation)
            self.session.flush()
            summary.observations += 1
            self._attach_sources(observation, spec.metric_key, market_sources)

        # --- technology spending growth --------------------------------
        tsg = entry.technology_spending_growth
        if tsg is not None:
            observation = MarketObservation(
                market_id=market.id,
                snapshot_id=snapshot.id,
                metric_key=MetricKey.TECHNOLOGY_SPENDING_GROWTH_PCT.value,
                value_numeric=tsg.value_pct,
                value_text=tsg.scope,
                unit="PCT",
                period_label=str(tsg.year or tsg.period or "snapshot"),
                fact_type=tsg.fact_type,
                confidence=entry.confidence_level,
                methodology=tsg.scope,
                observation_metadata={
                    "scope": tsg.scope,
                    "declared_source": tsg.source,
                    "value_supplied": tsg.value_pct is not None,
                },
            )
            self.session.add(observation)
            self.session.flush()
            summary.observations += 1
            # This one field names its own source, so attribution is exact.
            if tsg.source and tsg.source in self._source_cache:
                self.session.add(
                    ObservationSource(
                        observation_id=observation.id,
                        source_id=self._source_cache[tsg.source].id,
                        attribution="EXPLICIT",
                    )
                )

        # --- categories -------------------------------------------------
        for category in entry.market_categories:
            if category not in MARKET_CATEGORY_LABELS:
                summary.warnings.append(
                    f"{entry.country}: unknown market category {category!r}; skipped"
                )
                continue
            self.session.add(
                MarketSnapshotCategory(
                    market_id=market.id, snapshot_id=snapshot.id, category_key=category
                )
            )
            summary.categories += 1

        # --- competition -------------------------------------------------
        self.session.add(
            MarketCompetitionAssessment(
                market_id=market.id,
                snapshot_id=snapshot.id,
                level=entry.competition.level,
                fact_type=entry.competition.fact_type,
                included_in_score=entry.competition.included_in_score,
            )
        )
        summary.competition_assessments += 1

        # --- TAM/SAM/SOM (only where supplied) ---------------------------
        if entry.tam_sam_som is not None:
            t = entry.tam_sam_som
            self.session.add(
                MarketSizeEstimate(
                    market_id=market.id,
                    snapshot_id=snapshot.id,
                    fact_type=t.fact_type,
                    confidence_level=t.confidence_level,
                    tam_min=_rmin(t.tam_relevant_employer_firms),
                    tam_max=_rmax(t.tam_relevant_employer_firms),
                    sam_min=_rmin(t.sam_boro_icp_firms),
                    sam_max=_rmax(t.sam_boro_icp_firms),
                    som_accounts_min=_rmin(t.som_addressable_accounts_12_24m),
                    som_accounts_max=_rmax(t.som_addressable_accounts_12_24m),
                    ticket_min_usd=_rmin(t.ticket_usd),
                    ticket_max_usd=_rmax(t.ticket_usd),
                    sam_value_min_usd=_rmin(t.sam_economic_value_usd),
                    sam_value_max_usd=_rmax(t.sam_economic_value_usd),
                    som_pool_value_min_usd=_rmin(t.som_account_pool_value_usd),
                    som_pool_value_max_usd=_rmax(t.som_account_pool_value_usd),
                    warning=t.warning,
                )
            )
            summary.market_size_estimates += 1

        # --- deep dive ---------------------------------------------------
        if entry.deep_dive is not None:
            dd = entry.deep_dive
            self.session.add(
                MarketDeepDive(
                    market_id=market.id,
                    snapshot_id=snapshot.id,
                    priority_verticals=split_semicolon_list(dd.priority_verticals),
                    priority_geographies=split_semicolon_list(dd.priority_geographies),
                    recommended_channel=dd.recommended_channel,
                    common_buyers=split_semicolon_list(dd.common_buyers),
                    common_problem_pattern=dd.common_problem_pattern,
                    # Original strings retained verbatim.
                    raw_values=dd.model_dump(),
                )
            )
            summary.deep_dives += 1

    def _attach_sources(
        self, observation: MarketObservation, metric_key: str, market_sources: list[Source]
    ) -> None:
        """Link an observation to the market's declared sources.

        ``attribution`` distinguishes a hinted metric-level match from a plain
        market-level reference so nothing masquerades as an exact citation.
        """
        hints = METRIC_SOURCE_HINTS.get(metric_key, ())
        for source in market_sources:
            attribution = "METRIC_HINT" if source.source_key in hints else "MARKET_LEVEL"
            self.session.add(
                ObservationSource(
                    observation_id=observation.id,
                    source_id=source.id,
                    attribution=attribution,
                )
            )

    # -- scoring ---------------------------------------------------------

    def _ensure_base_scoring_model(self, doc: MarketIntelligenceDocument) -> ScoringModel:
        model = self.session.scalar(
            select(ScoringModel).where(
                ScoringModel.key == BASE_MODEL_KEY,
                ScoringModel.version == BASE_MODEL_VERSION,
            )
        )
        if model is not None:
            return model

        model = ScoringModel(
            key=BASE_MODEL_KEY,
            version=BASE_MODEL_VERSION,
            name="Market attractiveness",
            description=(
                "Base 0-100 market attractiveness model reproduced from the "
                "BoRo Studio International Market Intelligence 2026 artifact."
            ),
            normalization_method=doc.metadata.normalization,
            definition=base_model_definition(doc.metadata.model_dump()),
            active=True,
        )
        self.session.add(model)
        self.session.flush()

        for spec in BASE_MODEL_COMPONENTS:
            self.session.add(
                ScoringModelComponent(
                    scoring_model_id=model.id,
                    component_key=spec["component_key"],
                    weight=spec["weight"],
                    formula=spec["formula"],
                    required_metrics=spec["required_metrics"],
                    ordinal=spec["ordinal"],
                )
            )
        self.session.flush()
        return model

    def _create_imported_reference_run(
        self,
        snapshot: MarketSnapshot,
        model: ScoringModel,
        doc: MarketIntelligenceDocument,
    ) -> tuple[str, float]:
        """Persist the supplied scores/ranks verbatim as a score run."""
        now = datetime.now(UTC)
        run = ScoreRun(
            scoring_model_id=model.id,
            snapshot_id=snapshot.id,
            kind=ScoreRunKind.IMPORTED_REFERENCE.value,
            context={"kind": "base_market", "snapshot_key": snapshot.key},
            universe_definition={
                "method": "as_published",
                "declared_universe_size": doc.metadata.universe_size,
                "ranked_markets": len(doc.markets),
                "home_market_benchmark": doc.home_market_benchmark.country,
                "universe_only_markets": [
                    m.country for m in doc.excluded_from_top50_but_in_normalization_universe
                ],
            },
            status=ScoreRunStatus.COMPLETED.value,
            started_at=now,
            completed_at=now,
            engine_version=ENGINE_VERSION,
        )
        self.session.add(run)
        self.session.flush()

        max_delta = 0.0
        entries: list[tuple[MarketEntry, bool]] = [(m, False) for m in doc.markets]
        entries.append((doc.home_market_benchmark, True))

        for entry, is_home in entries:
            market = self._resolve_market(entry.country, entry.region, is_home)
            components = entry.subscores.as_dict()
            component_sum = sum(components.values())
            max_delta = max(max_delta, abs(component_sum - entry.market_score))

            score = MarketScore(
                score_run_id=run.id,
                market_id=market.id,
                rank=entry.rank,
                score=entry.market_score,
                confidence=None,
                coverage=1.0,
                score_metadata={
                    "source": "snapshot",
                    "is_home_market_benchmark": is_home,
                    "component_sum": component_sum,
                    "confidence_level_label": entry.confidence_level,
                },
            )
            self.session.add(score)
            self.session.flush()

            for spec in BASE_MODEL_COMPONENTS:
                key = spec["component_key"]
                value = components.get(key)
                self.session.add(
                    MarketScoreComponent(
                        market_score_id=score.id,
                        component_key=key,
                        raw_value=value,
                        normalized_value=(
                            value / spec["weight"] if value is not None else None
                        ),
                        weighted_score=value,
                        confidence=None,
                        coverage=1.0 if value is not None else 0.0,
                        explanation=f"Imported verbatim from snapshot. {spec['formula']}",
                        component_metadata={"source": "snapshot_subscore"},
                    )
                )

        # Universe-only markets: published total, no component breakdown.
        for entry in doc.excluded_from_top50_but_in_normalization_universe:
            market = self._resolve_market(entry.country, None, False)
            self.session.add(
                MarketScore(
                    score_run_id=run.id,
                    market_id=market.id,
                    rank=None,
                    score=entry.market_score,
                    confidence=None,
                    coverage=0.0,
                    score_metadata={
                        "source": "snapshot",
                        "universe_only": True,
                        "note": (
                            "In the normalization universe with a published total "
                            "score, but the snapshot supplies no raw metrics or "
                            "component breakdown for this market."
                        ),
                    },
                )
            )

        self.session.flush()
        return str(run.id), max_delta

    # -- idempotency -----------------------------------------------------

    def _summarize_existing(
        self, snapshot: MarketSnapshot, warnings: list[str]
    ) -> ImportSummary:
        from sqlalchemy import func

        counts = {
            "observations": self.session.scalar(
                select(func.count())
                .select_from(MarketObservation)
                .where(MarketObservation.snapshot_id == snapshot.id)
            ),
            "categories": self.session.scalar(
                select(func.count())
                .select_from(MarketSnapshotCategory)
                .where(MarketSnapshotCategory.snapshot_id == snapshot.id)
            ),
            "competition": self.session.scalar(
                select(func.count())
                .select_from(MarketCompetitionAssessment)
                .where(MarketCompetitionAssessment.snapshot_id == snapshot.id)
            ),
            "sizes": self.session.scalar(
                select(func.count())
                .select_from(MarketSizeEstimate)
                .where(MarketSizeEstimate.snapshot_id == snapshot.id)
            ),
            "deep_dives": self.session.scalar(
                select(func.count())
                .select_from(MarketDeepDive)
                .where(MarketDeepDive.snapshot_id == snapshot.id)
            ),
            "markets": self.session.scalar(select(func.count()).select_from(Market)),
            "sources": self.session.scalar(select(func.count()).select_from(Source)),
        }
        reference_run = self.session.scalar(
            select(ScoreRun).where(
                ScoreRun.snapshot_id == snapshot.id,
                ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value,
            )
        )
        ranked = self.session.scalar(
            select(func.count())
            .select_from(MarketScore)
            .where(
                MarketScore.score_run_id == reference_run.id,
                MarketScore.rank.is_not(None),
            )
        ) if reference_run else 0

        return ImportSummary(
            snapshot_key=snapshot.key,
            snapshot_id=str(snapshot.id),
            created=False,
            markets=counts["markets"] or 0,
            ranked_markets=ranked or 0,
            home_market_benchmarks=self.session.scalar(
                select(func.count()).select_from(Market).where(Market.is_home_market.is_(True))
            ) or 0,
            universe_only_markets=(counts["markets"] or 0) - (ranked or 0) - 1,
            sources=counts["sources"] or 0,
            observations=counts["observations"] or 0,
            categories=counts["categories"] or 0,
            competition_assessments=counts["competition"] or 0,
            market_size_estimates=counts["sizes"] or 0,
            deep_dives=counts["deep_dives"] or 0,
            warnings=warnings,
            reference_score_run_id=str(reference_run.id) if reference_run else None,
            max_score_delta=0.0,
        )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _rmin(r: Any) -> float | None:
    return getattr(r, "min", None) if r is not None else None


def _rmax(r: Any) -> float | None:
    return getattr(r, "max", None) if r is not None else None
