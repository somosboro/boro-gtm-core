"""Canonical commercial ontology — the contract GTM Core aligns to.

Authority, in order: the Markdown Price Book, then its YAML machine companion,
then this repository. Where GTM Core conflicts with the Price Book, **GTM Core
changes** — commercial policy is never bent to preserve an implementation.

Neither canonical file is committed here. Both carry internal economics, and
the Price Book states its margin target "is an internal management target, not
a customer-facing claim"; this repository is public. What *is* committed is
``commercial/CANONICAL_CONTRACT.json``: the ontology identifiers and shape,
with every price, margin, floor and discount deliberately absent — and a test
that fails if any of them reappear.
"""

from boro_gtm.commercial.contract import (
    CanonicalContract,
    ContractDriftError,
    load_contract,
    validate_yaml_against_contract,
)

__all__ = [
    "CanonicalContract",
    "ContractDriftError",
    "load_contract",
    "validate_yaml_against_contract",
]
