"""The M3 operational attribute registry, version ``M3-1.0``.

Twenty-four attributes, not hundreds. Each is here because a concrete research
question for the target profile needs it *and* because it can be evidenced
rather than guessed.

The registry is where several of M3's load-bearing rules are enforced, so that
they hold by construction rather than by the care of whoever writes a claim:

* an operating-model attribute may never be ``FACT`` — a company does not
  state "our dispatch is decentralised"; we infer it;
* a technology attribute's fact type is capped by how it was detected, so a
  script tag cannot assert an operational platform;
* a quantity is a RANGE, because sources disagree by construction and a scalar
  would force a false winner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boro_gtm.core.enums import FactType
from boro_gtm.discovery.registry import AttributeNotInRegistryError
from boro_gtm.research.enums import EVIDENCE_CLASS_FACT_CEILING

RESEARCH_REGISTRY_VERSION = "M3-1.0"
RESEARCH_POLICY_VERSION = "1.0"
OWNER_MILESTONE = "M3"

#: Fact types ordered weakest to strongest; used for ceilings and precedence.
FACT_TYPE_ORDER = ("HYPOTHESIS", "INFERENCE", "PROXY", "ESTIMATE", "FACT")

_ALL = tuple(f.value for f in FactType)
_NO_FACT = ("PROXY", "INFERENCE", "HYPOTHESIS")


@dataclass(frozen=True, slots=True)
class ResearchAttribute:
    """The contract for one operational attribute."""

    key: str
    group: str
    value_kind: str          # SCALAR | RANGE | SET
    value_type: str          # BOOLEAN | NUMERIC | TEXT | ENUM | REFERENCE | JSON
    cardinality: str         # ONE | MANY
    allowed_fact_types: tuple[str, ...]
    temporal: str            # POINT_IN_TIME | DURABLE | INTERVAL
    staleness_days: int | None     # None = never goes stale
    required: bool = True
    allowed_units: tuple[str, ...] = ()
    enum_values: tuple[str, ...] = ()
    #: Technology attributes carry an evidence class inside the value, and the
    #: class caps the fact type (M3-ADR-011).
    evidence_class_capped: bool = False
    applies_to_verticals: tuple[str, ...] = ()   # empty = all

    def validate(self, value: dict[str, Any] | None, unit: str | None,
                 fact_type: str | None) -> None:
        """Reject a claim that does not satisfy this contract."""
        if fact_type is not None and fact_type not in self.allowed_fact_types:
            raise AttributeNotInRegistryError(
                f"fact_type {fact_type!r} is not allowed for {self.key!r}",
                details={"attribute": self.key,
                         "allowed": list(self.allowed_fact_types)},
            )
        if unit is not None and unit not in self.allowed_units:
            raise AttributeNotInRegistryError(
                f"unit {unit!r} is not allowed for {self.key!r}",
                details={"attribute": self.key, "allowed": list(self.allowed_units)},
            )
        if value is None:
            return
        if self.value_kind == "RANGE":
            lo, hi = value.get("min"), value.get("max")
            if lo is not None and hi is not None and float(lo) > float(hi):
                raise AttributeNotInRegistryError(
                    f"{self.key!r}: min exceeds max",
                    details={"min": lo, "max": hi},
                )
        if self.enum_values:
            candidate = value.get("value")
            if candidate is not None and candidate not in self.enum_values:
                raise AttributeNotInRegistryError(
                    f"{candidate!r} is not a valid {self.key!r} value",
                    details={"valid": list(self.enum_values)},
                )
        if self.evidence_class_capped:
            self._check_evidence_ceiling(value, fact_type)

    def _check_evidence_ceiling(self, value: dict[str, Any], fact_type: str | None) -> None:
        evidence_class = value.get("evidence_class")
        if evidence_class is None:
            raise AttributeNotInRegistryError(
                f"{self.key!r} requires an evidence_class in its value",
                details={"valid": sorted(EVIDENCE_CLASS_FACT_CEILING)},
            )
        ceiling = EVIDENCE_CLASS_FACT_CEILING.get(evidence_class)
        if ceiling is None:
            raise AttributeNotInRegistryError(
                f"unknown evidence_class {evidence_class!r}",
                details={"valid": sorted(EVIDENCE_CLASS_FACT_CEILING)},
            )
        if fact_type and FACT_TYPE_ORDER.index(fact_type) > FACT_TYPE_ORDER.index(ceiling):
            # A script tag proves a script loaded on a web page. It does not
            # prove the field workforce uses that platform operationally.
            raise AttributeNotInRegistryError(
                f"evidence_class {evidence_class!r} caps {self.key!r} at "
                f"{ceiling}, not {fact_type}",
                details={"evidence_class": evidence_class, "ceiling": ceiling},
            )


def _tech(key: str) -> ResearchAttribute:
    return ResearchAttribute(
        key=key, group="OPERATIONAL_SYSTEMS", value_kind="SET", value_type="JSON",
        cardinality="MANY", allowed_fact_types=_ALL, temporal="INTERVAL",
        staleness_days=365, evidence_class_capped=True,
    )


def _operating(key: str, values: tuple[str, ...], many: bool = False) -> ResearchAttribute:
    return ResearchAttribute(
        key=key, group="OPERATING_MODEL", value_kind="SET" if many else "SCALAR",
        value_type="ENUM", cardinality="MANY" if many else "ONE",
        # Never FACT: a company does not publish its dispatch topology.
        allowed_fact_types=_NO_FACT, temporal="INTERVAL", staleness_days=365,
        enum_values=values, required=False,
    )



# --- PROCESS_OBSERVATION -------------------------------------------------
# Added in revision 5 so the canonical EV-* signals have primitives to be
# derived *from*. Every one records what a source described, never a verdict
# about it: `handoff_observation` says "the posting describes work passing
# from the technician to the office", not "the company is fragmented".
#
# The naming is deliberate. There is no `fragmentation_score`, no
# `owner_bottleneck`, no `needs_automation` — those are interpretations, and
# interpretation belongs to M4, after M3 has said what was observed.


#: What a process observation may be asserted as, before the evidence class
#: narrows it further. These are the attributes most often read out of job ads
#: and page fingerprints, so every one is evidence-class capped: only an
#: explicit company statement can carry a process observation to FACT.
_PROCESS_FACT_TYPES = ("FACT", "PROXY", "INFERENCE", "HYPOTHESIS")


def _process(key: str, fact_types: tuple[str, ...] = _PROCESS_FACT_TYPES) -> ResearchAttribute:
    """An observed step, actor or transfer described by a source."""
    return ResearchAttribute(
        key=key, group="PROCESS_OBSERVATION", value_kind="SET", value_type="JSON",
        cardinality="MANY", allowed_fact_types=fact_types, temporal="INTERVAL",
        staleness_days=540, required=False, evidence_class_capped=True,
    )


PROCESS_OBSERVATIONS: tuple[ResearchAttribute, ...] = (
    # Who a source says does what — the primitive under EV-MULTI-HANDOFF and
    # EV-OWNER-BOTTLENECK, neither of which M3 may conclude.
    _process("actor_responsibilities"),
    # A described transfer of work between roles, teams or systems.
    _process("handoff_observation"),
    # A described approval step, with the role that performs it.
    _process("approval_step"),
    # A system named as used at a described process step.
    _process("system_touchpoint"),
    # The same information described as entered more than once.
    _process("data_reentry_observation"),
    # The described path from a field finding to the office.
    _process("field_finding_handoff"),
    # The described path from a finding to a quote or estimate.
    _process("estimate_handoff"),
    # The described path from job completion to invoicing.
    _process("completion_to_billing_handoff"),
    # Where a source says a record of truth lives.
    _process("source_of_truth_observation"),
    # The described process for work discovered on site beyond the order.
    _process("additional_work_process"),
    # Whether in-house fabrication is described as an operation.
    ResearchAttribute(
        "fabrication_operation_present", "PROCESS_OBSERVATION", "SCALAR", "BOOLEAN",
        "ONE", _PROCESS_FACT_TYPES, "DURABLE", 730, required=False,
        evidence_class_capped=True,
    ),
)

RESEARCH_ATTRIBUTES: tuple[ResearchAttribute, ...] = (
    # --- WORKFORCE -------------------------------------------------------
    ResearchAttribute("technician_count", "WORKFORCE", "RANGE", "NUMERIC", "ONE",
                      ("ESTIMATE", "PROXY", "FACT"), "POINT_IN_TIME", 365,
                      allowed_units=("PEOPLE",)),
    ResearchAttribute("field_workforce_present", "WORKFORCE", "SCALAR", "BOOLEAN", "ONE",
                      ("FACT", "PROXY", "INFERENCE"), "DURABLE", 730),
    ResearchAttribute("field_roles", "WORKFORCE", "SET", "TEXT", "MANY",
                      ("FACT", "PROXY"), "POINT_IN_TIME", 365, required=False),
    ResearchAttribute("hiring_field_roles", "WORKFORCE", "SET", "TEXT", "MANY",
                      ("FACT", "PROXY"), "INTERVAL", 180, required=False),
    # --- FOOTPRINT -------------------------------------------------------
    ResearchAttribute("branch_count", "FOOTPRINT", "RANGE", "NUMERIC", "ONE",
                      ("FACT", "ESTIMATE", "PROXY"), "POINT_IN_TIME", 365,
                      allowed_units=("LOCATIONS",)),
    ResearchAttribute("service_area", "FOOTPRINT", "SET", "TEXT", "MANY",
                      ("FACT", "PROXY", "INFERENCE"), "INTERVAL", 540),
    ResearchAttribute("operating_markets", "FOOTPRINT", "SET", "REFERENCE", "MANY",
                      ("FACT", "PROXY", "INFERENCE"), "INTERVAL", 540, required=False),
    ResearchAttribute("fleet_presence", "FOOTPRINT", "SCALAR", "BOOLEAN", "ONE",
                      ("FACT", "PROXY", "INFERENCE"), "DURABLE", 730),
    ResearchAttribute("fleet_size", "FOOTPRINT", "RANGE", "NUMERIC", "ONE",
                      ("ESTIMATE", "PROXY"), "POINT_IN_TIME", 365,
                      allowed_units=("VEHICLES",), required=False),
    # --- SERVICE_MODEL ---------------------------------------------------
    ResearchAttribute("installation", "SERVICE_MODEL", "SCALAR", "BOOLEAN", "ONE",
                      ("FACT", "PROXY"), "DURABLE", 730),
    ResearchAttribute("preventive_maintenance", "SERVICE_MODEL", "SCALAR", "BOOLEAN", "ONE",
                      ("FACT", "PROXY"), "DURABLE", 730),
    ResearchAttribute("corrective_maintenance", "SERVICE_MODEL", "SCALAR", "BOOLEAN", "ONE",
                      ("FACT", "PROXY"), "DURABLE", 730),
    ResearchAttribute("emergency_service", "SERVICE_MODEL", "SCALAR", "BOOLEAN", "ONE",
                      ("FACT", "PROXY"), "DURABLE", 730),
    ResearchAttribute("recurring_service_contracts", "SERVICE_MODEL", "SCALAR", "BOOLEAN",
                      "ONE", ("FACT", "PROXY", "INFERENCE"), "DURABLE", 730),
    ResearchAttribute("service_categories", "SERVICE_MODEL", "SET", "TEXT", "MANY",
                      ("FACT", "PROXY"), "DURABLE", 730, required=False),
    # --- OPERATIONAL_SYSTEMS --------------------------------------------
    _tech("erp"), _tech("field_service_management"), _tech("dispatch_system"),
    _tech("crm"), _tech("customer_portal"), _tech("technician_mobile_app"),
    # --- OPERATING_MODEL -------------------------------------------------
    _operating("dispatch_centralization",
               ("CENTRAL", "PER_BRANCH", "HYBRID", "LIKELY_REQUIRED", "UNKNOWN_STRUCTURE")),
    _operating("work_order_process",
               ("DIGITAL_END_TO_END", "PARTIAL_DIGITAL", "PER_BRANCH_INTAKE", "PAPER_LED")),
    # `evidence_collection_method` is the one operating-model attribute a
    # company does state about itself — a downloadable PDF form is a fact.
    ResearchAttribute("evidence_collection_method", "OPERATING_MODEL", "SET", "ENUM", "MANY",
                      ("FACT", "PROXY", "INFERENCE"), "INTERVAL", 365, required=False,
                      enum_values=("PAPER_FORM", "MOBILE_APP", "PHOTO_CAPTURE",
                                   "SIGNATURE_CAPTURE", "EMAIL_REPORT")),
    _operating("parts_inventory_process",
               ("CENTRAL_WAREHOUSE", "VAN_STOCK", "SUPPLIER_DIRECT", "UNKNOWN_STRUCTURE")),
    _operating("asset_tracking",
               ("PER_ASSET_HISTORY", "PER_SITE", "NONE_EVIDENT", "UNKNOWN_STRUCTURE")),
    # --- CHANGE_SIGNALS --------------------------------------------------
    ResearchAttribute("hiring_signal", "CHANGE_SIGNALS", "SET", "JSON", "MANY",
                      ("FACT", "PROXY"), "INTERVAL", 180, required=False),
    ResearchAttribute("branch_expansion", "CHANGE_SIGNALS", "SET", "JSON", "MANY",
                      ("FACT", "PROXY", "INFERENCE"), "INTERVAL", 540, required=False),
    # An acquisition that happened stays happened.
    ResearchAttribute("acquisition", "CHANGE_SIGNALS", "SET", "JSON", "MANY",
                      ("FACT", "PROXY"), "INTERVAL", None, required=False),
    ResearchAttribute("system_migration", "CHANGE_SIGNALS", "SET", "JSON", "MANY",
                      _ALL, "INTERVAL", 365, required=False),
    ResearchAttribute("certification", "CHANGE_SIGNALS", "SET", "TEXT", "MANY",
                      ("FACT", "PROXY"), "INTERVAL", 730, required=False),
) + PROCESS_OBSERVATIONS

_BY_KEY: dict[str, ResearchAttribute] = {a.key: a for a in RESEARCH_ATTRIBUTES}

#: The default target set: every required attribute.
DEFAULT_TARGET_ATTRIBUTES: tuple[str, ...] = tuple(
    sorted(a.key for a in RESEARCH_ATTRIBUTES if a.required)
)


def get_attribute(key: str) -> ResearchAttribute:
    attribute = _BY_KEY.get(key)
    if attribute is None:
        raise AttributeNotInRegistryError(
            f"{key!r} is not in the M3 registry",
            details={"registry_version": RESEARCH_REGISTRY_VERSION,
                     "known": sorted(_BY_KEY)},
        )
    return attribute


def all_attributes() -> tuple[ResearchAttribute, ...]:
    return RESEARCH_ATTRIBUTES


def is_applicable(attribute: ResearchAttribute, vertical_key: str | None) -> bool:
    """Whether an attribute applies to a vertical.

    A non-applicable attribute leaves coverage's numerator **and** denominator
    untouched, so unknown is never scored as zero.
    """
    if not attribute.applies_to_verticals:
        return True
    return vertical_key in attribute.applies_to_verticals
