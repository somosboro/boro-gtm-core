"""Inference rules: derived claims, stated rule, never FACT.

An inference is the one place M3 asserts something no source said, so it is
fenced on three sides:

* the **rule is named and versioned**, so the same evidence and the same rule
  version always give the same output;
* the claim is `INFERENCE`, which the registry enforces — the operating-model
  attributes an inference targets cannot reach `FACT` at all, because a company
  does not publish "our dispatch is decentralised";
* the derived claim carries **every input claim's evidence** as its own links,
  so the walk back to bytes still terminates.

The last point is why an inference is one claim over a multi-origin lineage
rather than several claims: it is one assertion justified by several
observations, and the `lineage_tag` exists to say so deliberately.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import ClaimEvidenceLink
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION
from boro_gtm.research.services.claims import PendingAssertion, assert_claim

#: Bumped when a rule's logic changes, so old claims stay explainable under the
#: version that produced them.
INFERENCE_RULE_VERSION = "1"


@dataclass(frozen=True, slots=True)
class InferenceRule:
    """One stated implication over already-asserted claims."""

    rule_id: str
    version: str
    #: Attribute keys whose claims the rule reads.
    reads: tuple[str, ...]
    #: The attribute it writes, and the enum value it concludes.
    writes: str
    conclusion: str
    rationale: str


#: A company that advertises round-the-clock response must be able to reach a
#: technician out of hours. That does not tell us *how* dispatch is organised,
#: only that some on-call arrangement is required — which is exactly what
#: `LIKELY_REQUIRED` says and why the value is not `CENTRAL`.
R_DISPATCH_001 = InferenceRule(
    rule_id="R-DISPATCH-001",
    version=INFERENCE_RULE_VERSION,
    reads=("emergency_service", "branch_count"),
    writes="dispatch_centralization",
    conclusion="LIKELY_REQUIRED",
    rationale=(
        "emergency_service = true with more than one location implies an "
        "out-of-hours dispatch requirement"
    ),
)

RULES: tuple[InferenceRule, ...] = (R_DISPATCH_001,)


@dataclass(slots=True)
class InferenceOutcome:
    rule: InferenceRule
    claim: CompanyClaim | None
    created: bool
    reason: str | None = None


def _supporting(
    session: Session, company_id: uuid.UUID, attribute_key: str
) -> list[CompanyClaim]:
    return list(session.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id == company_id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
            CompanyClaim.attribute_key == attribute_key,
            CompanyClaim.availability == "OBSERVED",
        )
    ).all())


def apply_dispatch_rule(
    session: Session, *, company_id: uuid.UUID, now: datetime
) -> InferenceOutcome:
    """Run `R-DISPATCH-001`, or say why it did not fire."""
    rule = R_DISPATCH_001
    emergency = [
        claim for claim in _supporting(session, company_id, "emergency_service")
        if (claim.value_jsonb or {}).get("value") is True
    ]
    if not emergency:
        return InferenceOutcome(rule, None, False, "no emergency_service = true claim")

    branches = _supporting(session, company_id, "branch_count")
    multi = [c for c in branches if ((c.value_jsonb or {}).get("max") or 0) > 1]
    if not multi:
        return InferenceOutcome(rule, None, False, "no evidence of several locations")

    inputs = emergency + multi
    evidence_ids = list(session.scalars(
        select(ClaimEvidenceLink.evidence_item_id).where(
            ClaimEvidenceLink.claim_id.in_([c.id for c in inputs])
        )
    ).all())
    if not evidence_ids:
        return InferenceOutcome(rule, None, False, "input claims carry no evidence")

    pending = PendingAssertion(
        attribute_key=rule.writes,
        value={
            "value": rule.conclusion,
            "inference_rule_id": rule.rule_id,
            "inference_rule_version": rule.version,
        },
        unit=None,
        # The registry forbids FACT here, so this is not a courtesy.
        fact_type="INFERENCE",
        support_kind="DERIVED",
        evidence_item_ids=sorted(set(evidence_ids), key=str),
    )
    result = assert_claim(
        session, company_id=company_id, pending=pending, now=now,
        inference_rule_version=rule.version,
    )
    return InferenceOutcome(rule, result.claim, result.created)


def apply_all(
    session: Session, *, company_id: uuid.UUID, now: datetime
) -> list[InferenceOutcome]:
    return [apply_dispatch_rule(session, company_id=company_id, now=now)]
