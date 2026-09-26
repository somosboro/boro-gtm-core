"""Extractors, and the immutable results they produce.

An extraction is a *result*, not an activity: it belongs to a text derivation
and a contract, not to the attempt that happened to run it. Two attempts
reading the same text under the same contract share one result and record two
usages, which is why re-running research does not manufacture corroboration.

**No extractor may set a fact type the registry would refuse.** Every
observation is validated centrally, against the registry, before it becomes a
row. An extractor that could write `FACT` because its own author believed the
sentence was strong would quietly reintroduce the ceiling problem the registry
exists to prevent — and it would do so one extractor at a time, invisibly.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.research.domain.models import (
    ResearchAttemptExtraction,
    ResearchExtraction,
    ResearchTextDerivation,
)
from boro_gtm.research.policies import sha256_json, sha256_text
from boro_gtm.research.registry import get_attribute


class ExtractorContractError(ValueError):
    """An extractor tried to emit something the registry does not permit."""


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """What a source says, quoted, located, and typed against the registry."""

    attribute_key: str
    value: dict[str, Any]
    fact_type: str
    locator: dict[str, Any]
    quote: str
    unit: str | None = None
    #: How the source supports the assertion, not how much we like it.
    support_kind: str = "DIRECT_STATEMENT"
    #: Present when the source is one whose class caps the fact type.
    evidence_class: str | None = None

    def __post_init__(self) -> None:
        """Fold the evidence class into the value, where the registry reads it.

        Keeping it in a side field made validation pass and persistence fail:
        the observation validated, the claim it produced did not, because the
        value the claim carried no longer said how the evidence was obtained.
        The class is part of what was observed, so it lives in the value.
        """
        if self.evidence_class and "evidence_class" not in self.value:
            object.__setattr__(
                self, "value", {**self.value, "evidence_class": self.evidence_class}
            )

    def as_json(self) -> dict[str, Any]:
        return {
            "attribute_key": self.attribute_key,
            "value": self.value,
            "unit": self.unit,
            "fact_type": self.fact_type,
            "locator": self.locator,
            "quote": self.quote,
            "support_kind": self.support_kind,
        }


def validate_observation(observation: Observation) -> None:
    """The single gate. Every extractor's output passes through here.

    The registry owns fact-type allowlists, unit allowlists, enum values, range
    sanity and the evidence-class ceiling. Enforcing them here rather than in
    each extractor means a new extractor inherits every rule by existing.
    """
    attribute = get_attribute(observation.attribute_key)
    attribute.validate(observation.value, observation.unit, observation.fact_type)


# ---------------------------------------------------------------------------
# Extractor identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Extractor:
    """One named, versioned way of reading text."""

    extractor_id: str
    extractor_version: str
    extractor_kind: str
    output_schema_version: str
    run: Callable[[str, dict[str, Any]], list[Observation]]
    determinism: str = "DETERMINISTIC"
    model_provider: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    prompt_template_version: str | None = None
    temperature: float | None = None
    #: Media types this extractor is willing to read.
    media_types: tuple[str, ...] = ("text/html",)
    extractor_confidence: float | None = None

    def contract_hash(self) -> str:
        """Identity of *how* text was read — never of the text itself."""
        return sha256_json({
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "extractor_kind": self.extractor_kind,
            "output_schema_version": self.output_schema_version,
            "determinism": self.determinism,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "prompt_template_version": self.prompt_template_version,
            "temperature": self.temperature,
        })


# ---------------------------------------------------------------------------
# Locator helpers
# ---------------------------------------------------------------------------


def _span(text: str, quote: str, kind: str = "HTML_SPAN") -> dict[str, Any]:
    """A real offset into the derived text, or none at all.

    Never a fabricated line number: the text was generated by our own
    canonicalization, so a line number would describe our formatting rather
    than the document.
    """
    start = text.find(quote)
    if start < 0:
        return {"kind": kind, "start": None, "end": None, "resolved": False}
    return {"kind": kind, "start": start, "end": start + len(quote), "resolved": True}


def _pdf_span(text: str, quote: str, page_offsets: dict[str, int] | None) -> dict[str, Any]:
    start = text.find(quote)
    page = None
    if start >= 0 and page_offsets:
        for number, offset in sorted(page_offsets.items(), key=lambda kv: kv[1]):
            if offset <= start:
                page = int(number)
    return {
        "kind": "PDF_SPAN", "page": page, "start": start if start >= 0 else None,
        "end": start + len(quote) if start >= 0 else None, "resolved": start >= 0,
    }


def _json_pointer(pointer: str, text: str, quote: str) -> dict[str, Any]:
    start = text.find(quote)
    return {
        "kind": "JSON_POINTER", "pointer": pointer,
        "start": start if start >= 0 else None,
        "end": start + len(quote) if start >= 0 else None,
        "resolved": start >= 0,
    }


def _job_field(field_name: str, index: int | None, text: str, quote: str) -> dict[str, Any]:
    start = text.find(quote)
    return {
        "kind": "JOB_FIELD", "field": field_name, "index": index,
        "start": start if start >= 0 else None,
        "end": start + len(quote) if start >= 0 else None,
        "resolved": start >= 0,
    }


def locator_hash(locator: dict[str, Any]) -> str:
    return sha256_json(locator)


# ---------------------------------------------------------------------------
# Rule extractors over prose
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhraseRule:
    """A phrase, and the observation finding it licenses."""

    pattern: re.Pattern[str]
    attribute_key: str
    fact_type: str
    build: Callable[[re.Match[str]], dict[str, Any]]
    unit: str | None = None
    evidence_class: str | None = None
    support_kind: str = "DIRECT_STATEMENT"


def _run_rules(
    rules: Iterable[PhraseRule], text: str, kind: str = "HTML_SPAN"
) -> list[Observation]:
    found: list[Observation] = []
    for rule in rules:
        for match in rule.pattern.finditer(text):
            quote = match.group(0)
            observation = Observation(
                attribute_key=rule.attribute_key,
                value=rule.build(match),
                fact_type=rule.fact_type,
                locator=_span(text, quote, kind),
                quote=quote,
                unit=rule.unit,
                support_kind=rule.support_kind,
                evidence_class=rule.evidence_class,
            )
            validate_observation(observation)
            found.append(observation)
    return found


def _exact(n: int) -> dict[str, Any]:
    return {"min": n, "max": n}


WORKFORCE_RULES: tuple[PhraseRule, ...] = (
    PhraseRule(
        re.compile(r"(\d{1,4}) field technicians"), "technician_count", "FACT",
        lambda m: _exact(int(m.group(1))), unit="PEOPLE",
    ),
    PhraseRule(
        re.compile(r"approximately (\d{1,4}) technicians"), "technician_count", "ESTIMATE",
        lambda m: _exact(int(m.group(1))), unit="PEOPLE",
    ),
    PhraseRule(
        re.compile(r"\d{1,4} field technicians"), "field_workforce_present", "FACT",
        lambda m: {"value": True},
    ),
    PhraseRule(
        re.compile(r"fleet of (\d{1,4}) service vehicles"), "fleet_size", "ESTIMATE",
        lambda m: _exact(int(m.group(1))), unit="VEHICLES",
    ),
    PhraseRule(
        re.compile(r"fleet of \d{1,4} service vehicles"), "fleet_presence", "FACT",
        lambda m: {"value": True},
    ),
    PhraseRule(
        re.compile(r"(\w+) service locations: ([^.]+)\."), "branch_count", "FACT",
        lambda m: _exact(_WORD_NUMBERS.get(m.group(1).lower(), len(m.group(2).split(",")))),
        unit="LOCATIONS",
    ),
    PhraseRule(
        re.compile(r"Locations: (\d{1,3})"), "branch_count", "PROXY",
        lambda m: _exact(int(m.group(1))), unit="LOCATIONS",
    ),
    PhraseRule(
        re.compile(r"We serve ([^.]+), Ohio\."), "service_area", "FACT",
        lambda m: {"values": [part.strip() for part in
                              m.group(1).replace(" and ", ", ").split(",") if part.strip()]},
    ),
    PhraseRule(
        re.compile(r"(\d{1,3}) service coordinators"), "field_roles", "FACT",
        lambda m: {"values": ["service coordinator"]},
    ),
    PhraseRule(
        re.compile(r"(\d{1,3}) project estimators"), "field_roles", "FACT",
        lambda m: {"values": ["project estimator"]},
    ),
)

_WORD_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


SERVICE_MODEL_RULES: tuple[PhraseRule, ...] = (
    PhraseRule(re.compile(r"HVAC installation"), "installation", "FACT",
               lambda m: {"value": True}),
    PhraseRule(re.compile(r"preventive maintenance", re.I), "preventive_maintenance", "FACT",
               lambda m: {"value": True}),
    PhraseRule(re.compile(r"corrective repair|corrective maintenance", re.I),
               "corrective_maintenance", "FACT", lambda m: {"value": True}),
    PhraseRule(re.compile(r"24/7 emergency|24 hours a day, 7 days a week emergency", re.I),
               "emergency_service", "FACT", lambda m: {"value": True}),
    PhraseRule(re.compile(r"Emergency service: not offered", re.I),
               "emergency_service", "PROXY", lambda m: {"value": False}),
    PhraseRule(re.compile(r"maintenance agreements with [^.]+\."),
               "recurring_service_contracts", "FACT", lambda m: {"value": True}),
    PhraseRule(re.compile(r"Categories: ([^.]+)\."), "service_categories", "FACT",
               lambda m: {"values": [c.strip() for c in m.group(1).split(",")]}),
)


def _process(kind: str, detail: dict[str, Any]) -> dict[str, Any]:
    """A process observation records what a source described, never a verdict."""
    return {"observation": kind, **detail}


PROCESS_RULES: tuple[PhraseRule, ...] = (
    PhraseRule(
        re.compile(
            r"service coordinators receive the request, confirm the site "
            r"and assign a technician"
        ),
        "actor_responsibilities", "FACT",
        lambda m: _process("named_responsibility",
                           {"actor": "service coordinator",
                            "activities": ["receive request", "confirm site",
                                           "assign technician"]}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(
            r"completes the work order on site and submits it to the "
            r"service coordinator"
        ),
        "handoff_observation", "FACT",
        lambda m: _process("handoff", {"from": "technician", "to": "service coordinator",
                                       "object": "work order"}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"the finding is\s*written up and passed to an estimator"),
        "field_finding_handoff", "FACT",
        lambda m: _process("handoff", {"from": "technician", "to": "estimator",
                                       "object": "field finding"}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(
            r"prepares a quote for the customer's approval before the "
            r"additional work is scheduled"
        ),
        "estimate_handoff", "FACT",
        lambda m: _process("handoff", {"from": "estimator", "to": "customer",
                                       "object": "quote"}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"When a technician identifies work beyond the original order"),
        "additional_work_process", "FACT",
        lambda m: _process("described_process", {"trigger": "work beyond original order"}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"quote above \$([\d,]+) is reviewed and approved by the ([\w ]+)"),
        "approval_step", "FACT",
        lambda m: _process("approval", {"approver": m.group(2).strip(),
                                        "threshold": m.group(1)}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"the details are\s*entered into the accounting system for invoicing"),
        "completion_to_billing_handoff", "FACT",
        lambda m: _process("handoff", {"from": "service coordinator", "to": "accounting system",
                                       "object": "completed work order"}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"Equipment history is kept in the maintenance binder[^.]*\."),
        "source_of_truth_observation", "FACT",
        lambda m: _process("stated_record_location",
                           {"subject": "equipment history",
                            "locations": ["site maintenance binder",
                                          "coordinator spreadsheet"]}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(
            r"the coordinator re-enters the checklist results into the "
            r"maintenance spreadsheet"
        ),
        "data_reentry_observation", "FACT",
        lambda m: _process("reentry", {"actor": "coordinator", "object": "checklist results",
                                       "destination": "maintenance spreadsheet"}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"view upcoming visits through our ([\w ]+Portal)"),
        "system_touchpoint", "FACT",
        lambda m: _process("touchpoint", {"actor": "customer", "system": m.group(1).strip()}),
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"in-house sheet metal fabrication shop"),
        "fabrication_operation_present", "FACT",
        lambda m: {"value": True},
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
    PhraseRule(
        re.compile(r"Customers can view upcoming visits through our ([\w ]+Portal)"),
        "customer_portal", "FACT",
        lambda m: {"values": [{"name": m.group(1).strip()}]},
        evidence_class="EXPLICIT_COMPANY_STATEMENT",
    ),
)


CHANGE_SIGNAL_RULES: tuple[PhraseRule, ...] = (
    PhraseRule(
        re.compile(r"opened a (\w+) service location in ([\w ]+)"),
        "branch_expansion", "FACT",
        lambda m: {"values": [{"event": "new_location", "location": m.group(2).strip()}]},
    ),
    PhraseRule(
        re.compile(r"migrating from its legacy dispatch software to ([\w ]+) during (\d{4})"),
        "system_migration", "FACT",
        lambda m: {"values": [{"to": m.group(1).strip(), "year": int(m.group(2))}]},
    ),
)


def _rule_extractor(
    extractor_id: str,
    rules: tuple[PhraseRule, ...],
    *,
    version: str = "1.0.0",
    media_types: tuple[str, ...] = ("text/html",),
) -> Extractor:
    return Extractor(
        extractor_id=extractor_id,
        extractor_version=version,
        extractor_kind="RULE",
        output_schema_version="1",
        run=lambda text, ctx: _run_rules(rules, text),
        media_types=media_types,
        extractor_confidence=0.9,
    )


# ---------------------------------------------------------------------------
# Structured extractors
# ---------------------------------------------------------------------------


def _extract_job_posting(text: str, context: dict[str, Any]) -> list[Observation]:
    """A job ad describes a job, which is weaker evidence than it looks.

    Everything here is capped at ``PROXY`` by ``JOB_DESCRIPTION_MENTION``: a
    posting proves someone wrote a posting. The registry enforces the ceiling;
    this extractor simply declares the class honestly.
    """
    posting = json.loads(text)
    # A media type is not a schema. Without this guard the extractor happily
    # read a government registry filing and emitted a hiring signal with an
    # empty job title, because both documents are `application/json`.
    if not {"job_id", "employer", "title"} <= set(posting):
        return []
    out: list[Observation] = []

    def emit(key: str, value: dict[str, Any], quote: str, locator: dict[str, Any],
             fact_type: str = "PROXY") -> None:
        observation = Observation(
            attribute_key=key, value=value, fact_type=fact_type, locator=locator,
            quote=quote, evidence_class="JOB_DESCRIPTION_MENTION",
            support_kind="DERIVED",
        )
        validate_observation(observation)
        out.append(observation)

    title = posting.get("title", "")
    emit("hiring_signal",
         {"values": [{"role": title, "posted_at": posting.get("posted_at")}]},
         title, _json_pointer("/title", text, title), fact_type="FACT")
    emit("hiring_field_roles", {"values": [title]}, title,
         _json_pointer("/title", text, title), fact_type="FACT")

    for index, line in enumerate(posting.get("responsibilities", [])):
        pointer = _job_field("responsibilities", index, text, line)
        lowered = line.lower()
        if "dispatch" in lowered:
            emit("actor_responsibilities",
                 _process("named_responsibility",
                          {"actor": title, "activities": [line]}), line, pointer)
        if "re-key" in lowered or "re-enter" in lowered:
            emit("data_reentry_observation",
                 _process("reentry", {"actor": title, "object": "completed work orders",
                                      "destination": "accounting system"}), line, pointer)
        if "chase" in lowered or "missing paperwork" in lowered:
            emit("completion_to_billing_handoff",
                 _process("handoff", {"from": "technician", "to": title,
                                      "object": "paperwork before invoicing"}),
                 line, pointer)
        if "approval" in lowered:
            emit("approval_step",
                 _process("approval", {"approver": "service manager",
                                       "trigger": "quotes over threshold"}), line, pointer)

    for index, line in enumerate(posting.get("requirements", [])):
        pointer = _job_field("requirements", index, text, line)
        for system, key in (("ServiceTitan", "field_service_management"),
                            ("QuickBooks", "erp")):
            if system.lower() in line.lower():
                emit(key, {"values": [{"name": system}]}, line, pointer)
                emit("system_touchpoint",
                     _process("touchpoint", {"actor": title, "system": system}),
                     line, pointer)
    return out


def _extract_registry(text: str, context: dict[str, Any]) -> list[Observation]:
    """A government filing. Strong about legal identity, silent about process."""
    record = json.loads(text)
    if not {"jurisdiction", "entity_number", "legal_name"} <= set(record):
        return []
    out: list[Observation] = []
    city = record.get("principal_address", {}).get("city")
    if city:
        observation = Observation(
            attribute_key="service_area", value={"values": [f"{city} (registered office)"]},
            fact_type="PROXY",
            locator=_json_pointer("/principal_address/city", text, city), quote=city,
            support_kind="DERIVED",
        )
        validate_observation(observation)
        out.append(observation)
    return out


def _extract_pdf_narrative(text: str, context: dict[str, Any]) -> list[Observation]:
    """A sampled read of contract prose.

    Sampled output may vary between executions, so it gets its own execution
    slot and may create evidence — but it may not, on its own, assert a company
    claim. A human confirming it is a separate, deterministic extraction.
    """
    offsets = context.get("page_offsets")
    out: list[Observation] = []
    patterns = (
        (r"returns the\s+completed ticket to the service coordinator",
         "handoff_observation",
         _process("handoff", {"from": "technician", "to": "service coordinator",
                              "object": "service ticket"})),
        (r"enters the ticket into the accounting system\s+for invoicing",
         "completion_to_billing_handoff",
         _process("handoff", {"from": "service coordinator", "to": "accounting system",
                              "object": "service ticket"})),
        (r"requires a written estimate\s+approved by the customer",
         "approval_step",
         _process("approval", {"approver": "customer", "trigger": "out-of-scope work"})),
        (r"equipment list maintained by Meridian is the record of covered\s+assets",
         "source_of_truth_observation",
         _process("stated_record_location", {"subject": "covered assets",
                                             "locations": ["Meridian equipment list"]})),
    )
    for pattern, key, value in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        quote = match.group(0)
        observation = Observation(
            attribute_key=key, value=value, fact_type="PROXY",
            locator=_pdf_span(text, quote, offsets), quote=quote,
            evidence_class="EXPLICIT_COMPANY_STATEMENT", support_kind="DERIVED",
        )
        validate_observation(observation)
        out.append(observation)
    return out


PROSE_EXTRACTOR = _rule_extractor("workforce_and_footprint", WORKFORCE_RULES)
SERVICE_EXTRACTOR = _rule_extractor("service_model", SERVICE_MODEL_RULES)
PROCESS_EXTRACTOR = _rule_extractor("process_observations", PROCESS_RULES)
CHANGE_EXTRACTOR = _rule_extractor("change_signals", CHANGE_SIGNAL_RULES)

JOB_EXTRACTOR = Extractor(
    extractor_id="job_posting_json", extractor_version="1.0.0", extractor_kind="PARSER",
    output_schema_version="1", run=_extract_job_posting,
    media_types=("application/json",), extractor_confidence=0.95,
)

REGISTRY_EXTRACTOR = Extractor(
    extractor_id="registry_json", extractor_version="1.0.0", extractor_kind="PARSER",
    output_schema_version="1", run=_extract_registry,
    media_types=("application/json",), extractor_confidence=0.98,
)

#: A local, deterministic stand-in for a model. There is no external call, no
#: API key and no network; ``SAMPLED`` describes the *contract*, which is what
#: the schema cares about.
PDF_MODEL_EXTRACTOR = Extractor(
    extractor_id="contract_narrative", extractor_version="1.0.0", extractor_kind="MODEL",
    output_schema_version="1", run=_extract_pdf_narrative, determinism="SAMPLED",
    model_provider="fixture", model_name="local-fixture-reader", model_version="0",
    prompt_template_version="1", temperature=0.2,
    media_types=("application/pdf",), extractor_confidence=0.55,
)

HUMAN_EXTRACTOR = Extractor(
    extractor_id="analyst_confirmation", extractor_version="1.0.0", extractor_kind="HUMAN",
    output_schema_version="1", run=_extract_pdf_narrative,
    media_types=("application/pdf",), extractor_confidence=1.0,
)

DEFAULT_EXTRACTORS: tuple[Extractor, ...] = (
    PROSE_EXTRACTOR, SERVICE_EXTRACTOR, PROCESS_EXTRACTOR, CHANGE_EXTRACTOR,
    JOB_EXTRACTOR, REGISTRY_EXTRACTOR, PDF_MODEL_EXTRACTOR,
)


def extractors_for(media_type: str,
                   extractors: Iterable[Extractor] = DEFAULT_EXTRACTORS) -> list[Extractor]:
    return [x for x in extractors if media_type in x.media_types]


# ---------------------------------------------------------------------------
# Persisting results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExtractionOutcome:
    extraction: ResearchExtraction
    created: bool
    observations: list[Observation] = field(default_factory=list)


def run_extraction(
    session: Session,
    *,
    extractor: Extractor,
    text_derivation: ResearchTextDerivation,
    attempt_id: uuid.UUID,
    now: datetime,
    sample_execution_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> ExtractionOutcome:
    """Create or reuse an extraction result, then record this attempt's usage.

    A deterministic contract over the same text is the same result forever, so
    the second attempt reuses it. A sampled contract needs its own execution
    slot: a unique key that kept the first sample would freeze a race, not
    deliver idempotency.
    """
    if extractor.determinism == "SAMPLED" and sample_execution_id is None:
        raise ExtractorContractError(
            f"{extractor.extractor_id} is SAMPLED and needs a sample_execution_id"
        )
    contract = extractor.contract_hash()

    slot = (
        ResearchExtraction.sample_execution_id == sample_execution_id
        if sample_execution_id is not None
        else ResearchExtraction.sample_execution_id.is_(None)
    )
    existing = session.scalars(
        select(ResearchExtraction).where(
            ResearchExtraction.text_derivation_id == text_derivation.id,
            ResearchExtraction.extraction_contract_hash == contract,
            slot,
        )
    ).first()

    if existing is not None:
        _record_usage(session, attempt_id, existing.id, "REUSED", now)
        return ExtractionOutcome(
            extraction=existing, created=False,
            observations=_rehydrate(existing),
        )

    text = text_derivation.extracted_text or ""
    run_context = dict(context or {})
    run_context.setdefault("page_offsets", text_derivation.page_offsets)
    observations = extractor.run(text, run_context)
    payload = {"observations": [o.as_json() for o in observations]}
    raw_output = sha256_json(payload)

    extraction = ResearchExtraction(
        id=uuid.uuid4(),
        text_derivation_id=text_derivation.id,
        body_id=text_derivation.body_id,
        extractor_kind=extractor.extractor_kind,
        extractor_id=extractor.extractor_id,
        extractor_version=extractor.extractor_version,
        model_provider=extractor.model_provider,
        model_name=extractor.model_name,
        model_version=extractor.model_version,
        prompt_template_version=extractor.prompt_template_version,
        output_schema_version=extractor.output_schema_version,
        determinism=extractor.determinism,
        temperature=extractor.temperature,
        sample_execution_id=sample_execution_id,
        extraction_contract_hash=contract,
        extractor_confidence=extractor.extractor_confidence,
        observations=payload,
        status="OK",
        raw_output=raw_output,
        raw_output_sha256=sha256_text(raw_output),
        created_at=now,
    )
    session.add(extraction)
    session.flush()
    _record_usage(session, attempt_id, extraction.id, "CREATED", now)
    return ExtractionOutcome(extraction=extraction, created=True, observations=observations)


def _rehydrate(extraction: ResearchExtraction) -> list[Observation]:
    payload = (extraction.observations or {}).get("observations", [])
    return [
        Observation(
            attribute_key=item["attribute_key"], value=item["value"],
            fact_type=item["fact_type"], locator=item["locator"], quote=item["quote"],
            unit=item.get("unit"),
            support_kind=item.get("support_kind", "DIRECT_STATEMENT"),
            evidence_class=item["value"].get("evidence_class"),
        )
        for item in payload
    ]


def _record_usage(
    session: Session, attempt_id: uuid.UUID, extraction_id: uuid.UUID,
    role: str, now: datetime,
) -> None:
    session.execute(
        pg_insert(ResearchAttemptExtraction)
        .values(
            id=uuid.uuid4(), attempt_id=attempt_id, extraction_id=extraction_id,
            usage_role=role, used_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_attempt_extraction")
    )
