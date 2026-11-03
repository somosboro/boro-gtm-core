"""What a person does: move a conversation forward, record money, suppress.

The engine never advances a target past REPLIED on its own. Conversations,
opportunities, proposals and outcomes are Amaru's calls, recorded here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.models import CeDeal, CeProblemSignal, CeTarget
from boro_gtm.engine.outreach import cancel_pending
from boro_gtm.engine.pipeline import EngineError, now_utc, suppress, transition

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
