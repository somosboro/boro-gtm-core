"""Load and validate the canonical commercial contract.

Two distinct checks, deliberately separate:

* :func:`load_contract` validates the checked-in contract's **internal
  consistency** — that its declared counts match its own lists. This runs
  everywhere, including CI, with no access to the canonical sources.
* :func:`validate_yaml_against_contract` validates a canonical YAML **at an
  external path** against the contract. It runs only where that file is
  available, and it is how drift is actually caught.

The second is what §18 means by a drift gate. The first is what keeps the
committed contract from rotting on its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boro_gtm.core.errors import GtmError

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "commercial" / "CANONICAL_CONTRACT.json"

#: Substrings that must never appear as keys in the committed contract. The
#: canonical sources carry internal economics; this repository is public.
FORBIDDEN_KEY_FRAGMENTS = (
    "price", "margin", "floor", "discount", "usd", "cost", "revenue",
    "rate_card", "economics",
)

#: …with these exceptions. A digest of a file named "price book" reveals
#: nothing about its contents, and the Q1 field *names* are ontology labels
#: GTM Core must know in order to refuse to populate them — knowing a field is
#: called ``price_floor_usd`` discloses nothing; knowing it equals 25000 would.
ALLOWED_KEYS = frozenset({"price_book_sha256"})

#: The contract may contain only these top-level keys. A new one has to be
#: added deliberately, which is the point: it forces a second look at whether
#: what is being committed belongs in a public repository.
ALLOWED_TOP_LEVEL = frozenset({
    "contract_version", "canonical_semantic_version", "canonical_effective_date",
    "authority", "source_digests", "counts", "capability_ids",
    "commercial_levels", "capability_matrix_buckets", "sales_motion_stages_ordered",
    "qualification_dimension_ids", "qualification_score_range",
    "qualification_when_scored", "qualification_routes", "classifier_dimension_ids",
    "classifier_score_range", "classifier_rule", "classifier_anti_rule",
    "core_gates", "evidence_signal_ids", "evidence_signals",
    "account_opportunity_minimum_fields", "evidence_object_fields",
    "evidence_object_rule", "product_ids", "intervention_mode_ids",
    "founder_decision_ids", "outbound_rules", "feature_selling_prevention",
})


class ContractDriftError(GtmError):
    """The canonical contract and the canonical source disagree."""

    code = "CANONICAL_CONTRACT_DRIFT"
    http_status = 500


class ContractLeakError(GtmError):
    """The committed contract carries something it must not."""

    code = "CANONICAL_CONTRACT_LEAK"
    http_status = 500


@dataclass(frozen=True, slots=True)
class CanonicalContract:
    """The non-sensitive shape of the canonical commercial ontology."""

    raw: dict[str, Any]

    # -- identifiers -----------------------------------------------------
    @property
    def capability_ids(self) -> list[str]:
        return list(self.raw["capability_ids"])

    @property
    def evidence_signal_ids(self) -> list[str]:
        return list(self.raw["evidence_signal_ids"])

    @property
    def sales_motion_stages(self) -> list[str]:
        return list(self.raw["sales_motion_stages_ordered"])

    @property
    def qualification_dimension_ids(self) -> list[str]:
        return list(self.raw["qualification_dimension_ids"])

    @property
    def classifier_dimension_ids(self) -> list[str]:
        return list(self.raw["classifier_dimension_ids"])

    @property
    def account_fields(self) -> list[str]:
        return list(self.raw["account_opportunity_minimum_fields"])

    @property
    def evidence_object_fields(self) -> list[str]:
        return list(self.raw["evidence_object_fields"])

    @property
    def counts(self) -> dict[str, int]:
        return dict(self.raw["counts"])

    def evidence_signal(self, evidence_id: str) -> dict[str, Any]:
        for signal in self.raw["evidence_signals"]:
            if signal["evidence_id"] == evidence_id:
                return signal
        raise ContractDriftError(
            f"{evidence_id!r} is not a canonical evidence signal",
            details={"known": self.evidence_signal_ids},
        )


def _assert_no_economics(node: Any, path: str = "") -> list[str]:
    """Walk the contract and report any key that looks like economics."""
    leaks: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            lowered = str(key).lower()
            if key not in ALLOWED_KEYS and any(
                fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS
            ):
                leaks.append(here)
            leaks += _assert_no_economics(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            leaks += _assert_no_economics(value, f"{path}[{index}]")
    return leaks


def load_contract(path: Path | None = None) -> CanonicalContract:
    """Load the committed contract and check it against itself."""
    source = path or CONTRACT_PATH
    if not source.exists():
        raise ContractDriftError(
            f"the canonical contract is missing at {source}",
            details={"path": str(source)},
        )
    raw = json.loads(source.read_text(encoding="utf-8"))

    unexpected = sorted(set(raw) - ALLOWED_TOP_LEVEL)
    if unexpected:
        raise ContractLeakError(
            "the canonical contract gained a top-level key that has not been "
            "reviewed for public disclosure",
            details={"keys": unexpected, "allowed": sorted(ALLOWED_TOP_LEVEL)},
        )

    leaks = _assert_no_economics(raw)
    if leaks:
        raise ContractLeakError(
            "the committed canonical contract carries commercial economics, "
            "which must stay out of a public repository",
            details={"keys": leaks},
        )

    counts = raw["counts"]
    derived = {
        "capabilities": len(raw["capability_ids"]),
        "sales_motion_stages": len(raw["sales_motion_stages_ordered"]),
        "qualification_dimensions": len(raw["qualification_dimension_ids"]),
        "classifier_dimensions": len(raw["classifier_dimension_ids"]),
        "account_opportunity_minimum_fields": len(raw["account_opportunity_minimum_fields"]),
        "evidence_object_fields": len(raw["evidence_object_fields"]),
        "products": len(raw["product_ids"]),
        "intervention_modes": len(raw["intervention_mode_ids"]),
        "founder_decisions_required": len(raw["founder_decision_ids"]),
        "evidence_signals": len(raw["evidence_signal_ids"]),
        "commercial_levels": len(raw["commercial_levels"]),
        "hard_transformation_overrides": counts["hard_transformation_overrides"],
    }
    disagreements = {
        key: (counts.get(key), value)
        for key, value in derived.items()
        if counts.get(key) != value
    }
    if disagreements:
        raise ContractDriftError(
            "the contract's declared counts disagree with its own lists",
            details={"declared_vs_derived": disagreements},
        )
    if len(set(raw["capability_ids"])) != len(raw["capability_ids"]):
        raise ContractDriftError("duplicate capability ids in the contract")
    if len(set(raw["evidence_signal_ids"])) != len(raw["evidence_signal_ids"]):
        raise ContractDriftError("duplicate evidence signal ids in the contract")
    return CanonicalContract(raw=raw)


def validate_yaml_against_contract(
    yaml_path: Path, contract: CanonicalContract | None = None
) -> dict[str, Any]:
    """Validate a canonical YAML against the committed contract.

    Returns a report. Raises :class:`ContractDriftError` on any disagreement,
    which is what makes this a gate rather than a diagnostic.
    """
    import yaml as _yaml

    contract = contract or load_contract()
    document = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    problems: list[str] = []

    capability_ids = sorted(c["capability_id"] for c in document["capabilities"])
    if capability_ids != contract.capability_ids:
        missing = sorted(set(contract.capability_ids) - set(capability_ids))
        extra = sorted(set(capability_ids) - set(contract.capability_ids))
        problems.append(f"capabilities differ (missing={missing}, extra={extra})")

    # Every commercial level must place every capability exactly once.
    for level, buckets in document["capability_matrix"].items():
        placed = [c for values in buckets.values() if isinstance(values, list) for c in values]
        if sorted(placed) != contract.capability_ids:
            problems.append(
                f"capability_matrix[{level}] does not cover all "
                f"{len(contract.capability_ids)} capabilities exactly once "
                f"(n={len(placed)}, unique={len(set(placed))})"
            )

    stages = [s["stage"] for s in document["sales_motion"]]
    if stages != contract.sales_motion_stages:
        problems.append(f"sales motion order changed: {stages}")

    rubric = document["qualification"]["rubric"]
    qual_ids = [x["dimension_id"] for x in rubric["dimensions"]]
    if qual_ids != contract.qualification_dimension_ids:
        problems.append(f"qualification dimensions changed: {qual_ids}")
    low, high = contract.raw["qualification_score_range"]
    if [rubric["score_min_per_dimension"], rubric["score_max_per_dimension"]] != [low, high]:
        problems.append("qualification score range changed")
    for dimension in rubric["dimensions"]:
        if sorted(str(k) for k in dimension["scores"]) != [str(n) for n in range(low, high + 1)]:
            problems.append(f"qualification dimension {dimension['dimension_id']} "
                            f"lacks a full {low}-{high} rubric")

    classifier = document["commercial_level_classifier"]
    class_ids = [x["dimension_id"] for x in classifier["dimensions"]]
    if class_ids != contract.classifier_dimension_ids:
        problems.append(f"classifier dimensions changed: {class_ids}")
    clow, chigh = contract.raw["classifier_score_range"]
    for dimension in classifier["dimensions"]:
        if sorted(str(k) for k in dimension["scores"]) != [str(n) for n in range(clow, chigh + 1)]:
            problems.append(f"classifier dimension {dimension['dimension_id']} "
                            f"lacks a full {clow}-{chigh} rubric")
    gates = classifier["classification_rules"]["BORO-OS-C"]["gates"]
    if list(gates) != list(contract.raw["core_gates"]):
        problems.append("Core gates changed")
    if len(classifier["hard_transformation_overrides"]) != contract.counts[
        "hard_transformation_overrides"
    ]:
        problems.append("Transformation overrides changed")

    signal_ids = [s["evidence_id"] for s in document["evidence_signals"]]
    if signal_ids != contract.evidence_signal_ids:
        problems.append(f"evidence signals changed: {signal_ids}")

    model = document["account_opportunity_model"]
    if list(model["minimum_fields"]) != contract.account_fields:
        problems.append("Q1 minimum fields changed")
    if list(model["evidence_object"]["fields"]) != contract.evidence_object_fields:
        problems.append("Q2 evidence object fields changed")

    for label, key, actual in (
        ("products", "products", [p["product_id"] for p in document["products"]]),
        ("intervention modes", "intervention_mode_ids",
         [m["mode_id"] for m in document["intervention_modes"]]),
        ("founder decisions", "founder_decision_ids",
         [f["id"] for f in document["founder_decisions_required"]]),
    ):
        expected = contract.raw["product_ids"] if key == "products" else contract.raw[key]
        if list(actual) != list(expected):
            problems.append(f"{label} changed: {actual}")

    digest = hashlib.sha256(yaml_path.read_bytes()).hexdigest()
    recorded = contract.raw["source_digests"]["gtm_schema_sha256"]

    if problems:
        raise ContractDriftError(
            "the canonical YAML has drifted from the committed contract",
            details={"problems": problems, "yaml_sha256": digest,
                     "contract_expects_sha256": recorded},
        )
    return {
        "validated": str(yaml_path),
        "yaml_sha256": digest,
        "matches_recorded_digest": digest == recorded,
        "counts": contract.counts,
    }
