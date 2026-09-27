"""The assertion fingerprint's lineage key, in isolation.

The frozen invariant (schema graph §4.2) is that a lineage is the sorted set of
`(source_id, artifact_id)` **evidence origins**. The implementation regressed to
artifact ids alone — which is revision 2's superseded model — and the
regression was invisible because the design document still carried the old
line. These tests state the invariant a third time, in code that fails.
"""

from __future__ import annotations

import pytest

from boro_gtm.research.policies import assertion_contract_hash, assertion_fingerprint

COMPANY = "11111111-1111-1111-1111-111111111111"
SOURCE_A = "aaaaaaaa-0000-0000-0000-000000000001"
SOURCE_B = "bbbbbbbb-0000-0000-0000-000000000002"
ARTIFACT_X = "xxxxxxxx-0000-0000-0000-00000000000a"
ARTIFACT_Y = "yyyyyyyy-0000-0000-0000-00000000000b"


def _fingerprint(origins, **overrides):
    kwargs = {
        "subject_company_id": COMPANY,
        "attribute_key": "emergency_service",
        "attribute_registry_version": "M3-1.0",
        "value": {"value": True},
        "unit": None,
        "fact_type": "FACT",
        "availability": "OBSERVED",
        "period_granularity": "UNDATED",
        "observed_at": None,
        "lineage_origins": origins,
        "contract_hash": assertion_contract_hash(),
    }
    kwargs.update(overrides)
    return assertion_fingerprint(**kwargs)


def test_two_sources_serving_one_document_are_two_lineages():
    """The whole reason the key is a pair and not an artifact id.

    Source A and source B both serving artifact X may carry different trust,
    publication context and dates. An artifact-only key merged them into one
    claim and discarded all three.
    """
    a = _fingerprint([(SOURCE_A, ARTIFACT_X)])
    b = _fingerprint([(SOURCE_B, ARTIFACT_X)])
    assert a != b


def test_one_source_serving_two_documents_are_two_lineages():
    a = _fingerprint([(SOURCE_A, ARTIFACT_X)])
    b = _fingerprint([(SOURCE_A, ARTIFACT_Y)])
    assert a != b


def test_the_lineage_key_is_order_independent():
    """A set, not a list: the order evidence arrived in is not identity."""
    forward = _fingerprint([(SOURCE_A, ARTIFACT_X), (SOURCE_B, ARTIFACT_Y)])
    backward = _fingerprint([(SOURCE_B, ARTIFACT_Y), (SOURCE_A, ARTIFACT_X)])
    assert forward == backward


def test_a_multi_origin_inference_is_one_lineage():
    """Three postings and a services page is one assertion, not four."""
    combined = _fingerprint([
        (SOURCE_A, ARTIFACT_X), (SOURCE_B, ARTIFACT_Y),
    ])
    assert combined != _fingerprint([(SOURCE_A, ARTIFACT_X)])
    assert combined != _fingerprint([(SOURCE_B, ARTIFACT_Y)])


def test_the_policy_contract_is_part_of_identity():
    """A recalibration re-asserts; it does not silently rewrite (M3-ADR-032)."""
    origins = [(SOURCE_A, ARTIFACT_X)]
    v1 = _fingerprint(origins)
    v2 = _fingerprint(origins, contract_hash=assertion_contract_hash("rule-v2"))
    assert v1 != v2


def test_unit_and_value_are_part_of_identity():
    origins = [(SOURCE_A, ARTIFACT_X)]
    base = _fingerprint(origins, attribute_key="technician_count",
                        value={"min": 40, "max": 40}, unit="PEOPLE")
    other_unit = _fingerprint(origins, attribute_key="technician_count",
                              value={"min": 40, "max": 40}, unit="FTE")
    other_value = _fingerprint(origins, attribute_key="technician_count",
                               value={"min": 41, "max": 41}, unit="PEOPLE")
    assert len({base, other_unit, other_value}) == 3


@pytest.mark.parametrize("field,value", [
    ("fact_type", "PROXY"),
    ("period_granularity", "DATE"),
    ("attribute_registry_version", "M3-2.0"),
])
def test_each_identity_input_changes_the_fingerprint(field, value):
    origins = [(SOURCE_A, ARTIFACT_X)]
    assert _fingerprint(origins) != _fingerprint(origins, **{field: value})
