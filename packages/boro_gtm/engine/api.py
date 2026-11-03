"""Engine API, mounted at /api/v1/engine. Closed by default (CE-ADR-014).

Every route requires `Authorization: Bearer <GTM_ENGINE_API_TOKEN>`. With no
token configured, every route answers 503: an unconfigured deployment is shut,
not open.
"""

from __future__ import annotations

import hmac
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from boro_gtm.core.db import get_db
from boro_gtm.engine import actions, reports
from boro_gtm.engine.outreach import set_pause
from boro_gtm.engine.settings import engine_settings
from boro_gtm.engine.strategy import active_strategy


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = engine_settings().api_token
    if not token:
        raise HTTPException(status_code=503, detail="engine API disabled: no token configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="invalid token")


router = APIRouter(prefix="/engine", tags=["engine"], dependencies=[Depends(require_token)])


class Advance(BaseModel):
    to_state: str
    note: str | None = None
    amount: Decimal | None = None
    currency: str | None = None


class Suppress(BaseModel):
    value: str


class Pause(BaseModel):
    paused: bool
    reason: str


@router.get("/today")
def get_today(db: Session = Depends(get_db)) -> dict[str, Any]:
    return reports.today(db, active_strategy())


@router.get("/funnel")
def get_funnel(by: str = "lane", db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    if by not in ("lane", "country", "variant", "segment"):
        raise HTTPException(status_code=422, detail="by must be lane, country, variant or segment")
    return reports.funnel(db, by=by)


@router.get("/ready")
def get_ready(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return reports.ready(db)


@router.get("/replies")
def get_replies(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return reports.replies(db)


@router.get("/opportunities")
def get_opportunities(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return reports.opportunities(db)


@router.get("/health")
def get_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    return reports.health(db)


@router.post("/targets/{target_id}/advance")
def post_advance(target_id: uuid.UUID, body: Advance,
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    result = actions.advance(db, target_id, body.to_state, actor="api", note=body.note,
                             amount=body.amount, currency=body.currency)
    db.commit()
    return result


@router.post("/suppressions")
def post_suppression(body: Suppress, db: Session = Depends(get_db)) -> dict[str, Any]:
    added = actions.manual_suppress(db, body.value)
    db.commit()
    return {"added": added}


@router.post("/controls/pause")
def post_pause(body: Pause, db: Session = Depends(get_db)) -> dict[str, Any]:
    set_pause(db, paused_=body.paused, reason=body.reason)
    db.commit()
    return {"paused": body.paused}
