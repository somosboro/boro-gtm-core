"""Gmail history as commercial state: taking over what BoRo already sent.

Until cutover the ChatGPT automations send from the same mailbox. Gmail is the
authority on what actually went out, so a company Gmail shows BoRo wrote to is
never written to "for the first time" again. When that conversation is still
running, the engine adopts it: the external first email (and any follow-up)
becomes a SENT message on the target, the state follows, and the next
follow-up is scheduled on the lane's cadence, so nothing is lost at cutover.

A draft is not a send: only messages Gmail labelled SENT are ingested as
outbound (see `replies._store`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeControl,
    CeMailEvent,
    CeMessage,
    CeTarget,
)
from boro_gtm.engine.pipeline import email_domain, now_utc, transition
from boro_gtm.engine.strategy import Strategy

#: A first email older than this belongs to a finished sequence (F2 lands by
#: day 14–21, closing seven days later); it blocks re-contact, it is not adopted.
ADOPT_WINDOW = timedelta(days=35)
PRE_CONTACT = (S.DISCOVERED, S.ENRICH, S.COMPLIANCE_REVIEW, S.READY)
STEP_STATE = {"INITIAL": S.CONTACTED, "F1": S.F1, "F2": S.F2}
EXTERNAL_VARIANT = "external_gmail"
RECONCILIATION_KEY = "gmail_history_reconciliation"


def _external_out(session: Session, domain: str, email: str | None = None) -> list[CeMailEvent]:
    """Outbound Gmail messages to this company that the engine did not send."""
    rows = session.scalars(select(CeMailEvent).where(
        CeMailEvent.direction == "OUT", CeMailEvent.message_id.is_(None))
        .order_by(CeMailEvent.sent_at.nulls_first())).all()
    return [e for e in rows if any(
        a == email or _at(a, domain) for a in (e.to_emails or []))]


def _at(address: str, domain: str) -> bool:
    found = email_domain(address) or ""
    return found == domain or found.endswith("." + domain)


def _thread(session: Session, thread_id: str) -> list[CeMailEvent]:
    return list(session.scalars(select(CeMailEvent).where(
        CeMailEvent.gmail_thread_id == thread_id).order_by(CeMailEvent.sent_at.nulls_first())))


def _contact_for(session: Session, account: CeAccount, event: CeMailEvent) -> CeContact:
    address = next((a for a in event.to_emails if _at(a, account.domain)), event.to_emails[0])
    contact = session.scalar(select(CeContact).where(CeContact.email == address))
    if contact is None:
        # Not a guess: BoRo's own sent mail proves the address was used.
        contact = CeContact(account_id=account.id, email=address,
                            is_generic=False, source_url=f"gmail:{event.gmail_message_id}",
                            verified_at=now_utc())
        session.add(contact)
        session.flush()
    return contact


def record_external_send(session: Session, strategy: Strategy, target: CeTarget,
                         contact: CeContact, step: str, event: CeMailEvent,
                         *, actor: str) -> CeMessage:
    """Make one Gmail-proven send the target's `step` message, replacing any shadow."""
    from boro_gtm.engine.outreach import language_for

    initial = session.scalar(select(CeMessage).where(
        CeMessage.target_id == target.id, CeMessage.step == "INITIAL"))
    row = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id,
                                                 CeMessage.step == step))
    replaced = None
    if row is not None:
        replaced = {"status": row.status, "to": row.to_email, "due_at": row.due_at.isoformat(),
                    "decided_at": row.created_at.isoformat(), "subject": row.subject,
                    "variant": row.variant}
    else:
        row = CeMessage(target_id=target.id, step=step, created_at=now_utc(), attempts=0)
        session.add(row)
    row.contact_id = contact.id
    row.lane, row.segment, row.country = target.lane, target.segment, target.country
    row.language = language_for(target.country)
    row.variant, row.offer, row.strategy_version = EXTERNAL_VARIANT, "external", strategy.version
    row.to_email = contact.email
    row.subject = event.subject or ""
    row.body = "(sent outside the engine; the text is in Gmail)"
    row.rfc822_message_id = (event.rfc822_message_id
                             or f"<gmail-{event.gmail_message_id}@external.invalid>")
    row.in_reply_to = initial.rfc822_message_id if (initial and step != "INITIAL") else None
    row.status, row.due_at, row.sent_at = "SENT", event.sent_at, event.sent_at
    row.gmail_message_id, row.gmail_thread_id = event.gmail_message_id, event.gmail_thread_id
    row.last_error = None
    session.flush()
    event.message_id, event.target_id = row.id, target.id
    transition(session, target, STEP_STATE[step], reason=f"gmail_history_{step.lower()}",
               actor=actor, at=event.sent_at,
               details={"gmail_message_id": event.gmail_message_id,
                        "replaced_engine_message": replaced})
    return row


def adopt_target(session: Session, strategy: Strategy, target: CeTarget, *,
                 actor: str = "system:history", now: datetime | None = None) -> str:
    """Adopt the running Gmail sequence for this target, or close it as already contacted.

    Returns `adopted`, `already_contacted` (BLOCKED, never re-contacted) or
    `skipped` (not a pre-contact target).
    """
    from boro_gtm.engine.outreach import _schedule_next, cancel_pending
    from boro_gtm.engine.replies import apply_reply

    if target.state not in PRE_CONTACT:
        return "skipped"
    moment = now or now_utc()
    account = session.get(CeAccount, target.account_id)
    contact = session.get(CeContact, target.contact_id) if target.contact_id else None
    outs = _external_out(session, account.domain, contact.email if contact else None)
    if not outs:
        # Only the engine's own sends (under another pursuit) proved contact.
        transition(session, target, S.BLOCKED, reason="engine_initial_already_sent", actor=actor)
        return "already_contacted"
    thread = _thread(session, outs[-1].gmail_thread_id)
    thread_out = [e for e in thread if e.direction == "OUT"]
    if thread[0].direction == "IN":
        transition(session, target, S.BLOCKED, reason="gmail_existing_conversation",
                   actor=actor, details={"gmail_thread_id": thread[0].gmail_thread_id})
        return "already_contacted"
    if thread_out[0].sent_at is None or moment - thread_out[0].sent_at > ADOPT_WINDOW:
        transition(session, target, S.BLOCKED, reason="gmail_history_sequence_finished",
                   actor=actor, details={"first_sent": str(thread_out[0].sent_at)})
        return "already_contacted"

    contact = _contact_for(session, account, thread_out[0])
    target.contact_id = contact.id
    if target.state == S.DISCOVERED:
        transition(session, target, S.ENRICH, reason="adopting_gmail_sequence", actor=actor)
    if target.state != S.READY:
        transition(session, target, S.READY, reason="adopting_gmail_sequence", actor=actor)
    last = None
    for step, event in zip(("INITIAL", "F1", "F2"), thread_out, strict=False):
        last = record_external_send(session, strategy, target, contact, step, event, actor=actor)
    # Replies already in the thread take effect now that it has an owner.
    for event in thread:
        if event.direction != "IN" or event.sent_at < thread_out[0].sent_at:
            continue
        event.target_id, event.message_id = target.id, last.id
        event.processed_at = None
        apply_reply(session, strategy, event, actor=actor)
    if target.state in (S.CONTACTED, S.F1):
        cancel_pending(session, target.id, "adopted_gmail_sequence")
        _schedule_next(session, strategy, target, last)
    session.flush()
    return "adopted"


def apply_outbound(session: Session, strategy: Strategy, event: CeMailEvent, *,
                   actor: str = "system:history") -> int:
    """A follow-up someone else sent in a thread the engine owns: record it.

    Without this, a follow-up the ChatGPT automation sent after adoption would
    leave the engine's own (shadow) follow-up looking unsent.
    """
    from boro_gtm.engine.outreach import _schedule_next, cancel_pending

    if event.direction != "OUT" or event.message_id is None or event.target_id is None:
        return 0
    if session.scalar(select(CeMessage.id).where(
            CeMessage.gmail_message_id == event.gmail_message_id)):
        return 0  # the engine's own send, already recorded
    if event.rfc822_message_id and session.scalar(select(CeMessage.id).where(
            CeMessage.rfc822_message_id == event.rfc822_message_id)):
        return 0
    target = session.get(CeTarget, event.target_id)
    step = {S.CONTACTED: "F1", S.F1: "F2"}.get(target.state)
    if step is None:
        return 0
    initial = session.scalar(select(CeMessage).where(
        CeMessage.target_id == target.id, CeMessage.step == "INITIAL"))
    contact = session.get(CeContact, initial.contact_id)
    row = record_external_send(session, strategy, target, contact, step, event, actor=actor)
    cancel_pending(session, target.id, f"external_{step.lower()}")
    if step == "F1":
        _schedule_next(session, strategy, target, row)
    return 1


def reconcile(session: Session, strategy: Strategy, *, now: datetime | None = None,
              actor: str = "system:history") -> dict[str, Any]:
    """Apply Gmail's outbound history to every open pre-contact target, and record it.

    Also lists what the engine cannot take over: recent external sequences to
    companies it has no open pursuit for. Those are reported, never invented.
    """
    moment = now or now_utc()
    outs = list(session.scalars(select(CeMailEvent).where(
        CeMailEvent.direction == "OUT", CeMailEvent.message_id.is_(None))))
    domains: dict[str, list[CeMailEvent]] = {}
    for event in outs:
        for address in event.to_emails or []:
            found = email_domain(address)
            if found:
                domains.setdefault(found, []).append(event)
    stats: dict[str, Any] = {"external_outbound": len(outs), "adopted": [],
                             "already_contacted": [], "terminal_but_contacted": [],
                             "recent_without_target": []}
    accounts = {a.domain: a for a in session.scalars(select(CeAccount).where(
        CeAccount.domain.in_(list(domains))))} if domains else {}
    for domain, events in sorted(domains.items()):
        dated = [e.sent_at for e in events if e.sent_at is not None]
        recent = bool(dated) and max(dated) > moment - ADOPT_WINDOW
        account = accounts.get(domain)
        target = None
        if account is not None:
            target = session.scalar(select(CeTarget).where(
                CeTarget.account_id == account.id, CeTarget.state.in_(PRE_CONTACT)))
            if target is None and recent:
                latest = session.scalar(select(CeTarget).where(
                    CeTarget.account_id == account.id).order_by(CeTarget.created_at.desc())
                    .limit(1))
                if latest is not None and latest.state in S.TERMINAL_STATES:
                    stats["terminal_but_contacted"].append(
                        {"domain": domain, "state": latest.state,
                         "reason": latest.state_reason})
        if target is not None:
            outcome = adopt_target(session, strategy, target, actor=actor, now=moment)
            key = "adopted" if outcome == "adopted" else "already_contacted"
            stats[key].append({"domain": domain, "target_id": str(target.id),
                               "state": target.state})
        elif account is None and recent:
            stats["recent_without_target"].append(
                {"domain": domain, "last_sent": max(dated).isoformat()})
    record = {"at": moment.isoformat(), "adopted": len(stats["adopted"]),
              "already_contacted": len(stats["already_contacted"]),
              "terminal_but_contacted": stats["terminal_but_contacted"],
              "recent_without_target": stats["recent_without_target"]}
    control = session.get(CeControl, RECONCILIATION_KEY)
    if control is None:
        session.add(CeControl(key=RECONCILIATION_KEY, value=record, updated_at=moment))
    else:
        control.value, control.updated_at = record, moment
    session.flush()
    return stats
