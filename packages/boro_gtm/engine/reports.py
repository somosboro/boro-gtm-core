"""Operational visibility without a dashboard (CE-ADR-011).

Every number is a query over durable rows, so the CLI, the API and an ad hoc
psql session can never disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeDeal,
    CeMailEvent,
    CeMessage,
    CeSignal,
    CeSourceRun,
    CeSuppression,
    CeTarget,
    CeTargetEvent,
    CeTaskRun,
    CeWorkerHeartbeat,
)
from boro_gtm.engine.outreach import paused
from boro_gtm.engine.pipeline import now_utc
from boro_gtm.engine.strategy import Strategy

FUNNEL = (S.CONTACTED, S.REPLIED, S.CONVERSATION, S.OPPORTUNITY, S.PROPOSAL, S.WON)


def _day_bounds(strategy: Strategy, day: datetime | None) -> tuple[datetime, datetime]:
    cal = BusinessCalendar(strategy)
    local = cal.local(day or now_utc())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def today(session: Session, strategy: Strategy, *, day: datetime | None = None) -> dict[str, Any]:
    start, end = _day_bounds(strategy, day)

    def entered(state: str) -> int:
        return session.scalar(select(func.count()).select_from(CeTargetEvent).where(
            CeTargetEvent.to_state == state, CeTargetEvent.at >= start,
            CeTargetEvent.at < end)) or 0

    def sent(step: str, status: str = "SENT") -> int:
        return session.scalar(select(func.count()).select_from(CeMessage).where(
            CeMessage.step == step, CeMessage.status == status,
            CeMessage.sent_at >= start, CeMessage.sent_at < end)) or 0

    inbound = select(func.count()).select_from(CeMailEvent).where(
        CeMailEvent.direction == "IN", CeMailEvent.sent_at >= start, CeMailEvent.sent_at < end)
    human = ("bounce", "auto_reply")
    revenue = session.scalar(select(func.coalesce(func.sum(CeDeal.amount), 0)).where(
        CeDeal.closed_at >= start, CeDeal.closed_at < end,
        CeDeal.target_id.in_(select(CeTarget.id).where(CeTarget.state == S.WON))))
    return {
        "day": start.date().isoformat(),
        "sourced": entered(S.DISCOVERED),
        "enrich_now": session.scalar(select(func.count()).select_from(CeTarget).where(
            CeTarget.state.in_((S.DISCOVERED, S.ENRICH)))) or 0,
        "ready_now": session.scalar(select(func.count()).select_from(CeTarget).where(
            CeTarget.state == S.READY)) or 0,
        "became_ready": entered(S.READY),
        "initial_sent": sent("INITIAL"), "f1_sent": sent("F1"), "f2_sent": sent("F2"),
        "initial_shadow": sent("INITIAL", "SHADOW"),
        "human_replies": session.scalar(inbound.where(
            CeMailEvent.classification.not_in(human))) or 0,
        "bounces": session.scalar(inbound.where(CeMailEvent.classification == "bounce")) or 0,
        "conversations": entered(S.CONVERSATION), "opportunities": entered(S.OPPORTUNITY),
        "proposals": entered(S.PROPOSAL), "won": entered(S.WON),
        "revenue": float(revenue or 0),
        "suppressions": session.scalar(select(func.count()).select_from(CeSuppression).where(
            CeSuppression.created_at >= start, CeSuppression.created_at < end)) or 0,
        "sending_paused": paused(session),
    }


def funnel(session: Session, *, by: str = "lane") -> list[dict[str, Any]]:
    """Per lane (never pooled), how far contacted targets got (CE-ADR-012)."""
    column = {"lane": CeMessage.lane, "country": CeMessage.country,
              "variant": CeMessage.variant, "segment": CeMessage.segment}[by]
    contacted = select(CeMessage.target_id, column.label("key")).where(
        CeMessage.step == "INITIAL", CeMessage.status == "SENT").subquery()
    reached = select(CeTargetEvent.target_id, CeTargetEvent.to_state).distinct().subquery()
    rows: dict[str, dict[str, Any]] = {}
    for key, state, count in session.execute(
            select(contacted.c.key, reached.c.to_state, func.count())
            .join(reached, reached.c.target_id == contacted.c.target_id)
            .group_by(contacted.c.key, reached.c.to_state)):
        rows.setdefault(key or "unknown", {"key": key or "unknown"})[state] = count
    out = []
    for row in rows.values():
        base = row.get(S.CONTACTED, 0)
        entry = {"key": row["key"], **{s.lower(): row.get(s, 0) for s in FUNNEL}}
        entry["reply_rate"] = round(row.get(S.REPLIED, 0) / base, 4) if base else None
        out.append(entry)
    return sorted(out, key=lambda r: r["key"])


def ready(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.execute(
        select(CeTarget, CeAccount, CeContact).join(CeAccount, CeAccount.id == CeTarget.account_id)
        .outerjoin(CeContact, CeContact.id == CeTarget.contact_id)
        .where(CeTarget.state == S.READY)
        .order_by(CeTarget.score.desc().nulls_last()).limit(limit)).all()
    return [{"target_id": str(t.id), "lane": t.lane, "score": t.score, "priority": t.priority,
             "company": a.name or a.domain, "domain": a.domain, "country": t.country,
             "contact": c.email if c else None} for t, a, c in rows]


def messages(session: Session, *, statuses: tuple[str, ...], due_before: datetime | None = None,
             since: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
    stmt = select(CeMessage).where(CeMessage.status.in_(statuses))
    if due_before is not None:
        stmt = stmt.where(CeMessage.due_at <= due_before)
    if since is not None:
        stmt = stmt.where(func.coalesce(CeMessage.sent_at, CeMessage.due_at) >= since)
    return [{"message_id": str(m.id), "target_id": str(m.target_id), "step": m.step,
             "lane": m.lane, "to": m.to_email, "subject": m.subject, "status": m.status,
             "due_at": m.due_at.isoformat(),
             "sent_at": m.sent_at.isoformat() if m.sent_at else None,
             "error": m.last_error}
            for m in session.scalars(stmt.order_by(CeMessage.due_at).limit(limit))]


def replies(session: Session, *, days: int = 14, limit: int = 100) -> list[dict[str, Any]]:
    since = now_utc() - timedelta(days=days)
    rows = session.scalars(select(CeMailEvent).where(
        CeMailEvent.direction == "IN", CeMailEvent.target_id.is_not(None),
        CeMailEvent.sent_at >= since).order_by(CeMailEvent.sent_at.desc()).limit(limit))
    return [{"at": e.sent_at.isoformat() if e.sent_at else None, "from": e.from_email,
             "subject": e.subject, "label": e.classification, "source": e.classification_source,
             "escalate": e.escalate, "escalation_reasons": e.escalation_reasons,
             "target_id": str(e.target_id)} for e in rows]


def opportunities(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(CeTarget, CeAccount, CeDeal).join(CeAccount, CeAccount.id == CeTarget.account_id)
        .outerjoin(CeDeal, CeDeal.target_id == CeTarget.id)
        .where(CeTarget.state.in_((S.REPLIED, S.CONVERSATION, S.OPPORTUNITY, S.PROPOSAL, S.WON)))
        .order_by(CeTarget.state_changed_at.desc())).all()
    return [{"target_id": str(t.id), "company": a.name or a.domain, "lane": t.lane,
             "state": t.state, "since": t.state_changed_at.isoformat(),
             "amount": float(d.amount) if d and d.amount is not None else None,
             "currency": d.currency if d else None} for t, a, d in rows]


def health(session: Session) -> dict[str, Any]:
    beats = [{"worker": h.worker, "last_seen_at": h.last_seen_at.isoformat(),
              "age_seconds": int((now_utc() - h.last_seen_at).total_seconds()),
              "last_task": h.last_task} for h in session.scalars(select(CeWorkerHeartbeat))]
    failed = [{"task": r.task, "at": r.started_at.isoformat(), "error": (r.error or "")[:300]}
              for r in session.scalars(select(CeTaskRun).where(CeTaskRun.status == "FAILED")
                                       .order_by(CeTaskRun.started_at.desc()).limit(20))]
    last = {task: at.isoformat() for task, at in session.execute(
        select(CeTaskRun.task, func.max(CeTaskRun.started_at)).where(CeTaskRun.status == "OK")
        .group_by(CeTaskRun.task))}
    # A sourcing pass can hold one process for a while; the engine is healthy
    # when the outreach worker is alive.
    alive = any(b["age_seconds"] < 300 for b in beats if b["worker"] != "sourcing") or (
        bool(beats) and all(b["worker"] == "sourcing" for b in beats)
        and any(b["age_seconds"] < 3600 for b in beats))
    return {"healthy": alive and not paused(session), "workers": beats, "last_ok": last,
            "recent_failures": failed, "sending_paused": paused(session)}


def sources(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    return [{"source": r.source_key, "status": r.status, "started_at": r.started_at.isoformat(),
             "inspected": r.results_inspected, "accepted": r.accepted, "duplicates": r.duplicates,
             "expired": r.expired, "rejected": r.rejected, "reasons": r.rejection_reasons,
             "exhausted": r.cursor_exhausted, "error": r.error}
            for r in session.scalars(select(CeSourceRun).order_by(CeSourceRun.id.desc())
                                     .limit(limit))]


def product_signals(session: Session, min_companies: int = 3) -> list[dict[str, Any]]:
    from boro_gtm.engine.models import CeProblemSignal

    rows = session.execute(
        select(CeProblemSignal.problem_key, func.count(func.distinct(CeProblemSignal.account_id)),
               func.bool_or(CeProblemSignal.willingness_to_pay))
        .group_by(CeProblemSignal.problem_key)
        .having(func.count(func.distinct(CeProblemSignal.account_id)) >= min_companies)).all()
    return [{"problem": k, "companies": n, "willingness_to_pay_seen": bool(w)} for k, n, w in rows]


def signal_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(CeSignal)) or 0
