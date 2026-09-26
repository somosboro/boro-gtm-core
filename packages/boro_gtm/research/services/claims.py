"""Asserting operational claims into M2's existing claim ledger.

There is one claim ledger in this system and M3 does not get a second one. A
parallel store would be the same defect as a parallel source of truth in a
customer's business: two places that disagree, and no rule for which wins.

Two rules dominate:

**Unknown is not false.** No evidence for an ERP means `availability =
NOT_AVAILABLE`, a NULL value and a NULL fact type — never `erp = false`, never
`0`, never "does not use". The distinction between *we looked and found
nothing* and *they do not have one* is the whole point of the milestone.

**A claim is one assertion from one evidence lineage.** Two independent
sources are two claims, because they may legitimately differ in fact type,
trust, confidence and date, and merging them would destroy all four.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    ClaimEvidenceLink,
    ResearchArtifactDerivation,
    ResearchEvidenceItem,
    ResearchSource,
)
from boro_gtm.research.policies import (
    ASSERTION_POLICY_VERSION,
    TRUST_POLICY_VERSION,
    assertion_contract_hash,
    assertion_fingerprint,
    compute_confidence,
    publisher_for,
    trust_tier,
)
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION, get_attribute
from boro_gtm.research.services.evidence import (
    independent_publisher_count,
    lineage_artifact_ids,
)
from boro_gtm.research.services.extraction import Observation


@dataclass(slots=True)
class ConfidenceDivergenceError(ValueError):
    """Appending evidence would change a confidence that cannot be rewritten.

    `company_claims` is append-only, and design rule 1 forbids an append-only
    row from holding a value that changes. Confidence is a function of the
    claim's links, so it is only safe to store if appending a link cannot move
    it. The assertion fingerprint is supposed to guarantee that: a link may
    only join a claim whose lineage it already shares.

    If this is ever raised, the fingerprint's lineage key is too coarse to keep
    that promise, and the fix is to the key — not to the append-only rule.
    See M3-ADR-047.
    """


@dataclass(slots=True)
class PendingAssertion:
    """One value, and every evidence item that says it."""

    attribute_key: str
    value: dict
    unit: str | None
    fact_type: str
    support_kind: str
    evidence_item_ids: list[uuid.UUID] = field(default_factory=list)
    observed_at: date | None = None
    period_granularity: str = "UNDATED"


@dataclass(slots=True)
class AssertionResult:
    claim: CompanyClaim
    created: bool
    links_created: int


def group_observations(
    observations: list[tuple[Observation, uuid.UUID]],
) -> list[PendingAssertion]:
    """Collapse identical values into one assertion with several evidence items.

    Identical means the same attribute, value, unit and fact type. A page that
    says "24/7 emergency" three times is one assertion supported three times,
    not three assertions.
    """
    grouped: dict[tuple, PendingAssertion] = {}
    for observation, evidence_id in observations:
        key = (
            observation.attribute_key,
            _canonical_value_key(observation.value),
            observation.unit,
            observation.fact_type,
        )
        pending = grouped.get(key)
        if pending is None:
            pending = PendingAssertion(
                attribute_key=observation.attribute_key,
                value=observation.value,
                unit=observation.unit,
                fact_type=observation.fact_type,
                support_kind=observation.support_kind,
            )
            grouped[key] = pending
        pending.evidence_item_ids.append(evidence_id)
    return list(grouped.values())


def _canonical_value_key(value: dict) -> str:
    from boro_gtm.research.policies import canonical_json

    return canonical_json(value)


def assert_claim(
    session: Session,
    *,
    company_id: uuid.UUID,
    pending: PendingAssertion,
    now: datetime,
    inference_rule_version: str | None = None,
) -> AssertionResult:
    """Write one claim, or attach new evidence to the one already there.

    Because the fingerprint excludes the extractor, a better extractor that
    agrees with an older one over the same documents appends a link instead of
    minting a twin. Without that, re-reading one page three times would look
    like three independent sources.

    Confidence is computed **before** the insert and never rewritten: the row
    is append-only, so a derived value stored on it has to be one that cannot
    move afterwards.
    """
    if not pending.evidence_item_ids:
        raise ValueError(
            "an M3 claim requires evidence; unknown_attribute_state() is how "
            "absence is reported, and it writes no row"
        )
    attribute = get_attribute(pending.attribute_key)
    attribute.validate(pending.value, pending.unit, pending.fact_type)

    contract = assertion_contract_hash(inference_rule_version)
    lineage = lineage_artifact_ids(session, pending.evidence_item_ids)
    fingerprint = assertion_fingerprint(
        subject_company_id=company_id,
        attribute_key=pending.attribute_key,
        attribute_registry_version=RESEARCH_REGISTRY_VERSION,
        value=pending.value,
        unit=pending.unit,
        fact_type=pending.fact_type,
        availability="OBSERVED",
        period_granularity=pending.period_granularity,
        observed_at=pending.observed_at,
        lineage_artifact_ids=lineage,
        contract_hash=contract,
    )
    inferred = inference_rule_version is not None

    existing = session.scalars(
        select(CompanyClaim).where(CompanyClaim.assertion_fingerprint == fingerprint)
    ).first()
    created = False

    if existing is None:
        confidence = _derive_confidence(
            session,
            fact_type=pending.fact_type,
            evidence_item_ids=pending.evidence_item_ids,
            inferred=inferred,
        )
        claim = CompanyClaim(
            id=uuid.uuid4(),
            attribute_key=pending.attribute_key,
            attribute_registry_version=RESEARCH_REGISTRY_VERSION,
            value_jsonb=pending.value,
            unit=pending.unit,
            fact_type=pending.fact_type,
            availability="OBSERVED",
            confidence=confidence,
            subject_company_id=company_id,
            observed_at=pending.observed_at,
            period_granularity=pending.period_granularity,
            assertion_fingerprint=fingerprint,
            created_at=now,
        )
        session.add(claim)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            # Another worker asserted the same thing first. Re-read; the
            # evidence links below are idempotent, so both callers converge.
            session.expunge(claim)
            claim = session.scalars(
                select(CompanyClaim).where(
                    CompanyClaim.assertion_fingerprint == fingerprint
                )
            ).one()
        else:
            created = True
    else:
        claim = existing

    links_created = _link_evidence(session, claim=claim, pending=pending, now=now)

    if not created and links_created:
        _assert_confidence_still_holds(session, claim=claim, inferred=inferred)

    return AssertionResult(claim=claim, created=created, links_created=links_created)


def _derive_confidence(
    session: Session, *, fact_type: str, evidence_item_ids: list[uuid.UUID],
    inferred: bool,
) -> float:
    """Reproducible from the evidence alone, which is what makes it storable."""
    tiers = [
        trust_tier(_source_class_of(session, evidence_id))
        for evidence_id in evidence_item_ids
    ]
    return compute_confidence(
        fact_type=fact_type,
        trust_tiers=tiers,
        independent_publishers=independent_publisher_count(session, evidence_item_ids),
        inferred=inferred,
    )


def _assert_confidence_still_holds(
    session: Session, *, claim: CompanyClaim, inferred: bool
) -> None:
    """The append-only guard. Recompute, compare, never write."""
    links = session.scalars(
        select(ClaimEvidenceLink).where(ClaimEvidenceLink.claim_id == claim.id)
    ).all()
    derived = _derive_confidence(
        session,
        fact_type=claim.fact_type,
        evidence_item_ids=[link.evidence_item_id for link in links],
        inferred=inferred,
    )
    if claim.confidence is not None and abs(float(claim.confidence) - derived) > 1e-9:
        raise ConfidenceDivergenceError(
            f"appending evidence moves confidence for claim {claim.id} from "
            f"{float(claim.confidence)} to {derived}, but company_claims is "
            "append-only; the assertion fingerprint's lineage key is too coarse"
        )


def _link_evidence(
    session: Session, *, claim: CompanyClaim, pending: PendingAssertion, now: datetime
) -> int:
    """Freeze the trust inputs in force for *this* assertion, on every link."""
    created = 0
    for evidence_id in pending.evidence_item_ids:
        source_class = _source_class_of(session, evidence_id)
        result = session.execute(
            pg_insert(ClaimEvidenceLink)
            .values(
                id=uuid.uuid4(),
                claim_id=claim.id,
                evidence_item_id=evidence_id,
                support_kind=pending.support_kind,
                source_class=source_class,
                trust_policy_version=TRUST_POLICY_VERSION,
                trust_tier=trust_tier(source_class),
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_claim_evidence")
            .returning(ClaimEvidenceLink.id)
        )
        if result.first() is not None:
            created += 1
    return created


def _source_class_of(session: Session, evidence_id: uuid.UUID) -> str:
    host = session.scalars(
        select(ResearchSource.host)
        .join(ResearchEvidenceItem, ResearchEvidenceItem.source_id == ResearchSource.id)
        .where(ResearchEvidenceItem.id == evidence_id)
    ).one()
    return publisher_for(host).source_class


def unknown_attribute_state(attribute_key: str) -> dict[str, object]:
    """How M3 reports an attribute it looked for and did not find.

    **No row is written.** Acceptance C9 is explicit that absence is a research
    gap, not a claim: "no `erp` claim exists with value false, 0 or 'none'; an
    `erp` gap of kind `NO_EVIDENCE` exists instead."

    An earlier draft of this module wrote a `NOT_AVAILABLE` claim here, which
    the deferred `company_claims_m3_requires_evidence` trigger rejects — an M3
    claim must cite evidence, and absence has none by definition. The trigger
    was right. A negative fact is a different case entirely and *is* a claim:
    a page stating "we do not offer emergency service" is positive evidence of
    a negative, and asserts `emergency_service = false` as a FACT.
    """
    get_attribute(attribute_key)  # refuse an attribute nobody registered
    return {
        "attribute_key": attribute_key,
        "availability": "NOT_AVAILABLE",
        "value": None,
        "fact_type": None,
    }


def claims_by_attribute(
    session: Session, company_id: uuid.UUID
) -> dict[str, list[CompanyClaim]]:
    rows = session.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id == company_id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
        )
    ).all()
    grouped: dict[str, list[CompanyClaim]] = defaultdict(list)
    for claim in rows:
        grouped[claim.attribute_key].append(claim)
    return dict(grouped)


def claim_observation_date(
    session: Session, claim_id: uuid.UUID
) -> tuple[date | None, str]:
    """The date the **source** stated, never when we fetched it.

    A crawl date is not an event date. Substituting `retrieved_at` would make a
    decade-old page read as today's news, which is the single most damaging
    thing a projection can do to evidence.
    """
    rows = session.execute(
        select(
            ResearchArtifactDerivation.source_published_at,
            ResearchArtifactDerivation.source_published_granularity,
        )
        .join(
            ResearchEvidenceItem,
            ResearchEvidenceItem.artifact_derivation_id == ResearchArtifactDerivation.id,
        )
        .join(ClaimEvidenceLink, ClaimEvidenceLink.evidence_item_id == ResearchEvidenceItem.id)
        .where(ClaimEvidenceLink.claim_id == claim_id)
    ).all()
    dated = [(d, g) for d, g in rows if d is not None]
    if not dated:
        return None, "UNDATED"
    return max(dated, key=lambda pair: pair[0])


ASSERTION_POLICY = ASSERTION_POLICY_VERSION
