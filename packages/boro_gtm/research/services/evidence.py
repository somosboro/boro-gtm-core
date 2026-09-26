"""First-class evidence items, and the independence rule over them.

An evidence item is the smallest thing M3 can point at: one exact observation,
in one exact document, retrieved once, read once. It exists **independently of
whatever consumes it** — a claim, an identity signal, or nothing at all. That
independence is why a signal about a possible duplicate company needs no claim
to justify it, and why deleting a claim cannot destroy what was observed.

Independence, for corroboration, is deliberately conservative: two items count
as separate voices only when they come from a **different publisher** *and* a
**different semantic document**. A mirror is one voice on two domains; a page
re-read by a better extractor is one voice read twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.research.domain.models import (
    ResearchArtifactDerivation,
    ResearchEvidenceItem,
    ResearchFetchEvent,
    ResearchSource,
)
from boro_gtm.research.policies import (
    PUBLISHER_POLICY_VERSION,
    publisher_for,
    sha256_text,
)
from boro_gtm.research.services.extraction import Observation, locator_hash


class EvidenceProvenanceError(ValueError):
    """The extraction, the retrieval and the derivation do not agree on a body."""


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    """The single lineage an evidence item is allowed to describe."""

    extraction_id: uuid.UUID
    fetch_event_id: uuid.UUID
    artifact_derivation_id: uuid.UUID
    body_id: uuid.UUID
    source: ResearchSource


def create_evidence_item(
    session: Session,
    *,
    context: EvidenceContext,
    observation: Observation,
    now: datetime,
) -> tuple[ResearchEvidenceItem, bool]:
    """One observation, bound to one body by three composite foreign keys.

    The provenance check below is belt-and-braces: the database already makes a
    mismatched lineage unrepresentable. Raising here turns what would be an
    opaque ``IntegrityError`` into a sentence that names the disagreement.
    """
    fetch = session.get(ResearchFetchEvent, context.fetch_event_id)
    derivation = session.get(ResearchArtifactDerivation, context.artifact_derivation_id)
    if fetch is None or fetch.body_id != context.body_id:
        raise EvidenceProvenanceError(
            "the retrieval named does not carry the body this evidence cites"
        )
    if derivation is None or derivation.body_id != context.body_id:
        raise EvidenceProvenanceError(
            "the canonicalization named does not derive from the body this evidence cites"
        )

    digest = locator_hash(observation.locator)
    publisher = publisher_for(context.source.host)
    result = session.execute(
        pg_insert(ResearchEvidenceItem)
        .values(
            id=uuid.uuid4(),
            extraction_id=context.extraction_id,
            fetch_event_id=context.fetch_event_id,
            artifact_derivation_id=context.artifact_derivation_id,
            body_id=context.body_id,
            source_id=context.source.id,
            locator=observation.locator,
            locator_hash=digest,
            quote=observation.quote,
            quote_sha256=sha256_text(observation.quote) if observation.quote else None,
            publisher_key=publisher.key,
            publisher_policy_version=PUBLISHER_POLICY_VERSION,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_evidence_item_identity")
        .returning(ResearchEvidenceItem.id)
    )
    row = result.first()
    if row is not None:
        return session.get(ResearchEvidenceItem, row[0]), True
    return session.scalars(
        select(ResearchEvidenceItem).where(
            ResearchEvidenceItem.extraction_id == context.extraction_id,
            ResearchEvidenceItem.fetch_event_id == context.fetch_event_id,
            ResearchEvidenceItem.artifact_derivation_id == context.artifact_derivation_id,
            ResearchEvidenceItem.locator_hash == digest,
        )
    ).one(), False


def independent_publisher_count(
    session: Session, evidence_item_ids: list[uuid.UUID]
) -> int:
    """How many genuinely separate voices this evidence represents.

    Two items are the *same* voice if they share a publisher **or** a semantic
    document, so the count is the number of connected components over the
    publisher/document graph. Both halves matter, and an earlier version of
    this function got it wrong by counting distinct ``(publisher, document)``
    pairs:

    * one publisher saying something on two of its own pages counted as two
      voices — a company could corroborate itself by adding a page;
    * a third party mirroring a company document would have counted as two,
      which is the inflation the independence rule exists to prevent.
    """
    if not evidence_item_ids:
        return 0
    rows = session.execute(
        select(
            ResearchEvidenceItem.publisher_key,
            ResearchArtifactDerivation.artifact_id,
        )
        .join(
            ResearchArtifactDerivation,
            ResearchArtifactDerivation.id == ResearchEvidenceItem.artifact_derivation_id,
        )
        .where(ResearchEvidenceItem.id.in_(evidence_item_ids))
    ).all()
    if not rows:
        return 0

    parent: dict[object, object] = {}

    def find(node: object) -> object:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: object, right: object) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[a] = b

    for publisher, artifact in rows:
        union(("publisher", publisher), ("artifact", artifact))
    return len({find(("publisher", publisher)) for publisher, _ in rows})


def lineage_artifact_ids(
    session: Session, evidence_item_ids: list[uuid.UUID]
) -> list[str]:
    """The sorted distinct semantic documents behind a set of evidence."""
    if not evidence_item_ids:
        return []
    rows = session.scalars(
        select(ResearchArtifactDerivation.artifact_id)
        .join(
            ResearchEvidenceItem,
            ResearchEvidenceItem.artifact_derivation_id == ResearchArtifactDerivation.id,
        )
        .where(ResearchEvidenceItem.id.in_(evidence_item_ids))
    ).all()
    return sorted({str(artifact_id) for artifact_id in rows})
