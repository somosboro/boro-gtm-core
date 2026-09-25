"""The alignment documents are contracts, so they are parsed, not trusted.

`M3_CANONICAL_COMMERCIAL_ALIGNMENT.md` and `GTM_ACCOUNT_FIELD_OWNERSHIP.md`
state coverage and ownership that the rest of the system depends on. Every
count this project wrote by hand has eventually drifted, so these are read
mechanically instead: a matrix citing an attribute nobody seeds, or a canonical
field with no owner, fails here rather than in a quarter's time.

Covers acceptance O5–O8, O14 and O15.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from boro_gtm.commercial.contract import load_contract
from boro_gtm.research.registry import (
    RESEARCH_ATTRIBUTES,
    AttributeNotInRegistryError,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"
ALIGNMENT = DOCS / "M3_CANONICAL_COMMERCIAL_ALIGNMENT.md"
FIELD_OWNERSHIP = DOCS / "GTM_ACCOUNT_FIELD_OWNERSHIP.md"
MILESTONES = DOCS / "GTM_MILESTONE_OWNERSHIP.md"

CONTRACT = load_contract()
REGISTRY_KEYS = {a.key for a in RESEARCH_ATTRIBUTES}

# M3 owns exactly these two of the canonical twenty-five (M3-ADR-044). The
# canonical names carry their own `[]` for array fields; they are kept verbatim.
M3_WRITABLE_FIELDS = {"evidence[]", "evidence_confidence"}
M3_FORBIDDEN_FIELDS = {
    "qualification_score", "qualification_route", "budget_band", "buyer_role",
    "commercial_level_candidate", "commercial_level_final",
    "selected_capabilities[]", "price_floor_usd", "quoted_price_usd",
    "normalized_gross_margin",
}


def _rows(path: Path, first_cell: re.Pattern[str]) -> list[list[str]]:
    """Markdown table rows whose first cell matches, cells stripped."""
    out = []
    for line in path.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and first_cell.fullmatch(cells[0]):
            out.append(cells)
    return out


def _keys(cell: str) -> set[str]:
    return set(re.findall(r"`([a-z0-9_]+)`", cell))


@pytest.fixture(scope="module")
def signal_matrix() -> dict[str, list[str]]:
    rows = _rows(ALIGNMENT, re.compile(r"\d{1,2}"))
    matrix = {}
    for cells in rows:
        signal = cells[1].strip("`*")
        if signal.startswith("EV-"):
            matrix[signal] = cells
    return matrix


# --- O5/O6: the coverage matrix --------------------------------------------


def test_every_canonical_evidence_signal_appears_in_the_matrix(signal_matrix):
    """O5 (part): eighteen canonical signals, eighteen matrix rows."""
    assert set(signal_matrix) == set(CONTRACT.evidence_signal_ids)
    assert len(signal_matrix) == 18


def test_no_canonical_signal_is_left_uncovered(signal_matrix):
    """O5: a signal with nothing behind it is a gap, and gaps must be visible."""
    uncovered = {
        sig: cells[4] for sig, cells in signal_matrix.items()
        if "NONE" in cells[4].upper()
    }
    assert uncovered == {}, f"signals with no primitive behind them: {uncovered}"

    verdicts = {re.sub(r"[^A-Z]", "", c[4]) for c in signal_matrix.values()}
    assert verdicts <= {"DIRECT", "PARTIAL"}


def test_the_matrix_cites_only_attributes_that_are_actually_registered(signal_matrix):
    """O6: claimed coverage must name a primitive the registry really seeds."""
    unknown = {}
    for signal, cells in signal_matrix.items():
        missing = _keys(cells[5]) - REGISTRY_KEYS
        if missing:
            unknown[signal] = sorted(missing)
    assert unknown == {}, f"matrix cites unregistered attributes: {unknown}"


def test_coverage_improved_rather_than_being_declared(signal_matrix):
    """The before/after columns are the claim; check the arithmetic."""
    before = [re.sub(r"[^A-Z]", "", c[3]) for c in signal_matrix.values()]
    after = [re.sub(r"[^A-Z]", "", c[4]) for c in signal_matrix.values()]
    assert before.count("NONE") == 9
    assert after.count("NONE") == 0
    assert after.count("DIRECT") >= before.count("DIRECT")


# --- O7/O8: the new primitives are observations ----------------------------


def test_process_observations_are_observational_not_judgemental():
    """O7: no primitive may carry a verdict-shaped name."""
    verdict_words = (
        "maturity", "readiness", "efficiency", "fit", "score", "risk",
        "quality", "gap", "bottleneck", "pain", "opportunity", "recommend",
    )
    observations = [a for a in RESEARCH_ATTRIBUTES if a.group == "PROCESS_OBSERVATION"]
    assert len(observations) == 11
    offenders = [
        a.key for a in observations
        if any(w in a.key for w in verdict_words)
    ]
    assert offenders == [], f"judgement-shaped primitives: {offenders}"


def test_a_process_observation_reaches_fact_only_by_explicit_statement():
    """O8: these are the attributes most often read out of job ads.

    A posting saying "coordinates with the office" describes what the posting
    describes. It is not proof of how the company works, so the evidence class
    caps it — only the company saying so can make a process observation a fact.
    """
    observations = [a for a in RESEARCH_ATTRIBUTES if a.group == "PROCESS_OBSERVATION"]
    uncapped = [a.key for a in observations if not a.evidence_class_capped]
    assert uncapped == [], f"process observations escaping the ceiling: {uncapped}"

    for attribute in observations:
        value = {"evidence_class": "JOB_DESCRIPTION_MENTION", "items": []}
        with pytest.raises(AttributeNotInRegistryError):
            attribute.validate(value, None, "FACT")
        attribute.validate(value, None, "PROXY")  # the ceiling, not an error
        attribute.validate(
            {"evidence_class": "EXPLICIT_COMPANY_STATEMENT", "items": []}, None, "FACT"
        )


def test_the_new_primitives_did_not_move_coverage_denominators():
    """Optional by construction: adding evidence must not re-score every company."""
    observations = [a for a in RESEARCH_ATTRIBUTES if a.group == "PROCESS_OBSERVATION"]
    assert [a.key for a in observations if a.required] == []
    assert sum(1 for a in RESEARCH_ATTRIBUTES if a.required) == 16


# --- O14/O15: field and stage ownership ------------------------------------


@pytest.fixture(scope="module")
def ownership() -> dict[str, list[str]]:
    rows = _rows(FIELD_OWNERSHIP, re.compile(r"\d{1,2}"))
    out = {}
    for cells in rows:
        out[cells[1].strip("`")] = cells
    return out


def test_every_canonical_q1_field_has_exactly_one_owning_milestone(ownership):
    """O15 (part): twenty-five fields, twenty-five owners, no field orphaned."""
    assert set(ownership) == set(CONTRACT.account_fields)
    assert len(ownership) == 25
    for field, cells in ownership.items():
        owner = cells[2]
        assert re.search(r"M\d", owner) or "finance" in owner.lower(), (
            f"{field} has no milestone owner: {owner!r}"
        )


def test_m3_writes_exactly_two_canonical_fields(ownership):
    """O14: the ten forbidden fields are the plausible-looking mistakes."""
    m3_owned = {
        f for f, cells in ownership.items()
        if re.fullmatch(r"\*{0,2}M3\*{0,2}", cells[2].strip())
    }
    assert m3_owned == M3_WRITABLE_FIELDS

    for field in M3_FORBIDDEN_FIELDS:
        owner = ownership[field][2]
        assert "M3" not in owner, f"M3 must not own {field}: {owner!r}"


def test_qualification_and_classification_fields_are_null_before_their_stage(ownership):
    """A default in qualification_score is indistinguishable from a real score."""
    for field in ("qualification_score", "commercial_level_final", "budget_band"):
        nullable = ownership[field][5]
        assert "Yes" in nullable, f"{field} must be nullable before its stage"


def test_every_canonical_sales_stage_is_mapped_or_explicitly_out_of_scope():
    """O15: fourteen stages; four are delivery and say so, rather than silently missing."""
    rows = _rows(MILESTONES, re.compile(r"\d{1,2}"))
    mapped = {}
    for cells in rows:
        stage = cells[1].strip("`")
        if stage in CONTRACT.sales_motion_stages:
            mapped[stage] = cells[2]
    assert set(mapped) == set(CONTRACT.sales_motion_stages)

    for stage, milestone in mapped.items():
        assert re.search(r"M\d", milestone) or milestone.strip() in {"—", "-"}, (
            f"{stage} is neither owned nor marked out of scope: {milestone!r}"
        )


def test_the_stage_table_preserves_the_canonical_order():
    """Order is semantic: it is what makes "qualification comes after response"
    enforceable rather than aspirational (M3-ADR-046)."""
    rows = _rows(MILESTONES, re.compile(r"\d{1,2}"))
    order = CONTRACT.sales_motion_stages
    listed = [c[1].strip("`") for c in rows if c[1].strip("`") in order]
    assert listed == list(order)

    position = {stage: i for i, stage in enumerate(order)}
    assert position["QUALIFICATION"] > position["RESPONSE"]
    assert position["EVIDENCE"] < position["OUTREACH"]
