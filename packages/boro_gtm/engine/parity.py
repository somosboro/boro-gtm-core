"""Shadow parity: what the engine would have done on a day vs what was actually sent.

The ChatGPT automations send from the same mailbox, so "what was actually sent"
is Gmail's outbound history for that day (first emails of new threads not sent
by the engine). The engine's decisions are its SHADOW first emails for the day,
including those later replaced when Gmail history adopted the company.

Critical mismatches block cutover (cutover.py): a first email to a company
Gmail shows was already contacted when the engine decided, a support or other
role mailbox, a suppressed address, an address off the company's domain, an
expired explicit-demand opportunity. Identity, contact and signal correctness
beyond these are judged by the READY audit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import controls
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.models import (
    CeAccount,
    CeMailEvent,
    CeMessage,
    CeSignal,
    CeTarget,
    CeTargetEvent,
)
from boro_gtm.engine.outreach import followup_due
from boro_gtm.engine.pipeline import (
    email_domain,
    is_role_mailbox,
    same_company_domain,
    suppression_for,
)
from boro_gtm.engine.strategy import Strategy


def _domain(address: str) -> str:
    return email_domain(address) or ""


def _same_company(address: str, domain: str) -> bool:
    return same_company_domain(address, domain)


def _external_initials(session: Session, cal: BusinessCalendar, day: date) -> list[CeMailEvent]:
    """First messages of outbound threads on `day` that the engine did not send."""
    engine_ids = set(session.scalars(select(CeMessage.gmail_message_id).where(
        CeMessage.gmail_message_id.is_not(None), CeMessage.variant != "external_gmail")))
    out = []
    start = datetime.combine(day, datetime.min.time(), cal.tz)
    for event in session.scalars(select(CeMailEvent).where(
            CeMailEvent.direction == "OUT", CeMailEvent.sent_at >= start,
            CeMailEvent.sent_at < start + timedelta(days=1)).order_by(CeMailEvent.sent_at)):
        if event.gmail_message_id in engine_ids:
            continue
        first = session.scalar(select(CeMailEvent).where(
            CeMailEvent.gmail_thread_id == event.gmail_thread_id).order_by(
            CeMailEvent.sent_at).limit(1))
        if first is not None and first.id == event.id:
            out.append(event)
    return out


def _shadow_decisions(session: Session, cal: BusinessCalendar, day: date) -> list[dict[str, Any]]:
    start = datetime.combine(day, datetime.min.time(), cal.tz)
    end = start + timedelta(days=1)
    rows = []
    for m in session.scalars(select(CeMessage).where(
            CeMessage.step == "INITIAL", CeMessage.status == "SHADOW",
            CeMessage.due_at >= start, CeMessage.due_at < end)):
        rows.append({"target_id": m.target_id, "to": m.to_email, "due_at": m.due_at,
                     "decided_at": m.created_at, "subject": m.subject, "lane": m.lane,
                     "variant": m.variant, "replaced_by_gmail": False})
    # Rehearsals Gmail history later replaced with the real (external) send.
    for event in session.scalars(select(CeTargetEvent).where(
            CeTargetEvent.reason == "gmail_history_initial")):
        replaced = (event.details or {}).get("replaced_engine_message") or {}
        if replaced.get("status") != "SHADOW":
            continue
        due = datetime.fromisoformat(replaced["due_at"])
        if start <= due < end:
            target = session.get(CeTarget, event.target_id)
            decided = replaced.get("decided_at")
            rows.append({"target_id": event.target_id, "to": replaced["to"], "due_at": due,
                         "decided_at": datetime.fromisoformat(decided) if decided else None,
                         "subject": replaced.get("subject"),
                         "lane": target.lane, "variant": replaced.get("variant"),
                         "replaced_by_gmail": True})
    return rows


def day_report(session: Session, strategy: Strategy, day: date) -> dict[str, Any]:
    cal = BusinessCalendar(strategy)
    external = _external_initials(session, cal, day)
    decisions = _shadow_decisions(session, cal, day)
    vps: list[dict[str, Any]] = []
    critical: list[dict[str, Any]] = []
    for d in decisions:
        target = session.get(CeTarget, d["target_id"])
        account = session.get(CeAccount, target.account_id)
        signal = session.get(CeSignal, target.signal_id) if target.signal_id else None
        decided = d["decided_at"] or d["due_at"]
        earlier = session.scalar(select(CeMailEvent).where(
            CeMailEvent.direction == "OUT", CeMailEvent.sent_at < decided,
            CeMailEvent.message_id.is_(None) | (CeMailEvent.target_id != target.id)).where(
            CeMailEvent.to_emails.any(d["to"])).limit(1))
        domain_earlier = [e for e in session.scalars(select(CeMailEvent).where(
            CeMailEvent.direction == "OUT", CeMailEvent.sent_at < decided))
            if any(_same_company(a, account.domain) for a in e.to_emails or [])
            and e.target_id != target.id]
        problems = []
        if earlier is not None or domain_earlier:
            problems.append("would_contact_already_contacted")
        if is_role_mailbox(d["to"]):
            problems.append("role_or_support_mailbox")
        if suppression_for(session, d["to"]) is not None:
            problems.append("suppressed_address")
        if not _same_company(d["to"], account.domain):
            problems.append("contact_not_at_company_domain")
        if target.lane == "EXPLICIT_DEMAND" and target.expires_at and target.expires_at <= d[
                "due_at"]:
            problems.append("expired_explicit_demand")
        f1 = followup_due(strategy, lane=target.lane, step="F1", initial_sent=d["due_at"],
                          expires_at=target.expires_at)
        row = {"company": account.name or account.domain, "domain": account.domain,
               "contact": d["to"], "lane": target.lane,
               "source": signal.source_key if signal else None, "score": target.score,
               "due_at": d["due_at"].isoformat(),
               "f1_due_if_sent": f1.isoformat() if f1 else None,
               "replaced_by_gmail": d["replaced_by_gmail"], "critical": problems}
        vps.append(row)
        if problems:
            critical.append(row)
    chatgpt = []
    vps_domains = {r["domain"] for r in vps}
    for event in external:
        domain = next((_domain(a) for a in event.to_emails or []), "")
        account = session.scalar(select(CeAccount).where(CeAccount.domain == domain))
        target = (session.scalar(select(CeTarget).where(CeTarget.account_id == account.id)
                                 .order_by(CeTarget.created_at.desc()).limit(1))
                  if account else None)
        next_f1 = session.scalar(select(CeMessage).where(
            CeMessage.target_id == target.id, CeMessage.step == "F1")) if target else None
        chatgpt.append({"to": event.to_emails, "domain": domain, "subject": event.subject,
                        "sent_at": event.sent_at.isoformat() if event.sent_at else None,
                        "engine_knows_account": account is not None,
                        "engine_state": target.state if target else None,
                        "engine_lane": target.lane if target else None,
                        "engine_score": target.score if target else None,
                        "engine_state_reason": target.state_reason if target else None,
                        "engine_f1": ({"due_at": next_f1.due_at.isoformat(),
                                       "status": next_f1.status} if next_f1 else None),
                        "vps_chose_same_company": domain in vps_domains})
    chatgpt_domains = {c["domain"] for c in chatgpt}
    return {"day": day.isoformat(), "business_day": cal.is_business_day(day),
            "chatgpt_sent": len(chatgpt), "vps_shadow": len(vps),
            "both_chose": sorted(vps_domains & chatgpt_domains),
            "only_chatgpt": sorted(chatgpt_domains - vps_domains),
            "only_vps": sorted(vps_domains - chatgpt_domains),
            "vps_lanes": {lane: sum(1 for r in vps if r["lane"] == lane)
                          for lane in {r["lane"] for r in vps}},
            "critical_mismatches": len(critical), "critical": critical,
            "vps": vps, "chatgpt": chatgpt}


def record(session: Session, report: dict[str, Any]) -> dict[str, Any]:
    stored = controls.get(session, controls.PARITY_KEY) or {"days": {}}
    stored["days"][report["day"]] = {
        "business_day": report["business_day"], "chatgpt_sent": report["chatgpt_sent"],
        "vps_shadow": report["vps_shadow"], "both_chose": len(report["both_chose"]),
        "critical_mismatches": report["critical_mismatches"],
        "critical": [{"domain": c["domain"], "problems": c["critical"]}
                     for c in report["critical"]]}
    controls.put(session, controls.PARITY_KEY, stored)
    return stored
