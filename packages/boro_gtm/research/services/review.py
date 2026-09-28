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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError, NotFoundError, ValidationError
from boro_gtm.research.domain.models import (
    IdentityReviewSignalEvent,
    IdentityReviewSignalOccurrence,
    OperationalResearchAttempt,
    OperationalResearchRun,
    ResearchArtifactBody,
    ResearchEvidenceItem,
    ResearchEvidenceReview,
    ResearchExtraction,
    ResearchFetchEvent,
    ResearchReviewCandidate,
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


SAMPLED_REASON = "SAMPLED_REQUIRES_CONFIRMATION"
LOW_CONFIDENCE_REASON = "LOW_CONFIDENCE_REQUIRES_REVIEW"


class NotReviewableError(GtmError):
    """The evidence item is not awaiting a human decision.

    "Reviewable" is a state, not the caller's knowledge of an id. Without this,
    an ordinary deterministic observation could be pushed through confirmation
    and acquire a HUMAN extraction nobody asked for.
    """

    code = "EVIDENCE_NOT_REVIEWABLE"
    http_status = 409


class UnresolvableProvenanceError(GtmError):
    """The evidence does not resolve to exactly one canonical company."""

    code = "EVIDENCE_PROVENANCE_UNRESOLVABLE"
    http_status = 409


@dataclass(slots=True)
class ReviewOutcome:
    """The durable record a review produced."""

    review_id: uuid.UUID
    evidence_item_id: uuid.UUID
    company_id: uuid.UUID
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
            "company_id": self.company_id,
            "decision": self.decision,
            "actor": self.actor,
            "note": self.note,
            "reviewed_at": self.reviewed_at,
            "human_extraction_id": self.human_extraction_id,
            "resulting_claim_id": self.resulting_claim_id,
            "created_evidence_item_ids": self.created_evidence_item_ids,
            "model_extraction_unchanged": self.model_extraction_unchanged,
        }


# ---------------------------------------------------------------------------
# Provenance: who the evidence is *about*
# ---------------------------------------------------------------------------


def company_of_evidence(session: Session, evidence_item_id: uuid.UUID) -> uuid.UUID:
    """Derive the subject company from the evidence's own provenance.

    evidence → fetch event → attempt → run → company.

    The caller does not get to choose. When the company was a request field,
    evidence captured while researching company A could be confirmed into
    company B — which is not a permissions problem, it is evidence about one
    organisation being asserted about another.
    """
    companies = session.scalars(
        select(OperationalResearchRun.company_id)
        .join(OperationalResearchAttempt,
              OperationalResearchAttempt.run_id == OperationalResearchRun.id)
        .join(ResearchFetchEvent,
              ResearchFetchEvent.attempt_id == OperationalResearchAttempt.id)
        .join(ResearchEvidenceItem,
              ResearchEvidenceItem.fetch_event_id == ResearchFetchEvent.id)
        .where(ResearchEvidenceItem.id == evidence_item_id)
        .distinct()
    ).all()
    if len(companies) != 1:
        raise UnresolvableProvenanceError(
            f"evidence {evidence_item_id} resolves to {len(companies)} companies; "
            "a review may only assert about the company its evidence was "
            "captured for",
            {"evidence_item_id": str(evidence_item_id), "companies": len(companies)},
        )
    return companies[0]


# ---------------------------------------------------------------------------
# The review queue
# ---------------------------------------------------------------------------


def raise_candidate(
    session: Session,
    *,
    evidence_item_id: uuid.UUID,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    company_id: uuid.UUID,
    attribute_key: str,
    reason: str,
    now: datetime,
    extractor_confidence: float | None = None,
) -> bool:
    """Put one observation in the queue. Idempotent per question."""
    result = session.execute(
        pg_insert(ResearchReviewCandidate)
        .values(
            id=uuid.uuid4(), evidence_item_id=evidence_item_id, run_id=run_id,
            attempt_id=attempt_id, company_id=company_id,
            attribute_key=attribute_key, reason=reason,
            extractor_confidence=extractor_confidence, raised_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_candidate_identity")
        .returning(ResearchReviewCandidate.id)
    )
    return result.first() is not None


def pending_candidates(
    session: Session,
    *,
    company_id: uuid.UUID | None = None,
    reason: str | None = None,
    limit: int = 50,
) -> list[ResearchReviewCandidate]:
    """Candidates nobody has decided on yet, durably.

    Survives the process that raised them, which is the whole point: a
    reviewer arriving tomorrow must be able to find what is waiting.
    """
    decided = select(ResearchEvidenceReview.evidence_item_id)
    statement = (
        select(ResearchReviewCandidate)
        .where(ResearchReviewCandidate.evidence_item_id.not_in(decided))
        .order_by(ResearchReviewCandidate.raised_at, ResearchReviewCandidate.id)
        .limit(limit)
    )
    if company_id is not None:
        statement = statement.where(ResearchReviewCandidate.company_id == company_id)
    if reason is not None:
        statement = statement.where(ResearchReviewCandidate.reason == reason)
    return list(session.scalars(statement).all())


def candidate_for(
    session: Session, evidence_item_id: uuid.UUID
) -> ResearchReviewCandidate | None:
    return session.scalars(
        select(ResearchReviewCandidate)
        .where(ResearchReviewCandidate.evidence_item_id == evidence_item_id)
        .order_by(ResearchReviewCandidate.raised_at)
        .limit(1)
    ).first()


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def review_evidence(
    session: Session,
    *,
    evidence_item_id: uuid.UUID,
    decision: str,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewOutcome:
    """Record a human decision about **one** observation. Always durable.

    The subject company is derived from provenance and the confirmation is
    scoped to the reviewed span — neither is the caller's to choose, and both
    were before.
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

    candidate = candidate_for(session, evidence_item_id)
    if candidate is None:
        raise NotReviewableError(
            f"evidence {evidence_item_id} is not awaiting review; only a sampled "
            "reading or a low-confidence observation is reviewable",
            {"evidence_item_id": str(evidence_item_id)},
        )
    company_id = company_of_evidence(session, evidence_item_id)
    if company_id != candidate.company_id:      # pragma: no cover - defensive
        raise UnresolvableProvenanceError(
            "the candidate and the evidence provenance disagree on the company"
        )

    extraction = session.get(ResearchExtraction, item.extraction_id)
    before = (extraction.raw_output_sha256, extraction.extraction_contract_hash,
              str(extraction.observations))

    human_extraction_id: uuid.UUID | None = None
    resulting_claim_id: uuid.UUID | None = None
    created_ids: list[uuid.UUID] = []

    if decision == CONFIRM:
        human_extraction_id, resulting_claim_id, created_ids = _confirm(
            session, item=item, company_id=company_id,
            attribute_key=candidate.attribute_key, now=now,
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
        review_id=review_id, evidence_item_id=evidence_item_id,
        company_id=company_id, decision=decision, actor=actor, note=note,
        reviewed_at=now, human_extraction_id=human_extraction_id,
        resulting_claim_id=resulting_claim_id,
        created_evidence_item_ids=created_ids,
        model_extraction_unchanged=unchanged,
    )


def _confirm(
    session: Session, *, item: ResearchEvidenceItem, company_id: uuid.UUID,
    attribute_key: str, now: datetime,
) -> tuple[uuid.UUID, uuid.UUID | None, list[uuid.UUID]]:
    """Create the HUMAN lineage for **the reviewed observation only**.

    Scoped by the reviewed item's locator hash. Confirming one span used to run
    the human extractor across the whole document and assert every observation
    in it — four claims from one review, on evidence nobody had looked at.
    """
    from boro_gtm.research.services.claims import PendingAssertion, assert_claim
    from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
    from boro_gtm.research.services.extraction import (
        extractors_for,
        run_extraction,
        scoped_human_extractor,
    )

    sampled = session.get(ResearchExtraction, item.extraction_id)
    text_derivation = session.get(ResearchTextDerivation, sampled.text_derivation_id)
    fetch = session.get(ResearchFetchEvent, item.fetch_event_id)

    source_extractor = next(
        (x for x in extractors_for(_media_type_of(session, item))
         if x.extractor_id == sampled.extractor_id),
        None,
    )
    if source_extractor is None:      # pragma: no cover - fixture contract
        raise NotReviewableError(
            f"no extractor named {sampled.extractor_id} is registered, so the "
            "reviewed observation cannot be reproduced"
        )

    human = scoped_human_extractor(
        locator_hash=item.locator_hash, attribute_key=attribute_key,
        source_extractor=source_extractor,
    )
    # The same context the pipeline extracted under. An HTML locator carries a
    # structural path computed from the raw document, so re-running without it
    # produces a *different* locator hash and the reviewed span matches
    # nothing — which presented as "the span no longer resolves".
    body = session.get(ResearchArtifactBody, item.body_id)
    if body.raw_body is None:
        raise NotReviewableError(
            f"body {body.id} was pruned at {body.pruned_at}; the reviewed span "
            "cannot be reproduced without a refetch",
            {"body_id": str(body.id)},
        )
    reading = run_extraction(
        session, extractor=human, text_derivation=text_derivation,
        attempt_id=fetch.attempt_id, now=now, context={"raw": body.raw_body},
    )
    if not reading.observations:      # pragma: no cover - fixture contract
        raise NotReviewableError(
            f"the reviewed span no longer resolves in {text_derivation.id}; "
            "confirm against a re-derived reading instead"
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

    from boro_gtm.research.services.claims import group_observations

    groups = group_observations(session, confirming)
    if len(groups) > 1:     # pragma: no cover - the scope makes this unreachable
        raise NotReviewableError(
            "the reviewed scope resolves to several assertions; a review "
            "confirms one attribute at one span",
            {"groups": len(groups)},
        )
    # One attribute at one span is one value from one origin, so at most one
    # assertion. `resulting_claim_id` is singular because the operation is.
    resulting_claim_id = None
    for pending in groups:
        outcome = assert_claim(
            session, company_id=company_id,
            pending=PendingAssertion(
                attribute_key=pending.attribute_key, value=pending.value,
                unit=pending.unit, fact_type=pending.fact_type,
                support_kind=pending.support_kind,
                evidence_item_ids=pending.evidence_item_ids,
            ),
            now=now,
        )
        resulting_claim_id = outcome.claim.id
    session.flush()
    return reading.extraction.id, resulting_claim_id, sorted(created_ids, key=str)


def _media_type_of(session: Session, item: ResearchEvidenceItem) -> str:
    from boro_gtm.research.domain.models import ResearchBodyClassification

    return session.scalar(
        select(ResearchBodyClassification.sniffed_media_type)
        .where(ResearchBodyClassification.body_id == item.body_id)
        .limit(1)
    )


def reviews_for(
    session: Session, evidence_item_id: uuid.UUID
) -> list[ResearchEvidenceReview]:
    return list(session.scalars(
        select(ResearchEvidenceReview)
        .where(ResearchEvidenceReview.evidence_item_id == evidence_item_id)
        .order_by(ResearchEvidenceReview.reviewed_at, ResearchEvidenceReview.id)
    ).all())


def evidence_created_by(
    session: Session, review_row: ResearchEvidenceReview
) -> list[uuid.UUID]:
    """The HUMAN evidence a confirmation produced, from persisted provenance.

    Reconstructed rather than remembered: the history endpoint used to emit an
    empty list for every past review, which is false for every confirmation.
    """
    if review_row.human_extraction_id is None:
        return []
    return sorted(session.scalars(
        select(ResearchEvidenceItem.id).where(
            ResearchEvidenceItem.extraction_id == review_row.human_extraction_id
        ).order_by(ResearchEvidenceItem.id)
    ).all(), key=str)


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
    # Serialize the lifecycle deliberately. Without this, two writers both read
    # ACKNOWLEDGED, both find their terminal move legal, and one hits
    # `uq_signal_terminal_event` as a raw IntegrityError. The row lock makes the
    # loser wait, re-read, and get a domain error naming the transition.
    session.execute(
        select(IdentityReviewSignalOccurrence.id)
        .where(IdentityReviewSignalOccurrence.id == occurrence.id)
        .with_for_update()
    ).all()
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
    try:
        with session.begin_nested():
            result = _insert_signal_event(
                session, event_id, occurrence.id, status, actor, note, now
            )
    except IntegrityError as exc:
        # `uq_signal_terminal_event`: another reviewer reached a *different*
        # terminal state. The lock above makes this all but unreachable; it
        # remains as the backstop for a writer that bypasses this function.
        raise IllegalSignalTransitionError(
            f"occurrence {occurrence.occurrence_number} already reached a "
            "terminal state; re-read before deciding",
            {"occurrence": occurrence.occurrence_number, "status": status},
        ) from exc
    if result.first() is None:
        raise IllegalSignalTransitionError(
            f"another reviewer already recorded {status} for occurrence "
            f"{occurrence.occurrence_number}; re-read before deciding",
            {"occurrence": occurrence.occurrence_number, "status": status},
        )
    session.flush()
    return session.get(IdentityReviewSignalEvent, event_id)


def _insert_signal_event(
    session: Session, event_id: uuid.UUID, occurrence_id: uuid.UUID,
    status: str, actor: str, note: str | None, now: datetime,
):
    """A Core insert with ON CONFLICT, the pattern M2's resolution service uses.

    An ORM `add` is flushed outside the savepoint, so its failure escapes as a
    `PendingRollbackError` that this function cannot translate.
    """
    return session.execute(
        pg_insert(IdentityReviewSignalEvent)
        .values(
            id=event_id, occurrence_id=occurrence_id, status=status, actor=actor,
            note=note, occurred_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_signal_event")
        .returning(IdentityReviewSignalEvent.id)
    )
