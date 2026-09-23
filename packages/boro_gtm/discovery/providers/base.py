"""Provider abstraction (design §4.6).

Strict boundaries, because they are what keep provider schemas out of the
canonical domain and the test suite off the network:

* ``search`` is the only method permitted to perform I/O.
* ``canonicalize``, ``derive_key`` and ``normalize`` are **pure** — no I/O, no
  database, no clock. They are therefore testable from fixtures alone.
* No adapter ever writes a canonical company table. Adapters emit raw records;
  only the resolution service writes companies.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from boro_gtm.core.errors import ValidationError
from boro_gtm.discovery.enums import (
    STRATEGY_MEDIA_TYPES,
    CanonicalizationStrategy,
    ProviderIdentityCapability,
)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider can do, and how its identity works."""

    provider_key: str
    name: str
    identity_capability: str
    canonicalization_strategy: str
    canonicalization_version: str
    media_type: str
    normalizer_version: str
    key_fields: tuple[str, ...] = ()
    key_algorithm_version: str | None = None
    trust_tier: float = 0.5
    supported_filters: tuple[str, ...] = ()
    is_fixture: bool = False

    def __post_init__(self) -> None:
        legal = STRATEGY_MEDIA_TYPES.get(self.canonicalization_strategy)
        if legal is None:
            raise ValidationError(
                f"Unknown canonicalization strategy "
                f"{self.canonicalization_strategy!r}",
                details={"known": sorted(STRATEGY_MEDIA_TYPES)},
            )
        if self.media_type not in legal:
            # A provider may not borrow the JSON canonicalizer for a payload it
            # does not fit (acceptance A14).
            raise ValidationError(
                f"Provider {self.provider_key!r} declares strategy "
                f"{self.canonicalization_strategy!r}, which is not valid for "
                f"media type {self.media_type!r}",
                details={"allowed_media_types": sorted(legal)},
            )
        if (
            self.identity_capability == ProviderIdentityCapability.DERIVED_STABLE_KEY.value
            and not (self.key_fields and self.key_algorithm_version)
        ):
            raise ValidationError(
                f"Provider {self.provider_key!r} declares DERIVED_STABLE_KEY "
                "but does not declare key_fields and key_algorithm_version",
                details={"provider_key": self.provider_key},
            )


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One result as the provider returned it, before any interpretation."""

    body: bytes
    content_type: str
    #: The provider's own identifier, when it issues one.
    native_external_id: str | None = None
    source_url: str | None = None


@dataclass(slots=True)
class CandidateCompany:
    """The normalized shape a provider record maps onto.

    Deliberately small: it carries only what resolution and claim-writing need.
    Provider-specific fields never reach this structure.
    """

    legal_name: str | None = None
    trading_names: list[str] = field(default_factory=list)
    registrable_domain: str | None = None
    other_domains: list[str] = field(default_factory=list)
    country: str | None = None
    city: str | None = None
    postal_code: str | None = None
    address: str | None = None
    phone: str | None = None
    employee_count_min: int | None = None
    employee_count_max: int | None = None
    founded_year: int | None = None
    legal_form: str | None = None
    registry_ids: dict[str, str] = field(default_factory=dict)
    vertical_hints: list[str] = field(default_factory=list)
    raw_extras: dict[str, Any] = field(default_factory=dict)


def canonical_json_v1(payload: Any) -> bytes:
    """``JSON_CANONICAL_V1`` — the algorithm M0 uses for snapshot identity.

    Sorted keys, tight separators, no ASCII escaping, no NaN. Reusing it means
    a provider reformatting its JSON is a no-op here for the same reason a
    reindented source file is a no-op import in M0 (ADR-017).
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def canonical_csv_row_v1(payload: Any) -> bytes:
    """``CSV_ROW_V1`` — deterministic canonicalization for delimited rows.

    Column order is normalized by sorting on header name, values are stripped
    and empty strings become null, so reordered or re-spaced exports converge.
    Marked test-only in :class:`CanonicalizationStrategy` until a real CSV
    provider validates it against live data.
    """
    if not isinstance(payload, dict):
        raise ValidationError("CSV_ROW_V1 expects a mapping of column -> value")
    normalized = {
        str(k).strip().lower(): (None if str(v).strip() == "" else str(v).strip())
        for k, v in payload.items()
    }
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


CANONICALIZERS = {
    CanonicalizationStrategy.JSON_CANONICAL_V1.value: canonical_json_v1,
    CanonicalizationStrategy.CSV_ROW_V1.value: canonical_csv_row_v1,
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProviderAdapter(ABC):
    """The contract every discovery provider implements."""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Static declaration. No I/O."""

    @abstractmethod
    def search(self, query: dict[str, Any]) -> Iterator[RawRecord]:
        """The only method permitted to touch the network."""

    @abstractmethod
    def parse(self, raw: RawRecord) -> dict[str, Any]:
        """Bytes to a structured payload. Pure."""

    @abstractmethod
    def normalize(self, parsed: dict[str, Any]) -> CandidateCompany:
        """Provider schema to the canonical candidate shape. Pure."""

    def canonicalize(self, parsed: dict[str, Any]) -> bytes:
        """Canonical byte form used for semantic identity. Pure.

        Dispatches on the declared strategy; an adapter overrides this only if
        it needs a strategy not in :data:`CANONICALIZERS`.
        """
        caps = self.capabilities()
        return CANONICALIZERS[caps.canonicalization_strategy](parsed)

    def derive_key(self, candidate: CandidateCompany) -> str | None:
        """Derive a stable key from declared fields. Pure.

        Returns ``None`` when the declared fields are not all present, which
        the ingestion service treats as "no stable identity for this record"
        rather than inventing a partial key.
        """
        caps = self.capabilities()
        if caps.identity_capability != ProviderIdentityCapability.DERIVED_STABLE_KEY.value:
            return None
        parts: list[str] = []
        for field_name in caps.key_fields:
            value = getattr(candidate, field_name, None)
            if value is None or str(value).strip() == "":
                return None
            parts.append(str(value).strip().casefold())
        material = "|".join([caps.key_algorithm_version or "", *parts])
        return sha256_hex(material.encode("utf-8"))
