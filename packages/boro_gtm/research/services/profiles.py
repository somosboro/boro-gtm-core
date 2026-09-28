"""The two stored projections, and the precedence rule that makes them stable.

Both are **derived**: truncate them and a rebuild reproduces them from the
claim ledger alone. That means no clock, no randomness, no dependence on
insertion order, and no dependence on whatever the projection previously said.
Every one of those has broken a projection in this project before, so each is
enforced by a test rather than trusted.

The split between the two is the load-bearing part:

* `operational_research_profiles` is **company-global**: what we believe about
  this company from all evidence, whoever went looking. A fact is a fact
  whoever asked.
* `operational_research_plan_profiles` is **per research question**: coverage,
  confidence and contradiction rate, all three of which depend on the target
  set and on applicability, and none of which is a property of the company.

Collapsing them, or copying coverage onto the company profile, would produce
something that reads like a score — and something that reads like a score gets
used as one.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    ClaimEvidenceLink,
    OperationalResearchGap,
    OperationalResearchPlanProfile,
    OperationalResearchProfile,
    OperationalResearchRun,
    ResearchArtifactDerivation,
    ResearchEvidenceItem,
    ResearchFetchEvent,
)
from boro_gtm.research.policies import (
    ASSERTION_POLICY_VERSION,
    PUBLISHER_POLICY_VERSION,
)
from boro_gtm.research.registry import (
    FACT_TYPE_ORDER,
    RESEARCH_REGISTRY_VERSION,
    get_attribute,
    is_applicable,
)
from boro_gtm.research.services.evidence import independent_publisher_count
from boro_gtm.strategy.domain.models import Vertical

#: Every required attribute weighs the same. The formula is written as a
#: weighted sum anyway, because the registry may later disagree — and a
#: hard-coded count would have to be found and rewritten when it does.
ATTRIBUTE_WEIGHT = 1.0


# ---------------------------------------------------------------------------
# Deterministic precedence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimFacts:
    """Everything the precedence rule needs, read once per claim."""

    claim: CompanyClaim
    trust_tier: float
    source_published_at: date | None
    earliest_retrieved_at: Any
    evidence_item_ids: tuple[uuid.UUID, ...]


def _claim_facts(session: Session, claims: list[CompanyClaim]) -> list[ClaimFacts]:
    if not claims:
        return []
    ids = [c.id for c in claims]

    trust: dict[uuid.UUID, float] = defaultdict(float)
    evidence: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for claim_id, evidence_id, tier in session.execute(
        select(
            ClaimEvidenceLink.claim_id,
            ClaimEvidenceLink.evidence_item_id,
            ClaimEvidenceLink.trust_tier,
        ).where(ClaimEvidenceLink.claim_id.in_(ids))
    ).all():
        # The tier recorded on the link at assertion time, never recomputed.
        trust[claim_id] = max(trust[claim_id], float(tier))
        evidence[claim_id].append(evidence_id)

    published: dict[uuid.UUID, date | None] = {}
    retrieved: dict[uuid.UUID, Any] = {}
    for claim_id, pub, ret in session.execute(
        select(
            ClaimEvidenceLink.claim_id,
            func.max(ResearchArtifactDerivation.source_published_at),
            func.min(ResearchFetchEvent.retrieved_at),
        )
        .join(ResearchEvidenceItem,
              ResearchEvidenceItem.id == ClaimEvidenceLink.evidence_item_id)
        .join(ResearchArtifactDerivation,
              ResearchArtifactDerivation.id
              == ResearchEvidenceItem.artifact_derivation_id)
        .join(ResearchFetchEvent,
              ResearchFetchEvent.id == ResearchEvidenceItem.fetch_event_id)
        .where(ClaimEvidenceLink.claim_id.in_(ids))
        .group_by(ClaimEvidenceLink.claim_id)
    ).all():
        published[claim_id] = pub
        retrieved[claim_id] = ret

    return [
        ClaimFacts(
            claim=claim,
            trust_tier=trust.get(claim.id, 0.0),
            source_published_at=published.get(claim.id),
            earliest_retrieved_at=retrieved.get(claim.id),
            evidence_item_ids=tuple(sorted(evidence.get(claim.id, []), key=str)),
        )
        for claim in claims
    ]


def precedence_key(facts: ClaimFacts) -> tuple:
    """The frozen order from M3-ADR-012, as a sort key.

    Fact type, then source trust, then the source's own publication date with
    NULLs last, then the earliest retrieval across the lineage, then the lowest
    claim id.

    The final tiebreak is the id and **not** "keep the current value": M2 proved
    that rule is not deterministic on a rebuild from empty (M2-ADR-024).
    """
    fact_rank = (
        FACT_TYPE_ORDER.index(facts.claim.fact_type)
        if facts.claim.fact_type in FACT_TYPE_ORDER else -1
    )
    has_date = facts.source_published_at is not None
    # Every rung is negated so that "more" sorts first under `min`. The
    # retrieval rung was not, so an *older* earliest retrieval won — the exact
    # inverse of the rule. `earliest_retrieved_at` still means the earliest
    # retrieval across the lineage; what changed is that a more recent one now
    # outranks an older one, as the frozen precedence says.
    retrieved = facts.earliest_retrieved_at
    return (
        -fact_rank,
        -facts.trust_tier,
        0 if has_date else 1,
        -(facts.source_published_at.toordinal() if has_date else 0),
        -retrieved.timestamp() if retrieved is not None else 0.0,
        str(facts.claim.id),
    )


# ---------------------------------------------------------------------------
# Value envelopes
# ---------------------------------------------------------------------------


def _envelope(attribute_key: str, group: list[ClaimFacts]) -> dict[str, Any]:
    """Preserve disagreement; never silently pick a winner and drop the rest."""
    attribute = get_attribute(attribute_key)
    values = [facts.claim.value_jsonb for facts in group]
    best = min(group, key=precedence_key)

    envelope: dict[str, Any] = {}
    if attribute.value_kind == "RANGE":
        lows = [v.get("min") for v in values if v and v.get("min") is not None]
        highs = [v.get("max") for v in values if v and v.get("max") is not None]
        envelope = {
            "min": min(lows) if lows else None,
            "max": max(highs) if highs else None,
        }
    elif attribute.value_kind == "SET":
        merged: list[Any] = []
        for value in values:
            for item in (value or {}).get("values", []):
                if item not in merged:
                    merged.append(item)
        envelope = {"values": sorted(merged, key=_stable)}
    else:
        distinct = []
        for value in values:
            scalar = (value or {}).get("value")
            if scalar not in distinct:
                distinct.append(scalar)
        envelope = {"values": sorted(distinct, key=_stable)}

    return {
        "envelope": envelope,
        "best": best.claim.value_jsonb,
        "best_claim_id": str(best.claim.id),
        "best_fact_type": best.claim.fact_type,
        "confidence": (
            float(best.claim.confidence) if best.claim.confidence is not None else None
        ),
        "unit": best.claim.unit,
        "observed_at": (
            best.source_published_at.isoformat() if best.source_published_at else None
        ),
        "observed_granularity": "DATE" if best.source_published_at else "UNDATED",
    }


def _stable(value: Any) -> str:
    """One ordering for mixed JSON scalars, so rebuilds do not differ."""
    from boro_gtm.research.policies import canonical_json

    return canonical_json(value)


def _contradicted(attribute_key: str, group: list[ClaimFacts]) -> bool:
    """Disagreement, not merely several claims.

    Two lineages asserting the *same* value is corroboration. A contradiction
    is two lineages asserting different values for the same attribute.
    """
    attribute = get_attribute(attribute_key)
    if attribute.value_kind == "SET":
        # A set attribute accumulates; two sources naming different services is
        # not a disagreement about the same slot.
        return False
    distinct = {_stable(facts.claim.value_jsonb) for facts in group}
    return len(distinct) > 1


# ---------------------------------------------------------------------------
# Company-global profile
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CompanyProfileResult:
    profile: OperationalResearchProfile
    attributes: int
    contradicted: int


def _lock(session: Session, key: str) -> None:
    """One rebuild per company at a time; the loser simply re-derives.

    A transaction-scoped advisory lock rather than row locks, because the
    projection is written by DELETE + INSERT and there is no row to lock until
    one exists.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
    )


def rebuild_company_profile(
    session: Session, company_id: uuid.UUID
) -> CompanyProfileResult:
    """Derive the company-global profile from the claim ledger alone."""
    _lock(session, f"m3-company-profile:{company_id}")

    claims = session.scalars(
        select(CompanyClaim)
        .where(
            CompanyClaim.subject_company_id == company_id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
            CompanyClaim.availability == "OBSERVED",
        )
        .order_by(CompanyClaim.id)
    ).all()

    grouped: dict[str, list[ClaimFacts]] = defaultdict(list)
    for facts in _claim_facts(session, claims):
        grouped[facts.claim.attribute_key].append(facts)

    projected: dict[str, Any] = {}
    contradictions: dict[str, Any] = {}
    publishers: dict[str, int] = {}
    contributing: set[uuid.UUID] = set()

    for attribute_key in sorted(grouped):
        group = sorted(grouped[attribute_key], key=precedence_key)
        state = _envelope(attribute_key, group)
        state["availability"] = "OBSERVED"
        state["claim_ids"] = sorted(str(f.claim.id) for f in group)
        projected[attribute_key] = state

        contradicted = _contradicted(attribute_key, group)
        contradictions[attribute_key] = {
            "contradiction": contradicted,
            "claim_ids": state["claim_ids"] if contradicted else [],
        }
        evidence_ids = [e for f in group for e in f.evidence_item_ids]
        publishers[attribute_key] = independent_publisher_count(session, evidence_ids)
        contributing.update(f.claim.id for f in group)

    profile = session.get(OperationalResearchProfile, company_id)
    if profile is None:
        profile = OperationalResearchProfile(company_id=company_id)
        session.add(profile)
    profile.facts = projected
    profile.contradictions = contradictions
    profile.corroborating_publisher_counts = publishers
    profile.derived_from_claim_ids = sorted(contributing, key=str)
    profile.assertion_policy_version = ASSERTION_POLICY_VERSION
    profile.publisher_policy_version = PUBLISHER_POLICY_VERSION
    session.flush()

    return CompanyProfileResult(
        profile=profile,
        attributes=len(projected),
        contradicted=sum(1 for v in contradictions.values() if v["contradiction"]),
    )


# ---------------------------------------------------------------------------
# Plan-scoped profile
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Coverage:
    """Coverage and its two companions, deliberately not collapsed."""

    coverage: float
    confidence_summary: float | None
    contradiction_rate: float
    required_attribute_count: int
    covered_attribute_count: int
    not_applicable_attribute_count: int
    open_gap_count: int


def _vertical_key(session: Session, run: OperationalResearchRun) -> str | None:
    if run.vertical_id is None:
        return None
    return session.scalar(select(Vertical.key).where(Vertical.id == run.vertical_id))


def compute_coverage(session: Session, run: OperationalResearchRun) -> Coverage:
    """`covered_required_weight / total_required_weight`, and nothing else.

    Three rules that are easy to break and each destroy the number's meaning:

    * **unknown is not zero evidence** — an attribute nobody evidenced is
      missing from the numerator, and that is all it means;
    * **not applicable leaves both sides untouched** — a vertical that has no
      fleet must not be scored as a company hiding its fleet;
    * **optional attributes never enter the denominator**, so finding extra
      evidence cannot dilute required coverage.

    Coverage is not a lead score, a fit, a priority or a sales-readiness
    figure. It is the fraction of what we set out to learn that we learned.
    """
    vertical = _vertical_key(session, run)
    targets = list(run.target_attribute_keys)

    evidenced = set(session.scalars(
        select(CompanyClaim.attribute_key).where(
            CompanyClaim.subject_company_id == run.company_id,
            CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
            CompanyClaim.availability == "OBSERVED",
        )
    ).all())

    total_weight = 0.0
    covered_weight = 0.0
    required = 0
    covered = 0
    not_applicable = 0

    for key in targets:
        attribute = get_attribute(key)
        if not is_applicable(attribute, vertical):
            not_applicable += 1
            continue                      # neither numerator nor denominator
        if not attribute.required:
            continue                      # optional never enters the denominator
        required += 1
        total_weight += ATTRIBUTE_WEIGHT
        if key in evidenced:
            covered += 1
            covered_weight += ATTRIBUTE_WEIGHT

    coverage = round(covered_weight / total_weight, 4) if total_weight else 0.0

    profile = session.get(OperationalResearchProfile, run.company_id)
    facts = (profile.facts if profile else {}) or {}
    contradictions = (profile.contradictions if profile else {}) or {}

    scored = [
        facts[key]["confidence"] for key in targets
        if key in facts and facts[key].get("confidence") is not None
    ]
    confidence_summary = (
        round(sum(scored) / len(scored), 4) if scored else None
    )
    present = [key for key in targets if key in contradictions]
    contradicted = sum(
        1 for key in present if contradictions[key]["contradiction"]
    )
    contradiction_rate = (
        round(contradicted / len(present), 4) if present else 0.0
    )

    open_gaps = 0
    for gap_id in session.scalars(
        select(OperationalResearchGap.id).where(
            OperationalResearchGap.run_id == run.id
        )
    ).all():
        from boro_gtm.research.services.gaps import current_status

        if current_status(session, gap_id) in {"RAISED", "ATTEMPTED"}:
            open_gaps += 1

    return Coverage(
        coverage=coverage,
        confidence_summary=confidence_summary,
        contradiction_rate=contradiction_rate,
        required_attribute_count=required,
        covered_attribute_count=covered,
        not_applicable_attribute_count=not_applicable,
        open_gap_count=open_gaps,
    )


def rebuild_plan_profile(
    session: Session, run_id: uuid.UUID
) -> OperationalResearchPlanProfile:
    """One summary per research question. Never copied onto the company."""
    _lock(session, f"m3-plan-profile:{run_id}")
    run = session.get(OperationalResearchRun, run_id)
    if run is None:
        raise ValueError(f"no research run {run_id}")

    coverage = compute_coverage(session, run)
    profile = session.get(OperationalResearchPlanProfile, run_id)
    if profile is None:
        profile = OperationalResearchPlanProfile(run_id=run_id, company_id=run.company_id)
        session.add(profile)
    profile.company_id = run.company_id
    profile.coverage = coverage.coverage
    profile.confidence_summary = coverage.confidence_summary
    profile.contradiction_rate = coverage.contradiction_rate
    profile.required_attribute_count = coverage.required_attribute_count
    profile.covered_attribute_count = coverage.covered_attribute_count
    profile.not_applicable_attribute_count = coverage.not_applicable_attribute_count
    profile.open_gap_count = coverage.open_gap_count
    profile.assertion_policy_version = ASSERTION_POLICY_VERSION
    profile.publisher_policy_version = PUBLISHER_POLICY_VERSION
    session.flush()
    return profile


def rebuild_all(
    session: Session, *, company_id: uuid.UUID, run_id: uuid.UUID | None = None
) -> tuple[CompanyProfileResult, OperationalResearchPlanProfile | None]:
    """The completion point: company first, then the question that asked."""
    company = rebuild_company_profile(session, company_id)
    plan = rebuild_plan_profile(session, run_id) if run_id else None
    return company, plan
