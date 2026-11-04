"""Checks the engine runs against its own production database, then undoes.

`gtm engine verify` exercises bounce, opt-out and follow-up handling on real
rows inside a savepoint that is always rolled back, and checks follow-up dates
against the real calendar (holidays included) and against every follow-up the
engine has actually scheduled. The outcome is recorded for the cutover report.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import controls
from boro_gtm.engine import states as S
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeMailEvent,
    CeMessage,
    CeSuppression,
    CeTarget,
)
from boro_gtm.engine.outreach import followup_due
from boro_gtm.engine.pipeline import now_utc
from boro_gtm.engine.replies import apply_reply
from boro_gtm.engine.strategy import Strategy

_PROBE_DOMAIN = "verify-probe.invalid"


def _probe_target(session: Session, strategy: Strategy, suffix: str) -> tuple[CeTarget, str]:
    moment = now_utc()
    account = CeAccount(domain=f"{suffix}.{_PROBE_DOMAIN}", name="verification probe",
                        created_at=moment)
    session.add(account)
    session.flush()
    email = f"person@{suffix}.{_PROBE_DOMAIN}"
    contact = CeContact(account_id=account.id, email=email, source_url="verify:probe",
                        verified_at=moment)
    session.add(contact)
    session.flush()
    target = CeTarget(account_id=account.id, contact_id=contact.id, lane="PARTNER_DELIVERY",
                      state=S.CONTACTED, strategy_version=strategy.version, created_at=moment,
                      state_changed_at=moment)
    session.add(target)
    session.flush()
    message = CeMessage(target_id=target.id, contact_id=contact.id, step="INITIAL",
                        lane=target.lane, language="es", variant="verify", offer="verify",
                        strategy_version=strategy.version, to_email=email, subject="probe",
                        body="probe", rfc822_message_id=f"<{uuid.uuid4()}@verify.invalid>",
                        status="SENT", due_at=moment, sent_at=moment,
                        gmail_thread_id=f"verify-{suffix}", created_at=moment)
    session.add(message)
    session.flush()
    return target, email


def _inbound(session: Session, target: CeTarget, sender: str, label: str,
             headers: dict[str, Any]) -> CeMailEvent:
    message = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id))
    event = CeMailEvent(gmail_message_id=f"verify-{uuid.uuid4().hex[:20]}",
                        gmail_thread_id=message.gmail_thread_id, direction="IN",
                        from_email=sender, to_emails=[], subject="probe", headers=headers,
                        sent_at=now_utc(), classification=label, target_id=target.id,
                        message_id=message.id, ingested_at=now_utc())
    session.add(event)
    session.flush()
    return event


def check_reply_handling(session: Session, strategy: Strategy) -> dict[str, Any]:
    nested = session.begin_nested()
    try:
        bounce_target, bounced = _probe_target(session, strategy, "bounce")
        apply_reply(session, strategy, _inbound(session, bounce_target, "mailer-daemon@x.invalid",
                                                "bounce", {"failed_recipient": bounced}))
        optout_target, opted = _probe_target(session, strategy, "optout")
        apply_reply(session, strategy, _inbound(session, optout_target, opted, "opt_out", {}))
        suppressed = set(session.scalars(select(CeSuppression.value).where(
            CeSuppression.value.in_((bounced, opted)))))
        result = {
            "bounce_suppresses_address": bounced in suppressed,
            "bounce_stops_sequence": bounce_target.state == S.SUPPRESSED,
            "opt_out_suppresses_address": opted in suppressed,
            "opt_out_stops_sequence": optout_target.state == S.SUPPRESSED,
        }
    finally:
        nested.rollback()
    return {"ok": all(result.values()), **result}


def check_followup_dates(session: Session, strategy: Strategy,
                         today: date | None = None) -> dict[str, Any]:
    cal = BusinessCalendar(strategy)
    problems: list[str] = []
    start = today or now_utc().date()
    # Every business day of the next six weeks as an initial date, holidays
    # and month ends included, for every lane.
    for offset in range(42):
        day = start + timedelta(days=offset)
        if not cal.is_business_day(day):
            continue
        initial = cal.opening(day) + timedelta(hours=1)
        for lane in ("PARTNER_DELIVERY", "TRIGGER_OVERFLOW", "EXPLICIT_DEMAND"):
            rule = strategy.cadence(lane)
            f1 = followup_due(strategy, lane=lane, step="F1", initial_sent=initial)
            counted = 0
            cursor = cal.local(initial).date()
            while cursor < cal.local(f1).date():
                cursor += timedelta(days=1)
                counted += cal.is_business_day(cursor)
            if counted != rule["f1_business_days"]:
                problems.append(f"{lane} {day}: F1 after {counted} business days")
            if not cal.within_hours(f1):
                problems.append(f"{lane} {day}: F1 outside business hours")
            f2 = followup_due(strategy, lane=lane, step="F2", initial_sent=initial, f1_sent=f1)
            if f2 is None or not cal.within_hours(f2):
                problems.append(f"{lane} {day}: F2 missing or outside hours")
                continue
            if f2 - f1 < timedelta(days=rule["f2_min_days_after_f1"]):
                problems.append(f"{lane} {day}: F2 too soon after F1")
            if lane == "EXPLICIT_DEMAND":
                gap = (cal.local(f2).date() - cal.local(initial).date()).days
                if not rule["f2_min_calendar_days"] <= gap <= rule["f2_max_calendar_days"]:
                    problems.append(f"{lane} {day}: F2 {gap} days after initial")
            elif f2 - initial < timedelta(days=14):
                problems.append(f"{lane} {day}: F2 before day 14")
    scheduled = 0
    for message in session.scalars(select(CeMessage).where(
            CeMessage.step == "F1", CeMessage.status.in_(("SCHEDULED", "SENT")))):
        initial = session.scalar(select(CeMessage).where(
            CeMessage.target_id == message.target_id, CeMessage.step == "INITIAL"))
        target = session.get(CeTarget, message.target_id)
        if initial is None or initial.sent_at is None:
            continue
        expected = followup_due(strategy, lane=target.lane, step="F1",
                                initial_sent=initial.sent_at, expires_at=target.expires_at)
        scheduled += 1
        if message.status == "SCHEDULED" and expected and abs(
                (message.due_at - expected).total_seconds()) > 3600:
            problems.append(f"target {target.id}: F1 {message.due_at} ≠ {expected}")
    return {"ok": not problems, "real_followups_checked": scheduled,
            "problems": problems[:20]}


def run(session: Session, strategy: Strategy) -> dict[str, Any]:
    result = {"at": now_utc().isoformat(),
              "reply_handling": check_reply_handling(session, strategy),
              "followup_dates": check_followup_dates(session, strategy)}
    result["ok"] = result["reply_handling"]["ok"] and result["followup_dates"]["ok"]
    controls.put(session, controls.VERIFY_KEY, result)
    return result
