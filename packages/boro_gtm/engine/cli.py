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


@app.command("import-status")
def import_status(source_key: str = typer.Argument(..., help="e.g. import:BoRo_Week1_…")) -> None:
    """Where every row of an import stands now: READY, ENRICH, BLOCKED, CONTACTED…"""
    from boro_gtm.engine.importer import import_status as status

    with session_scope() as s:
        _echo(status(s, source_key))


@app.command("manual")
def manual() -> None:
    """Platform applications a person must make (Upwork, LinkedIn, ATS), and manual sources."""
    from sqlalchemy import select

    from boro_gtm.engine.models import CeSignal
    from boro_gtm.engine.sources.manual import MANUAL_SOURCES

    with session_scope() as s:
        rows = s.scalars(select(CeSignal).where(
            CeSignal.attributes["channel"].astext == "PLATFORM_APPLICATION")
            .order_by(CeSignal.observed_at.desc()).limit(200))
        _echo({"applications": [{"lane": r.lane, "company": r.company_name, "url": r.url,
                                 "channel": (r.attributes or {}).get("pool_channel"),
                                 "signal": r.title, "observed_at": r.observed_at}
                                for r in rows],
               "manual_sources": MANUAL_SOURCES})


@app.command("reconcile-history")
def reconcile_history() -> None:
    """Apply Gmail's outbound history to open targets: adopt running sequences, block the rest."""
    from boro_gtm.engine import history
    from boro_gtm.engine.strategy import active_strategy

    with session_scope() as s:
        _echo(history.reconcile(s, active_strategy()))


@app.command("verify")
def verify() -> None:
    """Exercise bounce, opt-out and follow-up handling on the real database (rolled back)."""
    from boro_gtm.engine import verify as v
    from boro_gtm.engine.strategy import active_strategy

    with session_scope() as s:
        result = v.run(s, active_strategy())
    _echo(result)
    if not result["ok"]:
        raise typer.Exit(code=2)


@app.command("parity")
def parity(day: str = typer.Option(None, help="YYYY-MM-DD (default: today, Chile)"),
           record: bool = typer.Option(False, "--record")) -> None:
    """Shadow decisions vs what Gmail shows was actually sent that day."""
    from boro_gtm.engine import parity as p
    from boro_gtm.engine.business_days import BusinessCalendar
    from boro_gtm.engine.pipeline import now_utc
    from boro_gtm.engine.strategy import active_strategy

    strategy = active_strategy()
    when = (date.fromisoformat(day) if day else
            BusinessCalendar(strategy).business_day_of(now_utc()))
    with session_scope() as s:
        result = p.day_report(s, strategy, when)
        if record:
            result["recorded"] = p.record(s, result)
    _echo(result)


@app.command("audit-sample")
def audit_sample(n: int = 30, seed: int = 11) -> None:
    """A READY sample across lanes and sources, with what a reviewer must judge."""
    from boro_gtm.engine import audit
    from boro_gtm.engine.strategy import active_strategy

    with session_scope() as s:
        _echo(audit.sample(s, active_strategy(), n=n, seed=seed))


@app.command("record-audit")
def record_audit(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Record READY audit verdicts (JSON: sample, metrics, defects, by)."""
    from boro_gtm.engine import audit

    with session_scope() as s:
        _echo(audit.record(s, json.loads(path.read_text())))


@app.command("lookup")
def lookup(domain: str = typer.Argument(..., help="company domain, e.g. cmcorp.cl")) -> None:
    """Everything the engine knows about one company: pursuit, messages, Gmail history."""
    from sqlalchemy import select

    from boro_gtm.engine.models import CeAccount, CeMailEvent, CeMessage, CeTarget
    from boro_gtm.engine.pipeline import account_key, same_company_domain

    key = account_key(domain) or domain.lower()
    with session_scope() as s:
        account = s.scalar(select(CeAccount).where(CeAccount.domain == key))
        targets = list(s.scalars(select(CeTarget).where(
            CeTarget.account_id == account.id).order_by(CeTarget.created_at))) if account else []
        messages = [m for t in targets for m in s.scalars(select(CeMessage).where(
            CeMessage.target_id == t.id).order_by(CeMessage.due_at))]
        gmail = [e for e in s.scalars(select(CeMailEvent).order_by(CeMailEvent.sent_at))
                 if any(same_company_domain(a, key) for a in (e.to_emails or []))
                 or (e.from_email and same_company_domain(e.from_email, key))]
        _echo({"domain": key, "account": account.name if account else None,
               "targets": [{"id": t.id, "lane": t.lane, "state": t.state,
                            "reason": t.state_reason, "score": t.score} for t in targets],
               "messages": [{"step": m.step, "status": m.status, "to": m.to_email,
                             "due_at": m.due_at, "sent_at": m.sent_at, "variant": m.variant}
                            for m in messages],
               "gmail": [{"direction": e.direction, "sent_at": e.sent_at, "to": e.to_emails,
                          "from": e.from_email, "subject": e.subject,
                          "classification": e.classification} for e in gmail]})


@app.command("ingest-history")
def ingest_history(since: str = typer.Option(..., help="YYYY-MM-DD")) -> None:
    """Backfill Gmail history (sent and received) so past contacts are never re-contacted."""
    from boro_gtm.engine import replies as r

    engine = _engine()
    gateway = engine.gateway()
    if gateway is None:
        raise typer.BadParameter("no Gmail credentials (GTM_ENGINE_GMAIL_CREDENTIALS)")
    with session_scope() as s:
        # Effects apply: bounces and opt-outs in history become suppressions,
        # and running sequences are adopted (engine.history).
        _echo(r.ingest(s, engine.strategy, gateway, since=date.fromisoformat(since),
                       max_messages=20000, apply_effects=True, backfill=True))


@app.command("advance")
def advance(target_id: str, to_state: str, note: str = typer.Option(None),
            amount: float = typer.Option(None), currency: str = typer.Option(None)) -> None:
    """Record a human pipeline move: CONVERSATION, OPPORTUNITY, PROPOSAL, WON or LOST."""
    from boro_gtm.engine.actions import advance as do_advance

    with session_scope() as s:
        _echo(do_advance(s, uuid.UUID(target_id), to_state.upper(), actor="cli", note=note,
                         amount=Decimal(str(amount)) if amount is not None else None,
                         currency=currency))


@app.command("sister-review")
def sister_review(target_id: str = typer.Argument(None, help="omit to list the held targets"),
                  decision: str = typer.Option(None, help="same or different"),
                  note: str = typer.Option(None)) -> None:
    """Targets held because Gmail shows a send only to a same-name domain (other TLD).

    `--decision same` closes the pursuit; `--decision different` makes it plannable again.
    """
    from boro_gtm.engine import actions

    with session_scope() as s:
        if target_id is None:
            _echo(actions.sister_review_list(s))
        else:
            _echo(actions.sister_review(s, uuid.UUID(target_id), (decision or "").lower(),
                                        actor="cli", note=note))


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


@app.command("gmail-connect")
def gmail_connect(
        client_secret_file: Path = typer.Option(
            ..., exists=True, dir_okay=False,
            help="client_secret_….json of a Desktop-app OAuth client"),
        out: Path = typer.Option(
            Path.home() / ".boro-gtm" / "gmail.json", dir_okay=False,
            help="where the credentials file is written (never inside a repository)"),
        browser: bool = typer.Option(True, "--browser/--no-browser",
                                     help="open the consent page in the default browser"),
        timeout: int = typer.Option(300, help="seconds to wait for the sign-in")) -> None:
    """Connect the corporate mailbox once: browser sign-in, PKCE, loopback redirect.

    Prints no code and no token; writes OUT only if the grant carries exactly
    gmail.send + gmail.readonly and opens the corporate sender's mailbox.
    """
    from boro_gtm.engine.gmail_oauth import OAuthError, connect
    from boro_gtm.engine.strategy import active_strategy

    try:
        result = connect(client_secret_file, out, active_strategy().sender["email"],
                         open_browser=None if browser else (lambda _url: None),
                         echo=typer.echo, timeout=timeout)
    except OAuthError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=1) from None
    _echo(result)


@app.command("gmail-check")
def gmail_check() -> None:
    """Refresh the token and confirm it opens the corporate mailbox. Prints no secret."""
    engine = _engine()
    gateway = engine.gateway()
    if gateway is None:
        raise typer.BadParameter("no usable Gmail credentials (GTM_ENGINE_GMAIL_CREDENTIALS)")
    ids, _ = gateway.list_ids("in:sent", None)
    _echo({"mailbox": gateway.mailbox(), "token_refresh": "ok",
           "sent_messages_visible": len(ids) > 0})
