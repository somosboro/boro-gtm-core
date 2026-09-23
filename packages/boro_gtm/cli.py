"""Command line interface.

    python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json
    python -m boro_gtm.cli market-intelligence recalculate \
        --snapshot MI-2026-09-21-V1 --model market-attractiveness:1.0
    python -m boro_gtm.cli strategy seed
    python -m boro_gtm.cli discovery seed
    python -m boro_gtm.cli discovery run --provider fixture_json_directory \
        --fixture ./data/fixtures/discovery_sample.json
    python -m boro_gtm.cli discovery project
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from boro_gtm.core.config import get_settings
from boro_gtm.core.db import session_scope
from boro_gtm.core.enums import ScoreRunKind
from boro_gtm.core.errors import GtmError, NotFoundError
from boro_gtm.core.logging import configure_logging

app = typer.Typer(help="BoRo GTM Core — working codename", no_args_is_help=True)
mi_app = typer.Typer(help="Market intelligence commands", no_args_is_help=True)
strategy_app = typer.Typer(help="Strategy registry commands", no_args_is_help=True)
discovery_app = typer.Typer(help="Company discovery commands", no_args_is_help=True)
db_app = typer.Typer(help="Database health commands", no_args_is_help=True)
app.add_typer(mi_app, name="market-intelligence")
app.add_typer(strategy_app, name="strategy")
app.add_typer(discovery_app, name="discovery")
app.add_typer(db_app, name="db")


def _bootstrap() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)


def _echo(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


@mi_app.command("import")
def import_snapshot(
    path: Path = typer.Argument(..., exists=True, readable=True, help="Source JSON file"),
    recalculate: bool = typer.Option(
        True, help="Also create reference-reproduction and native runs after import."
    ),
    show_warnings: bool = typer.Option(False, help="Print all import warnings."),
) -> None:
    """Import a market-intelligence snapshot (idempotent, transactional)."""
    _bootstrap()
    from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
    from boro_gtm.market_intelligence.services import scoring_service

    try:
        with session_scope() as session:
            summary = SnapshotImporter(session).import_file(path)
            payload = summary.to_dict()

            if recalculate and summary.created:
                repro = scoring_service.create_base_score_run(
                    session, summary.snapshot_key,
                    ScoreRunKind.REFERENCE_REPRODUCTION.value,
                )
                native = scoring_service.create_base_score_run(
                    session, summary.snapshot_key,
                    ScoreRunKind.NATIVE_RECALCULATION.value,
                )
                session.flush()
                comparison = scoring_service.compare_runs(
                    session, summary.reference_score_run_id, repro.id
                )
                payload["reference_reproduction_run_id"] = str(repro.id)
                payload["native_recalculation_run_id"] = str(native.id)
                payload["max_score_delta"] = comparison["max_score_delta"]
                payload["rank_mismatches"] = len(comparison["rank_mismatches"])

            if not show_warnings:
                payload["warnings"] = f"{len(summary.warnings)} warnings (use --show-warnings)"
            _echo(payload)
    except GtmError as exc:
        typer.echo(json.dumps(exc.to_payload(), indent=2), err=True)
        raise typer.Exit(code=1) from exc


@mi_app.command("recalculate")
def recalculate(
    snapshot: str = typer.Option(..., help="Snapshot key, e.g. MI-2026-09-21-V1"),
    model: str = typer.Option("market-attractiveness:1.0", help="key:version"),
    mode: str = typer.Option(
        ScoreRunKind.REFERENCE_REPRODUCTION.value,
        help="reference_reproduction | native_recalculation",
    ),
) -> None:
    """Create a new base score run for an existing snapshot."""
    _bootstrap()
    from boro_gtm.market_intelligence.services import scoring_service

    model_key, _, model_version = model.partition(":")
    try:
        with session_scope() as session:
            run = scoring_service.create_base_score_run(
                session, snapshot, mode, model_key, model_version or "1.0"
            )
            session.flush()
            payload = {
                "score_run_id": str(run.id),
                "snapshot_key": snapshot,
                "model": f"{model_key}:{model_version}",
                "mode": mode,
                "status": run.status,
            }
            reference = scoring_service.get_snapshot(session, snapshot)
            from sqlalchemy import select

            from boro_gtm.market_intelligence.domain.models import ScoreRun

            imported = session.scalar(
                select(ScoreRun).where(
                    ScoreRun.snapshot_id == reference.id,
                    ScoreRun.kind == ScoreRunKind.IMPORTED_REFERENCE.value,
                )
            )
            if imported is not None:
                payload["comparison_vs_imported_reference"] = scoring_service.compare_runs(
                    session, imported.id, run.id
                )
            _echo(payload)
    except GtmError as exc:
        typer.echo(json.dumps(exc.to_payload(), indent=2), err=True)
        raise typer.Exit(code=1) from exc


@strategy_app.command("seed")
def seed_strategy() -> None:
    """Seed verticals, ICPs, offers, channels and market x vertical profiles."""
    _bootstrap()
    from boro_gtm.strategy.seeds.loader import seed_all

    with session_scope() as session:
        result = seed_all(session)
    _echo(result)


@app.command("version")
def version() -> None:
    from boro_gtm import __version__

    _echo({"name": get_settings().app_name, "version": __version__})



# ---------------------------------------------------------------------------
# Discovery (M2)
# ---------------------------------------------------------------------------


@discovery_app.command("seed")
def discovery_seed() -> None:
    """Seed the attribute registry and the fixture providers (idempotent)."""
    _bootstrap()
    from boro_gtm.discovery import seeds

    with session_scope() as session:
        summary = seeds.seed_all(session)
    _echo(summary)


@discovery_app.command("run")
def discovery_run(
    provider: str = typer.Option(..., help="Provider key, e.g. fixture_json_directory"),
    fixture: Path = typer.Option(
        ..., exists=True, readable=True,
        help="JSON file holding the records this fixture provider will return.",
    ),
    market: str | None = typer.Option(None, help="M1 market ISO2 code for context."),
    allow_partial: bool = typer.Option(
        False, help="Permit canonical writes from a run that never completed its fetch."
    ),
) -> None:
    """Fetch, normalize and resolve one run against a fixture provider.

    No production provider exists, so this only ever reads a local file. The
    network is never touched.
    """
    _bootstrap()
    from sqlalchemy import select

    from boro_gtm.discovery.domain.models import DiscoveryProvider
    from boro_gtm.discovery.providers.base import RawRecord
    from boro_gtm.discovery.providers.fixtures import FIXTURE_ADAPTERS
    from boro_gtm.discovery.services import runs
    from boro_gtm.market_intelligence.domain.models import Market

    adapter_cls = FIXTURE_ADAPTERS.get(provider)
    if adapter_cls is None:
        raise typer.BadParameter(
            f"Unknown fixture provider {provider!r}. "
            f"Known: {', '.join(sorted(FIXTURE_ADAPTERS))}"
        )

    payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
    records = [
        RawRecord(
            body=json.dumps(item["payload"]).encode("utf-8"),
            content_type=item.get("content_type", "application/json"),
            native_external_id=item.get("external_id"),
        )
        for item in payload
    ]

    try:
        with session_scope() as session:
            row = session.scalar(
                select(DiscoveryProvider).where(
                    DiscoveryProvider.provider_key == provider)
            )
            if row is None:
                raise NotFoundError(
                    f"Provider {provider!r} is not seeded. Run: discovery seed",
                    details={"provider_key": provider},
                )
            market_id = None
            if market:
                market_id = session.scalar(
                    select(Market.id).where(Market.iso2 == market.upper()))
                if market_id is None:
                    raise NotFoundError(
                        f"No M1 market with ISO-3166-1 alpha-2 code {market!r}. "
                        "Import a snapshot first, or check the code.",
                        details={"iso2": market.upper()},
                    )

            adapter = adapter_cls(records)
            run = runs.create_run(session, row, market_id=market_id,
                                  allow_partial_resolution=allow_partial)
            report = runs.fetch(session, run, adapter, {"source": str(fixture)})
            normalized = runs.normalize_run(session, run, adapter)
            stats = runs.resolve_run(session, run, adapter)
            _echo({
                "run_id": str(run.id), "status": run.status,
                "records": report.records,
                "versions_created": report.versions_created,
                "bodies_created": report.bodies_created,
                "record_errors": report.record_errors,
                "normalization_errors": report.normalization_errors,
                "normalizations_added": normalized,
                **stats,
            })
    except GtmError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@discovery_app.command("project")
def discovery_project(
    triggered_by: str = typer.Option("cli", help="Recorded on the projection run."),
) -> None:
    """Rebuild every derived projection from evidence."""
    _bootstrap()
    from boro_gtm.discovery.services.projection import rebuild_projections

    with session_scope() as session:
        run = rebuild_projections(session, triggered_by=triggered_by)
        _echo({
            "projection_run_id": str(run.id),
            "row_counts": run.row_counts,
            "content_digests": run.content_digests,
        })


@discovery_app.command("firewall")
def discovery_firewall() -> None:
    """Print the M1 write fingerprint — row counts of every protected table."""
    _bootstrap()
    from boro_gtm.discovery.services.firewall import m1_write_fingerprint

    with session_scope() as session:
        _echo(m1_write_fingerprint(session))


@db_app.command("check")
def db_check(
    strict: bool = typer.Option(
        False, help="Also report columns the database has and the ORM does not."
    ),
) -> None:
    """Verify the database schema matches the ORM this build expects.

    An Alembic revision records *that* a migration ran, never what it did, so
    a database can report the current head while its physical schema differs
    from the migration as it now ships. This is the check that catches that.
    """
    _bootstrap()
    from boro_gtm.core.db import get_engine
    from boro_gtm.core.schema_check import check_migration_integrity, check_schema

    report = check_schema(get_engine(), strict_extra=strict)
    integrity = check_migration_integrity()
    payload = {
        "schema_matches_orm": report.ok,
        "problems": report.problems(),
        "migration_files_intact": not integrity,
        "migration_problems": integrity,
    }
    _echo(payload)
    if not report.ok or integrity:
        raise typer.Exit(code=1)


@db_app.command("record-migrations")
def db_record_migrations() -> None:
    """Record the current migration digests in migrations/MANIFEST.json.

    Run this when adding a migration. Running it to silence a failure on an
    *existing* migration would defeat the guard, so the message says so.
    """
    _bootstrap()
    from boro_gtm.core.schema_check import check_migration_integrity, write_manifest

    before = check_migration_integrity()
    changed = [p for p in before if "content changed" in p]
    if changed:
        typer.echo(
            "Refusing to re-record: these migrations changed after they were "
            "first recorded, which is the condition the manifest exists to "
            "detect. Add a corrective migration instead, or pass them through "
            "review deliberately.",
            err=True,
        )
        for problem in changed:
            typer.echo(f"  {problem}", err=True)
        raise typer.Exit(code=1)
    _echo({"recorded": sorted(write_manifest())})


if __name__ == "__main__":
    app()
