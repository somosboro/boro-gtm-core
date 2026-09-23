"""ORM ↔ database parity, and migration-file integrity.

**Why this module exists.** A database can report the correct Alembic
revision while its physical schema differs from what the shipped migration
now creates. That happens whenever a migration file is edited after it has
already run somewhere — routine during development, invisible afterwards,
because Alembic records only *that* a revision ran, never *what it did*.

The failure mode is nasty: ``alembic current`` and ``alembic heads`` agree,
every migration is "applied", and the defect surfaces as a 500 the first time
a request touches the missing column.

Two independent guards live here:

* :func:`check_schema` compares SQLAlchemy metadata against the live
  PostgreSQL catalog and reports drift as data.
* :func:`check_migration_integrity` verifies the migration files on disk
  still hash to what the manifest recorded, so a mutated migration is caught
  in CI rather than in someone's database.

Neither guard mutates anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, inspect

from boro_gtm.core.errors import GtmError

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"
MANIFEST_PATH = REPO_ROOT / "migrations" / "MANIFEST.json"

class SchemaDriftError(GtmError):
    """The database does not match the ORM the running code expects."""

    code = "SCHEMA_DRIFT"
    http_status = 500


class MigrationIntegrityError(GtmError):
    """A migration file changed after it was recorded in the manifest."""

    code = "MIGRATION_INTEGRITY"
    http_status = 500


@dataclass(slots=True)
class SchemaReport:
    """Everything that disagrees between the ORM and the database."""

    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)
    nullability_drift: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    missing_constraints: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(asdict(self).values())

    def problems(self) -> list[str]:
        out: list[str] = []
        for kind, items in asdict(self).items():
            out.extend(f"{kind}: {item}" for item in items)
        return out

    def summary(self) -> str:
        return "; ".join(self.problems()) if self.problems() else "schema matches the ORM"


def _orm_metadata():
    """Import every mapped module, so the metadata is complete."""
    from boro_gtm.core import registry  # noqa: F401 - imports all model modules
    from boro_gtm.core.db import Base

    return Base.metadata


def check_schema(engine: Engine, *, strict_extra: bool = True) -> SchemaReport:
    """Compare the ORM's metadata against the live PostgreSQL catalog.

    ``strict_extra`` reports columns the database has and the ORM does not.
    Those are usually the residue of a mutated migration, which is exactly the
    condition this module exists to catch, but they are reported separately
    because they do not break reads the way a *missing* column does.
    """
    metadata = _orm_metadata()
    inspector = inspect(engine)
    report = SchemaReport()

    db_tables = set(inspector.get_table_names())
    db_views = set(inspector.get_view_names())

    for table in metadata.sorted_tables:
        name = table.name
        if name not in db_tables and name not in db_views:
            report.missing_tables.append(name)
            continue

        db_columns = {c["name"]: c for c in inspector.get_columns(name)}
        for column in table.columns:
            db_column = db_columns.get(column.name)
            if db_column is None:
                report.missing_columns.append(f"{name}.{column.name}")
                continue
            # A column the ORM says is NOT NULL but the database allows NULL
            # will accept rows the application believes impossible.
            if column.nullable is False and db_column.get("nullable") is True:
                report.nullability_drift.append(
                    f"{name}.{column.name} (ORM NOT NULL, DB nullable)"
                )

        if strict_extra:
            orm_columns = {c.name for c in table.columns}
            for extra in sorted(set(db_columns) - orm_columns):
                report.extra_columns.append(f"{name}.{extra}")

        # Indexes and constraints are compared **structurally**, by the columns
        # they cover, never by name. A generated name longer than
        # PostgreSQL's 63-character limit is shortened with a hash suffix, so
        # name comparison reports every long foreign key as missing — noise
        # that would train a reader to ignore this report entirely.
        db_index_cols = {
            tuple(sorted(i.get("column_names") or []))
            for i in inspector.get_indexes(name)
        }
        db_unique_cols = {
            tuple(sorted(c.get("column_names") or []))
            for c in inspector.get_unique_constraints(name)
        }
        for index in table.indexes:
            cols = tuple(sorted(c.name for c in index.columns))
            if cols not in db_index_cols and cols not in db_unique_cols:
                report.missing_indexes.append(f"{name}({', '.join(cols)})")

        db_fks = {
            (
                tuple(sorted(fk.get("constrained_columns") or [])),
                fk.get("referred_table"),
                tuple(sorted(fk.get("referred_columns") or [])),
            )
            for fk in inspector.get_foreign_keys(name)
        }
        db_checks = {c.get("name") for c in inspector.get_check_constraints(name)}
        db_check_count = len(inspector.get_check_constraints(name))
        orm_check_count = 0

        for constraint in table.constraints:
            kind = type(constraint).__name__
            if kind == "ForeignKeyConstraint":
                cols = tuple(sorted(c.name for c in constraint.columns))
                referred = constraint.elements[0].column.table.name
                refcols = tuple(sorted(e.column.name for e in constraint.elements))
                if (cols, referred, refcols) not in db_fks:
                    report.missing_constraints.append(
                        f"{name} FK({', '.join(cols)}) -> {referred}"
                    )
            elif kind == "UniqueConstraint":
                cols = tuple(sorted(c.name for c in constraint.columns))
                if cols not in db_unique_cols and cols not in db_index_cols:
                    report.missing_constraints.append(
                        f"{name} UNIQUE({', '.join(cols)})"
                    )
            elif kind == "CheckConstraint":
                orm_check_count += 1
                # Short, explicitly chosen names survive verbatim and are worth
                # matching; generated long ones are only counted.
                raw = constraint.name
                if raw and len(str(raw)) <= 63 and str(raw) not in db_checks:
                    report.missing_constraints.append(f"{name} CHECK {raw}")

        if db_check_count < orm_check_count:
            report.missing_constraints.append(
                f"{name}: ORM declares {orm_check_count} CHECK constraints, "
                f"database has {db_check_count}"
            )

    return report


def assert_schema_matches(engine: Engine, *, strict_extra: bool = False) -> None:
    """Raise :class:`SchemaDriftError` when the database disagrees with the ORM."""
    report = check_schema(engine, strict_extra=strict_extra)
    if not report.ok:
        raise SchemaDriftError(
            "The database schema does not match the ORM this build expects. "
            "The Alembic revision may still report the current head: a "
            "revision records that a migration ran, never what it did. "
            "Re-create the database from migrations, or add a corrective "
            "migration.",
            details={"problems": report.problems()},
        )


# ---------------------------------------------------------------------------
# Migration file integrity
# ---------------------------------------------------------------------------


def migration_digests() -> dict[str, str]:
    """SHA-256 of every migration file on disk, keyed by filename."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(MIGRATIONS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"migrations": {}}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def check_migration_integrity() -> list[str]:
    """Report migrations whose bytes no longer match the manifest.

    An empty list means every recorded migration is byte-identical to what it
    was when it was recorded. New files not yet in the manifest are reported
    too, so the manifest cannot silently fall behind.
    """
    manifest = load_manifest().get("migrations", {})
    on_disk = migration_digests()
    problems: list[str] = []

    for name, recorded in sorted(manifest.items()):
        actual = on_disk.get(name)
        if actual is None:
            problems.append(f"{name}: recorded in the manifest but missing on disk")
        elif actual != recorded:
            problems.append(
                f"{name}: content changed after it was recorded "
                f"(manifest {recorded[:12]}…, on disk {actual[:12]}…)"
            )

    for name in sorted(set(on_disk) - set(manifest)):
        problems.append(f"{name}: present on disk but absent from the manifest")

    return problems


def write_manifest() -> dict[str, str]:
    """Record the current migration digests. Used when adding a migration."""
    digests = migration_digests()
    MANIFEST_PATH.write_text(
        json.dumps({"migrations": digests}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return digests
