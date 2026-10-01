"""Command line interface.

    python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json
    python -m boro_gtm.cli market-intelligence recalculate \
        --snapshot MI-2026-09-21-V1 --model market-attractiveness:1.0
    python -m boro_gtm.cli strategy seed
    python -m boro_gtm.cli discovery seed
    python -m boro_gtm.cli discovery run --provider fixture_json_directory \
        --fixture ./data/fixtures/discovery_sample.json
    python -m boro_gtm.cli discovery project
    python -m boro_gtm.cli research seed
    python -m boro_gtm.cli research ask --company <uuid>
    python -m boro_gtm.cli research run --run <uuid> --fixture-corpus
    python -m boro_gtm.cli research show --company <uuid>
    python -m boro_gtm.cli research coverage --run <uuid>
    python -m boro_gtm.cli research gaps --run <uuid>
    python -m boro_gtm.cli research signals
    python -m boro_gtm.cli research project --company <uuid>
    python -m boro_gtm.cli research prune --as-of 2027-01-01          # dry run
    python -m boro_gtm.cli research prune --as-of 2027-01-01 --execute
"""

from __future__ import annotations

import json
from dataclasses import replace
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
research_app = typer.Typer(
    help="Operational evidence commands (M3)", no_args_is_help=True
)
db_app = typer.Typer(help="Database health commands", no_args_is_help=True)
app.add_typer(mi_app, name="market-intelligence")
app.add_typer(strategy_app, name="strategy")
app.add_typer(discovery_app, name="discovery")
app.add_typer(research_app, name="research")
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

    Reads a local file and never touches the network. Live discovery is a
    separate command — `discovery plan-live` and `discovery run-live` — so no
    invocation of this one can reach the internet by accident.
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


@discovery_app.command("plan-live")
def discovery_plan_live(
    metros: str = typer.Option(
        "smoke", help="`smoke` (2), `phase-b` (10), `all` (25), or a comma-separated "
                      "list of metro keys.",
    ),
    intents: str = typer.Option(None, help="Comma-separated subset of the query intents."),
    max_queries: int = typer.Option(
        None, help="Hard cap on provider requests. Default: exactly one page of "
                   "every planned step (breadth, no pagination).",
    ),
    max_results: int = typer.Option(1000, help="Hard cap on records stored."),
) -> None:
    """Cost a live discovery plan. Touches no network and writes nothing.

    Run this before `run-live`: it prints the provider, the context, the metros,
    the intents and the planned request count, so nobody starts paid API spend
    without seeing the bill first (§8).
    """
    from boro_gtm.discovery.live.plan import (
        METROS,
        PHASE_B_METROS,
        QUERY_INTENTS,
        SMOKE_METROS,
        QueryBudget,
        plan_run,
    )

    keys = _metro_keys(metros, SMOKE_METROS, PHASE_B_METROS, METROS)
    chosen = tuple(i.strip() for i in intents.split(",")) if intents else QUERY_INTENTS
    unknown = [i for i in chosen if i not in QUERY_INTENTS]
    if unknown:
        raise typer.BadParameter(
            f"unknown intents {unknown}; known: {list(QUERY_INTENTS)}"
        )
    planned = plan_run(
        metros=keys, intents=chosen,
        budget=QueryBudget(max_queries=max_queries, max_results=max_results),
    )
    payload = planned.as_dict()
    payload["credential_present"] = _places_credential_present()
    payload["network"] = "none — planning only"
    _echo(payload)


@discovery_app.command("run-live")
def discovery_run_live(
    metros: str = typer.Option("smoke", help="`smoke`, `phase-b`, `all`, or metro keys."),
    intents: str = typer.Option(None, help="Comma-separated subset of the query intents."),
    max_queries: int = typer.Option(
        None, help="Hard cap on provider requests. Default: exactly one page of "
                   "every planned step (breadth, no pagination).",
    ),
    max_results: int = typer.Option(1000, help="Hard cap on records stored."),
    market: str | None = typer.Option(None, help="M1 market ISO2 code for context."),
    vertical: str | None = typer.Option(None, help="M1 vertical key for context."),
    yes: bool = typer.Option(
        False, "--yes", help="Required. Confirms real, billed provider requests.",
    ),
) -> None:
    """Discover real companies through the Google Places API. Spends money.

    `--yes` is mandatory and not a default, and the credential is checked before
    any request is made, so a missing key fails clearly rather than half-way
    through a plan.

    There is no partial-resolution option on the live path. A provider or network
    failure leaves the run `PARTIAL_FETCH` with its evidence retained and nothing
    canonical written, which is the safe production behaviour; the fixture path
    keeps its own `--allow-partial` for internal M2 work (M2-ADR-052).
    """
    _bootstrap()
    from sqlalchemy import select

    from boro_gtm.discovery.domain.models import DiscoveryProvider
    from boro_gtm.discovery.live.plan import (
        METROS,
        PHASE_B_METROS,
        QUERY_INTENTS,
        SMOKE_METROS,
        QueryBudget,
        plan_run,
    )
    from boro_gtm.discovery.live.runner import preflight_credential, run_live_discovery
    from boro_gtm.discovery.providers.google_places import (
        PROVIDER_KEY,
        GooglePlacesAdapter,
    )
    from boro_gtm.market_intelligence.domain.models import Market

    if not yes:
        raise typer.BadParameter(
            "pass --yes. This issues real, billed Google Places requests; run "
            "`discovery plan-live` first to see the planned request count."
        )

    keys = _metro_keys(metros, SMOKE_METROS, PHASE_B_METROS, METROS)
    chosen = tuple(i.strip() for i in intents.split(",")) if intents else QUERY_INTENTS
    planned = plan_run(
        metros=keys, intents=chosen,
        budget=QueryBudget(max_queries=max_queries, max_results=max_results),
    )

    try:
        # Before the network, before the database, before anything is spent.
        preflight_credential()
    except GtmError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    import httpx

    try:
        with session_scope() as session:
            provider_row = session.scalar(
                select(DiscoveryProvider).where(
                    DiscoveryProvider.provider_key == PROVIDER_KEY)
            )
            if provider_row is None:
                raise NotFoundError(
                    f"Provider {PROVIDER_KEY!r} is not seeded. Run: discovery seed",
                    details={"provider_key": PROVIDER_KEY},
                )
            market_id = None
            if market:
                market_id = session.scalar(
                    select(Market.id).where(Market.iso2 == market.upper()))
                if market_id is None:
                    raise NotFoundError(
                        f"No M1 market with code {market!r}.",
                        details={"iso2": market.upper()},
                    )
            vertical_id = _vertical_id(session, vertical) if vertical else None

            with httpx.Client(timeout=30.0) as client:
                adapter = GooglePlacesAdapter(client=client)
                report = run_live_discovery(
                    session, provider=provider_row, adapter=adapter,
                    planned=planned, market_id=market_id, vertical_id=vertical_id,
                )
            payload = report.as_dict()
            payload["plan"] = planned.as_dict()
            _echo(payload)
    except GtmError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@discovery_app.command("evaluate-holdout")
def discovery_evaluate_holdout(
    path: Path = typer.Argument(..., exists=True, readable=True,
                                help="CSV: company_name,canonical_domain"),
) -> None:
    """Score the discovered set against known accounts. Read-only, no network.

    The holdout is evaluation data. It is never loaded into M2 and never reaches
    the provider, so a run cannot score itself (§15).
    """
    _bootstrap()
    from boro_gtm.discovery.live.holdout import evaluate, read_holdout_csv

    rows, problems = read_holdout_csv(path)
    with session_scope() as session:
        result = evaluate(session, rows)
        session.rollback()
        payload = result.as_dict()
        payload["parse_problems"] = [{"row": r, "reason": w} for r, w in problems]
        _echo(payload)


def _metro_keys(spec: str, smoke, phase_b, all_metros) -> tuple[str, ...]:
    """`smoke` / `phase-b` / `all`, or an explicit list. Phases are prefixes."""
    named = {"smoke": smoke, "phase-b": phase_b,
             "all": tuple(m.key for m in all_metros)}
    if spec in named:
        return named[spec]
    return tuple(k.strip() for k in spec.split(",") if k.strip())


def _places_credential_present() -> bool:
    """Whether a key exists. The value is never read out or logged."""
    import os

    from boro_gtm.discovery.providers.google_places import API_KEY_ENV

    return bool(os.environ.get(API_KEY_ENV, "").strip())


def _vertical_id(session, key: str):
    from sqlalchemy import select

    from boro_gtm.market_intelligence.domain.models import Vertical

    found = session.scalar(select(Vertical.id).where(Vertical.key == key))
    if found is None:
        raise NotFoundError(f"No M1 vertical with key {key!r}.", details={"key": key})
    return found


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



# ---------------------------------------------------------------------------
# M3 — operational evidence
# ---------------------------------------------------------------------------


@research_app.command("seed")
def research_seed() -> None:
    """Seed the M3 operational attribute registry (idempotent)."""
    _bootstrap()
    from boro_gtm.research.seeds import seed_all

    with session_scope() as session:
        _echo(seed_all(session))


@research_app.command("ask")
def research_ask(
    company: str = typer.Option(..., help="Canonical company id."),
    vertical: str | None = typer.Option(None, help="Vertical id, if the plan has one."),
    attribute: list[str] = typer.Option(
        None, "--attribute", help="Target attribute key; repeatable. "
                                 "Defaults to every required attribute."
    ),
) -> None:
    """Create or reuse a research question.

    Asking the same question twice reuses it: the plan hash covers every input
    that defines the question, so a second ask is not a second question.
    """
    _bootstrap()
    import uuid as _uuid

    from boro_gtm.research.registry import DEFAULT_TARGET_ATTRIBUTES
    from boro_gtm.research.services.application import create_or_reuse_run

    with session_scope() as session:
        view, created = create_or_reuse_run(
            session,
            company_id=_uuid.UUID(company),
            vertical_id=_uuid.UUID(vertical) if vertical else None,
            target_attribute_keys=tuple(attribute or DEFAULT_TARGET_ATTRIBUTES),
            created_by="cli",
        )
        _echo({
            "run_id": str(view.id), "created": created,
            "research_plan_hash": view.research_plan_hash,
            "target_attribute_keys": view.target_attribute_keys,
        })


@research_app.command("load-cohort")
def research_load_cohort(
    path: Path = typer.Argument(..., exists=True, readable=True,
                                help="CSV: company_name,canonical_domain,website_url,source_id"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Report what would be loaded, write nothing."),
) -> None:
    """Load an operator's target list as canonical M2 companies. No research.

    Loading and researching are separate actions on purpose, so a bad list is
    discovered before anything reaches the internet.

    A row with no `canonical_domain` and no `website_url` is **flagged for
    identity review, not researched**. A company name is not an address: two
    contractors called "Allied Mechanical" are two companies, and guessing which
    site belongs to which produces wrong-account evidence that looks exactly like
    correct evidence.
    """
    _bootstrap()
    from datetime import UTC, datetime

    from boro_gtm.research.live.cohort import load_cohort, read_cohort_csv

    rows, problems = read_cohort_csv(path)
    with session_scope() as session:
        report = load_cohort(session, rows, now=datetime.now(UTC))
        payload = report.as_dict()
        payload["parse_problems"] = [{"row": r, "reason": why} for r, why in problems]
        payload["rows_read"] = len(rows)
        if dry_run:
            session.rollback()
            payload["dry_run"] = True
        _echo(payload)


@research_app.command("run")
def research_run(
    run: str = typer.Option(
        None, help="Research question id to execute. Fixture mode only."
    ),
    company: str = typer.Option(
        None, help="Canonical M2 company to research. Live mode only."
    ),
    fixture_corpus: bool = typer.Option(
        False, "--fixture-corpus",
        help="Read the deterministic fixture corpus. No network.",
    ),
    live: bool = typer.Option(
        False, "--live",
        help="Fetch the company's real public website. Reaches the internet.",
    ),
    seed_url: list[str] = typer.Option(
        None, "--seed-url",
        help="Extra first-party URL to research. Live mode only; must be inside "
             "the company's M2 domain scope.",
    ),
    max_pages: int = typer.Option(None, help="Live mode: documents to retrieve."),
    max_retrievals: int = typer.Option(None, help="Live mode: total requests."),
    conditional: bool = typer.Option(
        True, help="Send If-None-Match from the last successful retrieval."
    ),
) -> None:
    """Execute one research attempt: against the fixture corpus, or for real.

    Exactly one of ``--fixture-corpus`` and ``--live`` is required, and neither
    is a default. There is no way to run this and be unsure afterwards whether
    it touched the internet, which was the point of the original mandatory
    ``--fixture-corpus`` flag and is more important now that live mode exists.

    ``--live`` takes ``--company`` because live research starts from the M2
    company whose domain scope authorises it. Fixture mode takes ``--run``
    because the corpus has no company of its own.
    """
    _bootstrap()
    import uuid as _uuid

    if fixture_corpus == live:
        raise typer.BadParameter(
            "pass exactly one of --fixture-corpus or --live. --fixture-corpus "
            "reads local files and touches no network; --live fetches the "
            "company's real website. Neither is a default, so the output can "
            "never be mistaken for the other."
        )

    if fixture_corpus:
        if not run or company:
            raise typer.BadParameter(
                "--fixture-corpus takes --run (the corpus has no company of "
                "its own); --company belongs to --live."
            )
        from boro_gtm.research.services.application import execute_attempt

        with session_scope() as session:
            view = execute_attempt(
                session, run_id=_uuid.UUID(run), conditional=conditional
            )
            _echo({
                "attempt_id": str(view.id), "attempt_number": view.attempt_number,
                "status": view.status, "error": view.error,
                "source": "fixture-corpus",
            })
        return

    if not company or run:
        raise typer.BadParameter(
            "--live takes --company: real research is authorised by the M2 "
            "company's domain scope, not by a research question id."
        )
    from boro_gtm.research.live.policy import CrawlBudget
    from boro_gtm.research.live.runner import research_company_live

    budget = CrawlBudget()
    if max_pages is not None:
        budget = replace(budget, max_pages=max_pages)
    if max_retrievals is not None:
        budget = replace(budget, max_retrievals=max_retrievals)

    with session_scope() as session:
        report = research_company_live(
            session, company_id=_uuid.UUID(company), budget=budget,
            human_seed_urls=tuple(seed_url or ()), conditional=conditional,
        )
        payload = report.as_dict()
        payload["source"] = "live-first-party-web"
        _echo(payload)


@research_app.command("retry")
def research_retry(
    attempt: str = typer.Option(..., help="Terminal attempt to retry as n+1."),
    fixture_corpus: bool = typer.Option(False, "--fixture-corpus"),
) -> None:
    """Retry a terminal attempt. It never reopens one."""
    _bootstrap()
    import uuid as _uuid

    if not fixture_corpus:
        raise typer.BadParameter("pass --fixture-corpus; see `research run`.")
    from boro_gtm.research.services.application import retry_attempt

    with session_scope() as session:
        view = retry_attempt(session, _uuid.UUID(attempt))
        _echo({
            "attempt_id": str(view.id), "attempt_number": view.attempt_number,
            "status": view.status,
        })


@research_app.command("show")
def research_show(
    company: str = typer.Option(..., help="Canonical company id."),
    attribute: str | None = typer.Option(None, help="Show one attribute only."),
) -> None:
    """Company-global operational knowledge, with read-time staleness."""
    _bootstrap()
    import uuid as _uuid
    from datetime import UTC, datetime

    from boro_gtm.research.domain.models import OperationalResearchProfile
    from boro_gtm.research.services.staleness import company_staleness

    company_id = _uuid.UUID(company)
    with session_scope() as session:
        profile = session.get(OperationalResearchProfile, company_id)
        if profile is None:
            raise NotFoundError(f"no operational research profile for {company_id}")
        verdicts = company_staleness(session, company_id, datetime.now(UTC).date())
        facts = profile.facts or {}
        keys = [attribute] if attribute else sorted(facts)
        _echo({
            "company_id": company,
            "assertion_policy_version": profile.assertion_policy_version,
            "publisher_policy_version": profile.publisher_policy_version,
            "attributes": {
                key: {
                    "envelope": facts.get(key, {}).get("envelope"),
                    "best": facts.get(key, {}).get("best"),
                    "fact_type": facts.get(key, {}).get("best_fact_type"),
                    "confidence": facts.get(key, {}).get("confidence"),
                    "source_published_at": facts.get(key, {}).get(
                        "source_published_at"),
                    "observed_at": facts.get(key, {}).get("observed_at"),
                    "contradiction": (profile.contradictions or {})
                        .get(key, {}).get("contradiction", False),
                    "corroborating_publishers": (
                        profile.corroborating_publisher_counts or {}
                    ).get(key, 0),
                    "staleness": verdicts[key].state if key in verdicts else None,
                }
                for key in keys if key in facts
            },
        })


@research_app.command("coverage")
def research_coverage(run: str = typer.Option(..., help="Research question id.")) -> None:
    """Coverage, confidence and contradiction rate for one question.

    Three separate numbers. Collapsing them would produce something that reads
    like a score, and M3 does not score.
    """
    _bootstrap()
    import uuid as _uuid

    from boro_gtm.research.domain.models import OperationalResearchPlanProfile

    with session_scope() as session:
        profile = session.get(OperationalResearchPlanProfile, _uuid.UUID(run))
        if profile is None:
            raise NotFoundError(f"no plan profile for research run {run}")
        _echo({
            "run_id": run,
            "coverage": float(profile.coverage),
            "confidence_summary": (
                float(profile.confidence_summary)
                if profile.confidence_summary is not None else None
            ),
            "contradiction_rate": float(profile.contradiction_rate),
            "required_attribute_count": profile.required_attribute_count,
            "covered_attribute_count": profile.covered_attribute_count,
            "not_applicable_attribute_count": profile.not_applicable_attribute_count,
            "open_gap_count": profile.open_gap_count,
        })


@research_app.command("gaps")
def research_gaps(
    run: str = typer.Option(..., help="Research question id."),
    kind: str | None = typer.Option(None, help="Filter by gap kind."),
) -> None:
    """What this question looked for and has not found."""
    _bootstrap()
    import uuid as _uuid

    from sqlalchemy import select

    from boro_gtm.research.domain.models import OperationalResearchGap
    from boro_gtm.research.services.gaps import attempt_count, current_status

    with session_scope() as session:
        statement = select(OperationalResearchGap).where(
            OperationalResearchGap.run_id == _uuid.UUID(run)
        )
        if kind:
            statement = statement.where(OperationalResearchGap.gap_kind == kind.upper())
        rows = session.scalars(
            statement.order_by(
                OperationalResearchGap.gap_kind, OperationalResearchGap.attribute_key
            )
        ).all()
        _echo([
            {
                "attribute_key": row.attribute_key, "gap_kind": row.gap_kind,
                "status": current_status(session, row.id),
                "attempts": attempt_count(session, row.id),
            }
            for row in rows
        ])


@research_app.command("signals")
def research_signals(
    company: str | None = typer.Option(None, help="Filter by company."),
    open_only: bool = typer.Option(True, help="Only concerns with an open episode."),
) -> None:
    """The identity-review queue. M3-owned; nothing here writes M2 identity."""
    _bootstrap()
    import uuid as _uuid

    from sqlalchemy import select

    from boro_gtm.research.domain.models import (
        IdentityReviewSignal,
        IdentityReviewSignalOccurrence,
    )
    from boro_gtm.research.services.review import occurrence_status

    with session_scope() as session:
        statement = select(IdentityReviewSignal)
        if company:
            statement = statement.where(
                IdentityReviewSignal.company_id == _uuid.UUID(company)
            )
        if open_only:
            statement = statement.where(IdentityReviewSignal.id.in_(
                select(IdentityReviewSignalOccurrence.signal_id).where(
                    IdentityReviewSignalOccurrence.is_open.is_(True)
                )
            ))
        rows = session.scalars(
            statement.order_by(IdentityReviewSignal.first_raised_at)
        ).all()
        out = []
        for signal in rows:
            occurrences = session.scalars(
                select(IdentityReviewSignalOccurrence).where(
                    IdentityReviewSignalOccurrence.signal_id == signal.id
                ).order_by(IdentityReviewSignalOccurrence.occurrence_number)
            ).all()
            out.append({
                "signal_id": str(signal.id),
                "kind": signal.signal_kind,
                "concern": signal.normalized_concern,
                "occurrences": [
                    {
                        "number": o.occurrence_number, "open": o.is_open,
                        "status": occurrence_status(session, o.id),
                    }
                    for o in occurrences
                ],
            })
        _echo(out)


@research_app.command("project")
def research_project(
    company: str = typer.Option(..., help="Canonical company id."),
    run: str | None = typer.Option(None, help="Also rebuild this question's profile."),
) -> None:
    """Rebuild the projections from the claim ledger.

    Derived and idempotent: truncating them and rebuilding reproduces the same
    result, which is why this is safe to run at any time.
    """
    _bootstrap()
    import uuid as _uuid

    from boro_gtm.research.services.application import rebuild_profiles

    with session_scope() as session:
        _echo(rebuild_profiles(
            session, company_id=_uuid.UUID(company),
            run_id=_uuid.UUID(run) if run else None,
        ))


@research_app.command("prune")
def research_prune(
    as_of: str = typer.Option(
        ..., help="Date to measure retention horizons against, ISO-8601."
    ),
    execute: bool = typer.Option(
        False, "--execute",
        help="Actually prune. Without this the command only reports counts.",
    ),
    body_days: int | None = typer.Option(
        None, help="Override the raw-body horizon for every content type."
    ),
) -> None:
    """Prune retained payloads. **Dry run unless --execute is passed.**

    Pruning removes payloads and never provenance: hashes, quotes, sources,
    fetch history and claims all survive. The selection is deterministic, so the
    dry run reports exactly what an execute would touch.
    """
    _bootstrap()
    from datetime import UTC, datetime

    from boro_gtm.research.services.retention import apply_retention, plan_retention

    moment = datetime.fromisoformat(as_of)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    settings = get_settings()
    with session_scope() as session:
        plan = plan_retention(session, as_of=moment, body_days=body_days)
        payload = {
            "database": settings.database_url.rsplit("/", 1)[-1],
            "as_of": moment.isoformat(),
            "dry_run": not execute,
            **plan.as_counts(),
            "total": plan.total,
        }
        if execute:
            apply_retention(session, plan, as_of=moment)
            payload["pruned"] = True
        _echo(payload)


@research_app.command("worker")
def research_worker(
    fixture_corpus: bool = typer.Option(False, "--fixture-corpus"),
    max_jobs: int = typer.Option(1, help="How many queued jobs to claim and run."),
) -> None:
    """Claim and run queued M3 jobs from the existing PostgreSQL queue."""
    _bootstrap()
    if not fixture_corpus:
        raise typer.BadParameter("pass --fixture-corpus; see `research run`.")
    from boro_gtm.research.services.application import run_worker_once

    done = []
    with session_scope() as session:
        for _ in range(max_jobs):
            outcome = run_worker_once(session)
            if outcome is None:
                break
            done.append(outcome)
    _echo({"jobs_run": len(done), "outcomes": done})

if __name__ == "__main__":
    app()
