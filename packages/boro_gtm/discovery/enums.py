"""M2 closed vocabularies.

Mirrored by database CHECK constraints, exactly as M0/M1 vocabularies are
(see :data:`M2_CLOSED_VOCABULARIES` and migration ``0003``).
"""

from __future__ import annotations

from enum import StrEnum


class ProviderIdentityCapability(StrEnum):
    """How durable a provider's own record identity is (design §4.2)."""

    #: The provider issues a durable id it promises to keep stable.
    NATIVE_EXTERNAL_ID = "NATIVE_EXTERNAL_ID"
    #: No provider id; the adapter derives one from declared normalized fields.
    DERIVED_STABLE_KEY = "DERIVED_STABLE_KEY"
    #: No stable object identity is possible; keyed by content hash.
    CONTENT_ONLY = "CONTENT_ONLY"


class ExternalIdKind(StrEnum):
    """How a provider entity's key was obtained. A derived key is never
    presented as provider-issued."""

    NATIVE = "NATIVE"
    DERIVED = "DERIVED"
    CONTENT = "CONTENT"


class CanonicalizationStrategy(StrEnum):
    """Named canonicalization algorithms (design §4.4).

    ``JSON_CANONICAL_V1`` is M0's snapshot algorithm. Non-JSON strategies must
    be declared explicitly per provider; none is invented here.
    """

    JSON_CANONICAL_V1 = "JSON_CANONICAL_V1"
    #: Deterministic row canonicalization for delimited text. Test use only
    #: until a real CSV provider validates it.
    CSV_ROW_V1 = "CSV_ROW_V1"


#: Which media types each strategy is legal for. A provider may not borrow the
#: JSON canonicalizer for a payload it does not fit (acceptance A14).
STRATEGY_MEDIA_TYPES: dict[str, frozenset[str]] = {
    CanonicalizationStrategy.JSON_CANONICAL_V1.value: frozenset(
        {"application/json", "application/x-ndjson"}
    ),
    CanonicalizationStrategy.CSV_ROW_V1.value: frozenset({"text/csv"}),
}


class DiscoveryRunStatus(StrEnum):
    """Run lifecycle (design §7)."""

    PENDING = "PENDING"
    FETCHING = "FETCHING"
    #: Provider failed mid-run; some raw versions were persisted.
    PARTIAL_FETCH = "PARTIAL_FETCH"
    FETCHED = "FETCHED"
    NORMALIZING = "NORMALIZING"
    RESOLVING = "RESOLVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: Statuses a run passes through after a completed fetch. Informational only:
#: the gate on canonical writes is ``discovery_runs.fetch_completed_at``, since
#: these statuses are overwritten as the run proceeds.
POST_FETCH_STATUSES = frozenset(
    {DiscoveryRunStatus.FETCHED.value, DiscoveryRunStatus.NORMALIZING.value,
     DiscoveryRunStatus.RESOLVING.value}
)


class ResolutionDecision(StrEnum):
    MATCHED = "MATCHED"
    CREATED_NEW = "CREATED_NEW"
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"
    MERGED = "MERGED"
    SPLIT = "SPLIT"


class ResolutionMethod(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    CANDIDATE_AUTO = "CANDIDATE_AUTO"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class CompanyLifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    MERGED = "MERGED"
    DISSOLVED = "DISSOLVED"


class ValueKind(StrEnum):
    SCALAR = "SCALAR"
    RANGE = "RANGE"
    SET = "SET"


class ValueType(StrEnum):
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    NUMERIC = "NUMERIC"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"
    REFERENCE = "REFERENCE"
    DOMAIN = "DOMAIN"
    STRUCTURED = "STRUCTURED"


class Cardinality(StrEnum):
    ONE = "ONE"
    MANY = "MANY"


class ProjectionStrategy(StrEnum):
    HIGHEST_PRECEDENCE = "HIGHEST_PRECEDENCE"
    ENVELOPE = "ENVELOPE"
    UNION = "UNION"
    LATEST = "LATEST"


class ConflictStrategy(StrEnum):
    PRECEDENCE = "PRECEDENCE"
    ENVELOPE = "ENVELOPE"
    FLAG_AMBIGUOUS = "FLAG_AMBIGUOUS"


class ClaimAssertion(StrEnum):
    ASSERTED = "ASSERTED"
    RETRACTED = "RETRACTED"


class NameType(StrEnum):
    LEGAL = "LEGAL"
    TRADING = "TRADING"
    FORMER = "FORMER"
    LOCALIZED = "LOCALIZED"
    PROVIDER_DISPLAY = "PROVIDER_DISPLAY"


class DomainRole(StrEnum):
    IDENTITY = "IDENTITY"
    ALTERNATE = "ALTERNATE"
    REDIRECT = "REDIRECT"
    COUNTRY_TLD = "COUNTRY_TLD"
    DEFUNCT = "DEFUNCT"
    GROUP = "GROUP"


class LocationType(StrEnum):
    """Physical or registered places only. ``SERVICE_AREA`` is deliberately
    absent: a service area is not a place (ADR M2-015)."""

    HEADQUARTERS = "HEADQUARTERS"
    BRANCH = "BRANCH"
    DEPOT = "DEPOT"
    REGISTERED_OFFICE = "REGISTERED_OFFICE"


class PresenceType(StrEnum):
    HEADQUARTERED = "HEADQUARTERED"
    BRANCH = "BRANCH"
    OPERATES = "OPERATES"
    SERVES_REMOTELY = "SERVES_REMOTELY"


class RelationshipType(StrEnum):
    """Canonical directions only. Inverses are derived by view (ADR M2-021)."""

    SUBSIDIARY_OF = "SUBSIDIARY_OF"
    FRANCHISE_OF = "FRANCHISE_OF"
    ACQUIRED_BY = "ACQUIRED_BY"
    #: Symmetric: stored once with from_company_id < to_company_id.
    SISTER_OF = "SISTER_OF"


SYMMETRIC_RELATIONSHIP_TYPES = frozenset({RelationshipType.SISTER_OF.value})


class ClassificationMethod(StrEnum):
    PROVIDER_TAXONOMY = "PROVIDER_TAXONOMY"
    NAICS_MAPPING = "NAICS_MAPPING"
    KEYWORD_INFERENCE = "KEYWORD_INFERENCE"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


def _v(e: type[StrEnum]) -> tuple[str, ...]:
    return tuple(m.value for m in e)


#: Closed vocabularies mirrored by CHECK constraints in migration 0003.
M2_CLOSED_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "discovery_providers.identity_capability": _v(ProviderIdentityCapability),
    "discovery_providers.canonicalization_strategy": _v(CanonicalizationStrategy),
    "discovery_runs.status": _v(DiscoveryRunStatus),
    "provider_entities.external_id_kind": _v(ExternalIdKind),
    "provider_record_versions.canonicalization_strategy": _v(CanonicalizationStrategy),
    "entity_resolution_decisions.decision": _v(ResolutionDecision),
    "entity_resolution_decisions.method": _v(ResolutionMethod),
    "companies.lifecycle_status": _v(CompanyLifecycle),
    "attribute_definitions.value_kind": _v(ValueKind),
    "attribute_definitions.value_type": _v(ValueType),
    "attribute_definitions.cardinality": _v(Cardinality),
    "attribute_definitions.projection_strategy": _v(ProjectionStrategy),
    "attribute_definitions.conflict_strategy": _v(ConflictStrategy),
    "company_relationship_claims.relationship_type": _v(RelationshipType),
    "company_relationship_claims.assertion": _v(ClaimAssertion),
    "company_names.name_type": _v(NameType),
    "company_domains.domain_role": _v(DomainRole),
    "company_locations.location_type": _v(LocationType),
    "company_market_presences.presence_type": _v(PresenceType),
    "company_relationships.relationship_type": _v(RelationshipType),
    "company_verticals.classification_method": _v(ClassificationMethod),
    "discovery_jobs.status": _v(JobStatus),
}
