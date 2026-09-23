"""Deterministic projection rebuild (design §3.4, §5.3).

The projection function is **pure**. Its only inputs are the evidence tables,
the attribute registry version and the identity policy version. It reads no
clock, no sequence, no random source and no prior projection state — which is
what makes "truncate and rebuild byte-identically" a testable contract rather
than an aspiration.

Every hazard is closed structurally:

* rebuild timestamps live in ``projection_runs``, never in a projection
* projections use natural keys, so no generated UUID varies per rebuild
* arrays are sorted
* temporal projections store intervals; date filters live in views
* ties break on the lowest claim id, a total order over stored values
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from boro_gtm.core.enums import Availability, FactType
from boro_gtm.discovery.domain.models import (
    CompanyClaim,
    CompanyDomain,
    CompanyLocation,
    CompanyMarketPresence,
    CompanyName,
    CompanyProfile,
    CompanyRelationship,
    CompanyRelationshipClaim,
    CompanyVertical,
    DiscoveryProvider,
    EntityResolutionDecision,
    EntityResolutionHead,
    ProjectionRun,
    ProviderEntity,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import ClaimAssertion, ResolutionMethod
from boro_gtm.discovery.registry import (
    ATTRIBUTE_REGISTRY_VERSION,
    IDENTITY_POLICY_VERSION,
)
from boro_gtm.discovery.services.resolution import (
    is_identity_domain,
    rebuild_heads,
)

logger = logging.getLogger(__name__)

#: Sentinel for an interval with no stated start. A real date is required
#: because the interval start participates in the primary key; NULL would make
#: the key unusable. Chosen far enough back to be unambiguous.
OPEN_INTERVAL_START = date(1900, 1, 1)

#: Projection tables, in dependency order. This is exactly what the rebuild
#: contract covers — nothing else is truncated.
PROJECTION_TABLES = (
    CompanyProfile, CompanyName, CompanyDomain, CompanyLocation,
    CompanyMarketPresence, CompanyVertical, CompanyRelationship,
)

_FACT_RANK = {
    FactType.FACT.value: 5, FactType.PROXY.value: 4, FactType.ESTIMATE.value: 3,
    FactType.INFERENCE.value: 2, FactType.HYPOTHESIS.value: 1,
}


@dataclass(slots=True)
class _ScoredClaim:
    """A claim plus everything the precedence rules need, all stored values."""

    claim: CompanyClaim
    company_id: uuid.UUID
    human_review: bool
    trust_tier: float

    def precedence_key(self) -> tuple:
        """Deterministic total order. The trailing claim id is what makes it
        total — and it survives a rebuild from empty, unlike "keep the current
        value" (design §5.3)."""
        return (
            1 if self.human_review else 0,
            _FACT_RANK.get(self.claim.fact_type or "", 0),
            float(self.trust_tier),
            self.claim.observed_at or date.min,
            # Reversed so that, sorted descending overall, the *lowest* id wins.
            _uuid_desc(self.claim.id),
        )


def _uuid_desc(value: uuid.UUID) -> int:
    return -int(value)


def rebuild_projections(
    session: Session,
    *,
    registry_version: str = ATTRIBUTE_REGISTRY_VERSION,
    identity_policy_version: str = IDENTITY_POLICY_VERSION,
    triggered_by: str = "system:rebuild",
    record_run: bool = True,
) -> ProjectionRun | None:
    """Truncate every derived projection and rebuild it from evidence."""
    started = datetime.now(UTC)

    for model in PROJECTION_TABLES:
        session.execute(delete(model))
    session.flush()

    rebuild_heads(session)

    scored = _load_effective_claims(session, registry_version)
    _project_profiles(session, scored, registry_version)
    _project_names(session, scored)
    _project_domains(session, scored)
    _project_locations(session, scored)
    _project_market_presences(session, scored)
    _project_verticals(session, scored)
    _project_relationships(session)
    session.flush()

    if not record_run:
        return None

    run = ProjectionRun(
        started_at=started,
        completed_at=datetime.now(UTC),
        attribute_registry_version=registry_version,
        identity_policy_version=identity_policy_version,
        row_counts=_row_counts(session),
        content_digests=content_digests(session),
        triggered_by=triggered_by,
    )
    session.add(run)
    session.flush()
    return run


def _load_effective_claims(session: Session, registry_version: str
                           ) -> dict[uuid.UUID, list[_ScoredClaim]]:
    """Attribution runs through the *effective* decision, never a stored id.

    A claim written under a superseded decision therefore projects onto
    whichever company its provider entity now resolves to — and contributes to
    no other company's projection (design §5.5).
    """
    trust_by_provider = {
        row.id: float(row.trust_tier)
        for row in session.scalars(select(DiscoveryProvider)).all()
    }
    human_decisions = {
        row.id for row in session.scalars(
            select(EntityResolutionDecision).where(
                EntityResolutionDecision.method == ResolutionMethod.HUMAN_REVIEW.value
            )
        ).all()
    }

    # provider-sourced: claim -> version -> entity -> head -> company
    provider_rows = session.execute(
        select(CompanyClaim, EntityResolutionHead.company_id, ProviderEntity.provider_id)
        .join(ProviderRecordVersion,
              ProviderRecordVersion.id == CompanyClaim.provider_record_version_id)
        .join(ProviderEntity, ProviderEntity.id == ProviderRecordVersion.provider_entity_id)
        .join(EntityResolutionHead,
              EntityResolutionHead.provider_entity_id == ProviderEntity.id)
        .where(
            EntityResolutionHead.company_id.is_not(None),
            CompanyClaim.availability == Availability.OBSERVED.value,
            CompanyClaim.attribute_registry_version == registry_version,
        )
    ).all()

    by_company: dict[uuid.UUID, list[_ScoredClaim]] = defaultdict(list)
    for claim, company_id, provider_id in provider_rows:
        by_company[company_id].append(_ScoredClaim(
            claim=claim, company_id=company_id,
            human_review=claim.resolution_decision_id in human_decisions,
            trust_tier=trust_by_provider.get(provider_id, 0.5),
        ))

    # directly attributed claims (human or derived)
    direct = session.scalars(
        select(CompanyClaim).where(
            CompanyClaim.subject_company_id.is_not(None),
            CompanyClaim.availability == Availability.OBSERVED.value,
            CompanyClaim.attribute_registry_version == registry_version,
        )
    ).all()
    for claim in direct:
        by_company[claim.subject_company_id].append(_ScoredClaim(
            claim=claim, company_id=claim.subject_company_id,
            human_review=True, trust_tier=1.0,
        ))
    return by_company


def _winner(claims: list[_ScoredClaim]) -> _ScoredClaim:
    """Highest precedence; ties broken by lowest claim id."""
    return max(claims, key=lambda c: c.precedence_key())


def _sorted_ids(claims: list[_ScoredClaim]) -> list[uuid.UUID]:
    """Arrays are stored sorted so two rebuilds cannot differ by ordering."""
    return sorted({c.claim.id for c in claims})


def _by_attribute(claims: list[_ScoredClaim]) -> dict[str, list[_ScoredClaim]]:
    out: dict[str, list[_ScoredClaim]] = defaultdict(list)
    for scored in claims:
        out[scored.claim.attribute_key].append(scored)
    return out


def _scalar_winner(grouped, key, contributing) -> tuple[Any, bool]:
    """Pick the winning scalar value and report whether it was contested.

    A tie at equal precedence with differing values is a genuine conflict; the
    lowest claim id still wins so the rebuild stays deterministic, but the
    projection records that the evidence disagreed.
    """
    group = grouped.get(key)
    if not group:
        return None, False
    winner = _winner(group)
    contributing.append(winner)
    top_rank = winner.precedence_key()[:4]
    tied = [w for w in group if w.precedence_key()[:4] == top_rank]
    values = {json.dumps(w.claim.value_jsonb, sort_keys=True, default=str) for w in tied}
    return (winner.claim.value_jsonb or {}).get("value"), len(values) > 1


def _project_profiles(session, by_company, registry_version) -> None:
    for company_id, claims in by_company.items():
        grouped = _by_attribute(claims)
        contributing: list[_ScoredClaim] = []

        name, name_conflict = _scalar_winner(grouped, "legal_name", contributing)
        legal_form, form_conflict = _scalar_winner(grouped, "legal_form", contributing)
        founded, founded_conflict = _scalar_winner(grouped, "founded_year", contributing)
        conflict = name_conflict or form_conflict or founded_conflict

        # ENVELOPE, not a winner: providers disagreeing is information.
        envelope_min = envelope_max = None
        employee = grouped.get("employee_count") or []
        if employee:
            mins = [c.claim.value_jsonb.get("min") for c in employee
                    if (c.claim.value_jsonb or {}).get("min") is not None]
            maxs = [c.claim.value_jsonb.get("max") for c in employee
                    if (c.claim.value_jsonb or {}).get("max") is not None]
            envelope_min = min(mins) if mins else None
            envelope_max = max(maxs) if maxs else None
            contributing.extend(employee)

        identity_domains = [
            c for c in grouped.get("domain", [])
            if (c.claim.value_jsonb or {}).get("role") == "IDENTITY"
        ]
        primary_domain = None
        if identity_domains:
            winner = _winner(identity_domains)
            contributing.append(winner)
            primary_domain = (winner.claim.value_jsonb or {}).get("value")

        session.add(CompanyProfile(
            company_id=company_id,
            canonical_name=name,
            primary_domain=primary_domain,
            legal_form=legal_form,
            founded_year=int(founded) if founded is not None else None,
            employee_count_min=int(envelope_min) if envelope_min is not None else None,
            employee_count_max=int(envelope_max) if envelope_max is not None else None,
            derived_from_claim_ids=_sorted_ids(contributing),
            projection_conflict=conflict,
            attribute_registry_version=registry_version,
        ))


def _project_names(session, by_company) -> None:
    for company_id, claims in by_company.items():
        grouped = _by_attribute(claims)
        buckets: dict[tuple[str, str], list[_ScoredClaim]] = defaultdict(list)
        for key, name_type in (("legal_name", "LEGAL"), ("trading_name", "TRADING")):
            for scored in grouped.get(key, []):
                value = scored.claim.value_jsonb or {}
                normalized = value.get("normalized") or value.get("value")
                if normalized:
                    buckets[(normalized, name_type)].append(scored)
        primary = None
        legal = [k for k in buckets if k[1] == "LEGAL"]
        if legal:
            primary = min(legal)
        for (normalized, name_type), group in buckets.items():
            winner = _winner(group)
            session.add(CompanyName(
                company_id=company_id,
                name_normalized=normalized,
                name_type=name_type,
                name_raw=(winner.claim.value_jsonb or {}).get("value") or normalized,
                is_primary=((normalized, name_type) == primary),
                derived_from_claim_ids=_sorted_ids(group),
            ))


def _project_domains(session, by_company) -> None:
    claimed_identity: set[str] = set()
    for company_id in sorted(by_company, key=str):
        grouped = _by_attribute(by_company[company_id])
        buckets: dict[str, list[_ScoredClaim]] = defaultdict(list)
        roles: dict[str, str] = {}
        for scored in grouped.get("domain", []):
            value = scored.claim.value_jsonb or {}
            domain = value.get("value")
            if not domain:
                continue
            buckets[domain].append(scored)
            role = value.get("role", "ALTERNATE")
            if role == "IDENTITY" and not is_identity_domain(domain):
                # Policy is enforced where the projection is written, not left
                # to every reader to re-apply.
                role = "GROUP"
            if role == "IDENTITY":
                roles[domain] = "IDENTITY"
            roles.setdefault(domain, role)
        for domain in sorted(buckets):
            role = roles.get(domain, "ALTERNATE")
            if role == "IDENTITY":
                if domain in claimed_identity:
                    # Another company already holds this domain as identity.
                    # Demote rather than violating the partial unique index.
                    role = "GROUP"
                else:
                    claimed_identity.add(domain)
            session.add(CompanyDomain(
                company_id=company_id, domain_normalized=domain,
                domain_role=role, derived_from_claim_ids=_sorted_ids(buckets[domain]),
            ))


def _project_locations(session, by_company) -> None:
    for company_id, claims in by_company.items():
        grouped = _by_attribute(claims)
        buckets: dict[tuple[str, str], list[_ScoredClaim]] = defaultdict(list)
        for scored in grouped.get("location", []):
            value = scored.claim.value_jsonb or {}
            key = (value.get("location_type") or "HEADQUARTERS",
                   value.get("address_normalized") or "unknown")
            buckets[key].append(scored)
        for (location_type, address_normalized), group in buckets.items():
            winner = (_winner(group).claim.value_jsonb or {})
            market_id = winner.get("market_id")
            session.add(CompanyLocation(
                company_id=company_id,
                location_type=location_type,
                address_normalized=address_normalized,
                market_id=uuid.UUID(market_id) if market_id else None,
                city=winner.get("city"),
                postal_code=winner.get("postal_code"),
                address_raw=winner.get("address_raw"),
                derived_from_claim_ids=_sorted_ids(group),
            ))


def _project_market_presences(session, by_company) -> None:
    """Deterministic intervals. No clock is read (design §5.7)."""
    for company_id, claims in by_company.items():
        grouped = _by_attribute(claims)
        intervals: dict[tuple[uuid.UUID, str, date], list[_ScoredClaim]] = defaultdict(list)
        ends: dict[tuple[uuid.UUID, str, date], date | None] = {}
        retracted: set[tuple[uuid.UUID, str, date]] = set()

        for scored in sorted(grouped.get("market_presence", []), key=lambda c: str(c.claim.id)):
            value = scored.claim.value_jsonb or {}
            market_raw = value.get("market_id")
            if not market_raw:
                continue
            key = (
                uuid.UUID(str(market_raw)),
                value.get("presence_type") or "OPERATES",
                _as_date(value.get("valid_from")) or OPEN_INTERVAL_START,
            )
            if value.get("assertion") == ClaimAssertion.RETRACTED.value:
                retracted.add(key)
                continue
            intervals[key].append(scored)
            # Only a positive assertion closes an interval. Provider silence
            # never does (design §5.7).
            end = _as_date(value.get("valid_to"))
            if end is not None:
                ends[key] = end

        for key, group in intervals.items():
            if key in retracted:
                continue
            market_id, presence_type, effective_from = key
            winner = _winner(group)
            session.add(CompanyMarketPresence(
                company_id=company_id, market_id=market_id,
                presence_type=presence_type,
                effective_from=effective_from,
                effective_to=ends.get(key),
                fact_type=winner.claim.fact_type,
                confidence=winner.claim.confidence,
                derived_from_claim_ids=_sorted_ids(group),
            ))


def _project_verticals(session, by_company) -> None:
    for company_id, claims in by_company.items():
        grouped = _by_attribute(claims)
        buckets: dict[uuid.UUID, list[_ScoredClaim]] = defaultdict(list)
        for scored in grouped.get("vertical", []):
            raw = (scored.claim.value_jsonb or {}).get("vertical_id")
            if raw:
                buckets[uuid.UUID(str(raw))].append(scored)
        primary = min(buckets, key=str) if buckets else None
        for vertical_id, group in buckets.items():
            winner = _winner(group)
            session.add(CompanyVertical(
                company_id=company_id, vertical_id=vertical_id,
                classification_method=(winner.claim.value_jsonb or {}).get(
                    "classification_method", "PROVIDER_TAXONOMY"),
                fact_type=winner.claim.fact_type,
                confidence=winner.claim.confidence,
                is_primary=(vertical_id == primary),
                derived_from_claim_ids=_sorted_ids(group),
            ))


def _project_relationships(session) -> None:
    """Effective, non-retracted relationship intervals (design §12.4)."""
    claims = session.scalars(
        select(CompanyRelationshipClaim).order_by(CompanyRelationshipClaim.id)
    ).all()
    superseded = {c.supersedes_claim_id for c in claims if c.supersedes_claim_id}

    buckets: dict[tuple, list[CompanyRelationshipClaim]] = defaultdict(list)
    ends: dict[tuple, date | None] = {}
    retracted: set[tuple] = set()

    for claim in claims:
        if claim.id in superseded:
            continue
        key = (claim.from_company_id, claim.to_company_id, claim.relationship_type,
               claim.valid_from or OPEN_INTERVAL_START)
        if claim.assertion == ClaimAssertion.RETRACTED.value:
            retracted.add(key)
            continue
        buckets[key].append(claim)
        if claim.valid_to is not None:
            ends[key] = claim.valid_to

    for key, group in buckets.items():
        if key in retracted:
            continue
        from_id, to_id, rel_type, effective_from = key
        winner = min(group, key=lambda c: str(c.id))
        session.add(CompanyRelationship(
            from_company_id=from_id, to_company_id=to_id,
            relationship_type=rel_type,
            effective_from=effective_from, effective_to=ends.get(key),
            fact_type=winner.fact_type, confidence=winner.confidence,
            derived_from_claim_ids=sorted({c.id for c in group}),
        ))


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _row_counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.query(model).count() for model in PROJECTION_TABLES
    }


def content_digests(session: Session) -> dict[str, str]:
    """SHA-256 over a canonical row serialization, per projection table.

    **Telemetry only.** The rebuild contract is exact row-set equality; this
    exists so production drift is detectable without a second copy of the data
    (M2-ADR-030).
    """
    digests: dict[str, str] = {}
    for model in PROJECTION_TABLES:
        rows = [_canonical_row(model, obj) for obj in session.scalars(select(model)).all()]
        payload = json.dumps(sorted(rows), sort_keys=True, separators=(",", ":"))
        digests[model.__tablename__] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digests


def _canonical_row(model, obj) -> str:
    values = {}
    for column in model.__table__.columns:
        raw = getattr(obj, column.name)
        if isinstance(raw, uuid.UUID):
            raw = str(raw)
        elif isinstance(raw, date):
            raw = raw.isoformat()
        elif isinstance(raw, list):
            raw = [str(v) for v in raw]
        elif raw is not None and column.type.__class__.__name__ == "Numeric":
            raw = float(raw)
        values[column.name] = raw
    return json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)


def normalized_rows(session: Session) -> dict[str, list[str]]:
    """Exact normalized row-sets — what the acceptance contract compares."""
    return {
        model.__tablename__: sorted(
            _canonical_row(model, obj) for obj in session.scalars(select(model)).all()
        )
        for model in PROJECTION_TABLES
    }
