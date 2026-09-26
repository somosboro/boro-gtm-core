"""Research gaps: what we looked for and did not find, said out loud.

A gap is the honest half of coverage. Without it, "no ERP evidence" and "no
ERP" are the same row, and every downstream reader has to guess which one it
is looking at.

The parent row is identity only — a company, an attribute, a kind, per research
question. Everything that changes (was it attempted, by whom, did it resolve)
is an appended event, so the history of a stubborn gap is readable rather than
overwritten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.research.domain.models import (
    OperationalResearchGap,
    OperationalResearchGapEvent,
)


@dataclass(slots=True)
class GapOutcome:
    gap: OperationalResearchGap
    created: bool
    event_created: bool


def raise_gap(
    session: Session,
    *,
    run_id: uuid.UUID,
    company_id: uuid.UUID,
    attribute_key: str,
    gap_kind: str,
    attempt_id: uuid.UUID,
    now: datetime,
    note: str | None = None,
) -> GapOutcome:
    """Create the gap if it is new, and append a ``RAISED`` event if it is."""
    result = session.execute(
        pg_insert(OperationalResearchGap)
        .values(
            id=uuid.uuid4(), run_id=run_id, company_id=company_id,
            attribute_key=attribute_key, gap_kind=gap_kind, first_raised_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_gap_identity")
        .returning(OperationalResearchGap.id)
    )
    row = result.first()
    created = row is not None
    if created:
        gap = session.get(OperationalResearchGap, row[0])
    else:
        gap = session.scalars(
            select(OperationalResearchGap).where(
                OperationalResearchGap.run_id == run_id,
                OperationalResearchGap.attribute_key == attribute_key,
                OperationalResearchGap.gap_kind == gap_kind,
            )
        ).one()

    event_created = False
    if created:
        event_created = _append_event(
            session, gap_id=gap.id, kind="RAISED", attempt_id=attempt_id,
            now=now, note=note,
        )
    return GapOutcome(gap=gap, created=created, event_created=event_created)


def record_attempt(
    session: Session,
    *,
    gap_id: uuid.UUID,
    attempt_id: uuid.UUID,
    now: datetime,
    source_id: uuid.UUID | None = None,
    fetch_event_id: uuid.UUID | None = None,
    note: str | None = None,
) -> bool:
    """An ``ATTEMPTED`` event names the retrieval that performed it.

    Metrics such as "how many times have we chased this" are then derived from
    the events, never from a counter someone has to remember to increment.
    """
    return _append_event(
        session, gap_id=gap_id, kind="ATTEMPTED", attempt_id=attempt_id, now=now,
        source_id=source_id, fetch_event_id=fetch_event_id, note=note,
    )


def resolve_gap(
    session: Session,
    *,
    gap_id: uuid.UUID,
    attempt_id: uuid.UUID,
    claim_id: uuid.UUID,
    now: datetime,
) -> bool:
    return _append_event(
        session, gap_id=gap_id, kind="RESOLVED", attempt_id=attempt_id, now=now,
        resolved_by_claim_id=claim_id,
    )


def abandon_gap(
    session: Session, *, gap_id: uuid.UUID, attempt_id: uuid.UUID, now: datetime,
    note: str | None = None,
) -> bool:
    return _append_event(
        session, gap_id=gap_id, kind="ABANDONED", attempt_id=attempt_id, now=now,
        note=note,
    )


def _append_event(
    session: Session,
    *,
    gap_id: uuid.UUID,
    kind: str,
    attempt_id: uuid.UUID,
    now: datetime,
    source_id: uuid.UUID | None = None,
    fetch_event_id: uuid.UUID | None = None,
    resolved_by_claim_id: uuid.UUID | None = None,
    note: str | None = None,
) -> bool:
    result = session.execute(
        pg_insert(OperationalResearchGapEvent)
        .values(
            id=uuid.uuid4(), gap_id=gap_id, event_kind=kind, attempt_id=attempt_id,
            source_id=source_id, fetch_event_id=fetch_event_id,
            resolved_by_claim_id=resolved_by_claim_id, note=note, occurred_at=now,
        )
        .on_conflict_do_nothing(index_elements=[
            OperationalResearchGapEvent.gap_id,
            OperationalResearchGapEvent.event_kind,
            OperationalResearchGapEvent.attempt_id,
            OperationalResearchGapEvent.source_id,
            OperationalResearchGapEvent.fetch_event_id,
        ])
        .returning(OperationalResearchGapEvent.id)
    )
    return result.first() is not None


def current_status(session: Session, gap_id: uuid.UUID) -> str | None:
    """The latest event, because the gap row itself holds no status."""
    return session.scalars(
        select(OperationalResearchGapEvent.event_kind)
        .where(OperationalResearchGapEvent.gap_id == gap_id)
        .order_by(
            OperationalResearchGapEvent.occurred_at.desc(),
            OperationalResearchGapEvent.id.desc(),
        )
        .limit(1)
    ).first()


def open_gap_count(session: Session, run_id: uuid.UUID) -> int:
    gaps = session.scalars(
        select(OperationalResearchGap.id).where(OperationalResearchGap.run_id == run_id)
    ).all()
    return sum(1 for gap_id in gaps if current_status(session, gap_id) in
               {"RAISED", "ATTEMPTED"})


def attempt_count(session: Session, gap_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count())
        .select_from(OperationalResearchGapEvent)
        .where(
            OperationalResearchGapEvent.gap_id == gap_id,
            OperationalResearchGapEvent.event_kind == "ATTEMPTED",
        )
    ) or 0
