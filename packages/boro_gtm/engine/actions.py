"""What a person does: move a conversation forward, record money, suppress.

The engine never advances a target past REPLIED on its own. Conversations,
opportunities, proposals and outcomes are Amaru's calls, recorded here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeDeal,
    CeMessage,
    CeProblemSignal,
    CeTarget,
    CeTargetEvent,
)
from boro_gtm.engine.outreach import cancel_pending
from boro_gtm.engine.pipeline import (
    SISTER_CLEARED,
    SISTER_CONFIRMED,
    EngineError,
    held_for_sister_review,
    now_utc,
    sister_review_state,
    suppress,
    transition,
)

HUMAN_STATES = (S.CONVERSATION, S.OPPORTUNITY, S.PROPOSAL, S.WON, S.LOST)


def advance(session: Session, target_id: uuid.UUID, to_state: str, *, actor: str,
            note: str | None = None, amount: Decimal | None = None,
            currency: str | None = None) -> dict[str, Any]:
    if to_state not in HUMAN_STATES:
        raise EngineError(f"{to_state} is not a human pipeline move",
                          details={"allowed": list(HUMAN_STATES)})
    target = session.get(CeTarget, target_id)
    if target is None:
        raise EngineError("unknown target", details={"target_id": str(target_id)})
    if to_state == S.LOST and target.state in S.IN_SEQUENCE:
        cancel_pending(session, target.id, "lost")
    transition(session, target, to_state, reason=note or f"manual:{to_state.lower()}",
               actor=actor)
    if amount is not None or to_state in (S.PROPOSAL, S.WON, S.LOST):
        deal = session.get(CeDeal, target.id) or CeDeal(target_id=target.id, updated_at=now_utc())
        if amount is not None:
            deal.amount, deal.currency = amount, (currency or "USD").upper()
        if to_state == S.PROPOSAL:
            deal.proposal_sent_at = now_utc()
        if to_state in (S.WON, S.LOST):
            deal.closed_at = now_utc()
        if to_state == S.LOST:
            deal.lost_reason = note
        deal.updated_at = now_utc()
        session.merge(deal)
    session.flush()
    return {"target_id": str(target.id), "state": target.state}


def record_problem(session: Session, *, account_id: uuid.UUID, problem_key: str,
                   target_id: uuid.UUID | None = None, evidence_url: str | None = None,
                   quote: str | None = None, willingness_to_pay: bool = False) -> None:
    session.add(CeProblemSignal(problem_key=problem_key.strip().lower(), account_id=account_id,
                                target_id=target_id, evidence_url=evidence_url, quote=quote,
                                willingness_to_pay=willingness_to_pay, created_at=now_utc()))
    session.flush()


def manual_suppress(session: Session, value: str, *, reason: str = "MANUAL") -> bool:
    kind = "EMAIL" if "@" in value else "DOMAIN"
    return suppress(session, kind=kind, value=value, reason=reason, evidence_ref="manual")


def sister_review_list(session: Session) -> list[dict[str, Any]]:
    """Targets held because Gmail shows a send only to a same-name domain."""
    rows = []
    for event in held_for_sister_review(session):
        target = session.get(CeTarget, event.target_id)
        account = session.get(CeAccount, target.account_id)
        contact = session.get(CeContact, target.contact_id) if target.contact_id else None
        rows.append({"target_id": str(target.id), "company": account.name,
                     "domain": account.domain, "contact": contact.email if contact else None,
                     "lane": target.lane, "score": target.score, "held_at": event.at.isoformat(),
                     "gmail_sent_to": event.details.get("sent_to", [])})
    return rows


def sister_review(session: Session, target_id: uuid.UUID, decision: str, *, actor: str,
                  note: str | None = None) -> dict[str, Any]:
    """`same`: the sister domain is this company, close the pursuit (BLOCKED).
    `different`: another company; the target becomes plannable again."""
    target = session.get(CeTarget, target_id)
    if target is None:
        raise EngineError("unknown target", details={"target_id": str(target_id)})
    if sister_review_state(session, target.id) != "held" or target.state != S.READY:
        raise EngineError("this target is not held for sister-domain review",
                          details={"target_id": str(target_id), "state": target.state})
    details = {"note": note} if note else {}
    if decision == "same":
        cancel_pending(session, target.id, SISTER_CONFIRMED)
        transition(session, target, S.BLOCKED, reason=SISTER_CONFIRMED, actor=actor,
                   details=details)
    elif decision == "different":
        session.add(CeTargetEvent(target_id=target.id, from_state=target.state,
                                  to_state=target.state, reason=SISTER_CLEARED, actor=actor,
                                  details=details, at=now_utc()))
        # A first email the send-time check stopped is re-armed by the next plan.
        for message in session.scalars(select(CeMessage).where(
                CeMessage.target_id == target.id, CeMessage.step == "INITIAL",
                CeMessage.status == "BLOCKED")):
            message.status = "CANCELLED"
            message.last_error = SISTER_CLEARED
        session.flush()
    else:
        raise EngineError("decision must be 'same' or 'different'",
                          details={"decision": decision})
    return {"target_id": str(target.id), "decision": decision, "state": target.state}


def adopted_review_list(session: Session) -> list[dict[str, Any]]:
    """Adopted Gmail threads whose engine follow-ups wait for a person's confirmation."""
    from boro_gtm.engine.history import PARKED, adoptions_awaiting_review

    rows = []
    for event in adoptions_awaiting_review(session):
        target = session.get(CeTarget, event.target_id)
        account = session.get(CeAccount, target.account_id)
        parked = session.scalars(select(CeMessage).where(
            CeMessage.target_id == target.id, CeMessage.status == "CANCELLED",
            CeMessage.last_error == PARKED))
        rows.append({"target_id": str(target.id), "company": account.name,
                     "state": target.state, "thread_subject": event.details.get("subject"),
                     "to": event.details.get("to"), "first_sent": event.details.get("first_sent"),
                     "parked_follow_ups": [{"step": m.step, "due_at": m.due_at.isoformat()}
                                           for m in parked]})
    return rows


def adopted_review(session: Session, target_id: uuid.UUID, decision: str, *, actor: str,
                   strategy, note: str | None = None) -> dict[str, Any]:
    """`continue`: the thread is BoRo outreach, release its follow-ups.
    `stop`: it is not; close the pursuit (LOST) and never write in that thread."""
    from boro_gtm.engine.business_days import BusinessCalendar
    from boro_gtm.engine.history import (
        ADOPTION_CONFIRMED,
        ADOPTION_REJECTED,
        PARKED,
        adoption_state,
    )
    from boro_gtm.engine.outreach import _create_message

    target = session.get(CeTarget, target_id)
    if target is None:
        raise EngineError("unknown target", details={"target_id": str(target_id)})
    if adoption_state(session, target.id) != "held" or target.state not in S.IN_SEQUENCE:
        raise EngineError("this target has no adopted thread awaiting review",
                          details={"target_id": str(target_id), "state": target.state})
    details = {"note": note} if note else {}
    parked = list(session.scalars(select(CeMessage).where(
        CeMessage.target_id == target.id, CeMessage.status == "CANCELLED",
        CeMessage.last_error == PARKED)))
    if decision == "stop":
        cancel_pending(session, target.id, ADOPTION_REJECTED)
        transition(session, target, S.LOST, reason=ADOPTION_REJECTED, actor=actor,
                   details=details)
    elif decision == "continue":
        session.add(CeTargetEvent(target_id=target.id, from_state=target.state,
                                  to_state=target.state, reason=ADOPTION_CONFIRMED,
                                  actor=actor, details=details, at=now_utc()))
        session.flush()
        earliest = BusinessCalendar(strategy).next_open_moment(now_utc())
        for message in parked:
            contact = session.get(CeContact, message.contact_id)
            _create_message(session, strategy, target, contact, message.step,
                            max(message.due_at, earliest), in_reply_to=message.in_reply_to)
    else:
        raise EngineError("decision must be 'continue' or 'stop'",
                          details={"decision": decision})
    return {"target_id": str(target.id), "decision": decision, "state": target.state,
            "released": [m.step for m in parked] if decision == "continue" else []}
