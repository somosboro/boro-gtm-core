"""Classification, canonicalization, derivation metadata and readable text.

Three identities live in this file and they are deliberately different things:

* a **body** is bytes;
* an **artifact** is *what the document says*, under a named canonicalization
  contract;
* a **derivation** is one body read under one contract, and it is the only one
  of the three that may carry observational metadata.

The last point is the one that is easy to get wrong. A dated original and an
undated mirror are the same document; folding the publication date into the
artifact hash would split them into two, and independence detection — which
exists to stop a copy counting as a second voice — would quietly stop working.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.research.domain.models import (
    ResearchArtifact,
    ResearchArtifactDerivation,
    ResearchBodyClassification,
    ResearchTextDerivation,
)
from boro_gtm.research.enums import STRATEGY_MEDIA_TYPES
from boro_gtm.research.policies import (
    CLASSIFIER_POLICY_VERSION,
    REDACTION_POLICY_VERSION,
    STRATEGY_FOR_MEDIA_TYPE,
    TEXT_EXTRACTION_POLICY_VERSION,
    canonicalize,
    redact,
    sniff_media_type,
    text_derivation_contract_hash,
)


class CanonicalizationMismatchError(ValueError):
    """A strategy was applied to a media type it does not fit."""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_body(
    session: Session,
    *,
    body_id: uuid.UUID,
    raw: bytes,
    now: datetime,
    classifier_policy_version: str = CLASSIFIER_POLICY_VERSION,
) -> ResearchBodyClassification:
    """Sniff the media type from the bytes, under a named classifier version.

    A later classifier version produces a *new* row; the previous verdict is
    never edited, so a claim that cited a body when it was read as HTML can
    still be explained after the classifier changes its mind.
    """
    media_type, encoding = sniff_media_type(raw)
    result = session.execute(
        pg_insert(ResearchBodyClassification)
        .values(
            id=uuid.uuid4(),
            body_id=body_id,
            classifier_policy_version=classifier_policy_version,
            sniffed_media_type=media_type,
            confidence=1.0,
            encoding=encoding,
            classified_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_body_classification")
        .returning(ResearchBodyClassification.id)
    )
    row = result.first()
    if row is not None:
        return session.get(ResearchBodyClassification, row[0])
    return session.scalars(
        select(ResearchBodyClassification).where(
            ResearchBodyClassification.body_id == body_id,
            ResearchBodyClassification.classifier_policy_version
            == classifier_policy_version,
        )
    ).one()


def strategy_for(media_type: str) -> str | None:
    return STRATEGY_FOR_MEDIA_TYPE.get(media_type)


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def _parse_published(value: str | None) -> tuple[date | None, str]:
    """Never invent precision a source did not state."""
    if not value:
        return None, "UNDATED"
    text = value.strip()
    try:
        if len(text) == 4:
            return date(int(text), 1, 1), "YEAR"
        if len(text) == 7:
            return date(int(text[:4]), int(text[5:7]), 1), "MONTH"
        return date.fromisoformat(text[:10]), "DATE"
    except ValueError:
        return None, "UNDATED"


def get_or_create_artifact(
    session: Session, *, strategy: str, version: str, content_hash: str, now: datetime
) -> tuple[ResearchArtifact, bool]:
    result = session.execute(
        pg_insert(ResearchArtifact)
        .values(
            id=uuid.uuid4(),
            canonicalization_strategy=strategy,
            canonicalization_version=version,
            canonical_content_hash=content_hash,
            first_seen_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_artifact_identity")
        .returning(ResearchArtifact.id)
    )
    row = result.first()
    if row is not None:
        return session.get(ResearchArtifact, row[0]), True
    return session.scalars(
        select(ResearchArtifact).where(
            ResearchArtifact.canonicalization_strategy == strategy,
            ResearchArtifact.canonicalization_version == version,
            ResearchArtifact.canonical_content_hash == content_hash,
        )
    ).one(), False


def derive_artifact(
    session: Session,
    *,
    body_id: uuid.UUID,
    raw: bytes,
    media_type: str,
    now: datetime,
    strategy: str | None = None,
    canonicalization_version: str = "1",
) -> tuple[ResearchArtifactDerivation, ResearchArtifact, bool]:
    """Read one body under one canonicalization contract.

    Returns ``(derivation, artifact, derivation_created)``. Two bodies whose
    canonical content matches converge on one artifact and keep two
    derivations, which is how a mirror stays visible as a copy.
    """
    strategy = strategy or strategy_for(media_type)
    if strategy is None:
        raise CanonicalizationMismatchError(f"no canonicalization contract for {media_type}")
    permitted = STRATEGY_MEDIA_TYPES.get(strategy, frozenset())
    if media_type not in permitted:
        raise CanonicalizationMismatchError(
            f"{strategy} does not apply to {media_type}"
        )

    existing = session.scalars(
        select(ResearchArtifactDerivation).where(
            ResearchArtifactDerivation.body_id == body_id,
            ResearchArtifactDerivation.canonicalization_strategy == strategy,
            ResearchArtifactDerivation.canonicalization_version == canonicalization_version,
        )
    ).first()
    if existing is not None:
        return existing, session.get(ResearchArtifact, existing.artifact_id), False

    canonical = canonicalize(raw, strategy)
    artifact, _ = get_or_create_artifact(
        session, strategy=strategy, version=canonicalization_version,
        content_hash=canonical.content_hash, now=now,
    )
    published_at, granularity = _parse_published(canonical.published_at)

    result = session.execute(
        pg_insert(ResearchArtifactDerivation)
        .values(
            id=uuid.uuid4(),
            body_id=body_id,
            artifact_id=artifact.id,
            canonicalization_strategy=strategy,
            canonicalization_version=canonicalization_version,
            canonical_content_hash=canonical.content_hash,
            derivation_status="OK",
            source_published_at=published_at,
            source_published_granularity=granularity,
            language=canonical.language,
            title=canonical.title,
            derived_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_derivation_contract")
        .returning(ResearchArtifactDerivation.id)
    )
    row = result.first()
    if row is None:
        derivation = session.scalars(
            select(ResearchArtifactDerivation).where(
                ResearchArtifactDerivation.body_id == body_id,
                ResearchArtifactDerivation.canonicalization_strategy == strategy,
                ResearchArtifactDerivation.canonicalization_version
                == canonicalization_version,
            )
        ).one()
        return derivation, session.get(ResearchArtifact, derivation.artifact_id), False
    return session.get(ResearchArtifactDerivation, row[0]), artifact, True


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def derive_text(
    session: Session,
    *,
    body_id: uuid.UUID,
    raw: bytes,
    media_type: str,
    now: datetime,
    text_extraction_policy_version: str = TEXT_EXTRACTION_POLICY_VERSION,
    redaction_policy_version: str = REDACTION_POLICY_VERSION,
) -> tuple[ResearchTextDerivation, bool]:
    """Readable text, versioned separately from semantics.

    A text-extraction improvement must not force a refetch, and a redaction
    policy change must not rewrite the text a claim already quoted — so both
    versions are inside the contract hash and each contract keeps its own row.
    """
    contract = text_derivation_contract_hash(
        text_extraction_policy_version, redaction_policy_version
    )
    strategy = strategy_for(media_type)
    if strategy is None:
        raise CanonicalizationMismatchError(f"no text contract for {media_type}")
    canonical = canonicalize(raw, strategy)
    text = redact(canonical.text, redaction_policy_version)

    result = session.execute(
        pg_insert(ResearchTextDerivation)
        .values(
            id=uuid.uuid4(),
            body_id=body_id,
            text_extraction_policy_version=text_extraction_policy_version,
            redaction_policy_version=redaction_policy_version,
            text_derivation_contract_hash=contract,
            extracted_text=text,
            page_offsets=canonical.page_offsets,
            text_retention="RETAINED",
            status="OK",
            derived_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_text_derivation_contract")
        .returning(ResearchTextDerivation.id)
    )
    row = result.first()
    if row is not None:
        return session.get(ResearchTextDerivation, row[0]), True
    return session.scalars(
        select(ResearchTextDerivation).where(
            ResearchTextDerivation.body_id == body_id,
            ResearchTextDerivation.text_derivation_contract_hash == contract,
        )
    ).one(), False
