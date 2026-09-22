"""Command line interface.

    python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json
    python -m boro_gtm.cli market-intelligence recalculate \
        --snapshot MI-2026-09-21-V1 --model market-attractiveness:1.0
    python -m boro_gtm.cli strategy seed
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from boro_gtm.core.config import get_settings
from boro_gtm.core.db import session_scope
from boro_gtm.core.enums import ScoreRunKind
from boro_gtm.core.errors import GtmError
from boro_gtm.core.logging import configure_logging

app = typer.Typer(help="BoRo GTM Core — working codename", no_args_is_help=True)
mi_app = typer.Typer(help="Market intelligence commands", no_args_is_help=True)
strategy_app = typer.Typer(help="Strategy registry commands", no_args_is_help=True)
app.add_typer(mi_app, name="market-intelligence")
app.add_typer(strategy_app, name="strategy")


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


if __name__ == "__main__":
    app()
