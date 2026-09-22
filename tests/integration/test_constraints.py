"""A-2 / A-6 — database-level invariants.

These tests assert the *schema* enforces what the application believes, so
code and database cannot drift apart silently.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from boro_gtm.core.enums import CLOSED_VOCABULARIES
from boro_gtm.market_intelligence.domain.models import (
    Market,
    MarketObservation,
    MarketSnapshot,
)
from boro_gtm.market_intelligence.research_gaps.detector import GapContext, record_gap

pytestmark = pytest.mark.integration


def _check_constraints(session) -> dict[str, str]:
    rows = session.execute(
        text(
            """
            SELECT rel.relname || '.' || con.conname AS name,
                   pg_get_constraintdef(con.oid)      AS definition
              FROM pg_constraint con
              JOIN pg_class rel ON rel.oid = con.conrelid
              JOIN pg_namespace n ON n.oid = rel.relnamespace
             WHERE n.nspname = 'public' AND con.contype = 'c'
            """
        )
    ).all()
    return {r.name: r.definition for r in rows}


# ---------------------------------------------------------------------------
# A-2 — closed vocabularies are enforced by the database
# ---------------------------------------------------------------------------


def test_every_closed_vocabulary_has_a_check_constraint(migrated_engine) -> None:
    from sqlalchemy.orm import Session

    with Session(migrated_engine) as session:
        constraints = _check_constraints(session)

    for qualified, values in CLOSED_VOCABULARIES.items():
        table, column = qualified.split(".")
        if table == "market_snapshot_categories":
            # Enforced by a foreign key to the market_categories lookup.
            continue
        matching = [
            definition
            for name, definition in constraints.items()
            if name.startswith(f"{table}.") and column in definition
        ]
        assert matching, f"no CHECK constraint covers {qualified}"
        # Every allowed value must appear in at least one covering constraint.
        joined = " ".join(matching)
        for value in values:
            assert f"'{value}'" in joined, f"{qualified} constraint omits {value!r}"


def test_category_vocabulary_is_enforced_by_foreign_key(imported_session) -> None:
    with pytest.raises(IntegrityError):
        imported_session.execute(
            text(
                "INSERT INTO market_snapshot_categories (market_id, snapshot_id, category_key) "
                "SELECT market_id, snapshot_id, 'NOT_A_CATEGORY' "
                "FROM market_snapshot_categories LIMIT 1"
            )
        )


@pytest.mark.parametrize(
    ("table", "column", "bad_value"),
    [
        ("market_observations", "fact_type", "N/D"),
        ("market_observations", "fact_type", "UNKNOWN"),
        ("market_observations", "availability", "MAYBE"),
        ("market_observations", "period_granularity", "DECADE"),
        ("observation_sources", "attribution", "GUESSED"),
        ("score_runs", "kind", "vibes"),
        ("score_runs", "status", "NEARLY"),
        ("research_gaps", "status", "SHRUG"),
        ("research_gaps", "priority", "URGENT"),
    ],
)
def test_closed_vocabulary_rejects_unknown_value(
    imported_session, table, column, bad_value
) -> None:
    """The database refuses a value outside the closed vocabulary."""
    if table == "research_gaps":
        # Seed one gap so the UPDATE below has something to act on.
        market = imported_session.scalar(select(Market))
        record_gap(
            imported_session,
            GapContext(metric_key="population", market_id=market.id),
            "constraint probe",
        )
        imported_session.flush()

    existing = imported_session.execute(
        text(f"SELECT 1 FROM {table} LIMIT 1")
    ).first()
    assert existing is not None, f"{table} must have a row to probe"

    with pytest.raises((IntegrityError, DBAPIError)):
        imported_session.execute(
            text(f"UPDATE {table} SET {column} = :v"), {"v": bad_value}
        )
    imported_session.rollback()


def test_nd_cannot_be_inserted_as_a_fact_type(imported_session) -> None:
    """The exact regression: "N/D" is not an evidence kind (authoritative #2)."""
    sample = imported_session.scalar(select(MarketObservation))
    with pytest.raises((IntegrityError, DBAPIError)):
        imported_session.execute(
            text(
                "INSERT INTO market_observations "
                "(id, market_id, snapshot_id, metric_key, period_label, "
                " period_granularity, availability, fact_type, created_at) "
                "VALUES (gen_random_uuid(), :m, :s, 'probe_metric', 'snapshot', "
                "        'SNAPSHOT', 'OBSERVED', 'N/D', now())"
            ),
            {"m": sample.market_id, "s": sample.snapshot_id},
        )
    imported_session.rollback()


def test_availability_consistency_is_enforced(imported_session) -> None:
    """NOT_AVAILABLE may not carry a value or a fact type, and vice versa."""
    sample = imported_session.scalar(select(MarketObservation))
    # Plain ids survive the rollbacks below; an ORM instance would not.
    market_id, snapshot_id = sample.market_id, sample.snapshot_id

    # OBSERVED without a fact type is incoherent.
    with pytest.raises((IntegrityError, DBAPIError)):
        imported_session.execute(
            text(
                "INSERT INTO market_observations "
                "(id, market_id, snapshot_id, metric_key, period_label, "
                " period_granularity, availability, fact_type, created_at) "
                "VALUES (gen_random_uuid(), :m, :s, 'probe_a', 'snapshot', "
                "        'SNAPSHOT', 'OBSERVED', NULL, now())"
            ),
            {"m": market_id, "s": snapshot_id},
        )
    imported_session.rollback()

    # NOT_AVAILABLE carrying a value is incoherent.
    with pytest.raises((IntegrityError, DBAPIError)):
        imported_session.execute(
            text(
                "INSERT INTO market_observations "
                "(id, market_id, snapshot_id, metric_key, value_numeric, period_label, "
                " period_granularity, availability, fact_type, created_at) "
                "VALUES (gen_random_uuid(), :m, :s, 'probe_b', 1.0, 'snapshot', "
                "        'SNAPSHOT', 'NOT_AVAILABLE', NULL, now())"
            ),
            {"m": market_id, "s": snapshot_id},
        )
    imported_session.rollback()


def test_exact_date_requires_date_granularity(imported_session) -> None:
    """A year-only observation may not carry an invented exact date."""
    sample = imported_session.scalar(select(MarketObservation))
    with pytest.raises((IntegrityError, DBAPIError)):
        imported_session.execute(
            text(
                "INSERT INTO market_observations "
                "(id, market_id, snapshot_id, metric_key, value_numeric, period_label, "
                " period_granularity, observed_at, availability, fact_type, created_at) "
                "VALUES (gen_random_uuid(), :m, :s, 'probe_c', 1.0, '2025', "
                "        'YEAR', DATE '2025-01-01', 'OBSERVED', 'FACT', now())"
            ),
            {"m": sample.market_id, "s": sample.snapshot_id},
        )
    imported_session.rollback()


def test_minimum_rank_coverage_is_bounded(imported_session) -> None:
    with pytest.raises((IntegrityError, DBAPIError)):
        imported_session.execute(
            text("UPDATE scoring_models SET minimum_rank_coverage = 1.5")
        )
    imported_session.rollback()


# ---------------------------------------------------------------------------
# A-6 — evidence tables are append-only at the database level
# ---------------------------------------------------------------------------


def test_snapshots_cannot_be_updated(imported_session) -> None:
    snapshot = imported_session.scalar(select(MarketSnapshot))
    with pytest.raises(DBAPIError) as exc:
        imported_session.execute(
            text("UPDATE market_snapshots SET title = 'rewritten' WHERE id = :i"),
            {"i": snapshot.id},
        )
    assert "append-only" in str(exc.value)
    imported_session.rollback()


def test_observations_cannot_be_updated(imported_session) -> None:
    with pytest.raises(DBAPIError) as exc:
        imported_session.execute(
            text("UPDATE market_observations SET value_numeric = 1 WHERE true")
        )
    assert "append-only" in str(exc.value)
    imported_session.rollback()


def test_append_only_still_allows_insert_and_cascade_delete(session) -> None:
    """The invariant must not break imports, cascades or test teardown."""
    from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
    from tests.conftest import SOURCE_JSON

    summary = SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()
    assert summary.observations > 0

    snapshot = session.get(MarketSnapshot, uuid.UUID(summary.snapshot_id))
    session.delete(snapshot)  # cascades into observations
    session.flush()
    assert session.scalar(select(MarketObservation)) is None
