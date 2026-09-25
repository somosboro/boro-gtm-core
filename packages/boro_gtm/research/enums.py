"""Closed vocabularies for M3 Operational Research.

Every enum here is mirrored by a CHECK constraint in migration 0004. The
Python enum and the database constraint are two statements of one contract;
neither is allowed to drift, and the schema-parity check would catch it if
they did.
"""

from __future__ import annotations

from enum import StrEnum


class AttemptStatus(StrEnum):
    """One execution of a logical research question (design §4.2)."""

    PENDING = "PENDING"
    DISCOVERING = "DISCOVERING"
    FETCHING = "FETCHING"
    EXTRACTING = "EXTRACTING"
    ASSERTING = "ASSERTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


#: Terminal attempt states. A trigger rejects any transition out of these:
#: an execution that finished stays finished, and retry creates attempt n+1
#: on the same run (M3-ADR-019).
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {AttemptStatus.COMPLETED.value, AttemptStatus.PARTIAL.value,
     AttemptStatus.FAILED.value}
)

#: The only transitions the database permits.
LEGAL_ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"DISCOVERING", "PARTIAL", "FAILED"}),
    "DISCOVERING": frozenset({"FETCHING", "PARTIAL", "FAILED"}),
    "FETCHING": frozenset({"EXTRACTING", "PARTIAL", "FAILED"}),
    "EXTRACTING": frozenset({"ASSERTING", "PARTIAL", "FAILED"}),
    "ASSERTING": frozenset({"COMPLETED", "PARTIAL", "FAILED"}),
    "COMPLETED": frozenset(),
    "PARTIAL": frozenset(),
    "FAILED": frozenset(),
}


class DiscoveryMethod(StrEnum):
    SITEMAP = "SITEMAP"
    CRAWL_LINK = "CRAWL_LINK"
    SEARCH = "SEARCH"
    JOB_BOARD = "JOB_BOARD"
    REGISTRY = "REGISTRY"
    HUMAN_SEED = "HUMAN_SEED"
    API = "API"


class SourceRelation(StrEnum):
    REDIRECTS_TO = "REDIRECTS_TO"
    DECLARES_CANONICAL = "DECLARES_CANONICAL"
    LANGUAGE_VARIANT_OF = "LANGUAGE_VARIANT_OF"
    MIRROR_CANDIDATE = "MIRROR_CANDIDATE"


class EdgeOrigin(StrEnum):
    """How an edge came to be known (M3-ADR-027).

    ``MIRROR_CANDIDATE`` is an inference over *two* observations that no
    single fetch can witness, so it is always ``DERIVED`` and carries both.
    """

    FETCH_OBSERVED = "FETCH_OBSERVED"
    DERIVED = "DERIVED"
    HUMAN_ASSERTED = "HUMAN_ASSERTED"


class FetchOutcome(StrEnum):
    OK = "OK"
    NOT_MODIFIED = "NOT_MODIFIED"
    NOT_FOUND = "NOT_FOUND"
    GONE = "GONE"
    DENIED = "DENIED"
    LOGIN_WALL = "LOGIN_WALL"
    TIMEOUT = "TIMEOUT"
    ROBOTS_DENIED = "ROBOTS_DENIED"
    TOO_LARGE = "TOO_LARGE"
    MIME_MISMATCH = "MIME_MISMATCH"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"


#: Outcomes that yield bytes. Everything else must have a NULL body, except
#: NOT_MODIFIED, which references the body it *validated* (M3-ADR-037).
BODY_BEARING_OUTCOMES = frozenset({FetchOutcome.OK.value})
VALIDATING_OUTCOMES = frozenset({FetchOutcome.NOT_MODIFIED.value})


class Validator(StrEnum):
    ETAG = "ETAG"
    LAST_MODIFIED = "LAST_MODIFIED"


class RetentionState(StrEnum):
    RETAINED = "RETAINED"
    PRUNED = "PRUNED"


class CanonicalizationStrategy(StrEnum):
    """Content-type specific and versioned (design §3.3)."""

    HTML_TEXT_V1 = "HTML_TEXT_V1"
    PDF_TEXT_V1 = "PDF_TEXT_V1"
    JSON_CANONICAL_V1 = "JSON_CANONICAL_V1"
    PLAINTEXT_V1 = "PLAINTEXT_V1"


#: A strategy may only be applied to media types it actually fits.
STRATEGY_MEDIA_TYPES: dict[str, frozenset[str]] = {
    CanonicalizationStrategy.HTML_TEXT_V1.value: frozenset({"text/html"}),
    CanonicalizationStrategy.PDF_TEXT_V1.value: frozenset({"application/pdf"}),
    CanonicalizationStrategy.JSON_CANONICAL_V1.value: frozenset({"application/json"}),
    CanonicalizationStrategy.PLAINTEXT_V1.value: frozenset({"text/plain"}),
}


class DerivationStatus(StrEnum):
    OK = "OK"
    FAILED = "FAILED"


class ExtractorKind(StrEnum):
    RULE = "RULE"
    PARSER = "PARSER"
    MODEL = "MODEL"
    HUMAN = "HUMAN"


class Determinism(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    SAMPLED = "SAMPLED"


class UsageRole(StrEnum):
    CREATED = "CREATED"
    REUSED = "REUSED"


class SupportKind(StrEnum):
    DIRECT_STATEMENT = "DIRECT_STATEMENT"
    DERIVED = "DERIVED"
    CORROBORATING = "CORROBORATING"


class GapKind(StrEnum):
    NO_EVIDENCE = "NO_EVIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    CONTRADICTED = "CONTRADICTED"
    UNRESOLVABLE_SOURCE = "UNRESOLVABLE_SOURCE"
    NO_CRAWLABLE_SEED = "NO_CRAWLABLE_SEED"


class GapEventKind(StrEnum):
    RAISED = "RAISED"
    ATTEMPTED = "ATTEMPTED"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"


TERMINAL_GAP_EVENTS = frozenset({GapEventKind.RESOLVED.value, GapEventKind.ABANDONED.value})

LEGAL_GAP_TRANSITIONS: dict[str, frozenset[str]] = {
    "RAISED": frozenset({"ATTEMPTED", "RESOLVED", "ABANDONED"}),
    "ATTEMPTED": frozenset({"ATTEMPTED", "RESOLVED", "ABANDONED"}),
    "RESOLVED": frozenset(),
    "ABANDONED": frozenset(),
}


class SignalKind(StrEnum):
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    POSSIBLE_PARENT = "POSSIBLE_PARENT"
    POSSIBLE_ACQUISITION = "POSSIBLE_ACQUISITION"
    DOMAIN_MISMATCH = "DOMAIN_MISMATCH"
    NAME_MISMATCH = "NAME_MISMATCH"
    POSSIBLE_CEASED_TRADING = "POSSIBLE_CEASED_TRADING"


class SignalStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ACTIONED = "ACTIONED"
    DISMISSED = "DISMISSED"


TERMINAL_SIGNAL_STATUSES = frozenset(
    {SignalStatus.ACTIONED.value, SignalStatus.DISMISSED.value}
)

LEGAL_SIGNAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"ACKNOWLEDGED", "DISMISSED"}),
    "ACKNOWLEDGED": frozenset({"ACTIONED", "DISMISSED"}),
    "ACTIONED": frozenset(),
    "DISMISSED": frozenset(),
}


class Staleness(StrEnum):
    """Computed at read time, never stored (M3-ADR-007)."""

    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    UNKNOWN_AGE = "UNKNOWN_AGE"


class EvidenceClass(StrEnum):
    """Caps the fact type a technology claim may reach (M3-ADR-011)."""

    EXPLICIT_COMPANY_STATEMENT = "EXPLICIT_COMPANY_STATEMENT"
    CUSTOMER_PORTAL_BRANDING = "CUSTOMER_PORTAL_BRANDING"
    INTEGRATION_DOC = "INTEGRATION_DOC"
    JOB_DESCRIPTION_MENTION = "JOB_DESCRIPTION_MENTION"
    THIRD_PARTY_TECH_DATABASE = "THIRD_PARTY_TECH_DATABASE"
    SCRIPT_FINGERPRINT = "SCRIPT_FINGERPRINT"
    EMPLOYEE_PROFILE_MENTION = "EMPLOYEE_PROFILE_MENTION"


#: A script tag proves a script loaded on a web page. It does not prove the
#: field workforce uses that platform operationally.
EVIDENCE_CLASS_FACT_CEILING: dict[str, str] = {
    EvidenceClass.EXPLICIT_COMPANY_STATEMENT.value: "FACT",
    EvidenceClass.CUSTOMER_PORTAL_BRANDING.value: "FACT",
    EvidenceClass.INTEGRATION_DOC.value: "FACT",
    EvidenceClass.JOB_DESCRIPTION_MENTION.value: "PROXY",
    EvidenceClass.THIRD_PARTY_TECH_DATABASE.value: "PROXY",
    EvidenceClass.SCRIPT_FINGERPRINT.value: "HYPOTHESIS",
    EvidenceClass.EMPLOYEE_PROFILE_MENTION.value: "HYPOTHESIS",
}


class SourceClass(StrEnum):
    COMPANY_OWN_SITE = "COMPANY_OWN_SITE"
    GOVERNMENT_REGISTRY = "GOVERNMENT_REGISTRY"
    JOB_BOARD = "JOB_BOARD"
    THIRD_PARTY_DIRECTORY = "THIRD_PARTY_DIRECTORY"
    NEWS_MEDIA = "NEWS_MEDIA"
    VENDOR_SITE = "VENDOR_SITE"
    UNKNOWN = "UNKNOWN"


def _v(e: type[StrEnum]) -> tuple[str, ...]:
    return tuple(m.value for m in e)
