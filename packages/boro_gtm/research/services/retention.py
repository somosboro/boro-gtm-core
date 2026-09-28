"""Retention: drop payloads, keep provenance.

Storage is dominated by raw bodies, which are the least reusable thing M3
holds. They can go. What must never go is the ability to explain a claim.

After pruning, a body still reports its hash, its byte length and
`PRUNED` with the date; its fetch events keep every source, time and status;
its artifact keeps the publication date; and the claim's evidence link still
carries the quote and the quote hash. The claim degrades from *"we can show you
the whole page"* to *"we can show you the quoted span and prove it hashed to
this"* — a real but bounded loss, stated here rather than discovered later.

Every mutation goes through the one-way prune triggers. Nothing in this module
deletes a row: provenance identities are permanent, and a retention policy that
can delete evidence is not a retention policy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError
from boro_gtm.research.domain.models import (
    ResearchArtifactBody,
    ResearchExtraction,
    ResearchFetchEvent,
    ResearchTextDerivation,
)

#: Design §26. Days, and configurable — the defaults are stated, not implied.
RAW_BODY_RETENTION_DAYS = 90
TEXT_DERIVATION_RETENTION_DAYS = 730          # 24 months
MODEL_RAW_OUTPUT_RETENTION_DAYS = 90

#: Per content type, because a PDF costs far more to keep than a job JSON.
RAW_BODY_RETENTION_BY_MEDIA_TYPE: dict[str, int] = {
    "application/pdf": 30,
    "text/html": 90,
    "application/json": 180,
    "text/plain": 180,
}


class PrunedPayloadError(GtmError):
    """A payload that retention removed was needed again.

    Raised instead of silently deriving from nothing. A caller that needs the
    bytes must re-fetch the source, and if the page is gone, that is a research
    gap — not an empty derivation that looks like a successful one.
    """

    code = "PAYLOAD_PRUNED"
    http_status = 409


@dataclass(slots=True)
class RetentionPlan:
    """What a prune would do, before it does it."""

    bodies: list[uuid.UUID] = field(default_factory=list)
    text_derivations: list[uuid.UUID] = field(default_factory=list)
    model_outputs: list[uuid.UUID] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.bodies) + len(self.text_derivations) + len(self.model_outputs)

    def as_counts(self) -> dict[str, int]:
        return {
            "bodies": len(self.bodies),
            "text_derivations": len(self.text_derivations),
            "model_outputs": len(self.model_outputs),
        }


def _body_age_cutoffs(as_of: datetime) -> dict[str, datetime]:
    return {
        media_type: as_of - timedelta(days=days)
        for media_type, days in RAW_BODY_RETENTION_BY_MEDIA_TYPE.items()
    }


def plan_retention(
    session: Session,
    *,
    as_of: datetime,
    body_days: int | None = None,
    text_days: int = TEXT_DERIVATION_RETENTION_DAYS,
    model_days: int = MODEL_RAW_OUTPUT_RETENTION_DAYS,
) -> RetentionPlan:
    """Select what is eligible, deterministically and without mutating anything.

    A body's age is the **earliest retrieval that produced it**, not its
    `first_seen_at` alone — the two are the same today, and writing it this way
    means they stay the same if bodies ever arrive from an import.
    """
    plan = RetentionPlan()

    per_type = _body_age_cutoffs(as_of)
    default_cutoff = as_of - timedelta(days=body_days or RAW_BODY_RETENTION_DAYS)

    rows = session.execute(
        select(
            ResearchArtifactBody.id,
            ResearchArtifactBody.first_seen_at,
            ResearchFetchEvent.declared_content_type,
        )
        .outerjoin(ResearchFetchEvent, ResearchFetchEvent.body_id == ResearchArtifactBody.id)
        .where(
            ResearchArtifactBody.body_retention == "RETAINED",
            ResearchArtifactBody.raw_body.is_not(None),
        )
        .order_by(ResearchArtifactBody.id)
    ).all()
    seen: set[uuid.UUID] = set()
    for body_id, first_seen_at, declared in rows:
        if body_id in seen:
            continue
        seen.add(body_id)
        media_type = (declared or "").split(";")[0].strip()
        cutoff = per_type.get(media_type, default_cutoff) if body_days is None else default_cutoff
        if first_seen_at < cutoff:
            plan.bodies.append(body_id)

    text_cutoff = as_of - timedelta(days=text_days)
    plan.text_derivations = list(session.scalars(
        select(ResearchTextDerivation.id)
        .where(
            ResearchTextDerivation.text_retention == "RETAINED",
            ResearchTextDerivation.extracted_text.is_not(None),
            ResearchTextDerivation.derived_at < text_cutoff,
        )
        .order_by(ResearchTextDerivation.id)
    ).all())

    model_cutoff = as_of - timedelta(days=model_days)
    plan.model_outputs = list(session.scalars(
        select(ResearchExtraction.id)
        .where(
            ResearchExtraction.extractor_kind == "MODEL",
            ResearchExtraction.raw_output_retention == "RETAINED",
            ResearchExtraction.raw_output.is_not(None),
            ResearchExtraction.created_at < model_cutoff,
        )
        .order_by(ResearchExtraction.id)
    ).all())
    return plan


def apply_retention(
    session: Session, plan: RetentionPlan, *, as_of: datetime
) -> dict[str, int]:
    """Execute a plan. Only the one-way prune the triggers permit."""
    for body_id in plan.bodies:
        body = session.get(ResearchArtifactBody, body_id)
        body.raw_body = None
        body.body_retention = "PRUNED"
        body.pruned_at = as_of
    for derivation_id in plan.text_derivations:
        derivation = session.get(ResearchTextDerivation, derivation_id)
        derivation.extracted_text = None
        derivation.text_retention = "PRUNED"
        derivation.pruned_at = as_of
    for extraction_id in plan.model_outputs:
        extraction = session.get(ResearchExtraction, extraction_id)
        extraction.raw_output = None
        extraction.raw_output_retention = "PRUNED"
        extraction.raw_output_pruned_at = as_of
    session.flush()
    return plan.as_counts()


def require_body(session: Session, body_id: uuid.UUID) -> bytes:
    """The bytes, or an explicit failure. Never silently empty."""
    body = session.get(ResearchArtifactBody, body_id)
    if body is None:
        raise PrunedPayloadError(f"no body {body_id}")
    if body.raw_body is None:
        raise PrunedPayloadError(
            f"body {body_id} was pruned at {body.pruned_at}; its sha256 is "
            f"{body.raw_body_sha256} and re-deriving needs a refetch of the source"
        )
    return body.raw_body


def require_text(session: Session, text_derivation_id: uuid.UUID) -> str:
    derivation = session.get(ResearchTextDerivation, text_derivation_id)
    if derivation is None:
        raise PrunedPayloadError(f"no text derivation {text_derivation_id}")
    if derivation.extracted_text is None:
        raise PrunedPayloadError(
            f"text derivation {text_derivation_id} was pruned at "
            f"{derivation.pruned_at}; re-extraction needs the body or a refetch"
        )
    return derivation.extracted_text
