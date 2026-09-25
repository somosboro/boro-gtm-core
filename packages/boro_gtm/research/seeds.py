"""Seed the M3 operational attribute registry into ``attribute_definitions``.

The registry is held in code as configuration and persisted at seed time,
exactly as M0 persists its scoring models and M2 its attribute registry. Every
claim stamps the registry version it was written against, so a contract change
is a dated event rather than a silent reinterpretation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import AttributeDefinition
from boro_gtm.research.registry import (
    OWNER_MILESTONE,
    RESEARCH_ATTRIBUTES,
    RESEARCH_REGISTRY_VERSION,
)

#: M3's registry rides on the table M2 already defines, so its vocabularies
#: are M2's. These maps are the translation, stated once.
_VALUE_KIND = {"SCALAR": "SCALAR", "RANGE": "RANGE", "SET": "SET"}
_VALUE_TYPE = {
    "BOOLEAN": "BOOLEAN", "NUMERIC": "NUMERIC", "TEXT": "TEXT",
    "ENUM": "TEXT", "REFERENCE": "REFERENCE", "JSON": "STRUCTURED",
}


def _projection_strategy(attribute) -> str:
    """A quantity projects an envelope, a set unions, a scalar takes the best.

    The envelope matters: sources disagree about headcount by construction, and
    a scalar winner would hide the disagreement (M3-ADR-012).
    """
    if attribute.cardinality == "MANY":
        return "UNION"
    if attribute.value_kind == "RANGE":
        return "ENVELOPE"
    return "HIGHEST_PRECEDENCE"


def seed_research_registry(session: Session) -> int:
    """Insert-or-ignore every M3 attribute. Idempotent by natural key."""
    now = datetime.now(UTC)
    rows = []
    for attribute in RESEARCH_ATTRIBUTES:
        rows.append(
            {
                "attribute_key": attribute.key,
                "registry_version": RESEARCH_REGISTRY_VERSION,
                "owner_milestone": OWNER_MILESTONE,
                "value_kind": _VALUE_KIND[attribute.value_kind],
                "value_type": _VALUE_TYPE[attribute.value_type],
                "cardinality": attribute.cardinality,
                "projection_strategy": _projection_strategy(attribute),
                "conflict_strategy": (
                    "ENVELOPE" if attribute.value_kind == "RANGE" else "PRECEDENCE"
                ),
                "allowed_fact_types": list(attribute.allowed_fact_types),
                "allowed_units": list(attribute.allowed_units) or None,
                "shadow_column": None,
                "target_projection": "operational_research_profiles",
                "value_schema": {
                    "group": attribute.group,
                    "temporal": attribute.temporal,
                    "staleness_days": attribute.staleness_days,
                    "required": attribute.required,
                    "enum_values": list(attribute.enum_values) or None,
                    "evidence_class_capped": attribute.evidence_class_capped,
                },
                "created_at": now,
            }
        )
    session.execute(
        pg_insert(AttributeDefinition)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["attribute_key", "registry_version"])
    )
    session.flush()
    return len(rows)


def seed_all(session: Session) -> dict[str, int]:
    return {"research_attribute_definitions": seed_research_registry(session)}
