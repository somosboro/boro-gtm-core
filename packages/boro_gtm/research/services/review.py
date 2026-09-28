"""Human review — of evidence, and of identity concerns.

Both are append-only, and both refuse the same tempting shortcut.

**Confirming** a model's reading creates a `HUMAN` extraction and its own
evidence, and leaves the model's extraction and claim exactly as they were. The
human's judgement is a new observation, not an edit of the machine's.

**Rejecting** records that a reviewer did not accept it — and asserts nothing.
"A reviewer did not believe this" is not evidence that the opposite is true, and
writing a negative fact here would be M3 fabricating one. There is no mutable
`rejected` flag on the claim either: the row is append-only and a flag on it
would be exactly the "append-only except when we feel like it" that the schema
rejects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError, NotFoundError, ValidationError
from boro_gtm.research.domain.models import (
    IdentityReviewSignalEvent,
    IdentityReviewSignalOccurrence,
    ResearchEvidenceItem,
    ResearchEvidenceReview,
    ResearchExtraction,
    ResearchFetchEvent,
    ResearchSource,
    ResearchTextDerivation,
)
from boro_gtm.research.enums import LEGAL_SIGNAL_TRANSITIONS

CONFIRM = "CONFIRM"
REJECT = "REJECT"


class IllegalSignalTransitionError(GtmError):
    """A review decision the transition graph does not permit."""

    code = "SIGNAL_TRANSITION_ILLEGAL"
    http_status = 409


class DuplicateReviewError(GtmError):
    """The same reviewer already recorded this decision at this instant."""

    code = "REVIEW_DUPLICATE"
    http_status = 409


@dataclass(slots=True)
class ReviewOutcome:
    """The durable record a review produced."""

    review_id: uuid.UUID
    evidence_item_id: uuid.UUID
    decision: str
    actor: str
    note: str | None
    reviewed_at: datetime
    human_extraction_id: uuid.UUID | None = None
    resulting_claim_id: uuid.UUID | None = None
    created_evidence_item_ids: list[uuid.UUID] = field(default_factory=list)
    model_extraction_unchanged: bool = True

    def as_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "evidence_item_id": self.evidence_item_id,
            "decision": self.decision,
            "actor": self.actor,
            "note": self.note,
            "reviewed_at": self.reviewed_at,
            "human_extraction_id": self.human_extraction_id,
            "resulting_claim_id": self.resulting_claim_id,
            "created_evidence_item_ids": self.created_evidence_item_ids,
            "model_extraction_unchanged": self.model_extraction_unchanged,
        }


def pending_review_items(
    session: Session, *, company_id: uuid.UUID | None = None, limit: int = 50
) -> list[ResearchEvidenceItem]:
    """Sampled readings that have not been reviewed and support no claim.

    This is the queue a reviewer works. It exists *before* any claim does,
    which is the whole point: a sampled reading cannot assert on its own, so
    there is nothing else to key the workflow on.
    """
    reviewed = select(ResearchEvidenceReview.evidence_item_id)
    statement = (
        select(ResearchEvidenceItem)
        .join(ResearchExtraction,
              ResearchExtraction.id == ResearchEvidenceItem.extraction_id)
        .where(
            ResearchExtraction.determinism == "SAMPLED",
            ResearchEvidenceItem.id.not_in(reviewed),
        )
        .order_by(ResearchEvidenceItem.created_at, ResearchEvidenceItem.id)
        .limit(limit)
    )
    if company_id is not None:
        statement = statement.join(
            ResearchFetchEvent,
            ResearchFetchEvent.id == ResearchEvidenceItem.fetch_event_id,
        )
    return list(session.scalars(statement).all())


def review_evidence(
    session: Session,
    *,
    evidence_item_id: uuid.UUID,
    decision: str,
    actor: str,
    company_id: uuid.UUID | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewOutcome:
    """Record a human decision about one observation. Always durable.

    Both outcomes write a row. An earlier version returned a rejection as an
    in-memory object and persisted nothing, so after the request there was no
    record that anyone had reviewed anything, who, when, or why — in an
    evidence system, of all places.
    """
    now = now or datetime.now(UTC)
    decision = decision.strip().upper()
    if decision not in {CONFIRM, REJECT}:
        raise ValidationError(
            f"decision must be {CONFIRM} or {REJECT}", {"decision": decision}
        )
    item = session.get(ResearchEvidenceItem, evidence_item_id)
    if item is None:
        raise NotFoundError(f"no evidence item {evidence_item_id}")

    extraction = session.get(ResearchExtraction, item.extraction_id)
    before = (extraction.raw_output_sha256, extraction.extraction_contract_hash,
              str(extraction.observations))

    human_extraction_id: uuid.UUID | None = None
    resulting_claim_id: uuid.UUID | None = None
    created_ids: list[uuid.UUID] = []

    if decision == CONFIRM:
        human_extraction_id, resulting_claim_id, created_ids = _confirm(
            session, item=item, company_id=company_id, now=now
        )

    review_id = uuid.uuid4()
    result = session.execute(
        pg_insert(ResearchEvidenceReview)
        .values(
            id=review_id, evidence_item_id=evidence_item_id, decision=decision,
            actor=actor, note=note, human_extraction_id=human_extraction_id,
            resulting_claim_id=resulting_claim_id, reviewed_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_review_identity")
        .returning(ResearchEvidenceReview.id)
    )
    if result.first() is None:
        raise DuplicateReviewError(
            f"{actor} already recorded a review of evidence {evidence_item_id} "
            f"at {now.isoformat()}",
            {"evidence_item_id": str(evidence_item_id), "actor": actor},
        )
    session.flush()

    session.refresh(extraction)
    unchanged = (extraction.raw_output_sha256, extraction.extraction_contract_hash,
                 str(extraction.observations)) == before

    return ReviewOutcome(
        review_id=review_id, evidence_item_id=evidence_item_id, decision=decision,
        actor=actor, note=note, reviewed_at=now,
        human_extraction_id=human_extraction_id,
        resulting_claim_id=resulting_claim_id,
        created_evidence_item_ids=created_ids,
        model_extraction_unchanged=unchanged,
    )


def _confirm(
    session: Session, *, item: ResearchEvidenceItem, company_id: uuid.UUID | None,
    now: datetime,
) -> tuple[uuid.UUID, uuid.UUID | None, list[uuid.UUID]]:
    """Create the HUMAN lineage that makes a sampled reading assertable.

    The sampled evidence is untouched. What changes is that an assertable
    lineage now exists beside it.
    """
    from boro_gtm.research.services.claims import assert_claim, group_observations
    from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
    from boro_gtm.research.services.extraction import HUMAN_EXTRACTOR, run_extraction

    sampled = session.get(ResearchExtraction, item.extraction_id)
    text_derivation = session.get(ResearchTextDerivation, sampled.text_derivation_id)
    fetch = session.get(ResearchFetchEvent, item.fetch_event_id)

    reading = run_extraction(
        session, extractor=HUMAN_EXTRACTOR, text_derivation=text_derivation,
        attempt_id=fetch.attempt_id, now=now,
    )
    context = EvidenceContext(
        extraction_id=reading.extraction.id, fetch_event_id=item.fetch_event_id,
        artifact_derivation_id=item.artifact_derivation_id, body_id=item.body_id,
        source=session.get(ResearchSource, item.source_id),
    )

    created_ids: list[uuid.UUID] = []
    confirming = []
    for observation in reading.observations:
        evidence, _ = create_evidence_item(
            session, context=context, observation=observation, now=now
        )
        created_ids.append(evidence.id)
        confirming.append((observation, evidence.id))

    resulting_claim_id: uuid.UUID | None = None
    if confirming and company_id is not None:
        for pending in group_observations(session, confirming):
            outcome = assert_claim(
                session, company_id=company_id, pending=pending, now=now
            )
            if resulting_claim_id is None:
                resulting_claim_id = outcome.claim.id
    session.flush()
    return reading.extraction.id, resulting_claim_id, sorted(created_ids, key=str)


def reviews_for(
    session: Session, evidence_item_id: uuid.UUID
) -> list[ResearchEvidenceReview]:
    return list(session.scalars(
        select(ResearchEvidenceReview)
        .where(ResearchEvidenceReview.evidence_item_id == evidence_item_id)
        .order_by(ResearchEvidenceReview.reviewed_at, ResearchEvidenceReview.id)
    ).all())


# ---------------------------------------------------------------------------
# Identity review
# ---------------------------------------------------------------------------


#: Position in the review lifecycle. The graph is a DAG
#: (OPEN → ACKNOWLEDGED → ACTIONED | DISMISSED), so "furthest along" is a total
#: order and a safe tiebreak for two events at the same instant.
_REVIEW_RANK: dict[str, int] = {
    "OPEN": 0, "ACKNOWLEDGED": 1, "ACTIONED": 2, "DISMISSED": 2,
}


def occurrence_status(session: Session, occurrence_id: uuid.UUID) -> str | None:
    """The latest event. The occurrence row itself stores no status.

    Ties on `occurred_at` are broken by **lifecycle position**, not by row id.
    A signal raised and acknowledged within one second — which the fixture
    pipeline does every run — otherwise reported either state depending on which
    random UUID happened to sort higher. The same defect existed in the gap
    status and is fixed the same way.
    """
    events = session.execute(
        select(
            IdentityReviewSignalEvent.status,
            IdentityReviewSignalEvent.occurred_at,
        ).where(IdentityReviewSignalEvent.occurrence_id == occurrence_id)
    ).all()
    if not events:
        return None
    return max(events, key=lambda row: (row[1], _REVIEW_RANK.get(row[0], -1)))[0]


def append_signal_status(
    session: Session,
    *,
    signal_id: uuid.UUID,
    occurrence_number: int,
    status: str,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> IdentityReviewSignalEvent:
    """Append a decision to a review episode.

    The database enforces the transition graph too; checking here means the
    caller gets a sentence naming the illegal move instead of a trigger message.
    """
    now = now or datetime.now(UTC)
    occurrence = session.scalars(
        select(IdentityReviewSignalOccurrence).where(
            IdentityReviewSignalOccurrence.signal_id == signal_id,
            IdentityReviewSignalOccurrence.occurrence_number == occurrence_number,
        )
    ).first()
    if occurrence is None:
        raise NotFoundError(
            f"no occurrence {occurrence_number} on identity signal {signal_id}"
        )
    current = occurrence_status(session, occurrence.id) or "OPEN"
    if status not in LEGAL_SIGNAL_TRANSITIONS.get(current, frozenset()):
        raise IllegalSignalTransitionError(
            f"{current} → {status} is not a legal review transition",
            {"from": current, "to": status,
             "legal": sorted(LEGAL_SIGNAL_TRANSITIONS.get(current, frozenset()))},
        )
    # A Core insert with ON CONFLICT, the pattern M2's resolution service uses:
    # an ORM `add` is flushed outside the savepoint, so the failure escapes as a
    # PendingRollbackError instead of something this function can translate.
    event_id = uuid.uuid4()
    result = session.execute(
        pg_insert(IdentityReviewSignalEvent)
        .values(
            id=event_id, occurrence_id=occurrence.id, status=status, actor=actor,
            note=note, occurred_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_signal_event")
        .returning(IdentityReviewSignalEvent.id)
    )
    if result.first() is None:
        # `uq_signal_event` is the backstop for a decision computed from a stale
        # read: the legality check above passed because this transaction still
        # saw the earlier state, and another reviewer had already recorded
        # exactly this decision. The caller gets the domain error, not an index.
        raise IllegalSignalTransitionError(
            f"another reviewer already recorded {status} for occurrence "
            f"{occurrence.occurrence_number}; re-read before deciding",
            {"occurrence": occurrence.occurrence_number, "status": status},
        )
    session.flush()
    return session.get(IdentityReviewSignalEvent, event_id)
