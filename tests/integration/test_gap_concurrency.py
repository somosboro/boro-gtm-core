"""A-12 — research-gap writes are concurrency safe."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from boro_gtm.market_intelligence.domain.models import Market, MarketSnapshot
from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
from boro_gtm.market_intelligence.research_gaps.detector import GapContext, record_gap
from boro_gtm.strategy.domain.models import ResearchGap
from tests.conftest import SOURCE_JSON

pytestmark = pytest.mark.integration


def test_repeated_record_gap_is_a_no_op(imported_session) -> None:
    market = imported_session.scalar(select(Market).where(Market.iso2 == "PL"))
    context = GapContext(metric_key="population", market_id=market.id)

    first, created_first = record_gap(imported_session, context, "first")
    second, created_second = record_gap(imported_session, context, "second")

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    # The original reason is preserved; a repeat write does not overwrite it.
    assert second.reason == "first"
    assert imported_session.scalar(select(func.count()).select_from(ResearchGap)) == 1


def test_uses_on_conflict_rather_than_select_then_insert(imported_session) -> None:
    """The write must be a single conflict-tolerant statement."""
    from pathlib import Path

    source = Path(
        "packages/boro_gtm/market_intelligence/research_gaps/detector.py"
    ).read_text(encoding="utf-8")
    assert "on_conflict_do_nothing" in source
    assert "index_elements=[\"context_fingerprint\"]" in source


def test_concurrent_writers_do_not_surface_integrityerror(migrated_engine) -> None:
    """Two real connections racing on the same gap: both must succeed.

    Each thread owns its own session/connection and commits, so this exercises
    the genuine database race rather than a single-session shortcut.
    """
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)

    with factory() as setup:
        setup.execute(
            __import__("sqlalchemy").text(
                "TRUNCATE market_score_components, market_scores, score_runs, "
                "observation_sources, market_observations, "
                "market_snapshot_categories, market_competition_assessments, "
                "market_size_estimates, market_deep_dives, "
                "market_vertical_profiles, research_gaps, "
                "scoring_model_components, scoring_models, market_snapshots, "
                "markets, sources, verticals, icps, offers, channels, "
                "market_categories RESTART IDENTITY CASCADE"
            )
        )
        SnapshotImporter(setup).import_file(SOURCE_JSON)
        setup.commit()
        market_id = setup.scalar(select(Market.id).where(Market.iso2 == "PL"))
        snapshot_id = setup.scalar(select(MarketSnapshot.id))

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    outcomes: list[bool] = []

    def writer() -> None:
        session: Session = factory()
        try:
            context = GapContext(
                metric_key="population", market_id=market_id, snapshot_id=snapshot_id
            )
            barrier.wait(timeout=10)
            _, created = record_gap(session, context, "concurrent")
            session.commit()
            outcomes.append(created)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    try:
        assert not errors, f"concurrent writers raised: {errors}"
        assert len(outcomes) == 2
        # Exactly one writer created the row; the other read it back.
        assert sorted(outcomes) == [False, True]

        with factory() as check:
            count = check.scalar(
                select(func.count())
                .select_from(ResearchGap)
                .where(ResearchGap.metric_key == "population")
            )
        assert count == 1
    finally:
        with factory() as cleanup:
            cleanup.execute(
                __import__("sqlalchemy").text(
                    "TRUNCATE market_score_components, market_scores, score_runs, "
                    "observation_sources, market_observations, "
                    "market_snapshot_categories, market_competition_assessments, "
                    "market_size_estimates, market_deep_dives, "
                    "market_vertical_profiles, research_gaps, "
                    "scoring_model_components, scoring_models, market_snapshots, "
                    "markets, sources, verticals, icps, offers, channels, "
                    "market_categories RESTART IDENTITY CASCADE"
                )
            )
            cleanup.commit()


def test_unique_constraint_is_still_present(imported_session) -> None:
    """ON CONFLICT relies on the index; it must not have been dropped."""
    from sqlalchemy import text

    definition = imported_session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'uq_research_gaps_fingerprint'"
        )
    ).scalar_one()
    assert definition == "UNIQUE (context_fingerprint)"
