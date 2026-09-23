"""Guards against a database that reports the right revision but the wrong schema.

The field failure this file exists for: ``alembic current`` and ``alembic
heads`` both said ``0003_m2``, every migration was "applied", and
``GET /api/v1/discovery-runs`` returned 500 because
``discovery_runs.fetch_completed_at`` did not exist. The migration file had
been edited after it had already run in that database, and an Alembic revision
records only *that* a migration ran, never what it did.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from boro_gtm.core.schema_check import (
    SchemaDriftError,
    assert_schema_matches,
    check_migration_integrity,
    check_schema,
    load_manifest,
    migration_digests,
)

pytestmark = pytest.mark.integration


# --- ORM ↔ database parity -------------------------------------------------


def test_a_migrated_database_matches_the_orm(migrated_engine):
    """The baseline: migrations alone produce exactly what the ORM expects."""
    report = check_schema(migrated_engine, strict_extra=True)
    assert report.ok, report.summary()


def test_a_dropped_column_is_detected_even_though_alembic_reports_head(
    migrated_engine,
):
    """The exact field failure, reproduced and caught."""
    with migrated_engine.begin() as conn:
        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        conn.execute(
            text("ALTER TABLE discovery_runs DROP COLUMN fetch_completed_at")
        )
    try:
        # Alembic is still perfectly happy.
        with migrated_engine.connect() as conn:
            still = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert still == revision

        report = check_schema(migrated_engine)
        assert not report.ok
        assert "discovery_runs.fetch_completed_at" in report.summary()

        with pytest.raises(SchemaDriftError) as exc:
            assert_schema_matches(migrated_engine)
        assert exc.value.code == "SCHEMA_DRIFT"
        assert any(
            "fetch_completed_at" in p for p in exc.value.details["problems"]
        )
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE discovery_runs "
                    "ADD COLUMN fetch_completed_at TIMESTAMPTZ"
                )
            )
    assert check_schema(migrated_engine, strict_extra=True).ok, "restore failed"


def test_a_stale_extra_column_is_reported_in_strict_mode(migrated_engine):
    """Residue of a mutated migration, the other half of the same defect."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE provider_entities ADD COLUMN identity_collision BOOLEAN")
        )
    try:
        strict = check_schema(migrated_engine, strict_extra=True)
        assert not strict.ok
        assert "provider_entities.identity_collision" in strict.summary()
        # A column the ORM does not know about breaks no read, so the default
        # check stays quiet about it.
        assert check_schema(migrated_engine, strict_extra=False).ok
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE provider_entities DROP COLUMN identity_collision")
            )


def test_nullability_drift_is_detected(migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE companies ALTER COLUMN created_at DROP NOT NULL")
        )
    try:
        report = check_schema(migrated_engine)
        assert not report.ok
        assert "companies.created_at" in report.summary()
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE companies ALTER COLUMN created_at SET NOT NULL")
            )


def test_a_dropped_table_is_detected(migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS discovery_jobs CASCADE"))
    try:
        report = check_schema(migrated_engine)
        assert not report.ok
        assert "discovery_jobs" in report.summary()
    finally:
        # Rebuild it from the ORM rather than hand-writing DDL that could drift.
        from boro_gtm.core.db import Base

        Base.metadata.tables["discovery_jobs"].create(bind=migrated_engine)
    assert check_schema(migrated_engine).ok


# --- migration file integrity ---------------------------------------------


def test_every_migration_on_disk_is_recorded_in_the_manifest():
    manifest = load_manifest()["migrations"]
    on_disk = migration_digests()
    assert set(manifest) == set(on_disk), (
        "migrations/MANIFEST.json is out of step with migrations/versions/. "
        "Run: python -m boro_gtm.cli db record-migrations"
    )


def test_published_migrations_have_not_been_edited():
    """The guard for the root cause, not the symptom.

    A migration that has already run somewhere must never change. If this
    fails, the fix is a *new* corrective migration — not an edit to the old
    one, which would leave every existing database silently wrong.
    """
    problems = check_migration_integrity()
    assert problems == [], "\n".join(problems)


def test_a_mutated_migration_is_detected(tmp_path, monkeypatch):
    import boro_gtm.core.schema_check as sc

    fake = tmp_path / "versions"
    fake.mkdir()
    (fake / "0001_example.py").write_text("# original\n", encoding="utf-8")
    manifest = tmp_path / "MANIFEST.json"
    monkeypatch.setattr(sc, "MIGRATIONS_DIR", fake)
    monkeypatch.setattr(sc, "MANIFEST_PATH", manifest)

    sc.write_manifest()
    assert sc.check_migration_integrity() == []

    (fake / "0001_example.py").write_text("# edited after release\n", encoding="utf-8")
    problems = sc.check_migration_integrity()
    assert len(problems) == 1
    assert "content changed" in problems[0]


def test_a_new_unrecorded_migration_is_detected(tmp_path, monkeypatch):
    import boro_gtm.core.schema_check as sc

    fake = tmp_path / "versions"
    fake.mkdir()
    (fake / "0001_example.py").write_text("# one\n", encoding="utf-8")
    monkeypatch.setattr(sc, "MIGRATIONS_DIR", fake)
    monkeypatch.setattr(sc, "MANIFEST_PATH", tmp_path / "MANIFEST.json")
    sc.write_manifest()

    (fake / "0002_new.py").write_text("# two\n", encoding="utf-8")
    problems = sc.check_migration_integrity()
    assert len(problems) == 1
    assert "absent from the manifest" in problems[0]
