"""The acceptance flow: clean DB -> migrate -> import -> score -> seed -> query.

This mirrors 06 acceptance criterion F and exercises the documented CLI path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from boro_gtm.core.enums import ScoreRunKind
from boro_gtm.market_intelligence.domain.models import Market, MarketScore, ScoreRun
from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
from boro_gtm.market_intelligence.services import contextual_service, scoring_service
from boro_gtm.strategy.seeds.loader import seed_all
from tests.conftest import SOURCE_JSON
from tests.fixtures.golden import (
    EXPECTED_UNIVERSE_SIZE,
    GOLDEN_HOME_BENCHMARK,
    GOLDEN_SCORES,
    INITIAL_PORTFOLIO,
    SCORE_TOLERANCE,
)

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"


def test_full_pipeline_from_empty_database(session) -> None:
    # 1. The schema exists and is empty (migrations ran in the fixture).
    assert session.scalar(select(func.count()).select_from(Market)) == 0

    # 2. Import.
    summary = SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()
    assert summary.created is True
    assert summary.markets == EXPECTED_UNIVERSE_SIZE

    # 3. Re-import is a no-op.
    again = SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()
    assert again.created is False
    assert session.scalar(select(func.count()).select_from(Market)) == EXPECTED_UNIVERSE_SIZE

    # 4. Reference reproduction matches the published artifact.
    run = scoring_service.create_base_score_run(
        session, SNAPSHOT_KEY, ScoreRunKind.REFERENCE_REPRODUCTION.value
    )
    session.flush()
    markets = {m.id: m for m in session.scalars(select(Market)).all()}
    scores = {
        markets[r.market_id].iso2: r
        for r in session.scalars(
            select(MarketScore).where(MarketScore.score_run_id == run.id)
        ).all()
    }
    for iso2, (score, rank) in GOLDEN_SCORES.items():
        assert float(scores[iso2].score) == pytest.approx(score, abs=SCORE_TOLERANCE)
        assert scores[iso2].rank == rank
    home_iso, home_score = GOLDEN_HOME_BENCHMARK
    assert float(scores[home_iso].score) == pytest.approx(home_score, abs=SCORE_TOLERANCE)
    assert scores[home_iso].rank is None

    # 5. Native recalculation is honest rather than forced.
    native = scoring_service.create_base_score_run(
        session, SNAPSHOT_KEY, ScoreRunKind.NATIVE_RECALCULATION.value
    )
    session.flush()
    comparison = scoring_service.compare_runs(session, run.id, native.id)
    assert comparison["max_score_delta"] > SCORE_TOLERANCE

    # 6. Seed strategy objects.
    seeded = seed_all(session)
    session.flush()
    assert seeded["verticals"] == 9
    assert seeded["market_vertical_profiles"]["created"] > 0

    # 7. Contextual ranking over the initial portfolio.
    ctx_run, results, gaps = contextual_service.run_contextual_ranking(
        session,
        snapshot_key=SNAPSHOT_KEY,
        vertical_key="commercial_hvac",
        icp_key="boro_field_service_midmarket_v1",
        offer_key="operations_os_core",
        channel_key="multichannel",
        ticket_usd=20000,
        market_iso2=INITIAL_PORTFOLIO,
    )
    session.flush()
    assert len(results) == 10
    assert all(r.score is not None for r in results)
    assert gaps["total"] >= gaps["created"]

    # 8. Four distinct runs coexist; none overwrote another.
    kinds = [
        r.kind
        for r in session.scalars(select(ScoreRun)).all()
    ]
    assert sorted(kinds) == sorted(
        [
            ScoreRunKind.IMPORTED_REFERENCE.value,
            ScoreRunKind.REFERENCE_REPRODUCTION.value,
            ScoreRunKind.NATIVE_RECALCULATION.value,
            ScoreRunKind.CONTEXTUAL.value,
        ]
    )


def test_no_m3_tables_exist(session) -> None:
    """Scope guard: M2 persists companies and nothing downstream of them.

    People, outbound and CRM synchronisation are M3. ``companies`` is no
    longer forbidden — it is M2's identity anchor — but everything that would
    hang off a contact still is.
    """
    rows = session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    ).scalars().all()
    forbidden = {
        "people", "contacts", "emails", "campaigns", "messages", "replies",
        "sequences", "crm_sync", "opportunities", "email_accounts",
        "inbox_messages", "person_claims",
    }
    assert forbidden.isdisjoint(set(rows))
