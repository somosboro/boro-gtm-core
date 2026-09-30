"""Pure-function contracts for M2. No database, no network.

Covers acceptance A21–A28 and the design's purity requirements.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from boro_gtm.core.enums import FactType
from boro_gtm.discovery import registry
from boro_gtm.discovery.enums import (
    STRATEGY_MEDIA_TYPES,
    CanonicalizationStrategy,
    ProviderIdentityCapability,
)
from boro_gtm.discovery.providers.base import (
    CANONICALIZERS,
    ProviderCapabilities,
    canonical_csv_row_v1,
    canonical_json_v1,
)
from boro_gtm.discovery.providers.fixtures import FIXTURE_ADAPTERS
from boro_gtm.discovery.services.resolution import (
    DOMAIN_BLOCKLIST,
    is_identity_domain,
    normalize_domain,
    normalize_name,
)

PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "boro_gtm" / "discovery"


# --- domain normalization --------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("Acme.de", "acme.de"),
    ("www.acme.de", "acme.de"),
    ("https://www.acme.de/impressum?x=1", "acme.de"),
    ("shop.acme.de", "acme.de"),
    ("acme.de.", "acme.de"),
    ("acme.co.uk", "acme.co.uk"),
    ("shop.acme.co.uk", "acme.co.uk"),
    ("acme.com.au", "acme.com.au"),
    ("localhost", None),
    ("", None),
    (None, None),
])
def test_domain_normalization(raw, expected):
    assert normalize_domain(raw) == expected


def test_hosting_domains_never_carry_identity():
    for host in DOMAIN_BLOCKLIST:
        assert not is_identity_domain(f"someone.{host}")
    assert is_identity_domain("schmidt-kaelte.de")


@pytest.mark.parametrize(("raw", "expected"), [
    ("Schmidt Kältetechnik GmbH", "schmidt kältetechnik"),
    ("  ACME   Ltd. ", "acme"),
    ("Nowak Elektro Sp. z o.o.", "nowak elektro"),
    ("Bau  GmbH & Co. KG", "bau"),
    ("", None),
    (None, None),
])
def test_name_normalization_is_for_retrieval_only(raw, expected):
    assert normalize_name(raw) == expected


def test_normalized_names_of_different_firms_can_collide():
    """Which is exactly why a name match alone never merges."""
    assert normalize_name("Meier GmbH") == normalize_name("Meier Ltd")


# --- canonicalization ------------------------------------------------------


def test_json_canonicalization_ignores_formatting_and_key_order():
    a = {"b": 1, "a": {"y": 2, "x": [3, 4]}}
    b = json.loads(json.dumps(a, indent=4, sort_keys=False))
    assert canonical_json_v1(a) == canonical_json_v1(b)


def test_json_canonicalization_preserves_meaning():
    assert canonical_json_v1({"a": 1}) != canonical_json_v1({"a": "1"})
    assert canonical_json_v1({"a": 1}) != canonical_json_v1({"a": 2})


def test_json_canonicalization_refuses_nan():
    with pytest.raises(ValueError):
        canonical_json_v1({"a": float("nan")})


def test_csv_canonicalization_converges_on_column_order_and_spacing():
    a = {"Company": "Acme", "Domain": " acme.de ", "Zip": ""}
    b = {"domain": "acme.de", "zip": "  ", "company": "Acme"}
    assert canonical_csv_row_v1(a) == canonical_csv_row_v1(b)


def test_csv_canonicalization_treats_empty_as_null_not_zero():
    payload = json.loads(canonical_csv_row_v1({"staff": ""}).decode())
    assert payload["staff"] is None


def test_every_declared_strategy_has_an_implementation():
    for strategy in CanonicalizationStrategy:
        assert strategy.value in CANONICALIZERS
        assert strategy.value in STRATEGY_MEDIA_TYPES


def test_a_strategy_cannot_be_declared_for_a_media_type_it_does_not_fit():
    from boro_gtm.core.errors import ValidationError

    for strategy, media_types in STRATEGY_MEDIA_TYPES.items():
        wrong = next(
            m for other, ms in STRATEGY_MEDIA_TYPES.items() if other != strategy
            for m in ms if m not in media_types
        )
        with pytest.raises(ValidationError):
            ProviderCapabilities(
                provider_key="x", name="X",
                identity_capability=ProviderIdentityCapability.CONTENT_ONLY.value,
                canonicalization_strategy=strategy, canonicalization_version="1",
                media_type=wrong, normalizer_version="1",
            )


# --- attribute registry ----------------------------------------------------


def test_registry_rejects_a_fact_type_the_attribute_does_not_allow():
    definition = registry.get_definition("vertical")
    with pytest.raises(registry.AttributeNotInRegistryError):
        definition.validate({"vertical_id": "x"}, None, FactType.FACT.value)


def test_registry_rejects_an_unknown_unit():
    definition = registry.get_definition("employee_count")
    with pytest.raises(registry.AttributeNotInRegistryError):
        definition.validate({"min": 1, "max": 2}, "BANANAS", FactType.ESTIMATE.value)


def test_registry_rejects_an_inverted_range():
    definition = registry.get_definition("employee_count")
    with pytest.raises(registry.AttributeNotInRegistryError):
        definition.validate({"min": 90, "max": 10}, "PEOPLE", FactType.ESTIMATE.value)


def test_registry_rejects_an_unknown_attribute():
    with pytest.raises(registry.AttributeNotInRegistryError):
        registry.get_definition("favourite_colour")


def test_shadow_extraction_cannot_disagree_with_the_json_value():
    definition = registry.get_definition("employee_count")
    shadow = definition.extract_shadow({"min": 40, "max": 60})
    assert shadow["value_numeric"] == 40.0
    assert definition.extract_shadow(None)["value_numeric"] is None


def test_absence_extracts_no_shadow_at_all():
    for key in ("legal_name", "employee_count", "founded_year", "vertical"):
        shadow = registry.get_definition(key).extract_shadow(None)
        assert set(shadow.values()) == {None}


def test_a_closed_interval_must_not_end_before_it_starts():
    definition = registry.get_definition("market_presence")
    with pytest.raises(registry.AttributeNotInRegistryError):
        definition.validate(
            {"market_id": "11111111-1111-1111-1111-111111111111",
             "presence_type": "OPERATES",
             "valid_from": "2024-01-01", "valid_to": "2020-01-01"},
            None, FactType.INFERENCE.value)


# --- purity and scope ------------------------------------------------------


NETWORK_MODULES = {"requests", "httpx", "urllib3", "http", "socket",
                   "aiohttp", "ftplib", "smtplib", "telnetlib"}

#: `urllib` is only a network client through these submodules. `urllib.parse` is
#: string manipulation and nothing else, so importing it is not reaching the
#: network — an earlier version of this test counted it as an offence and
#: flagged a URL parser.
NETWORK_URLLIB_SUBMODULES = {"request", "error"}

#: The one module permitted a network client: the production provider whose
#: `search` is defined by the adapter contract as the only method that may do
#: I/O. Named explicitly, so adding a second one is a visible decision in this
#: file rather than a silent import somewhere else (M2-ADR-040).
NETWORK_PERMITTED = {"google_places.py"}


def _network_imports(path: Path) -> list[str]:
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            parts = module.split(".")
            root = parts[0]
            if root in NETWORK_MODULES:
                found.append(root)
            elif root == "urllib" and (
                len(parts) > 1 and parts[1] in NETWORK_URLLIB_SUBMODULES
            ):
                found.append(module)
    return found


def test_only_the_named_production_provider_imports_a_network_client():
    """Everything else in M2 stays unable to reach the network.

    The guarantee this protects is not "M2 has no provider" — it now has one —
    but that network access lives in exactly one file, so `parse`, `normalize`,
    `canonicalize`, `derive_key` and every service cannot quietly acquire it.
    """
    offenders = [
        f"{path.name}: {name}"
        for path in PACKAGE.rglob("*.py")
        if path.name not in NETWORK_PERMITTED
        for name in _network_imports(path)
    ]
    assert offenders == []


def test_url_parsing_is_not_counted_as_network_access():
    """Guards the detector itself, which used to flag `urllib.parse`."""
    assert _network_imports.__doc__ is None       # a helper, not a contract
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from urllib.parse import" in source:
            assert "urllib.parse" not in _network_imports(path)
            break
    else:                                          # pragma: no cover
        pytest.fail("no module imports urllib.parse; the guard tests nothing")


def test_the_production_provider_confines_its_io_to_search():
    """`search`/`search_page` may use the client. The pure stages may not.

    This is the part of the adapter contract that matters once a real provider
    exists: a `normalize` that fetched something would make normalization
    unrepeatable and canonical identity depend on the network.
    """
    path = PACKAGE / "providers" / "google_places.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    adapter = next(c for c in classes if c.name == "GooglePlacesAdapter")

    for method in (n for n in adapter.body if isinstance(n, ast.FunctionDef)):
        if method.name in ("search", "search_page", "__init__"):
            continue
        used = {
            ast.unparse(node) for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
        }
        assert not any("_client" in expr for expr in used), (
            f"{method.name} touches the HTTP client; only search may"
        )


def test_every_fixture_adapter_declares_itself_a_fixture():
    for adapter_cls in FIXTURE_ADAPTERS.values():
        caps = adapter_cls().capabilities()
        assert caps.is_fixture is True
        assert "TEST" in caps.name.upper()


def test_the_production_provider_is_not_registered_as_a_fixture():
    """`is_fixture` has to stay a real distinction once a real provider exists.

    A reader of `discovery_providers` should be able to tell which providers
    spend money from the flag, not by recognising a name.
    """
    from boro_gtm.discovery.providers.google_places import (
        PROVIDER_KEY,
        GooglePlacesAdapter,
    )

    caps = GooglePlacesAdapter().capabilities()
    assert caps.is_fixture is False
    assert PROVIDER_KEY not in FIXTURE_ADAPTERS
    assert "TEST" not in caps.name.upper()
    assert all(a().capabilities().is_fixture for a in FIXTURE_ADAPTERS.values())


def test_the_three_identity_capabilities_are_all_exercised():
    declared = {a().capabilities().identity_capability for a in FIXTURE_ADAPTERS.values()}
    assert declared == {c.value for c in ProviderIdentityCapability}


def test_fact_type_remains_exactly_five_members():
    assert [f.value for f in FactType] == [
        "FACT", "ESTIMATE", "PROXY", "INFERENCE", "HYPOTHESIS"]
    assert "N/D" not in {f.value for f in FactType}
