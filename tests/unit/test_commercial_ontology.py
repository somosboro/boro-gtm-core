"""The canonical commercial ontology, and the boundaries GTM Core must keep.

Authority runs Price Book > YAML companion > this repository. These tests do
not re-litigate commercial policy; they assert that GTM Core does not quietly
diverge from it, and that M3 cannot reach across the boundaries the Price Book
draws.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from boro_gtm.commercial.contract import (
    ALLOWED_TOP_LEVEL,
    CONTRACT_PATH,
    ContractDriftError,
    ContractLeakError,
    load_contract,
    validate_yaml_against_contract,
)

CANONICAL_YAML = Path("/Users/usuario/Downloads/BORO_OPERATIONS_OS_GTM_SCHEMA_v2.0.yaml")
yaml_available = pytest.mark.skipif(
    not CANONICAL_YAML.exists(),
    reason="canonical YAML is held outside the repository; see the alignment doc",
)

CANONICAL_EV_IDS = [
    "EV-RECURRING-SERVICE", "EV-FIELD-SCALE", "EV-MULTI-HANDOFF",
    "EV-FRAGMENTED-TOOLS", "EV-MANUAL-APPROVAL", "EV-DUPLICATE-ENTRY",
    "EV-ASSET-HISTORY", "EV-OWNER-BOTTLENECK", "EV-QUOTE-DELAY",
    "EV-MISSED-ADDITIONAL-WORK", "EV-FIELD-OFFICE-DISCONNECT",
    "EV-BILLING-READINESS", "EV-MULTI-LOCATION", "EV-GROWTH",
    "EV-SYSTEM-MIGRATION", "EV-JOB-POSTING", "EV-24-7", "EV-FABRICATION",
]


# --- the committed contract ------------------------------------------------


def test_the_contract_loads_and_agrees_with_itself():
    contract = load_contract()
    assert contract.counts == {
        "capabilities": 43,
        "sales_motion_stages": 14,
        "qualification_dimensions": 6,
        "classifier_dimensions": 12,
        "account_opportunity_minimum_fields": 25,
        "evidence_object_fields": 7,
        "products": 5,
        "intervention_modes": 4,
        "founder_decisions_required": 7,
        "evidence_signals": 18,
        "commercial_levels": 3,
        "hard_transformation_overrides": 5,
    }


def test_the_canonical_evidence_signal_ids_match_the_specification():
    """§21.15: the EV ids GTM Core maps against are the canonical ones."""
    assert load_contract().evidence_signal_ids == CANONICAL_EV_IDS


def test_the_committed_contract_carries_no_commercial_economics():
    """This repository is public; the canonical sources are not."""
    raw = json.loads(CONTRACT_PATH.read_text())
    blob = json.dumps(raw)
    # The canonical price floors, margin band and margin floor must be absent.
    for value in ("3000", "15000", "25000", "40000", "1250", "gross_margin_target"):
        assert value not in blob, f"{value!r} leaked into a public repository"
    assert "pricing_policy" not in raw
    assert set(raw) <= ALLOWED_TOP_LEVEL


def test_an_unreviewed_top_level_key_is_refused(tmp_path):
    raw = json.loads(CONTRACT_PATH.read_text())
    raw["normalized_economics"] = {"gross_margin_floor_pct": 45}
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ContractLeakError) as exc:
        load_contract(path)
    assert "normalized_economics" in exc.value.details["keys"]


def test_an_economics_key_anywhere_is_refused(tmp_path):
    raw = json.loads(CONTRACT_PATH.read_text())
    raw["authority"]["price_floor_usd"] = 25000
    path = tmp_path / "leaky2.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ContractLeakError):
        load_contract(path)


def test_a_contract_whose_counts_lie_is_refused(tmp_path):
    raw = json.loads(CONTRACT_PATH.read_text())
    raw["counts"]["capabilities"] = 36
    path = tmp_path / "drifted.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ContractDriftError) as exc:
        load_contract(path)
    assert "capabilities" in exc.value.details["declared_vs_derived"]


# --- the drift gate against the canonical YAML -----------------------------


@yaml_available
def test_the_canonical_yaml_matches_the_committed_contract():
    report = validate_yaml_against_contract(CANONICAL_YAML)
    assert report["matches_recorded_digest"] is True


def _mutated(document) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(document, handle)
    handle.close()
    return Path(handle.name)


@yaml_available
def test_the_validator_fails_when_a_capability_is_removed():
    """§21.17."""
    document = yaml.safe_load(CANONICAL_YAML.read_text())
    document["capabilities"] = document["capabilities"][:-1]
    with pytest.raises(ContractDriftError) as exc:
        validate_yaml_against_contract(_mutated(document))
    assert any("capabilities differ" in p for p in exc.value.details["problems"])


@yaml_available
def test_the_validator_fails_when_the_sales_motion_is_reordered():
    """§21.18."""
    document = yaml.safe_load(CANONICAL_YAML.read_text())
    motion = document["sales_motion"]
    motion[0], motion[1] = motion[1], motion[0]
    with pytest.raises(ContractDriftError) as exc:
        validate_yaml_against_contract(_mutated(document))
    assert any("sales motion order" in p for p in exc.value.details["problems"])


@yaml_available
def test_the_validator_fails_when_a_matrix_stops_covering_every_capability():
    document = yaml.safe_load(CANONICAL_YAML.read_text())
    document["capability_matrix"]["BORO-OS-C"]["standard"].pop()
    with pytest.raises(ContractDriftError) as exc:
        validate_yaml_against_contract(_mutated(document))
    assert any("capability_matrix" in p for p in exc.value.details["problems"])


@yaml_available
def test_the_validator_fails_when_a_qualification_dimension_changes():
    document = yaml.safe_load(CANONICAL_YAML.read_text())
    document["qualification"]["rubric"]["dimensions"][0]["dimension_id"] = "renamed"
    with pytest.raises(ContractDriftError) as exc:
        validate_yaml_against_contract(_mutated(document))
    assert any("qualification dimensions" in p for p in exc.value.details["problems"])


# --- M3 must not reach across the commercial boundaries --------------------


def test_m3_defines_no_capability_selection():
    """§8: capabilities are selectable building blocks chosen after architecture."""
    from boro_gtm.research import registry

    contract = load_contract()
    capability_ids = set(contract.capability_ids)
    keys = {a.key for a in registry.RESEARCH_ATTRIBUTES}
    assert not (keys & capability_ids)
    # Nor may an attribute be named after one.
    for key in keys:
        assert not key.upper().startswith("CAP-")


def test_m3_cannot_populate_canonical_qualification():
    """§14: qualification is scored after a diagnostic call, never from research."""
    from boro_gtm.research import registry

    contract = load_contract()
    forbidden = set(contract.qualification_dimension_ids) | {
        "qualification_score", "qualification_route", "executive_sponsorship",
        "budget_procurement_fit", "economic_consequence",
    }
    keys = {a.key for a in registry.RESEARCH_ATTRIBUTES}
    assert not (keys & forbidden)
    assert contract.raw["qualification_when_scored"] == "after a diagnostic call"


def test_m3_cannot_set_a_commercial_level():
    """§15: classification follows approved architecture."""
    from boro_gtm.research import registry

    contract = load_contract()
    forbidden = {"commercial_level_final", "commercial_level_candidate"} | set(
        contract.classifier_dimension_ids
    )
    keys = {a.key for a in registry.RESEARCH_ATTRIBUTES}
    assert not (keys & forbidden)


def test_m3_carries_no_commercial_judgement_attribute():
    """The named anti-patterns from §7, restated as a test."""
    from boro_gtm.research import registry

    forbidden_fragments = (
        "score", "pain", "fragmentation", "maturity", "needs_", "bad_",
        "lead", "priority", "sales_ready", "recommend", "qualif",
    )
    offenders = [
        a.key for a in registry.RESEARCH_ATTRIBUTES
        if any(fragment in a.key.lower() for fragment in forbidden_fragments)
    ]
    assert offenders == []


def test_price_cannot_flow_backward_into_classification():
    """§16: scope drives classification drives economics drives price."""
    contract = load_contract()
    # The canonical wording is the authority. It reads "Never classify from
    # contract value alone" — the same principle in the Price Book's own
    # vocabulary, which is deal value rather than price or budget.
    anti_rule = contract.raw["classifier_anti_rule"].lower()
    assert "never classify" in anti_rule
    assert any(term in anti_rule for term in ("contract value", "price", "budget"))
    # And no M3 attribute observes ability to pay.
    from boro_gtm.research import registry

    for attribute in registry.RESEARCH_ATTRIBUTES:
        assert "budget" not in attribute.key
        assert "revenue" not in attribute.key


def test_the_outbound_standard_forbids_feature_selling():
    contract = load_contract()
    prevention = json.dumps(contract.raw["feature_selling_prevention"]).lower()
    assert "feature" in prevention
    rules = " ".join(contract.raw["outbound_rules"]).lower()
    assert "hypothesis" in rules, "outbound states consequence as hypothesis, not diagnosis"


def test_the_q2_evidence_object_is_a_projection_not_a_store():
    """§11: M3 keeps the rich ledger; Q2 is the commercial view over it."""
    contract = load_contract()
    assert contract.evidence_object_fields == [
        "source", "source_type", "date_observed", "fact", "confidence",
        "inference_allowed", "related_hypothesis",
    ]
    assert "inference" in contract.raw["evidence_object_rule"].lower()
