"""Claim writing (design §5).

Every claim goes through the attribute registry, so no untyped value can be
stored. Provider-sourced claims carry **no** ``company_id``: attribution is
derived through the effective resolution decision, which is what lets a
corrected resolution move an entire claim history with zero writes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from boro_gtm.core.enums import Availability, FactType, PeriodGranularity
from boro_gtm.discovery.domain.models import CompanyClaim, CompanyRelationshipClaim
from boro_gtm.discovery.enums import ClaimAssertion
from boro_gtm.discovery.providers.base import CandidateCompany
from boro_gtm.discovery.registry import ATTRIBUTE_REGISTRY_VERSION, get_definition
from boro_gtm.discovery.services.resolution import (
    is_identity_domain,
    normalize_domain,
    normalize_name,
)
from boro_gtm.market_intelligence.scoring.confidence import evidence_confidence


def write_claim(
    session: Session,
    attribute_key: str,
    value_jsonb: dict[str, Any] | None,
    *,
    provider_record_version_id: uuid.UUID | None = None,
    subject_company_id: uuid.UUID | None = None,
    resolution_decision_id: uuid.UUID | None = None,
    fact_type: str | None = FactType.FACT.value,
    unit: str | None = None,
    observed_at: date | None = None,
    period_granularity: str = PeriodGranularity.UNDATED.value,
    confidence_label: str | None = None,
    registry_version: str = ATTRIBUTE_REGISTRY_VERSION,
    now: datetime | None = None,
) -> CompanyClaim:
    """Validate against the registry, then append one claim.

    Typed shadows are derived by the registry's single extractor, never passed
    in, so ``value_jsonb`` and its shadow cannot disagree.
    """
    now = now or datetime.now(UTC)
    definition = get_definition(attribute_key, registry_version)

    available = value_jsonb is not None
    if not available:
        fact_type = None
    definition.validate(value_jsonb if available else None, unit, fact_type)

    shadows = definition.extract_shadow(value_jsonb) if available else {
        "value_numeric": None, "value_text": None, "value_ref_id": None
    }

    claim = CompanyClaim(
        attribute_key=attribute_key,
        attribute_registry_version=registry_version,
        value_jsonb=value_jsonb if available else None,
        unit=unit if available else None,
        fact_type=fact_type,
        availability=(
            Availability.OBSERVED.value if available else Availability.NOT_AVAILABLE.value
        ),
        confidence=(
            evidence_confidence(fact_type, period_granularity, confidence_label)
            if available else None
        ),
        provider_record_version_id=provider_record_version_id,
        subject_company_id=subject_company_id,
        resolution_decision_id=resolution_decision_id,
        observed_at=observed_at,
        period_granularity=period_granularity,
        created_at=now,
        **shadows,
    )
    session.add(claim)
    session.flush()
    return claim


def write_claims_for_candidate(
    session: Session,
    candidate: CandidateCompany,
    provider_record_version_id: uuid.UUID,
    resolution_decision_id: uuid.UUID,
    *,
    vertical_ids: dict[str, uuid.UUID] | None = None,
    market_id: uuid.UUID | None = None,
    confidence_label: str | None = None,
    now: datetime | None = None,
) -> list[CompanyClaim]:
    """Turn one normalized candidate into typed claims."""
    now = now or datetime.now(UTC)
    written: list[CompanyClaim] = []

    def add(key: str, value: dict[str, Any] | None, fact_type: str,
            unit: str | None = None) -> None:
        written.append(write_claim(
            session, key, value,
            provider_record_version_id=provider_record_version_id,
            resolution_decision_id=resolution_decision_id,
            fact_type=fact_type, unit=unit,
            period_granularity=PeriodGranularity.SNAPSHOT.value,
            confidence_label=confidence_label, now=now,
        ))

    if candidate.legal_name:
        add("legal_name", {"value": candidate.legal_name,
                           "normalized": normalize_name(candidate.legal_name)},
            FactType.FACT.value)
    for trading in candidate.trading_names:
        add("trading_name", {"value": trading,
                             "normalized": normalize_name(trading)},
            FactType.FACT.value)

    domain = normalize_domain(candidate.registrable_domain)
    if domain:
        # A shared hosting domain is where a company lives, not who it is
        # (design §8), so it is recorded as GROUP and never carries identity.
        add("domain",
            {"value": domain,
             "role": "IDENTITY" if is_identity_domain(domain) else "GROUP"},
            FactType.FACT.value)
    for other in candidate.other_domains:
        normalized = normalize_domain(other)
        if normalized and normalized != domain:
            add("domain", {"value": normalized, "role": "ALTERNATE"},
                FactType.FACT.value)

    if candidate.legal_form:
        add("legal_form", {"value": candidate.legal_form}, FactType.FACT.value)
    if candidate.founded_year:
        add("founded_year", {"value": candidate.founded_year}, FactType.FACT.value)

    if candidate.employee_count_min is not None or candidate.employee_count_max is not None:
        # A range, so providers disagreeing projects an envelope rather than a
        # winner (design §5.3).
        add("employee_count",
            {"min": candidate.employee_count_min, "max": candidate.employee_count_max},
            FactType.ESTIMATE.value, unit="PEOPLE")

    if candidate.address or candidate.city:
        add("location", {
            "location_type": "HEADQUARTERS",
            "address_raw": candidate.address,
            "address_normalized": _normalize_address(candidate),
            "city": candidate.city,
            "postal_code": candidate.postal_code,
            "market_id": str(market_id) if market_id else None,
        }, FactType.FACT.value)

    if market_id is not None:
        # Presence is temporal: an open interval, with no end asserted.
        add("market_presence", {
            "market_id": str(market_id),
            "presence_type": "OPERATES",
            "valid_from": None,
            "valid_to": None,
            "assertion": ClaimAssertion.ASSERTED.value,
        }, FactType.INFERENCE.value)

    for hint in candidate.vertical_hints:
        vertical_id = (vertical_ids or {}).get(hint)
        if vertical_id is None:
            continue
        # A provider taxonomy hint is a PROXY, never a FACT.
        add("vertical", {"vertical_id": str(vertical_id),
                         "classification_method": "PROVIDER_TAXONOMY",
                         "source_label": hint},
            FactType.PROXY.value)

    for scheme, value in candidate.registry_ids.items():
        add("legal_entity", {"scheme": scheme, "identifier": value},
            FactType.FACT.value)

    return written


def write_relationship_claim(
    session: Session,
    from_company_id: uuid.UUID,
    to_company_id: uuid.UUID,
    relationship_type: str,
    *,
    valid_from: date | None = None,
    valid_to: date | None = None,
    assertion: str = ClaimAssertion.ASSERTED.value,
    supersedes_claim_id: uuid.UUID | None = None,
    provider_record_version_id: uuid.UUID | None = None,
    resolution_decision_id: uuid.UUID | None = None,
    fact_type: str = FactType.INFERENCE.value,
    observed_at: date | None = None,
    period_granularity: str = PeriodGranularity.UNDATED.value,
    now: datetime | None = None,
) -> CompanyRelationshipClaim:
    """Append a time-bounded relationship assertion.

    Symmetric types are stored once in canonical order, so the pair cannot be
    duplicated in the other direction.
    """
    from boro_gtm.discovery.enums import SYMMETRIC_RELATIONSHIP_TYPES

    now = now or datetime.now(UTC)
    if relationship_type in SYMMETRIC_RELATIONSHIP_TYPES and str(from_company_id) > str(
        to_company_id
    ):
        from_company_id, to_company_id = to_company_id, from_company_id

    claim = CompanyRelationshipClaim(
        from_company_id=from_company_id,
        to_company_id=to_company_id,
        relationship_type=relationship_type,
        valid_from=valid_from,
        valid_to=valid_to,
        assertion=assertion,
        supersedes_claim_id=supersedes_claim_id,
        provider_record_version_id=provider_record_version_id,
        resolution_decision_id=resolution_decision_id,
        fact_type=fact_type,
        confidence=evidence_confidence(fact_type, period_granularity, None),
        observed_at=observed_at,
        period_granularity=period_granularity,
        created_at=now,
    )
    session.add(claim)
    session.flush()
    return claim


def _normalize_address(candidate: CandidateCompany) -> str:
    parts = [candidate.address, candidate.postal_code, candidate.city, candidate.country]
    return " ".join(p.strip().casefold() for p in parts if p) or "unknown"
