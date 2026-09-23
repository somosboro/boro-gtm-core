"""The versioned attribute registry (design §5.2).

A free-text ``attribute_key`` with no contract is an untyped EAV store. This
module supplies the contract: what an attribute means, what shape its value
takes, which typed shadow is authoritative, and how conflicts project.

Held in code as configuration and persisted at seed time, exactly as M0
persists ``scoring_models``. Every claim stamps the registry version it was
written against, so a contract change is a dated event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from boro_gtm.core.enums import FactType
from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.enums import (
    Cardinality,
    ConflictStrategy,
    ProjectionStrategy,
    ValueKind,
    ValueType,
)

ATTRIBUTE_REGISTRY_VERSION = "1.0"
IDENTITY_POLICY_VERSION = "1.0"

_ALL_FACT_TYPES = tuple(f.value for f in FactType)


class AttributeNotInRegistryError(GtmError):
    """Raised when a claim references an attribute the registry does not define."""

    code = "ATTRIBUTE_NOT_IN_REGISTRY"
    http_status = 422


@dataclass(frozen=True, slots=True)
class AttributeDefinitionSpec:
    """The contract for one attribute."""

    attribute_key: str
    value_kind: str
    value_type: str
    cardinality: str
    projection_strategy: str
    conflict_strategy: str
    allowed_fact_types: tuple[str, ...] = _ALL_FACT_TYPES
    allowed_units: tuple[str, ...] = ()
    shadow_column: str | None = None
    target_projection: str | None = None
    value_schema: dict[str, Any] | None = None
    #: Temporal attributes carry valid_from/valid_to/assertion in value_jsonb.
    temporal: bool = False

    def extract_shadow(self, value_jsonb: dict[str, Any] | None) -> dict[str, Any]:
        """Derive the typed shadow columns from ``value_jsonb``.

        This is the single extractor; writers must not populate shadows by
        hand, so ``value_jsonb`` and its shadow cannot disagree.
        """
        out: dict[str, Any] = {
            "value_numeric": None, "value_text": None, "value_ref_id": None
        }
        if value_jsonb is None or self.shadow_column is None:
            return out
        raw = value_jsonb.get("value")
        if raw is None and self.value_kind == ValueKind.RANGE.value:
            # A range shadows its minimum, so range queries stay indexable.
            raw = value_jsonb.get("min")
        if raw is None and self.value_type == ValueType.REFERENCE.value:
            raw = value_jsonb.get("market_id") or value_jsonb.get("vertical_id")
        if raw is None:
            return out
        if self.shadow_column == "value_numeric":
            out["value_numeric"] = float(raw)
        elif self.shadow_column == "value_text":
            out["value_text"] = str(raw)
        elif self.shadow_column == "value_ref_id":
            out["value_ref_id"] = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        return out

    def validate(self, value_jsonb: dict[str, Any] | None, unit: str | None,
                 fact_type: str | None) -> None:
        """Reject a claim that does not satisfy this contract."""
        if fact_type is not None and fact_type not in self.allowed_fact_types:
            raise AttributeNotInRegistryError(
                f"fact_type {fact_type!r} is not allowed for {self.attribute_key!r}",
                details={"allowed": list(self.allowed_fact_types)},
            )
        if unit is not None and unit not in self.allowed_units:
            raise AttributeNotInRegistryError(
                f"unit {unit!r} is not allowed for {self.attribute_key!r}",
                details={"allowed": list(self.allowed_units)},
            )
        if value_jsonb is None:
            return
        required = _REQUIRED_KEYS.get(self.value_kind, ())
        missing = [k for k in required if k not in value_jsonb]
        if missing:
            raise AttributeNotInRegistryError(
                f"{self.attribute_key!r} requires {missing} in value_jsonb",
                details={"value_kind": self.value_kind},
            )
        if self.value_kind == ValueKind.RANGE.value:
            lo, hi = value_jsonb.get("min"), value_jsonb.get("max")
            if lo is not None and hi is not None and float(lo) > float(hi):
                raise AttributeNotInRegistryError(
                    f"{self.attribute_key!r}: min exceeds max",
                    details={"min": lo, "max": hi},
                )
        if self.temporal:
            _validate_interval(self.attribute_key, value_jsonb)


_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    ValueKind.SCALAR.value: ("value",),
    ValueKind.RANGE.value: (),          # at least one bound; checked below
    ValueKind.SET.value: (),
}


def _validate_interval(key: str, value: dict[str, Any]) -> None:
    lo, hi = value.get("valid_from"), value.get("valid_to")
    if lo and hi and _as_date(lo) >= _as_date(hi):
        raise AttributeNotInRegistryError(
            f"{key!r}: valid_from must precede valid_to",
            details={"valid_from": lo, "valid_to": hi},
        )
    assertion = value.get("assertion", "ASSERTED")
    if assertion not in ("ASSERTED", "RETRACTED"):
        raise AttributeNotInRegistryError(
            f"{key!r}: assertion must be ASSERTED or RETRACTED",
            details={"assertion": assertion},
        )


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


#: The seed registry. Only attributes the accepted design defines — nothing is
#: invented here (design §5.2).
ATTRIBUTE_REGISTRY: dict[str, AttributeDefinitionSpec] = {
    spec.attribute_key: spec
    for spec in (
        AttributeDefinitionSpec(
            "employee_count", ValueKind.RANGE.value, ValueType.INTEGER.value,
            Cardinality.ONE.value, ProjectionStrategy.ENVELOPE.value,
            ConflictStrategy.ENVELOPE.value,
            allowed_units=("PEOPLE",), shadow_column="value_numeric",
            target_projection="company_profiles",
        ),
        AttributeDefinitionSpec(
            "revenue_band", ValueKind.RANGE.value, ValueType.NUMERIC.value,
            Cardinality.ONE.value, ProjectionStrategy.ENVELOPE.value,
            ConflictStrategy.ENVELOPE.value,
            allowed_units=("USD", "EUR"), shadow_column="value_numeric",
        ),
        AttributeDefinitionSpec(
            "founded_year", ValueKind.SCALAR.value, ValueType.INTEGER.value,
            Cardinality.ONE.value, ProjectionStrategy.HIGHEST_PRECEDENCE.value,
            ConflictStrategy.PRECEDENCE.value,
            shadow_column="value_numeric", target_projection="company_profiles",
        ),
        AttributeDefinitionSpec(
            "legal_name", ValueKind.SCALAR.value, ValueType.TEXT.value,
            Cardinality.ONE.value, ProjectionStrategy.HIGHEST_PRECEDENCE.value,
            ConflictStrategy.PRECEDENCE.value,
            shadow_column="value_text", target_projection="company_names",
        ),
        AttributeDefinitionSpec(
            "trading_name", ValueKind.SET.value, ValueType.TEXT.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.PRECEDENCE.value,
            shadow_column="value_text", target_projection="company_names",
        ),
        AttributeDefinitionSpec(
            "legal_form", ValueKind.SCALAR.value, ValueType.TEXT.value,
            Cardinality.ONE.value, ProjectionStrategy.HIGHEST_PRECEDENCE.value,
            ConflictStrategy.PRECEDENCE.value,
            shadow_column="value_text", target_projection="company_profiles",
        ),
        AttributeDefinitionSpec(
            "legal_entity", ValueKind.SET.value, ValueType.STRUCTURED.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.FLAG_AMBIGUOUS.value,
        ),
        AttributeDefinitionSpec(
            "domain", ValueKind.SET.value, ValueType.DOMAIN.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.PRECEDENCE.value,
            shadow_column="value_text", target_projection="company_domains",
        ),
        AttributeDefinitionSpec(
            "vertical", ValueKind.SET.value, ValueType.REFERENCE.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.PRECEDENCE.value,
            # A classification is an interpretation of a company, never an
            # observation of one: a directory's category is a PROXY for the
            # vertical, and no provider taxonomy makes it a FACT.
            allowed_fact_types=(FactType.PROXY.value, FactType.INFERENCE.value,
                                FactType.HYPOTHESIS.value),
            shadow_column="value_ref_id", target_projection="company_verticals",
        ),
        AttributeDefinitionSpec(
            "market_presence", ValueKind.SET.value, ValueType.REFERENCE.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.PRECEDENCE.value,
            shadow_column="value_ref_id",
            target_projection="company_market_presences", temporal=True,
        ),
        AttributeDefinitionSpec(
            "location", ValueKind.SET.value, ValueType.STRUCTURED.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.PRECEDENCE.value,
            target_projection="company_locations",
        ),
        AttributeDefinitionSpec(
            "relationship", ValueKind.SET.value, ValueType.STRUCTURED.value,
            Cardinality.MANY.value, ProjectionStrategy.UNION.value,
            ConflictStrategy.PRECEDENCE.value,
            target_projection="company_relationships", temporal=True,
        ),
    )
}


def get_definition(attribute_key: str,
                   registry_version: str = ATTRIBUTE_REGISTRY_VERSION
                   ) -> AttributeDefinitionSpec:
    """Look up an attribute, or refuse the claim."""
    if registry_version != ATTRIBUTE_REGISTRY_VERSION:
        raise AttributeNotInRegistryError(
            f"Unknown attribute registry version {registry_version!r}",
            details={"known": [ATTRIBUTE_REGISTRY_VERSION]},
        )
    try:
        return ATTRIBUTE_REGISTRY[attribute_key]
    except KeyError as exc:
        raise AttributeNotInRegistryError(
            f"Attribute {attribute_key!r} is not defined in registry "
            f"version {registry_version}",
            details={"attribute_key": attribute_key,
                     "registry_version": registry_version},
        ) from exc
