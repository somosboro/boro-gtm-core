"""Sources, discovery, retrieval and byte identity.

Two rules shape every function here.

**A source is a locator, not a finding.** Discovering an address ten times
through seven methods creates one source and ten discovery observations. The
source row carries nothing about any particular run, because the moment it
does, two research questions start overwriting each other's state.

**A body is bytes, and nothing else.** The same bytes served from two domains
in two attempts are one row. Anything contextual — what a server *said* the
type was, when we asked, who asked — belongs to the retrieval that observed it.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.research.domain.models import (
    ResearchArtifactBody,
    ResearchFetchEvent,
    ResearchSource,
    ResearchSourceDiscovery,
    ResearchSourceEdge,
)
from boro_gtm.research.fixtures.transport import DiscoveredLocator, FetchResult
from boro_gtm.research.policies import (
    LOCATOR_POLICY_VERSION,
    host_of,
    normalize_locator,
    registrable_domain,
    sha256_json,
)
from boro_gtm.research.policies import sha256_text as _sha256_text


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def get_or_create_source(session: Session, url: str, now: datetime) -> ResearchSource:
    """One row per normalized locator, whoever asks and however often.

    ``ON CONFLICT DO NOTHING`` then re-read: two workers discovering the same
    address converge instead of one of them seeing an ``IntegrityError``.
    """
    normalized = normalize_locator(url)
    host = host_of(normalized)
    statement = (
        pg_insert(ResearchSource)
        .values(
            id=uuid.uuid4(),
            normalized_locator=normalized,
            locator_policy_version=LOCATOR_POLICY_VERSION,
            host=host,
            registrable_domain=registrable_domain(host),
            first_seen_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_source_locator")
    )
    session.execute(statement)
    source = session.scalars(
        select(ResearchSource).where(
            ResearchSource.normalized_locator == normalized,
            ResearchSource.locator_policy_version == LOCATOR_POLICY_VERSION,
        )
    ).one()
    return source


def record_discovery(
    session: Session,
    *,
    source: ResearchSource,
    attempt_id: uuid.UUID,
    candidate: DiscoveredLocator,
    now: datetime,
    parent: ResearchSource | None = None,
) -> ResearchSourceDiscovery | None:
    """How this attempt found this source. Context is part of the identity.

    Returns ``None`` when this exact observation was already recorded, so the
    caller can count *new* observations rather than guessing.
    """
    context = dict(candidate.context or {})
    context_hash = sha256_json(context) if context else None
    values = {
        "id": uuid.uuid4(),
        "source_id": source.id,
        "attempt_id": attempt_id,
        "discovery_method": candidate.method,
        "discovered_from_source_id": parent.id if parent is not None else None,
        "discovery_context": context or None,
        "discovery_context_hash": context_hash,
        "relevance_hint": candidate.relevance_hint,
        "discovered_at": now,
    }
    result = session.execute(
        pg_insert(ResearchSourceDiscovery)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[
            ResearchSourceDiscovery.source_id,
            ResearchSourceDiscovery.attempt_id,
            ResearchSourceDiscovery.discovery_method,
            ResearchSourceDiscovery.discovered_from_source_id,
            ResearchSourceDiscovery.discovery_context_hash,
        ])
        .returning(ResearchSourceDiscovery.id)
    )
    row = result.first()
    if row is None:
        return None
    return session.get(ResearchSourceDiscovery, row[0])


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def record_edge(
    session: Session,
    *,
    from_source: ResearchSource,
    to_source: ResearchSource,
    relation_type: str,
    edge_origin: str,
    now: datetime,
    observed_by_fetch_event_id: uuid.UUID | None = None,
    corroborating_fetch_event_id: uuid.UUID | None = None,
    asserted_by: str | None = None,
    rationale: str | None = None,
) -> ResearchSourceEdge | None:
    """An observed relationship between two locators. Never a merge.

    A redirect, a declared canonical and a mirror are all *evidence about*
    addresses. Collapsing two source rows because a page claimed they were the
    same would make a page's self-description authoritative over our own
    identity, which is exactly backwards.
    """
    values = {
        "id": uuid.uuid4(),
        "from_source_id": from_source.id,
        "to_source_id": to_source.id,
        "relation_type": relation_type,
        "edge_origin": edge_origin,
        "observed_by_fetch_event_id": observed_by_fetch_event_id,
        "corroborating_fetch_event_id": corroborating_fetch_event_id,
        "asserted_by": asserted_by,
        "rationale": rationale,
        "observed_at": now,
    }
    result = session.execute(
        pg_insert(ResearchSourceEdge)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[
            ResearchSourceEdge.from_source_id,
            ResearchSourceEdge.to_source_id,
            ResearchSourceEdge.relation_type,
            ResearchSourceEdge.observed_by_fetch_event_id,
        ])
        .returning(ResearchSourceEdge.id)
    )
    row = result.first()
    return session.get(ResearchSourceEdge, row[0]) if row else None


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def get_or_create_body(
    session: Session, raw: bytes, now: datetime
) -> tuple[ResearchArtifactBody, bool]:
    """Global byte identity. Returns ``(body, created)``."""
    digest = _sha256_bytes(raw)
    result = session.execute(
        pg_insert(ResearchArtifactBody)
        .values(
            id=uuid.uuid4(),
            raw_body_sha256=digest,
            raw_body=raw,
            byte_length=len(raw),
            body_retention="RETAINED",
            first_seen_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_body_sha256")
        .returning(ResearchArtifactBody.id)
    )
    row = result.first()
    if row is not None:
        return session.get(ResearchArtifactBody, row[0]), True
    body = session.scalars(
        select(ResearchArtifactBody).where(
            ResearchArtifactBody.raw_body_sha256 == digest
        )
    ).one()
    return body, False


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def last_successful_fetch(
    session: Session, source_id: uuid.UUID
) -> ResearchFetchEvent | None:
    """The most recent retrieval that yielded bytes, for conditional requests."""
    return session.scalars(
        select(ResearchFetchEvent)
        .where(
            ResearchFetchEvent.source_id == source_id,
            ResearchFetchEvent.fetch_outcome == "OK",
        )
        .order_by(ResearchFetchEvent.retrieved_at.desc(), ResearchFetchEvent.id.desc())
        .limit(1)
    ).first()


def record_fetch(
    session: Session,
    *,
    source: ResearchSource,
    attempt_id: uuid.UUID,
    result: FetchResult,
    now: datetime,
    validated_body_id: uuid.UUID | None = None,
    duration_ms: int | None = None,
) -> tuple[ResearchFetchEvent, ResearchArtifactBody | None, bool]:
    """Append one retrieval event. Every attempt is recorded, successful or not.

    A 304 stores no bytes and references the body it *validated*. Fabricating a
    received body for it would make "we re-downloaded this" and "the server told
    us nothing changed" indistinguishable a week later.
    """
    body: ResearchArtifactBody | None = None
    created = False
    body_id: uuid.UUID | None = None

    if result.outcome == "OK":
        if result.body is None:  # pragma: no cover - transport contract
            raise ValueError("an OK retrieval must carry bytes")
        body, created = get_or_create_body(session, result.body, now)
        body_id = body.id
    elif result.outcome == "NOT_MODIFIED":
        if validated_body_id is None:
            raise ValueError(
                "a 304 must name the body it validated; without it the event "
                "asserts freshness for nothing"
            )
        body_id = validated_body_id
        body = session.get(ResearchArtifactBody, validated_body_id)

    event = ResearchFetchEvent(
        id=uuid.uuid4(),
        source_id=source.id,
        attempt_id=attempt_id,
        body_id=body_id,
        fetch_outcome=result.outcome,
        http_status=result.http_status,
        final_url=result.final_url,
        declared_content_type=result.declared_content_type,
        content_length=result.content_length,
        etag=result.etag,
        last_modified=result.last_modified,
        validated_by=result.validated_by,
        validator_value=result.validator_value,
        request_headers_digest=_sha256_text(
            f"if-none-match={result.validator_value or ''}"
        ),
        error_class=result.error_class,
        error_detail=result.error_detail,
        duration_ms=duration_ms,
        retrieved_at=now,
    )
    session.add(event)
    session.flush()
    return event, body, created
