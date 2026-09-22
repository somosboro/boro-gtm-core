"""Schema and semantic validation of the source contract."""

from __future__ import annotations

import copy

import pytest

from boro_gtm.core.errors import ValidationError
from boro_gtm.market_intelligence.importers.contract import (
    parse_document,
    validate_semantics,
    validate_structure,
)


def test_supplied_document_validates(source_payload: dict) -> None:
    validate_structure(source_payload)
    report = validate_semantics(parse_document(source_payload))
    assert report.ok, report.errors


def test_supplied_document_emits_expected_warnings(source_payload: dict) -> None:
    """Missing optional evidence warns, never fails."""
    report = validate_semantics(parse_document(source_payload))
    assert report.warnings
    joined = " ".join(report.warnings)
    assert "TAM/SAM/SOM" in joined
    assert "software spending" in joined


def test_structure_rejects_wrong_market_count(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"] = payload["markets"][:49]
    with pytest.raises(ValidationError):
        validate_structure(payload)


def test_semantics_reject_duplicate_rank(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"][1]["rank"] = payload["markets"][0]["rank"]
    report = validate_semantics(parse_document(payload))
    assert not report.ok
    assert any("Duplicate ranks" in e for e in report.errors)


def test_semantics_reject_duplicate_market_names(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"][1]["country"] = payload["markets"][0]["country"]
    report = validate_semantics(parse_document(payload))
    assert any("Duplicate market names" in e for e in report.errors)


def test_semantics_reject_universe_size_mismatch(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["metadata"]["universe_size"] = 99
    report = validate_semantics(parse_document(payload))
    assert any("universe_size" in e for e in report.errors)


def test_semantics_reject_component_above_its_weight(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"][0]["subscores"]["outlook_5"] = 5.5
    with pytest.raises(ValidationError):
        validate_structure(payload)  # schema bounds catch it first

    payload = copy.deepcopy(source_payload)
    payload["markets"][0]["subscores"]["custom_component_10"] = 99.0
    report = validate_semantics(parse_document(payload))
    assert any("unknown subscore component" in w for w in report.warnings)


def test_semantics_reject_score_out_of_range(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["excluded_from_top50_but_in_normalization_universe"][0]["market_score"] = 140.0
    with pytest.raises(ValidationError):
        validate_structure(payload)


def test_semantics_reject_unknown_source_key(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"][0]["sources"].append("NOT_A_REAL_SOURCE")
    report = validate_semantics(parse_document(payload))
    assert any("unknown source keys" in e for e in report.errors)


def test_semantics_reject_invalid_fact_type(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"][0]["competition"]["fact_type"] = "VIBES"
    report = validate_semantics(parse_document(payload))
    assert any("invalid fact_type" in e for e in report.errors)


def test_nulls_survive_parsing(source_payload: dict) -> None:
    """A null raw metric must stay None, never become 0."""
    doc = parse_document(source_payload)
    missing = [
        m for m in doc.markets if m.raw.software_spending_2024_pct_gdp is None
    ]
    assert missing, "expected the dataset to contain unknown software spending"
    for market in missing:
        assert market.raw.software_spending_2024_pct_gdp is not None or True
        assert market.raw.software_spending_2024_pct_gdp is None


def test_subscores_sum_to_market_score(source_payload: dict) -> None:
    doc = parse_document(source_payload)
    for entry in doc.all_scored_markets:
        assert sum(entry.subscores.as_dict().values()) == pytest.approx(
            entry.market_score, abs=0.005
        )


# ---------------------------------------------------------------------------
# A-7 — the declared home-market benchmark must match the document
# ---------------------------------------------------------------------------


def test_home_benchmark_metadata_must_match_the_document(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["metadata"]["international_top50_excludes_home_market"] = "Peru"
    report = validate_semantics(parse_document(payload))
    assert not report.ok
    assert any(
        "international_top50_excludes_home_market" in e for e in report.errors
    ), report.errors


def test_home_benchmark_mismatch_detected_when_benchmark_changes(
    source_payload: dict,
) -> None:
    """The check is symmetric: changing the benchmark also trips it."""
    payload = copy.deepcopy(source_payload)
    payload["home_market_benchmark"]["country"] = "Uruguay"
    report = validate_semantics(parse_document(payload))
    assert any("international_top50_excludes_home_market" in e for e in report.errors)


def test_home_benchmark_accepts_a_spelling_variant(source_payload: dict) -> None:
    """ISO identity, not string equality — and no hardcoded country."""
    payload = copy.deepcopy(source_payload)
    payload["metadata"]["international_top50_excludes_home_market"] = "  chile  "
    report = validate_semantics(parse_document(payload))
    assert report.ok, report.errors


def test_home_benchmark_check_is_skipped_when_metadata_is_silent(
    source_payload: dict,
) -> None:
    payload = copy.deepcopy(source_payload)
    payload["metadata"].pop("international_top50_excludes_home_market", None)
    report = validate_semantics(parse_document(payload))
    assert report.ok, report.errors


def test_benchmark_validation_names_no_country_in_code() -> None:
    """The rule must be structural: no country literal in the validator."""
    from pathlib import Path

    source = Path(
        "packages/boro_gtm/market_intelligence/importers/contract.py"
    ).read_text(encoding="utf-8")
    for country in ("Chile", "CHL", '"CL"', "'CL'"):
        assert country not in source


def test_nd_is_accepted_on_input_but_is_not_a_fact_type(source_payload: dict) -> None:
    """The source token stays legal; the stored vocabulary does not include it."""
    from boro_gtm.core.enums import FactType

    report = validate_semantics(parse_document(source_payload))
    assert report.ok
    assert "N/D" not in {f.value for f in FactType}


def test_unknown_is_not_a_fact_type_token(source_payload: dict) -> None:
    payload = copy.deepcopy(source_payload)
    payload["markets"][0]["competition"]["fact_type"] = "UNKNOWN"
    report = validate_semantics(parse_document(payload))
    assert any("invalid fact_type" in e for e in report.errors)
