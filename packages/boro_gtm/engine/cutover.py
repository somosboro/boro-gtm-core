"""The cutover report: what must be true before SHADOW_MODE goes off (CE-ADR-015).

Each check is a query. `ready_for_live` is true only when all of them pass; the
report never switches anything itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.models import (
    CeContact,
    CeMailEvent,
    CeMessage,
    CeSuppression,
    CeTarget,
    CeWorkerHeartbeat,
)
from boro_gtm.engine.pipeline import now_utc
from boro_gtm.engine.settings import engine_settings
from boro_gtm.engine.strategy import active_strategy


def report(session: Session) -> dict[str, Any]:
    settings = engine_settings()
    strategy = active_strategy()
    cal = BusinessCalendar(strategy)
    checks: dict[str, dict[str, Any]] = {}

    history = session.scalar(select(func.count()).select_from(CeMailEvent).where(
        CeMailEvent.direction == "OUT")) or 0
    earliest = session.scalar(select(func.min(CeMailEvent.sent_at)))
    from pathlib import Path

    creds = settings.gmail_credentials
    checks["gmail_credentials"] = {"ok": bool(creds) and Path(creds).is_file()}
    checks["gmail_history_ingested"] = {
        "ok": history > 0 and earliest is not None and now_utc() - earliest > timedelta(days=60),
        "outbound_messages": history, "earliest": earliest}

    pending = select(CeMessage.to_email).where(CeMessage.status.in_(("SCHEDULED", "SHADOW")),
                                               CeMessage.step == "INITIAL")
    already = session.scalar(select(func.count()).select_from(CeMailEvent).where(
        CeMailEvent.direction == "OUT", CeMailEvent.to_emails.overlap(
            select(func.array_agg(pending.subquery().c.to_email)).scalar_subquery()))) or 0
    checks["no_planned_first_contact_already_in_gmail"] = {"ok": already == 0, "found": already}

    suppressed_but_ready = session.scalar(
        select(func.count()).select_from(CeTarget).join(CeContact,
                                                        CeContact.id == CeTarget.contact_id)
        .where(CeTarget.state == S.READY, CeContact.email.in_(
            select(CeSuppression.value).where(CeSuppression.kind == "EMAIL")))) or 0
    checks["no_suppressed_contact_is_ready"] = {"ok": suppressed_but_ready == 0,
                                                "found": suppressed_but_ready}

    outside = [m.id for m in session.scalars(select(CeMessage).where(
        CeMessage.status == "SCHEDULED")) if not cal.within_hours(m.due_at)]
    checks["every_scheduled_message_is_in_business_hours"] = {"ok": not outside,
                                                              "outside": len(outside)}

    stuck = session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.status == "SENDING")) or 0
    checks["nothing_stuck_sending"] = {"ok": stuck == 0, "found": stuck}

    shadowed = session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.status == "SHADOW")) or 0
    checks["shadow_rehearsal_ran"] = {"ok": shadowed > 0, "shadow_messages": shadowed}

    beat = session.scalar(select(func.max(CeWorkerHeartbeat.last_seen_at)))
    checks["worker_alive"] = {"ok": beat is not None and now_utc() - beat < timedelta(minutes=5),
                              "last_heartbeat": beat}

    duplicates = session.scalar(select(func.count()).select_from(
        select(CeMessage.target_id).where(CeMessage.step == "INITIAL")
        .group_by(CeMessage.target_id).having(func.count() > 1).subquery())) or 0
    checks["one_initial_per_target"] = {"ok": duplicates == 0, "found": duplicates}

    ready_for_live = all(c["ok"] for c in checks.values())
    return {"shadow_mode": settings.shadow_mode, "ready_for_live": ready_for_live,
            "checks": checks,
            "next_step": ("Disable the ChatGPT commercial automations (Demand Radar, Lead "
                          "Enrichment, Initial Outreach, Follow-ups & Replies), wait for their "
                          "last run to finish, run `gtm engine run ingest`, re-run this report, "
                          "then set GTM_ENGINE_SHADOW_MODE=false and restart the worker."
                          if ready_for_live else
                          "Keep SHADOW_MODE on. Fix the failing checks first.")}
