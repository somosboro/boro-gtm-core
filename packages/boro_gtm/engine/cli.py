"""`gtm engine …` — operate the commercial engine from a shell."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import typer

from boro_gtm.core.db import session_scope

app = typer.Typer(help="Commercial engine: source, enrich, contact, follow up, close.",
                  no_args_is_help=True)


def _echo(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


def _engine():
    from boro_gtm.engine.settings import engine_settings
    from boro_gtm.engine.strategy import active_strategy
    from boro_gtm.engine.worker import Engine

    return Engine(active_strategy(), engine_settings())


@app.command("summary")
def summary() -> None:
    """Today's numbers, and the funnel by lane."""
    from boro_gtm.engine import reports
    from boro_gtm.engine.settings import engine_settings
    from boro_gtm.engine.sourcing import pool_status
    from boro_gtm.engine.strategy import active_strategy

    with session_scope() as s:
        _echo({"shadow_mode": engine_settings().shadow_mode,
               "strategy": active_strategy().version,
               "today": reports.today(s, active_strategy()),
               "pool": pool_status(s), "funnel_by_lane": reports.funnel(s)})


@app.command("funnel")
def funnel(by: str = typer.Option("lane", help="lane, country, variant or segment")) -> None:
    from boro_gtm.engine import reports

    with session_scope() as s:
        _echo(reports.funnel(s, by=by))


@app.command("ready")
def ready(limit: int = 50) -> None:
    """READY targets, best first."""
    from boro_gtm.engine import reports

    with session_scope() as s:
        _echo(reports.ready(s, limit))


@app.command("sends")
def sends(days: int = 1) -> None:
    """Messages sent or shadowed in the last N days."""
    from boro_gtm.engine import reports
    from boro_gtm.engine.pipeline import now_utc

    with session_scope() as s:
        _echo(reports.messages(s, statuses=("SENT", "SHADOW", "SENDING", "BLOCKED", "FAILED"),
                               since=now_utc() - timedelta(days=days)))


@app.command("followups")
def followups(days: int = 7) -> None:
    """Scheduled messages due in the next N days."""
    from boro_gtm.engine import reports
    from boro_gtm.engine.pipeline import now_utc

    with session_scope() as s:
        _echo(reports.messages(s, statuses=("SCHEDULED",),
                               due_before=now_utc() + timedelta(days=days)))


@app.command("replies")
def replies(days: int = 14) -> None:
    """Inbound replies matched to targets, with escalations flagged."""
    from boro_gtm.engine import reports

    with session_scope() as s:
        _echo(reports.replies(s, days=days))


@app.command("opportunities")
def opportunities() -> None:
    from boro_gtm.engine import reports

    with session_scope() as s:
        _echo(reports.opportunities(s))


@app.command("health")
def health() -> None:
    """Worker heartbeats, last successful run per task, recent failures."""
    from boro_gtm.engine import reports

    with session_scope() as s:
        result = reports.health(s)
    _echo(result)
    if not result["healthy"]:
        raise typer.Exit(code=1)


@app.command("failed")
def failed() -> None:
    """Recent failed tasks, source runs and messages."""
    from boro_gtm.engine import reports

    with session_scope() as s:
        _echo({"tasks": reports.health(s)["recent_failures"], "sources": [
            r for r in reports.sources(s, 50) if r["status"] == "FAILED"],
            "messages": reports.messages(s, statuses=("FAILED", "BLOCKED"))})


@app.command("sources")
def sources(limit: int = 20) -> None:
    """Recent source runs, including what they could not find and why."""
    from boro_gtm.engine import reports

    with session_scope() as s:
        _echo(reports.sources(s, limit))


@app.command("run")
def run(task: str = typer.Argument(..., help="ingest, reconcile, guard, source, enrich, plan, "
                                            "send, close")) -> None:
    """Run one task now, with the same lock and record the worker uses."""
    from boro_gtm.engine.settings import engine_settings
    from boro_gtm.engine.worker import SCHEDULE, run_task

    if task not in {name for name, _, _ in SCHEDULE}:
        raise typer.BadParameter(f"unknown task {task}")
    _echo(run_task(_engine(), task, worker=f"cli:{engine_settings().worker_name}"))


@app.command("worker")
def worker(once: bool = typer.Option(False, "--once"),
           poll_seconds: int = typer.Option(30),
           tasks: str = typer.Option(None, help="Comma-separated subset of tasks.")) -> None:
    """The scheduler loop. Runs until SIGTERM."""
    from boro_gtm.core.config import get_settings
    from boro_gtm.core.logging import configure_logging
    from boro_gtm.engine.worker import loop

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    loop(once=once, poll_seconds=poll_seconds,
         tasks=frozenset(t.strip() for t in tasks.split(",")) if tasks else None)


@app.command("import-pool")
def import_pool(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Import an external CSV/XLSX pool. Idempotent; nothing in it is trusted."""
    from boro_gtm.engine.importer import import_pool as run_import
    from boro_gtm.engine.strategy import active_strategy

    with session_scope() as s:
        _echo(run_import(s, active_strategy(), path))


@app.command("ingest-history")
def ingest_history(since: str = typer.Option(..., help="YYYY-MM-DD")) -> None:
    """Backfill Gmail history (sent and received) so past contacts are never re-contacted."""
    from boro_gtm.engine import replies as r

    engine = _engine()
    gateway = engine.gateway()
    if gateway is None:
        raise typer.BadParameter("no Gmail credentials (GTM_ENGINE_GMAIL_CREDENTIALS)")
    with session_scope() as s:
        _echo(r.ingest(s, engine.strategy, gateway, since=date.fromisoformat(since),
                       max_messages=20000, apply_effects=False))


@app.command("advance")
def advance(target_id: str, to_state: str, note: str = typer.Option(None),
            amount: float = typer.Option(None), currency: str = typer.Option(None)) -> None:
    """Record a human pipeline move: CONVERSATION, OPPORTUNITY, PROPOSAL, WON or LOST."""
    from boro_gtm.engine.actions import advance as do_advance

    with session_scope() as s:
        _echo(do_advance(s, uuid.UUID(target_id), to_state.upper(), actor="cli", note=note,
                         amount=Decimal(str(amount)) if amount is not None else None,
                         currency=currency))


@app.command("suppress")
def suppress(value: str) -> None:
    """Never write to this address (or @domain) again."""
    from boro_gtm.engine.actions import manual_suppress

    with session_scope() as s:
        _echo({"added": manual_suppress(s, value.lstrip("@"))})


@app.command("pause")
def pause(reason: str = typer.Argument(...)) -> None:
    from boro_gtm.engine.outreach import set_pause

    with session_scope() as s:
        set_pause(s, paused_=True, reason=reason)
    _echo({"paused": True})


@app.command("resume")
def resume() -> None:
    from boro_gtm.engine.outreach import set_pause

    with session_scope() as s:
        set_pause(s, paused_=False, reason="resumed from cli")
    _echo({"paused": False})


@app.command("cutover-report")
def cutover_report() -> None:
    """Everything that must be true before SHADOW_MODE is switched off."""
    from boro_gtm.engine.cutover import report

    with session_scope() as s:
        result = report(s)
    _echo(result)
    if not result["ready_for_live"]:
        raise typer.Exit(code=2)


@app.command("gmail-auth-url")
def gmail_auth_url(client_id: str = typer.Option(...),
                   redirect_uri: str = typer.Option("http://127.0.0.1:8765/")) -> None:
    """Print the consent URL for the corporate mailbox (one-time, human step)."""
    from urllib.parse import urlencode

    from boro_gtm.engine.gmail import SCOPES

    typer.echo("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent",
        "login_hint": "abourgeoisroa@somosboro.com"}))


@app.command("gmail-auth-exchange")
def gmail_auth_exchange(code: str = typer.Option(..., prompt=True, hide_input=True),
                        client_secret_file: Path = typer.Option(..., exists=True),
                        out: Path = typer.Option(...),
                        redirect_uri: str = typer.Option("http://127.0.0.1:8765/")) -> None:
    """Exchange the consent code for a refresh token, written to OUT with mode 600."""
    import os

    import httpx

    from boro_gtm.engine.gmail import TOKEN_URL

    client = json.loads(client_secret_file.read_text())
    client = client.get("installed") or client.get("web") or client
    response = httpx.post(TOKEN_URL, data={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    if response.status_code != 200 or "refresh_token" not in response.json():
        raise typer.BadParameter(f"exchange failed (HTTP {response.status_code})")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"client_id": client["client_id"], "client_secret": client["client_secret"],
                   "refresh_token": response.json()["refresh_token"]}, handle)
    _echo({"written": str(out), "mode": "600"})
