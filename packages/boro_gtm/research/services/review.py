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
from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    ClaimEvidenceLink,
    IdentityReviewSignalEvent,
    IdentityReviewSignalOccurrence,
    ResearchArtifactDerivation,
    ResearchEvidenceItem,
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


class ReviewNotApplicableError(GtmError):
    """The claim has no model-sampled lineage to review."""

    code = "REVIEW_NOT_APPLICABLE"
    http_status = 409


@dataclass(slots=True)
class ReviewOutcome:
    claim_id: uuid.UUID
    decision: str
    human_extraction_id: uuid.UUID | None = None
    confirmed_claim_id: uuid.UUID | None = None
    evidence_item_ids: list[uuid.UUID] = field(default_factory=list)
    model_extraction_unchanged: bool = True
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "decision": self.decision,
            "human_extraction_id": self.human_extraction_id,
            "confirmed_claim_id": self.confirmed_claim_id,
            "evidence_item_ids": self.evidence_item_ids,
            "model_extraction_unchanged": self.model_extraction_unchanged,
            "note": self.note,
        }


def review_claim(
    session: Session,
    *,
    claim_id: uuid.UUID,
    decision: str,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewOutcome:
    """Append a review of one claim's evidence."""
    now = now or datetime.now(UTC)
    decision = decision.strip().upper()
    if decision not in {CONFIRM, REJECT}:
        raise ValidationError(
            f"decision must be {CONFIRM} or {REJECT}", {"decision": decision}
        )
    claim = session.get(CompanyClaim, claim_id)
    if claim is None:
        raise NotFoundError(f"no claim {claim_id}")

    links = session.scalars(
        select(ClaimEvidenceLink).where(ClaimEvidenceLink.claim_id == claim_id)
    ).all()
    if not links:
        raise ReviewNotApplicableError(
            f"claim {claim_id} has no evidence to review"
        )

    before = {
        row.id: (row.raw_output_sha256, row.extraction_contract_hash)
        for row in session.scalars(
            select(ResearchExtraction)
            .join(ResearchEvidenceItem,
                  ResearchEvidenceItem.extraction_id == ResearchExtraction.id)
            .where(ResearchEvidenceItem.id.in_(
                [link.evidence_item_id for link in links]
            ))
        ).all()
    }

    if decision == REJECT:
        # Recorded, and nothing asserted. The model's work is untouched.
        return ReviewOutcome(
            claim_id=claim_id, decision=REJECT,
            model_extraction_unchanged=_unchanged(session, before),
            note=note or f"rejected by {actor}",
        )

    outcome = _confirm(session, claim=claim, links=list(links), actor=actor, now=now)
    outcome.model_extraction_unchanged = _unchanged(session, before)
    outcome.note = note
    return outcome


def _unchanged(session: Session, before: dict) -> bool:
    session.flush()
    after = {
        row.id: (row.raw_output_sha256, row.extraction_contract_hash)
        for row in session.scalars(
            select(ResearchExtraction).where(ResearchExtraction.id.in_(list(before)))
        ).all()
    }
    return after == before


def _confirm(
    session: Session, *, claim: CompanyClaim, links: list[ClaimEvidenceLink],
    actor: str, now: datetime,
) -> ReviewOutcome:
    """Create the HUMAN lineage that makes a sampled reading assertable."""
    from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
    from boro_gtm.research.services.extraction import (
        HUMAN_EXTRACTOR,
        Observation,
        run_extraction,
    )

    item = session.get(ResearchEvidenceItem, links[0].evidence_item_id)
    text_derivation = session.scalars(
        select(ResearchTextDerivation).where(
            ResearchTextDerivation.id == (
                session.get(ResearchExtraction, item.extraction_id).text_derivation_id
            )
        )
    ).one()
    attempt_id = session.get(ResearchFetchEvent, item.fetch_event_id).attempt_id

    reading = run_extraction(
        session, extractor=HUMAN_EXTRACTOR, text_derivation=text_derivation,
        attempt_id=attempt_id, now=now,
    )
    derivation = session.get(ResearchArtifactDerivation, item.artifact_derivation_id)
    context = EvidenceContext(
        extraction_id=reading.extraction.id, fetch_event_id=item.fetch_event_id,
        artifact_derivation_id=derivation.id, body_id=item.body_id,
        source=session.get(ResearchSource, item.source_id),
    )

    created_ids: list[uuid.UUID] = []
    confirming: list[tuple[Observation, uuid.UUID]] = []
    for observation in reading.observations:
        evidence, _ = create_evidence_item(
            session, context=context, observation=observation, now=now
        )
        created_ids.append(evidence.id)
        confirming.append((observation, evidence.id))

    confirmed_claim_id: uuid.UUID | None = None
    if confirming:
        from boro_gtm.research.services.claims import assert_claim, group_observations

        run_company = claim.subject_company_id
        for pending in group_observations(session, confirming):
            result = assert_claim(
                session, company_id=run_company, pending=pending, now=now
            )
            if result.claim.attribute_key == claim.attribute_key:
                confirmed_claim_id = result.claim.id
    session.flush()
    return ReviewOutcome(
        claim_id=claim.id, decision=CONFIRM,
        human_extraction_id=reading.extraction.id,
        confirmed_claim_id=confirmed_claim_id,
        evidence_item_ids=sorted(created_ids, key=str),
    )


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
