"""M3's identity review queue. A queue, not an integration.

When research turns up something that questions who a company *is* — a parent
entity in a registry filing, a name that does not match, a domain that belongs
to someone else — M3 raises a signal for a human. It does **not** create
provider entities, write resolution decisions, merge companies or edit domains.
Those belong to M2, and an inference drawn from a marketing page is not a
reason to rewrite canonical identity.

The concern is durable; the review episode is not. A dismissed signal that
reappears opens a **new occurrence** against the same concern, so "we looked at
this before and said no" survives, and so does "it came back".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.research.domain.models import (
    IdentityReviewSignal,
    IdentityReviewSignalEvent,
    IdentityReviewSignalEvidence,
    IdentityReviewSignalOccurrence,
)
from boro_gtm.research.policies import SIGNAL_POLICY_VERSION, sha256_json


def normalize_concern(text: str) -> str:
    """A stable spelling for a concern, so the same worry is one row."""
    return " ".join(text.strip().upper().split())


def signal_fingerprint(
    *, company_id: uuid.UUID, related_company_id: uuid.UUID | None,
    signal_kind: str, normalized_concern: str,
) -> str:
    return sha256_json({
        "company_id": str(company_id),
        "related_company_id": str(related_company_id) if related_company_id else None,
        "signal_kind": signal_kind,
        "normalized_concern": normalized_concern,
        "signal_policy_version": SIGNAL_POLICY_VERSION,
    })


@dataclass(slots=True)
class SignalOutcome:
    signal: IdentityReviewSignal
    occurrence: IdentityReviewSignalOccurrence
    signal_created: bool
    occurrence_created: bool


def raise_signal(
    session: Session,
    *,
    company_id: uuid.UUID,
    signal_kind: str,
    concern: str,
    attempt_id: uuid.UUID,
    evidence_item_ids: list[uuid.UUID],
    now: datetime,
    related_company_id: uuid.UUID | None = None,
) -> SignalOutcome:
    """Raise a concern, or reopen one that was closed and has come back."""
    normalized = normalize_concern(concern)
    fingerprint = signal_fingerprint(
        company_id=company_id, related_company_id=related_company_id,
        signal_kind=signal_kind, normalized_concern=normalized,
    )
    result = session.execute(
        pg_insert(IdentityReviewSignal)
        .values(
            id=uuid.uuid4(), company_id=company_id, related_company_id=related_company_id,
            signal_kind=signal_kind, normalized_concern=normalized,
            signal_policy_version=SIGNAL_POLICY_VERSION,
            signal_fingerprint=fingerprint, first_raised_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_signal_fingerprint")
        .returning(IdentityReviewSignal.id)
    )
    row = result.first()
    signal_created = row is not None
    if signal_created:
        signal = session.get(IdentityReviewSignal, row[0])
    else:
        signal = session.scalars(
            select(IdentityReviewSignal).where(
                IdentityReviewSignal.signal_fingerprint == fingerprint
            )
        ).one()

    occurrence = session.scalars(
        select(IdentityReviewSignalOccurrence).where(
            IdentityReviewSignalOccurrence.signal_id == signal.id,
            IdentityReviewSignalOccurrence.is_open.is_(True),
        )
    ).first()
    occurrence_created = False
    if occurrence is None:
        occurrence = _open_occurrence(session, signal=signal, attempt_id=attempt_id, now=now)
        occurrence_created = True

    for evidence_id in evidence_item_ids:
        session.execute(
            pg_insert(IdentityReviewSignalEvidence)
            .values(
                id=uuid.uuid4(), signal_occurrence_id=occurrence.id,
                evidence_item_id=evidence_id, created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_signal_evidence")
        )
    return SignalOutcome(
        signal=signal, occurrence=occurrence,
        signal_created=signal_created, occurrence_created=occurrence_created,
    )


def _open_occurrence(
    session: Session, *, signal: IdentityReviewSignal, attempt_id: uuid.UUID,
    now: datetime,
) -> IdentityReviewSignalOccurrence:
    last = session.scalar(
        select(func.max(IdentityReviewSignalOccurrence.occurrence_number)).where(
            IdentityReviewSignalOccurrence.signal_id == signal.id
        )
    )
    previous = None
    if last:
        previous = session.scalars(
            select(IdentityReviewSignalOccurrence).where(
                IdentityReviewSignalOccurrence.signal_id == signal.id,
                IdentityReviewSignalOccurrence.occurrence_number == last,
            )
        ).one()
    occurrence = IdentityReviewSignalOccurrence(
        id=uuid.uuid4(),
        signal_id=signal.id,
        occurrence_number=(last or 0) + 1,
        raised_by_attempt_id=attempt_id,
        supersedes_occurrence_id=previous.id if previous is not None else None,
        is_open=True,
        raised_at=now,
    )
    session.add(occurrence)
    session.flush()
    session.add(IdentityReviewSignalEvent(
        id=uuid.uuid4(), occurrence_id=occurrence.id, status="OPEN",
        actor="m3-pipeline", occurred_at=now,
    ))
    session.flush()
    return occurrence


def append_status(
    session: Session, *, occurrence_id: uuid.UUID, status: str, actor: str,
    now: datetime, note: str | None = None,
) -> IdentityReviewSignalEvent:
    """Append a review decision. The transition trigger enforces legality."""
    event = IdentityReviewSignalEvent(
        id=uuid.uuid4(), occurrence_id=occurrence_id, status=status, actor=actor,
        note=note, occurred_at=now,
    )
    session.add(event)
    session.flush()
    return event
