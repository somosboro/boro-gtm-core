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


def _maximum_matching(edges: list[tuple[str, str]]) -> int:
    """Maximum cardinality matching over a bipartite graph, by augmenting paths.

    Kuhn's algorithm. Deterministic: both sides are iterated in sorted order, so
    the *size* is the same whatever order the rows arrived in — and the size is
    all that is reported.
    """
    adjacency: dict[str, list[str]] = {}
    for left, right in edges:
        adjacency.setdefault(left, [])
        if right not in adjacency[left]:
            adjacency[left].append(right)
    for left in adjacency:
        adjacency[left].sort()

    matched_right: dict[str, str] = {}

    def augment(left: str, seen: set[str]) -> bool:
        for right in adjacency[left]:
            if right in seen:
                continue
            seen.add(right)
            holder = matched_right.get(right)
            if holder is None or augment(holder, seen):
                matched_right[right] = left
                return True
        return False

    return sum(1 for left in sorted(adjacency) if augment(left, set()))


def independent_publisher_count(
    session: Session, evidence_item_ids: list[uuid.UUID]
) -> int:
    """How many genuinely separate voices this evidence represents.

    The frozen contract (schema graph §4.2a) is *the size of the largest set of
    pairwise-independent lineages*, where two are independent only when they
    differ in **both** publisher and document. A set of `(publisher, document)`
    pairs is pairwise independent exactly when no publisher and no document
    repeats — which is a **matching** in the bipartite publisher↔document graph.
    So the count is the maximum matching, and computing it is the contract, not
    an approximation of it.

    Two earlier implementations were both wrong, in opposite directions:

    * counting distinct pairs over-counted — one publisher on two of its own
      pages read as two voices, so a company could corroborate itself by adding
      a page;
    * counting connected components under-counted. On ``A–doc1, A–doc2,
      B–doc2, B–doc3, C–doc3`` every edge sits in one component, so it answered
      1, while ``A/doc1, B/doc2, C/doc3`` are three genuinely independent
      witnesses.

    Both mirrors and self-corroboration still behave: a publisher can be matched
    once and a document can be matched once, which is precisely the rule.
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
    return _maximum_matching(
        sorted({(str(publisher), str(artifact)) for publisher, artifact in rows})
    )


def lineage_evidence_origins(
    session: Session, evidence_item_ids: list[uuid.UUID]
) -> list[tuple[str, str]]:
    """The sorted distinct ``(source_id, artifact_id)`` origins behind evidence.

    An **evidence origin** is the pair that identifies a concrete observation,
    and a lineage is the sorted set of distinct origins justifying one
    assertion (schema graph §4.2).

    Artifact ids alone are not enough, and the schema graph says why: source A
    serving artifact X and source B serving artifact X are two genuinely
    different observations that may carry different trust, publication context
    and dates. Keying the lineage on the artifact collapsed them into one
    claim and destroyed all three.
    """
    if not evidence_item_ids:
        return []
    rows = session.execute(
        select(
            ResearchEvidenceItem.source_id,
            ResearchArtifactDerivation.artifact_id,
        )
        .join(
            ResearchArtifactDerivation,
            ResearchArtifactDerivation.id == ResearchEvidenceItem.artifact_derivation_id,
        )
        .where(ResearchEvidenceItem.id.in_(evidence_item_ids))
    ).all()
    return sorted({(str(source), str(artifact)) for source, artifact in rows})


def evidence_origin(
    session: Session, evidence_item_id: uuid.UUID
) -> tuple[str, str]:
    """The one origin an evidence item sits at."""
    source, artifact = session.execute(
        select(
            ResearchEvidenceItem.source_id,
            ResearchArtifactDerivation.artifact_id,
        )
        .join(
            ResearchArtifactDerivation,
            ResearchArtifactDerivation.id == ResearchEvidenceItem.artifact_derivation_id,
        )
        .where(ResearchEvidenceItem.id == evidence_item_id)
    ).one()
    return str(source), str(artifact)
