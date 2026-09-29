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
    OperationalResearchGap,
    OperationalResearchRun,
    ResearchEvidenceItem,
    ResearchEvidenceReview,
    ResearchExtraction,
    ResearchFetchEvent,
    ResearchReviewCandidate,
    ResearchSource,
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
    #: The observation reviewed. Everything else is derived through it: an
    #: evidence item can back several, so it could not be the key.
    review_candidate_id: uuid.UUID
    evidence_item_id: uuid.UUID
    attribute_key: str
    #: Both derived from provenance, never supplied.
    company_id: uuid.UUID
    run_id: uuid.UUID
    decision: str
    #: True when this was the first decision — the one that acted. A later
    #: review is durable dissent and changes nothing.
    is_operative: bool
    actor: str
    note: str | None
    reviewed_at: datetime
    human_extraction_id: uuid.UUID | None = None
    resulting_claim_id: uuid.UUID | None = None
    created_evidence_item_ids: list[uuid.UUID] = field(default_factory=list)
    #: The `INSUFFICIENT_EVIDENCE` gap this confirmation closed, if one was open.
    resolved_gap_id: uuid.UUID | None = None
    #: Whether the company and plan projections were refreshed synchronously.
    profiles_rebuilt: bool = False
    model_extraction_unchanged: bool = True

    def as_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "review_candidate_id": self.review_candidate_id,
            "evidence_item_id": self.evidence_item_id,
            "attribute_key": self.attribute_key,
            "company_id": self.company_id,
            "run_id": self.run_id,
            "decision": self.decision,
            "is_operative": self.is_operative,
            "actor": self.actor,
            "note": self.note,
            "reviewed_at": self.reviewed_at,
            "human_extraction_id": self.human_extraction_id,
            "resulting_claim_id": self.resulting_claim_id,
            "created_evidence_item_ids": self.created_evidence_item_ids,
            "resolved_gap_id": self.resolved_gap_id,
            "profiles_rebuilt": self.profiles_rebuilt,
            "model_extraction_unchanged": self.model_extraction_unchanged,
        }


# ---------------------------------------------------------------------------
# Provenance: who the evidence is *about*
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    """The research chain behind one evidence item, derived not stored.

    Every hop is a single-valued FK — evidence → fetch event → attempt → run →
    company — so this is a fact about the evidence, not a choice. The review
    candidate used to keep `company_id`, `run_id` and `attempt_id` as columns,
    which was three chances for a queued row to name the wrong account
    (M3-ADR-067).
    """

    evidence_item_id: uuid.UUID
    fetch_event_id: uuid.UUID
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    company_id: uuid.UUID


def provenance_of_evidence(
    session: Session, evidence_item_id: uuid.UUID
) -> EvidenceProvenance:
    """Walk the chain once. Refuses rather than guessing if it is not unique."""
    rows = session.execute(
        select(
            ResearchEvidenceItem.id, ResearchFetchEvent.id,
            OperationalResearchAttempt.id, OperationalResearchRun.id,
            OperationalResearchRun.company_id,
        )
        .join(ResearchFetchEvent,
              ResearchFetchEvent.id == ResearchEvidenceItem.fetch_event_id)
        .join(OperationalResearchAttempt,
              OperationalResearchAttempt.id == ResearchFetchEvent.attempt_id)
        .join(OperationalResearchRun,
              OperationalResearchRun.id == OperationalResearchAttempt.run_id)
        .where(ResearchEvidenceItem.id == evidence_item_id)
    ).all()
    if len(rows) != 1:
        raise UnresolvableProvenanceError(
            f"evidence {evidence_item_id} resolves to {len(rows)} research "
            "chains; a review may only assert about the company its evidence "
            "was captured for",
            {"evidence_item_id": str(evidence_item_id), "chains": len(rows)},
        )
    return EvidenceProvenance(*rows[0])


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
    attribute_key: str,
    observation_fingerprint: str,
    reason: str,
    now: datetime,
    extractor_confidence: float | None = None,
) -> bool:
    """Put **one observation occurrence** in the queue. Idempotent per occurrence.

    The unit is this reading *on this evidence item*. Two rules matching one span
    share an evidence item, so the fingerprint is needed; and the same reading
    found on a second source is a second thing a reviewer may trust or reject,
    so the evidence item is needed too. Keyed on `(run, fingerprint)` the
    pipeline made 30 raise attempts and landed 14 — sixteen questions vanished
    (M3-ADR-066).

    Company, run and attempt are not arguments: they follow from the evidence.
    """
    result = session.execute(
        pg_insert(ResearchReviewCandidate)
        .values(
            id=uuid.uuid4(), evidence_item_id=evidence_item_id,
            attribute_key=attribute_key,
            observation_fingerprint=observation_fingerprint, reason=reason,
            extractor_confidence=extractor_confidence, raised_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_candidate_occurrence")
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

    Survives the process that raised them, which is the whole point: a reviewer
    arriving tomorrow must be able to find what is waiting.

    **Pending is per candidate.** It used to exclude every candidate sharing an
    evidence item with a decided one, so deciding `technician_count` silently
    removed `field_workforce_present` from the queue.

    **Pending means undecided, not unresolved.** A candidate leaves the queue on
    its *first* decision. Later reviewers may still record an opinion — the log
    is append-only and a disagreement is information — but the queue does not
    reopen, and M3 has no consensus mechanism and needs none. The first
    decision is the operative one; the rest are recorded dissent.

    The company filter walks provenance rather than reading a stored column. It
    is four joins on indexed keys over a reviewer's queue, and it cannot show an
    operator another account's evidence because a row was written wrong.
    """
    decided = select(ResearchEvidenceReview.review_candidate_id)
    statement = (
        select(ResearchReviewCandidate)
        .where(ResearchReviewCandidate.id.not_in(decided))
        .order_by(ResearchReviewCandidate.raised_at, ResearchReviewCandidate.id)
        .limit(limit)
    )
    if company_id is not None:
        statement = statement.where(
            ResearchReviewCandidate.evidence_item_id.in_(
                select(ResearchEvidenceItem.id)
                .join(ResearchFetchEvent,
                      ResearchFetchEvent.id == ResearchEvidenceItem.fetch_event_id)
                .join(OperationalResearchAttempt,
                      OperationalResearchAttempt.id == ResearchFetchEvent.attempt_id)
                .join(OperationalResearchRun,
                      OperationalResearchRun.id == OperationalResearchAttempt.run_id)
                .where(OperationalResearchRun.company_id == company_id)
            )
        )
    if reason is not None:
        statement = statement.where(ResearchReviewCandidate.reason == reason)
    return list(session.scalars(statement).all())


def candidates_for_evidence(
    session: Session, evidence_item_id: uuid.UUID
) -> list[ResearchReviewCandidate]:
    """Every observation on one evidence item that is awaiting a decision."""
    return list(session.scalars(
        select(ResearchReviewCandidate)
        .where(ResearchReviewCandidate.evidence_item_id == evidence_item_id)
        .order_by(ResearchReviewCandidate.attribute_key, ResearchReviewCandidate.id)
    ).all())


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def review_candidate(
    session: Session,
    *,
    candidate_id: uuid.UUID,
    decision: str,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewOutcome:
    """Record a human decision about **one observation occurrence**.

    The target is the candidate, so a rejection says which observation the
    reviewer disbelieved, and a decision about a reading on one source leaves
    the same reading on another source untouched.

    The **first** decision is operative: it may append a HUMAN lineage, assert a
    claim, close the gap that was waiting on it, and refresh the account's
    projections. Every later decision is durable dissent — recorded, because a
    disagreement is information, and inert, because nothing re-runs and nothing
    reverses (M3-ADR-068).

    Nothing is executed. The observation being confirmed is the one already
    persisted on the extraction, so a confirmation does not depend on today's
    extractor, and does not need the raw body.
    """
    now = now or datetime.now(UTC)
    decision = decision.strip().upper()
    if decision not in {CONFIRM, REJECT}:
        raise ValidationError(
            f"decision must be {CONFIRM} or {REJECT}", {"decision": decision}
        )
    if session.get(ResearchReviewCandidate, candidate_id) is None:
        raise NotFoundError(f"no review candidate {candidate_id}")

    # Serialize on the candidate before deciding whether this decision is the
    # operative one. Two operators arriving together would both read "nobody
    # has decided" and both act; the lock makes the loser wait and then see the
    # decision that already happened (M3-ADR-068).
    candidate = session.scalars(
        select(ResearchReviewCandidate)
        .where(ResearchReviewCandidate.id == candidate_id)
        .with_for_update()
    ).one()

    operative_exists = session.scalar(
        select(ResearchEvidenceReview.id).where(
            ResearchEvidenceReview.review_candidate_id == candidate.id,
            ResearchEvidenceReview.is_operative.is_(True),
        ).limit(1)
    ) is not None
    is_operative = not operative_exists

    item = session.get(ResearchEvidenceItem, candidate.evidence_item_id)
    provenance = provenance_of_evidence(session, item.id)
    extraction = session.get(ResearchExtraction, item.extraction_id)
    before = (extraction.raw_output_sha256, extraction.extraction_contract_hash,
              str(extraction.observations))

    human_extraction_id: uuid.UUID | None = None
    resulting_claim_id: uuid.UUID | None = None
    created_ids: list[uuid.UUID] = []

    if decision == CONFIRM and is_operative:
        human_extraction_id, resulting_claim_id, created_ids = _confirm(
            session, candidate=candidate, item=item,
            company_id=provenance.company_id, now=now,
        )

    review_id = uuid.uuid4()
    result = session.execute(
        pg_insert(ResearchEvidenceReview)
        .values(
            id=review_id, review_candidate_id=candidate.id, decision=decision,
            is_operative=is_operative, actor=actor, note=note,
            human_extraction_id=human_extraction_id,
            resulting_claim_id=resulting_claim_id, reviewed_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_review_identity")
        .returning(ResearchEvidenceReview.id)
    )
    if result.first() is None:
        raise DuplicateReviewError(
            f"{actor} already recorded a review of candidate {candidate.id} "
            f"at {now.isoformat()}",
            {"candidate_id": str(candidate.id), "actor": actor},
        )
    session.flush()

    resolved_gap_id: uuid.UUID | None = None
    if is_operative and resulting_claim_id is not None:
        resolved_gap_id = _reconcile(
            session, candidate=candidate, provenance=provenance,
            review_id=review_id, claim_id=resulting_claim_id, now=now,
        )

    session.refresh(extraction)
    unchanged = (extraction.raw_output_sha256, extraction.extraction_contract_hash,
                 str(extraction.observations)) == before

    return ReviewOutcome(
        review_id=review_id, review_candidate_id=candidate.id,
        evidence_item_id=item.id, attribute_key=candidate.attribute_key,
        company_id=provenance.company_id, run_id=provenance.run_id,
        decision=decision, is_operative=is_operative, actor=actor, note=note,
        reviewed_at=now, human_extraction_id=human_extraction_id,
        resulting_claim_id=resulting_claim_id,
        created_evidence_item_ids=created_ids,
        resolved_gap_id=resolved_gap_id,
        profiles_rebuilt=resolved_gap_id is not None or resulting_claim_id is not None,
        model_extraction_unchanged=unchanged,
    )


def _reconcile(
    session: Session, *, candidate: ResearchReviewCandidate,
    provenance: EvidenceProvenance, review_id: uuid.UUID,
    claim_id: uuid.UUID, now: datetime,
) -> uuid.UUID | None:
    """Make the account state true immediately after an operative confirmation.

    Without this the operator saw a contradiction: the claim existed, and the
    attribute was still listed as an open `INSUFFICIENT_EVIDENCE` gap, the
    company profile still said nothing was known, and plan coverage still
    counted it as uncovered. All three were correct *at the time of the research
    attempt* and wrong the moment a human confirmed — and a GTM operator acts on
    what the account view says (M3-ADR-070).

    Synchronous on purpose. This is one attribute on one company; making it a
    queued job would mean the operator's screen is briefly lying, and there is
    nothing to gain by that.
    """
    from boro_gtm.research.services import gaps, profiles

    gap = session.scalars(
        select(OperationalResearchGap).where(
            OperationalResearchGap.run_id == provenance.run_id,
            OperationalResearchGap.attribute_key == candidate.attribute_key,
            OperationalResearchGap.gap_kind == "INSUFFICIENT_EVIDENCE",
        )
    ).first()
    resolved: uuid.UUID | None = None
    if gap is not None and gaps.current_status(session, gap.id) not in gaps.TERMINAL_GAP_KINDS:
        gaps.resolve_gap(
            session, gap_id=gap.id, claim_id=claim_id, review_id=review_id, now=now
        )
        resolved = gap.id

    # Rebuilt from the ledger, not patched: the projection is a function of the
    # claims, and the claim now exists.
    profiles.rebuild_all(
        session, company_id=provenance.company_id, run_id=provenance.run_id
    )
    return resolved


def stored_observation(
    session: Session, candidate: ResearchReviewCandidate
) -> dict:
    """The exact machine observation this candidate is about, from disk.

    `research_extractions.observations` is permanent under the retention
    contract — only `raw_output` is prunable — so the reviewed reading survives
    body pruning and extractor changes alike.
    """
    from boro_gtm.research.services.extraction import fingerprint_of_stored

    item = session.get(ResearchEvidenceItem, candidate.evidence_item_id)
    extraction = session.get(ResearchExtraction, item.extraction_id)
    payload = (extraction.observations or {}).get("observations", [])
    for stored in payload:
        if fingerprint_of_stored(stored) == candidate.observation_fingerprint:
            return stored
    raise NotReviewableError(
        f"candidate {candidate.id} names an observation that is not in "
        f"extraction {extraction.id}; the queue and the ledger disagree",
        {"candidate_id": str(candidate.id), "extraction_id": str(extraction.id)},
    )


def _confirm(
    session: Session, *, candidate: ResearchReviewCandidate,
    item: ResearchEvidenceItem, company_id: uuid.UUID, now: datetime,
) -> tuple[uuid.UUID, uuid.UUID | None, list[uuid.UUID]]:
    """Append a HUMAN lineage for the observation the human actually read.

    No extractor runs. An earlier version looked up the currently registered
    extractor by id, re-ran it over the derived text, and hoped the locator
    still reproduced — which made a durable review candidate depend on mutable
    future software, and impossible to confirm once retention pruned the bytes.
    The human is confirming the historical reading, not asking today's code what
    it would say now.
    """
    from boro_gtm.research.services.claims import (
        PendingAssertion,
        assert_claim,
        group_observations,
    )
    from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
    from boro_gtm.research.services.extraction import (
        observation_from_stored,
        record_human_extraction,
    )

    stored = stored_observation(session, candidate)
    observation = observation_from_stored(stored)
    sampled = session.get(ResearchExtraction, item.extraction_id)

    human = record_human_extraction(
        session, text_derivation_id=sampled.text_derivation_id,
        body_id=sampled.body_id, fetch_event_id=item.fetch_event_id,
        observation=observation,
        fingerprint=candidate.observation_fingerprint, now=now,
    )
    context = EvidenceContext(
        extraction_id=human.id, fetch_event_id=item.fetch_event_id,
        artifact_derivation_id=item.artifact_derivation_id, body_id=item.body_id,
        source=session.get(ResearchSource, item.source_id),
    )
    evidence, _ = create_evidence_item(
        session, context=context, observation=observation, now=now
    )
    groups = group_observations(session, [(observation, evidence.id)])
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
    return human.id, resulting_claim_id, [evidence.id]


def reviews_for(
    session: Session, candidate_id: uuid.UUID
) -> list[ResearchEvidenceReview]:
    """Every decision recorded about one observation, oldest first.

    Several are possible and all survive: the first is operative, later ones are
    recorded dissent. See `pending_candidates` for the policy.
    """
    return list(session.scalars(
        select(ResearchEvidenceReview)
        .where(ResearchEvidenceReview.review_candidate_id == candidate_id)
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


def gap_resolved_by(
    session: Session, review_row: ResearchEvidenceReview
) -> uuid.UUID | None:
    """The gap this review closed, from the gap ledger rather than memory."""
    from boro_gtm.research.domain.models import OperationalResearchGapEvent

    return session.scalar(
        select(OperationalResearchGapEvent.gap_id).where(
            OperationalResearchGapEvent.resolved_by_review_id == review_row.id
        ).limit(1)
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
