"""The canonical Q2 evidence projection — a view, never a second store.

The commercial ontology's Q2 evidence record has seven fields. M3 already
holds all of them, spread across the evidence ledger, so this module *reads*
them. Materialising a parallel table would create two places that can disagree
about what a source said, which is the failure this milestone is supposed to
help customers stop having.

Two rules are easy to break here and both are load-bearing:

* **`date_observed` is the date the source stated**, never `retrieved_at`. A
  crawl date is not an event date, and substituting it makes a decade-old page
  read as this morning's news.
* **absence projects as `NOT_AVAILABLE`**, with a null value and a null fact
  type — never `false`, never `0`.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    ClaimEvidenceLink,
    OperationalResearchGap,
    ResearchEvidenceItem,
    ResearchSource,
)
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION
from boro_gtm.research.services.claims import claim_observation_date


class UnsupportedClaimError(ValueError):
    """A claim reached the projection with no surviving evidence behind it."""


@dataclass(frozen=True, slots=True)
class Q2Evidence:
    """One canonical evidence record. Seven fields, and nothing invented."""

    source: str
    source_type: str
    date_observed: date | None
    fact: str
    confidence: float | None
    inference_allowed: bool
    #: Always ``None`` in pure M3. Hypotheses are M4's, and manufacturing one
    #: here would be M3 quietly doing interpretation under another name.
    related_hypothesis: None = None

    def as_dict(self) -> dict:
        return asdict(self)


def project_claim(session: Session, claim: CompanyClaim) -> list[Q2Evidence]:
    """Project one claim's evidence into the canonical seven-field shape."""
    rows = session.execute(
        select(
            ResearchSource.normalized_locator,
            ClaimEvidenceLink.source_class,
            ResearchEvidenceItem.quote,
        )
        .join(ResearchEvidenceItem, ResearchEvidenceItem.id == ClaimEvidenceLink.evidence_item_id)
        .join(ResearchSource, ResearchSource.id == ResearchEvidenceItem.source_id)
        .where(ClaimEvidenceLink.claim_id == claim.id)
    ).all()
    if not rows:
        raise UnsupportedClaimError(
            f"claim {claim.id} has no evidence; it must be omitted from the "
            "projection rather than emitted with an empty provenance list"
        )
    observed_at, _ = claim_observation_date(session, claim.id)
    return [
        Q2Evidence(
            source=locator,
            source_type=source_class,
            date_observed=observed_at,
            fact=quote or "",
            confidence=float(claim.confidence) if claim.confidence is not None else None,
            inference_allowed=claim.fact_type in {"INFERENCE", "HYPOTHESIS"},
        )
        for locator, source_class, quote in rows
    ]


@dataclass(frozen=True, slots=True)
class AttributeState:
    """What M3 knows about one attribute, for one company."""

    attribute_key: str
    availability: str
    fact_type: str | None
    value: dict | None
    confidence: float | None
    evidence: tuple[Q2Evidence, ...] = ()


def project_company(
    session: Session, company_id: uuid.UUID, attribute_keys: list[str]
) -> dict[str, list[AttributeState]]:
    """Every requested attribute, including the ones nobody evidenced.

    An attribute with no claim is reported present-and-unknown rather than
    omitted, because a reader cannot tell a missing key from a missing fact.
    """
    claims = session.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id == company_id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
            CompanyClaim.attribute_key.in_(attribute_keys),
        )
    ).all()
    by_key: dict[str, list[AttributeState]] = {}
    for claim in claims:
        try:
            evidence = tuple(project_claim(session, claim))
        except UnsupportedClaimError:
            # Recorded as omitted rather than emitted unsupported.
            continue
        by_key.setdefault(claim.attribute_key, []).append(AttributeState(
            attribute_key=claim.attribute_key,
            availability=claim.availability,
            fact_type=claim.fact_type,
            value=claim.value_jsonb,
            confidence=float(claim.confidence) if claim.confidence is not None else None,
            evidence=evidence,
        ))

    for key in attribute_keys:
        if key not in by_key:
            by_key[key] = [AttributeState(
                attribute_key=key, availability="NOT_AVAILABLE",
                fact_type=None, value=None, confidence=None,
            )]
    return by_key


def open_gap_keys(session: Session, run_id: uuid.UUID) -> set[str]:
    return set(session.scalars(
        select(OperationalResearchGap.attribute_key).where(
            OperationalResearchGap.run_id == run_id
        )
    ).all())
