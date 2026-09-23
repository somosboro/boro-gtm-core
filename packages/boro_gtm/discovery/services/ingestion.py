"""Ingestion: raw bytes → body → version → normalization (design §4, §7).

The cardinality rules this implements, all enforced by database constraints
rather than by convention:

* identical bytes                → one body, a sighting recorded
* different bytes, same semantics → new body, **same** semantic version
* semantic change                 → new semantic version
* different canonicalization      → new version even if the hash is equal
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import (
    DiscoveryProvider,
    DiscoveryQuery,
    ProviderEntity,
    ProviderRecordBody,
    ProviderRecordNormalization,
    ProviderRecordSighting,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import ExternalIdKind, ProviderIdentityCapability
from boro_gtm.discovery.providers.base import (
    CandidateCompany,
    ProviderAdapter,
    RawRecord,
    sha256_hex,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestResult:
    """What one raw record did to the store."""

    provider_entity_id: object
    version_id: object
    version_created: bool
    body_created: bool
    sighting_created: bool
    candidate: CandidateCompany
    external_id_kind: str
    #: Set when the adapter could not interpret the payload.
    normalization_error: str | None = None


def ingest_record(
    session: Session,
    adapter: ProviderAdapter,
    provider: DiscoveryProvider,
    raw: RawRecord,
    query: DiscoveryQuery | None = None,
    now: datetime | None = None,
) -> IngestResult:
    """Persist one raw record as append-only evidence.

    Pure adapter steps (parse, normalize, canonicalize, derive_key) run first;
    only then does anything touch the database.
    """
    now = now or datetime.now(UTC)
    caps = adapter.capabilities()

    # --- pure stage -----------------------------------------------------
    parsed = adapter.parse(raw)
    try:
        candidate = adapter.normalize(parsed)
        normalization_error: str | None = None
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        # Interpretation failing does not make the bytes less real. The record
        # is stored, the failure is stored with it, and a later normalizer
        # version can re-derive it (design §4.5).
        candidate = CandidateCompany()
        normalization_error = str(exc)[:2000]
        logger.warning(
            "Normalization failed; raw evidence retained",
            extra={"provider_key": caps.provider_key},
        )
    canonical_bytes = adapter.canonicalize(parsed)
    canonical_hash = sha256_hex(canonical_bytes)
    body_hash = sha256_hex(raw.body)

    external_id, kind = _resolve_external_identity(adapter, caps, raw, candidate,
                                                   canonical_hash)

    # --- provider entity ------------------------------------------------
    entity, _ = _upsert_entity(session, provider, external_id, kind,
                               caps.key_algorithm_version, now)

    # --- semantic version ------------------------------------------------
    # Identity includes the canonicalization contract: a hash is only
    # meaningful relative to the algorithm that produced it (ADR M2-028).
    version, version_created = _upsert_version(
        session, entity, canonical_hash, caps, parsed, query, now
    )

    # --- byte body -------------------------------------------------------
    body_created = _upsert_body(session, version, raw, body_hash, query, now)

    # --- sighting --------------------------------------------------------
    sighting_created = False
    if query is not None:
        sighting_created = _record_sighting(session, version, query, now)

    # --- normalization ---------------------------------------------------
    _upsert_normalization(session, version, caps.normalizer_version, candidate,
                          now, error=normalization_error)

    return IngestResult(
        provider_entity_id=entity.id,
        version_id=version.id,
        version_created=version_created,
        body_created=body_created,
        sighting_created=sighting_created,
        candidate=candidate,
        external_id_kind=kind,
        normalization_error=normalization_error,
    )


def _resolve_external_identity(adapter, caps, raw, candidate, canonical_hash
                               ) -> tuple[str, str]:
    """Decide the provider-side key, honestly labelled (design §4.2)."""
    capability = caps.identity_capability

    if capability == ProviderIdentityCapability.NATIVE_EXTERNAL_ID.value:
        if not raw.native_external_id:
            raise ValueError(
                f"Provider {caps.provider_key!r} declares NATIVE_EXTERNAL_ID "
                "but returned a record without one"
            )
        return raw.native_external_id, ExternalIdKind.NATIVE.value

    if capability == ProviderIdentityCapability.DERIVED_STABLE_KEY.value:
        derived = adapter.derive_key(candidate)
        if derived is None:
            # The declared key fields are not all present. Fall back to content
            # identity rather than inventing a partial key.
            return canonical_hash, ExternalIdKind.CONTENT.value
        return derived, ExternalIdKind.DERIVED.value

    # CONTENT_ONLY: no longitudinal provider identity is claimed.
    return canonical_hash, ExternalIdKind.CONTENT.value


def _upsert_entity(session, provider, external_id, kind, key_algorithm_version, now):
    stmt = (
        pg_insert(ProviderEntity)
        .values(
            provider_id=provider.id,
            provider_external_id=external_id,
            external_id_kind=kind,
            key_algorithm_version=(
                key_algorithm_version if kind == ExternalIdKind.DERIVED.value else None
            ),
            first_seen_at=now,
        )
        .on_conflict_do_nothing(index_elements=["provider_id", "provider_external_id"])
        .returning(ProviderEntity.id)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        return session.get(ProviderEntity, inserted), True
    entity = session.scalar(
        select(ProviderEntity).where(
            ProviderEntity.provider_id == provider.id,
            ProviderEntity.provider_external_id == external_id,
        )
    )
    return entity, False


def _upsert_version(session, entity, canonical_hash, caps, parsed, query, now):
    stmt = (
        pg_insert(ProviderRecordVersion)
        .values(
            provider_entity_id=entity.id,
            canonical_payload_hash=canonical_hash,
            canonicalization_strategy=caps.canonicalization_strategy,
            canonicalization_version=caps.canonicalization_version,
            parsed_payload=parsed,
            retrieved_at=now,
            discovery_query_id=query.id if query else None,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "provider_entity_id",
                "canonicalization_strategy",
                "canonicalization_version",
                "canonical_payload_hash",
            ]
        )
        .returning(ProviderRecordVersion.id)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        return session.get(ProviderRecordVersion, inserted), True
    existing = session.scalar(
        select(ProviderRecordVersion).where(
            ProviderRecordVersion.provider_entity_id == entity.id,
            ProviderRecordVersion.canonicalization_strategy == caps.canonicalization_strategy,
            ProviderRecordVersion.canonicalization_version == caps.canonicalization_version,
            ProviderRecordVersion.canonical_payload_hash == canonical_hash,
        )
    )
    return existing, False


def _upsert_body(session, version, raw, body_hash, query, now) -> bool:
    """One version may hold many byte-different bodies (ADR M2-026)."""
    stmt = (
        pg_insert(ProviderRecordBody)
        .values(
            provider_record_version_id=version.id,
            raw_body=raw.body,
            raw_body_sha256=body_hash,
            content_type=raw.content_type,
            retrieved_at=now,
            discovery_query_id=query.id if query else None,
        )
        .on_conflict_do_nothing(
            index_elements=["provider_record_version_id", "raw_body_sha256"]
        )
        .returning(ProviderRecordBody.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def _record_sighting(session, version, query, now) -> bool:
    stmt = (
        pg_insert(ProviderRecordSighting)
        .values(
            provider_record_version_id=version.id,
            discovery_query_id=query.id,
            seen_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["provider_record_version_id", "discovery_query_id"]
        )
        .returning(ProviderRecordSighting.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def _upsert_normalization(session, version, normalizer_version, candidate, now,
                          error: str | None = None) -> None:
    payload: dict | None = {
        "legal_name": candidate.legal_name,
        "trading_names": candidate.trading_names,
        "registrable_domain": candidate.registrable_domain,
        "other_domains": candidate.other_domains,
        "country": candidate.country,
        "city": candidate.city,
        "postal_code": candidate.postal_code,
        "address": candidate.address,
        "phone": candidate.phone,
        "employee_count_min": candidate.employee_count_min,
        "employee_count_max": candidate.employee_count_max,
        "founded_year": candidate.founded_year,
        "legal_form": candidate.legal_form,
        "registry_ids": candidate.registry_ids,
        "vertical_hints": candidate.vertical_hints,
    }
    if error is not None:
        payload = None
    stmt = (
        pg_insert(ProviderRecordNormalization)
        .values(
            provider_record_version_id=version.id,
            normalizer_version=normalizer_version,
            normalized_payload=payload,
            error=error,
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["provider_record_version_id", "normalizer_version"]
        )
    )
    session.execute(stmt)
