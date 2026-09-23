"""M2 configuration seeding: attribute registry and fixture providers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import AttributeDefinition, DiscoveryProvider
from boro_gtm.discovery.providers.fixtures import FIXTURE_ADAPTERS
from boro_gtm.discovery.registry import ATTRIBUTE_REGISTRY, ATTRIBUTE_REGISTRY_VERSION


def seed_attribute_registry(session: Session,
                            version: str = ATTRIBUTE_REGISTRY_VERSION) -> int:
    """Persist the code-held registry, as M0 persists scoring models."""
    now = datetime.now(UTC)
    created = 0
    for spec in ATTRIBUTE_REGISTRY.values():
        exists = session.scalar(
            select(AttributeDefinition).where(
                AttributeDefinition.registry_version == version,
                AttributeDefinition.attribute_key == spec.attribute_key,
            )
        )
        if exists is not None:
            continue
        session.add(AttributeDefinition(
            registry_version=version,
            attribute_key=spec.attribute_key,
            value_kind=spec.value_kind,
            value_type=spec.value_type,
            allowed_units=list(spec.allowed_units),
            allowed_fact_types=list(spec.allowed_fact_types),
            cardinality=spec.cardinality,
            projection_strategy=spec.projection_strategy,
            conflict_strategy=spec.conflict_strategy,
            shadow_column=spec.shadow_column,
            value_schema=spec.value_schema,
            target_projection=spec.target_projection,
            created_at=now,
        ))
        created += 1
    session.flush()
    return created


def seed_fixture_providers(session: Session) -> int:
    """Register the TEST/FIXTURE adapters.

    Flagged ``is_fixture = True`` so they can never be mistaken for production
    discovery sources. No real provider integration is invented.
    """
    created = 0
    for adapter_cls in FIXTURE_ADAPTERS.values():
        caps = adapter_cls().capabilities()
        exists = session.scalar(
            select(DiscoveryProvider).where(
                DiscoveryProvider.provider_key == caps.provider_key
            )
        )
        if exists is not None:
            continue
        session.add(DiscoveryProvider(
            provider_key=caps.provider_key,
            name=caps.name,
            identity_capability=caps.identity_capability,
            key_fields=list(caps.key_fields) or None,
            key_algorithm_version=caps.key_algorithm_version,
            canonicalization_strategy=caps.canonicalization_strategy,
            canonicalization_version=caps.canonicalization_version,
            media_type=caps.media_type,
            trust_tier=caps.trust_tier,
            capabilities={"supported_filters": list(caps.supported_filters),
                          "normalizer_version": caps.normalizer_version},
            is_active=True,
            is_fixture=caps.is_fixture,
        ))
        created += 1
    session.flush()
    return created


def seed_all(session: Session) -> dict[str, int]:
    return {
        "attribute_definitions": seed_attribute_registry(session),
        "discovery_providers": seed_fixture_providers(session),
    }
