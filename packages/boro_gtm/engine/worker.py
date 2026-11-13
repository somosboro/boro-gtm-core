"""One boring scheduler loop over durable tasks (CE-ADR-013).

Every task takes a PostgreSQL advisory lock, so any number of workers can run
and each task still executes once at a time; every execution is a `ce_task_runs`
row; every loop writes a heartbeat. A crashed worker leaves nothing that a
restarted one would repeat: sends are reconciled against Gmail, not redone.
"""

from __future__ import annotations

import hashlib
import logging
import signal
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from boro_gtm.core.db import get_engine, session_scope
from boro_gtm.engine import controls, enrichment, outreach, replies, sourcing
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.gmail import GmailApiGateway, GmailUnavailableError, MailGateway
from boro_gtm.engine.llm import provider_from_env
from boro_gtm.engine.models import CeTaskRun, CeWorkerHeartbeat
from boro_gtm.engine.pipeline import now_utc
from boro_gtm.engine.settings import EngineSettings, engine_settings
from boro_gtm.engine.sources.base import Fetcher, TransportFetcher
from boro_gtm.engine.strategy import Strategy, active_strategy

logger = logging.getLogger("boro_gtm.engine.worker")


def _lock_key(task: str) -> int:
    return int(hashlib.sha256(f"ce-task:{task}".encode()).hexdigest()[:15], 16)


class Engine:
    """Task implementations, each runnable on its own (CLI) or from the loop."""

    def __init__(self, strategy: Strategy, settings: EngineSettings,
                 gateway: MailGateway | None = None,
                 fetcher_factory: Callable[[], Fetcher] | None = None) -> None:
        self.strategy = strategy
        self.settings = settings
        self._gateway = gateway
        self._fetcher_factory = fetcher_factory or TransportFetcher

    def gateway(self) -> MailGateway | None:
        if self.settings.sender_account_enabled:
            # CE-ADR-022: the separate sender is configuration only for now. Turning it
            # on must not silently keep sending from the client mailbox, nor switch.
            raise GmailUnavailableError(
                "GTM_ENGINE_SENDER_ACCOUNT_ENABLED is set, but sending from a separate "
                "mailbox is not wired yet: complete docs/SENDER_ACCOUNT.md first")
        if self._gateway is None and self.settings.gmail_credentials:
            try:
                gateway = GmailApiGateway(self.strategy.sender["email"],
                                          self.settings.gmail_credentials)
            except GmailUnavailableError:
                return None
            # Never read or send through any mailbox but the corporate sender
            # (a personal Gmail token would otherwise work just as well).
            opened = gateway.mailbox()
            if opened != self.strategy.sender["email"].lower():
                raise GmailUnavailableError(
                    f"Gmail credentials open {opened}, not the corporate sender "
                    f"{self.strategy.sender['email']}; refusing to use them")
            self._gateway = gateway
        return self._gateway

    # -- tasks --------------------------------------------------------------
    def ingest(self, session: Session) -> dict[str, Any]:
        gateway = self.gateway()
        if gateway is None:
            return {"skipped": "no_gmail_credentials"}
        return replies.ingest(session, self.strategy, gateway, llm=provider_from_env())

    def reconcile(self, session: Session) -> Any:
        gateway = self.gateway()
        if gateway is None:
            return {"skipped": "no_gmail_credentials"}
        return outreach.reconcile_sending(session, self.strategy, gateway)

    def source(self, session: Session) -> dict[str, Any]:
        if not self.settings.sourcing_enabled:
            return {"skipped": "sourcing_disabled"}
        sourcing.rotate_exhausted(session)
        fetcher = self._fetcher_factory()
        try:
            return sourcing.run_ladder(session, self.strategy, fetcher)
        finally:
            getattr(fetcher, "close", lambda: None)()

    def enrich(self, session: Session) -> Any:
        fetcher = self._fetcher_factory()
        try:
            return enrichment.enrich_batch(session, self.strategy, fetcher, limit=25)
        finally:
            getattr(fetcher, "close", lambda: None)()

    def plan(self, session: Session) -> dict[str, Any]:
        shadow = self.settings.shadow_mode
        result = outreach.plan_initials(session, self.strategy, shadow=shadow)
        if not shadow:
            result["rearmed_followups"] = outreach.rearm_shadow_followups(session, self.strategy)
        return result

    def send(self, session: Session) -> Any:
        gateway = None if self.settings.shadow_mode else self.gateway()
        return outreach.send_due(session, self.strategy, gateway,
                                 shadow=self.settings.shadow_mode, limit=3)

    def close(self, session: Session) -> dict[str, Any]:
        return outreach.close_sequences(session, self.strategy)

    def guard(self, session: Session) -> dict[str, Any]:
        return outreach.deliverability_guard(session, self.strategy)


#: (task, minimum interval, only during business hours)
SCHEDULE: tuple[tuple[str, timedelta, bool], ...] = (
    ("ingest", timedelta(minutes=10), False),
    ("reconcile", timedelta(minutes=10), False),
    ("guard", timedelta(minutes=30), False),
    ("source", timedelta(hours=6), False),
    ("enrich", timedelta(minutes=20), False),
    ("plan", timedelta(minutes=30), True),
    ("send", timedelta(minutes=2), True),
    ("close", timedelta(hours=1), False),
)


def run_task(engine: Engine, task: str, *, worker: str) -> dict[str, Any]:
    # The lock lives on its own connection for the whole task. A session-level
    # advisory lock taken through a pooled Session could be released on a
    # different connection than it was taken on, which leaks it.
    with get_engine().connect() as lock_conn:
        got = lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key(task)})
        lock_conn.commit()
        if not got:
            return {"task": task, "status": "LOCKED_ELSEWHERE"}
        try:
            return _execute(engine, task, worker)
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(task)})
            lock_conn.commit()


def _execute(engine: Engine, task: str, worker: str) -> dict[str, Any]:
    with session_scope() as session:
        run = CeTaskRun(task=task, worker=worker, status="RUNNING", started_at=now_utc())
        session.add(run)
        session.commit()
        run_id = run.id
        try:
            result = getattr(engine, task)(session)
            session.commit()
            run = session.get(CeTaskRun, run_id)
            run.status, run.result = "OK", _jsonable(result)
        except Exception as exc:  # noqa: BLE001 - recorded and visible in `engine failed`
            session.rollback()
            run = session.get(CeTaskRun, run_id)
            run.status, run.error = "FAILED", f"{type(exc).__name__}: {exc}"[:4000]
            logger.exception("engine task failed", extra={"task": task})
        run.finished_at = now_utc()
        session.commit()
        return {"task": task, "status": run.status, "result": run.result, "error": run.error}


def _jsonable(value: Any) -> dict[str, Any]:
    import json

    return json.loads(json.dumps({"value": value}, default=str))


#: A failed or interrupted task is retried this soon, not a whole interval later.
RETRY_AFTER_FAILURE = timedelta(minutes=15)


def _due(session: Session, task: str, every: timedelta) -> bool:
    ok = session.scalar(select(func.max(CeTaskRun.started_at)).where(
        CeTaskRun.task == task, CeTaskRun.status == "OK"))
    failed = session.scalar(select(func.max(CeTaskRun.started_at)).where(
        CeTaskRun.task == task, CeTaskRun.status == "FAILED"))
    moment = now_utc()
    if failed is not None and (ok is None or failed > ok):
        return moment - failed >= RETRY_AFTER_FAILURE
    return ok is None or moment - ok >= every


def heartbeat(worker: str, task: str | None, details: dict[str, Any]) -> None:
    with session_scope() as session:
        beat = session.get(CeWorkerHeartbeat, worker)
        if beat is None:
            session.add(CeWorkerHeartbeat(worker=worker, last_seen_at=now_utc(), last_task=task,
                                          details=details))
        else:
            beat.last_seen_at, beat.details = now_utc(), details
            beat.last_task = task or beat.last_task


def loop(*, once: bool = False, poll_seconds: int = 30,
         tasks: frozenset[str] | None = None) -> None:
    """Run the schedule. `tasks` restricts this process to some tasks, so a slow
    crawl in one process never delays a send in another."""
    settings = engine_settings()
    strategy = active_strategy()
    engine = Engine(strategy, settings)
    calendar = BusinessCalendar(strategy)
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.__setitem__("flag", True))
    with session_scope() as session:
        # A run this worker left RUNNING was cut off by a restart. Say so,
        # rather than leaving it RUNNING for ever.
        interrupted = session.execute(text(
            "UPDATE ce_task_runs SET status = 'FAILED', finished_at = now(), "
            "error = 'interrupted: worker restarted' "
            "WHERE status = 'RUNNING' "
            "AND (worker = :w OR started_at < now() - interval '2 hours')"),
            {"w": settings.worker_name}).rowcount
        controls.record_worker_start(session, settings.worker_name, interrupted)
    logger.info("engine worker starting", extra={"worker": settings.worker_name,
                                                 "interrupted_runs": interrupted,
                                                 "shadow_mode": settings.shadow_mode,
                                                 "strategy": strategy.version})
    while not stop["flag"]:
        ran = None
        for task, every, business_only in SCHEDULE:
            if stop["flag"]:
                break
            if tasks is not None and task not in tasks:
                continue
            if business_only and not calendar.within_hours(now_utc()):
                continue
            with session_scope() as session:
                due = _due(session, task, every)
            if due:
                outcome = run_task(engine, task, worker=settings.worker_name)
                ran = task
                logger.info("engine task", extra={"task": task, "status": outcome["status"]})
                heartbeat(settings.worker_name, ran, {"shadow_mode": settings.shadow_mode,
                                                      "strategy": strategy.version})
        heartbeat(settings.worker_name, ran, {"shadow_mode": settings.shadow_mode,
                                              "strategy": strategy.version})
        if once:
            break
        time.sleep(poll_seconds)
