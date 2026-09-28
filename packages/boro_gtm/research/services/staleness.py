"""Staleness, computed at read time and never stored.

The rule is small and the reason it matters is not: **time passing does not
change what a source said.** A fact observed in 2024 is still a fact observed
in 2024 next year; what changes is how much weight a reader should give it. So
the verdict is derived on read from the registry's horizon and the source's own
observation date, and no stored row is edited because a clock advanced
(M3-ADR-007).

Two corollaries that are easy to get wrong:

* an **undated** source is `UNKNOWN_AGE`, never `FRESH` and never `STALE` —
  we do not know its age, and guessing either way is a fabrication;
* `retrieved_at` is never substituted for the observation date. A crawl date
  would make every document look as fresh as the last crawl.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    ClaimEvidenceLink,
    ResearchArtifactDerivation,
    ResearchEvidenceItem,
)
from boro_gtm.research.enums import Staleness
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION, get_attribute

#: Where FRESH ends and AGING begins, as a fraction of the registry horizon.
AGING_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class StalenessVerdict:
    attribute_key: str
    state: str
    observed_at: date | None
    age_days: int | None
    horizon_days: int | None


def staleness_of(
    attribute_key: str, observed_at: date | None, as_of: date
) -> StalenessVerdict:
    """FRESH / AGING / STALE / UNKNOWN_AGE, from the registry's own horizon."""
    attribute = get_attribute(attribute_key)
    horizon = attribute.staleness_days

    if observed_at is None:
        return StalenessVerdict(attribute_key, Staleness.UNKNOWN_AGE.value,
                                None, None, horizon)
    age = (as_of - observed_at).days
    if horizon is None:
        # A durable attribute — an incorporation date does not go stale.
        return StalenessVerdict(attribute_key, Staleness.FRESH.value,
                                observed_at, age, None)
    if age > horizon:
        state = Staleness.STALE.value
    elif age > horizon * AGING_THRESHOLD:
        state = Staleness.AGING.value
    else:
        state = Staleness.FRESH.value
    return StalenessVerdict(attribute_key, state, observed_at, age, horizon)


def claim_staleness(
    session: Session, claim_id: uuid.UUID, as_of: date
) -> StalenessVerdict:
    """The claim's own observation date, taken from its evidence."""
    claim = session.get(CompanyClaim, claim_id)
    if claim is None:
        raise ValueError(f"no claim {claim_id}")
    observed_at = session.scalar(
        select(ResearchArtifactDerivation.source_published_at)
        .join(ResearchEvidenceItem,
              ResearchEvidenceItem.artifact_derivation_id
              == ResearchArtifactDerivation.id)
        .join(ClaimEvidenceLink,
              ClaimEvidenceLink.evidence_item_id == ResearchEvidenceItem.id)
        .where(
            ClaimEvidenceLink.claim_id == claim_id,
            ResearchArtifactDerivation.source_published_at.is_not(None),
        )
        .order_by(ResearchArtifactDerivation.source_published_at.desc())
        .limit(1)
    )
    return staleness_of(claim.attribute_key, observed_at, as_of)


def company_staleness(
    session: Session, company_id: uuid.UUID, as_of: date
) -> dict[str, StalenessVerdict]:
    """Every M3 attribute we hold a claim for, judged as of one date."""
    rows = session.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id == company_id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
            CompanyClaim.availability == "OBSERVED",
        )
    ).all()
    out: dict[str, StalenessVerdict] = {}
    for claim in rows:
        verdict = claim_staleness(session, claim.id, as_of)
        previous = out.get(claim.attribute_key)
        # Keep the freshest reading per attribute: an attribute is as current as
        # the most recent evidence for it.
        if previous is None or _rank(verdict.state) < _rank(previous.state):
            out[claim.attribute_key] = verdict
    return out


_ORDER = {
    Staleness.FRESH.value: 0,
    Staleness.AGING.value: 1,
    Staleness.UNKNOWN_AGE.value: 2,
    Staleness.STALE.value: 3,
}


def _rank(state: str) -> int:
    return _ORDER[state]


def stale_required_attributes(
    session: Session, company_id: uuid.UUID, target_keys: list[str], as_of: date
) -> list[str]:
    """Required targets whose freshest evidence is stale.

    These earn a `STALE_EVIDENCE` gap. The historical evidence is untouched —
    it still exists and still says what it said. The gap records that nobody
    has checked lately, which is a different statement.
    """
    verdicts = company_staleness(session, company_id, as_of)
    return sorted(
        key for key in target_keys
        if get_attribute(key).required
        and key in verdicts
        and verdicts[key].state == Staleness.STALE.value
    )
