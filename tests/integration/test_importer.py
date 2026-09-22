"""Importer integration: idempotency, transactional safety, provenance, NULLs."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import func, select

from boro_gtm.core.enums import Availability, FactType, ScoreRunKind
from boro_gtm.core.errors import ImportConflictError, ValidationError
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketCompetitionAssessment,
    MarketDeepDive,
    MarketObservation,
    MarketScore,
    MarketSizeEstimate,
    MarketSnapshot,
    ObservationSource,
    ScoreRun,
    ScoringModel,
    Source,
)
from boro_gtm.market_intelligence.importers.snapshot_importer import (
    SnapshotImporter,
    compute_sha256,
    snapshot_identity,
)
from tests.conftest import SOURCE_JSON
from tests.fixtures.golden import (
    EXPECTED_HOME_BENCHMARKS,
    EXPECTED_RANKED_MARKETS,
    EXPECTED_SOURCES,
    EXPECTED_UNIVERSE_ONLY,
    EXPECTED_UNIVERSE_SIZE,
)

pytestmark = pytest.mark.integration


def test_import_creates_expected_registry(imported_session) -> None:
    session = imported_session
    assert session.scalar(select(func.count()).select_from(Market)) == EXPECTED_UNIVERSE_SIZE
    assert session.scalar(
        select(func.count()).select_from(Market).where(Market.is_home_market.is_(True))
    ) == EXPECTED_HOME_BENCHMARKS
    assert session.scalar(select(func.count()).select_from(Source)) == EXPECTED_SOURCES


def test_every_market_has_iso_codes(imported_session) -> None:
    for market in imported_session.scalars(select(Market)).all():
        assert market.iso2 and len(market.iso2) == 2
        assert market.iso3 and len(market.iso3) == 3


def test_snapshot_is_hashed_and_retains_raw_payload(
    session, source_bytes, source_payload
) -> None:
    """Identity is the canonical digest; the byte digest is kept separately."""
    summary = SnapshotImporter(session).import_file(SOURCE_JSON)
    snapshot = session.scalar(
        select(MarketSnapshot).where(MarketSnapshot.key == summary.snapshot_key)
    )
    assert snapshot.sha256 == snapshot_identity(source_payload)
    assert snapshot.source_file_sha256 == compute_sha256(source_bytes)
    assert snapshot.raw_payload["metadata"]["universe_size"] == EXPECTED_UNIVERSE_SIZE
    assert snapshot.source_filename == SOURCE_JSON.name


def test_reimport_is_idempotent(session) -> None:
    importer = SnapshotImporter(session)
    first = importer.import_file(SOURCE_JSON)
    session.flush()

    counts_before = {
        "snapshots": session.scalar(select(func.count()).select_from(MarketSnapshot)),
        "markets": session.scalar(select(func.count()).select_from(Market)),
        "observations": session.scalar(select(func.count()).select_from(MarketObservation)),
        "scores": session.scalar(select(func.count()).select_from(MarketScore)),
    }

    second = SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()

    assert first.created is True
    assert second.created is False
    assert second.snapshot_key == first.snapshot_key
    assert second.snapshot_id == first.snapshot_id
    assert {
        "snapshots": session.scalar(select(func.count()).select_from(MarketSnapshot)),
        "markets": session.scalar(select(func.count()).select_from(Market)),
        "observations": session.scalar(select(func.count()).select_from(MarketObservation)),
        "scores": session.scalar(select(func.count()).select_from(MarketScore)),
    } == counts_before


def test_same_key_different_content_is_a_conflict(session, source_payload) -> None:
    SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()

    mutated = copy.deepcopy(source_payload)
    mutated["markets"][0]["why_it_matters"] = "changed after publication"
    with pytest.raises(ImportConflictError):
        SnapshotImporter(session).import_payload(mutated)


def test_invalid_import_leaves_no_partial_rows(session, source_payload) -> None:
    """A failed import must not persist anything."""
    broken = copy.deepcopy(source_payload)
    broken["markets"][3]["rank"] = broken["markets"][2]["rank"]  # duplicate rank

    with pytest.raises(ValidationError):
        SnapshotImporter(session).import_payload(broken)

    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0
    assert session.scalar(select(func.count()).select_from(Market)) == 0
    assert session.scalar(select(func.count()).select_from(MarketObservation)) == 0


def test_unresolvable_country_aborts_import(session, source_payload) -> None:
    broken = copy.deepcopy(source_payload)
    broken["markets"][10]["country"] = "Republic of Nowhere"

    with pytest.raises(ValidationError):
        SnapshotImporter(session).import_payload(broken)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0


def test_summary_counts_match_the_source(session) -> None:
    summary = SnapshotImporter(session).import_file(SOURCE_JSON)
    assert summary.markets == EXPECTED_UNIVERSE_SIZE
    assert summary.ranked_markets == EXPECTED_RANKED_MARKETS
    assert summary.home_market_benchmarks == EXPECTED_HOME_BENCHMARKS
    assert summary.universe_only_markets == EXPECTED_UNIVERSE_ONLY
    assert summary.sources == EXPECTED_SOURCES
    assert summary.observations > 0
    assert summary.max_score_delta == pytest.approx(0.0, abs=1e-9)


def test_nulls_remain_null(imported_session) -> None:
    """ADR-004: unknown is never coerced to zero, and is not a fact type."""
    rows = imported_session.scalars(
        select(MarketObservation).where(
            MarketObservation.metric_key == "software_spending_pct_gdp"
        )
    ).all()
    unknown = [r for r in rows if r.value_numeric is None]
    assert unknown, "expected unknown software spending in the dataset"
    for row in unknown:
        assert row.value_numeric is None
        # Absence is an availability statement, never an evidence kind.
        assert row.availability == Availability.NOT_AVAILABLE.value
        assert row.fact_type is None
        assert row.observation_metadata["value_supplied"] is False


def test_fact_types_are_preserved(imported_session) -> None:
    competition = imported_session.scalars(
        select(MarketCompetitionAssessment)
    ).all()
    assert competition
    for row in competition:
        assert row.fact_type == "INFERENCE"
        # Competition is explicitly excluded from the score.
        assert row.included_in_score is False

    growth = imported_session.scalars(
        select(MarketObservation).where(
            MarketObservation.metric_key == "technology_spending_growth_pct"
        )
    ).all()
    assert growth
    for row in growth:
        assert row.fact_type in {None, "PROXY", "FACT"}
        if row.fact_type is None:
            assert row.availability == Availability.NOT_AVAILABLE.value
            # The source's own "N/D" token is retained for fidelity, but as
            # metadata about the source, not as a stored fact type.
            assert row.observation_metadata["declared_fact_type"] in {None, "N/D"}


def test_nd_is_never_stored_as_a_fact_type(imported_session) -> None:
    """Regression: "N/D" must not survive anywhere as an evidence kind."""
    allowed = {f.value for f in FactType}
    for row in imported_session.scalars(select(MarketObservation)).all():
        assert row.fact_type is None or row.fact_type in allowed
    for row in imported_session.scalars(select(MarketCompetitionAssessment)).all():
        assert row.fact_type is None or row.fact_type in allowed
    for row in imported_session.scalars(select(MarketSizeEstimate)).all():
        assert row.fact_type is None or row.fact_type in allowed


def test_temporal_provenance_is_recorded_without_inventing_precision(
    imported_session,
) -> None:
    """A year-only period must never become an exact date."""
    rows = imported_session.scalars(select(MarketObservation)).all()
    by_metric = {r.metric_key: r for r in rows}

    assert by_metric["gdp_nominal_usd_bn"].period_granularity == "YEAR"
    assert by_metric["gdp_nominal_usd_bn"].period_label == "2025"
    assert by_metric["language_access"].period_granularity == "SNAPSHOT"
    # The source says only "latest" for manufacturing value added.
    assert by_metric["manufacturing_value_added_usd_bn"].period_granularity == "UNDATED"

    # No row in this snapshot claims a day-precise observation date.
    assert all(r.observed_at is None for r in rows)
    assert all(r.period_granularity != "DATE" for r in rows)


def test_market_size_only_where_supplied(imported_session, source_payload) -> None:
    expected = sum(1 for m in source_payload["markets"] if m.get("tam_sam_som"))
    actual = imported_session.scalar(
        select(func.count()).select_from(MarketSizeEstimate)
    )
    assert actual == expected

    deep_dives = imported_session.scalar(select(func.count()).select_from(MarketDeepDive))
    assert deep_dives == sum(1 for m in source_payload["markets"] if m.get("deep_dive"))


def test_deep_dive_retains_raw_strings(imported_session) -> None:
    row = imported_session.scalar(select(MarketDeepDive))
    assert isinstance(row.priority_verticals, list)
    # The original semicolon string is preserved alongside the parsed list.
    assert isinstance(row.raw_values["priority_verticals"], str)
    assert ";" in row.raw_values["priority_verticals"]


def test_provenance_is_recorded_with_attribution(imported_session) -> None:
    links = imported_session.scalars(select(ObservationSource)).all()
    assert links
    attributions = {link.attribution for link in links}
    assert attributions <= {"EXPLICIT", "METRIC_HINT", "MARKET_LEVEL"}
    # No metric-level attribution is invented where the source lacks it.
    assert "MARKET_LEVEL" in attributions


def test_imported_reference_run_exists(imported_session) -> None:
    run = imported_session.scalar(
        select(ScoreRun).where(ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value)
    )
    assert run is not None
    assert run.status == "COMPLETED"
    scored = imported_session.scalar(
        select(func.count()).select_from(MarketScore).where(
            MarketScore.score_run_id == run.id
        )
    )
    assert scored == EXPECTED_UNIVERSE_SIZE


def test_scoring_model_is_registered_with_full_weight(imported_session) -> None:
    model = imported_session.scalar(
        select(ScoringModel).where(ScoringModel.key == "market-attractiveness")
    )
    assert model.version == "1.0"
    assert sum(float(c.weight) for c in model.components) == 100.0
    assert model.definition["percentile_method"] == "midrank_over_n_minus_1"


def test_markets_table_has_no_score_column() -> None:
    """ADR-002 is structural, so assert it structurally."""
    columns = set(Market.__table__.columns.keys())
    assert not any("score" in c for c in columns)
