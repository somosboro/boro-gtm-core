"""Deterministic ISO mapping."""

from __future__ import annotations

import pytest

from boro_gtm.core.errors import CountryResolutionError
from boro_gtm.market_intelligence.importers.countries import (
    known_countries,
    resolve_country,
)
from tests.fixtures.golden import EXPECTED_UNIVERSE_SIZE, REQUIRED_ISO_MAPPINGS


@pytest.mark.parametrize(("name", "expected"), sorted(REQUIRED_ISO_MAPPINGS.items()))
def test_required_iso_mappings(name: str, expected: tuple[str, str]) -> None:
    identity = resolve_country(name)
    assert (identity.iso2, identity.iso3) == expected


def test_mapping_covers_full_universe() -> None:
    assert len(known_countries()) == EXPECTED_UNIVERSE_SIZE


def test_iso_codes_are_unique() -> None:
    mapping = known_countries()
    iso2 = [v[0] for v in mapping.values()]
    iso3 = [v[1] for v in mapping.values()]
    assert len(set(iso2)) == len(iso2)
    assert len(set(iso3)) == len(iso3)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("usa", "United States"),
        ("U.S.A.", "United States"),
        ("uk", "United Kingdom"),
        ("UAE", "United Arab Emirates"),
        ("Czech Republic", "Czechia"),
        ("Republic of Korea", "South Korea"),
        ("  united   states  ", "United States"),
        ("türkiye", "Turkey"),
    ],
)
def test_aliases_and_whitespace(alias: str, canonical: str) -> None:
    assert resolve_country(alias).canonical_name == canonical


def test_case_insensitive() -> None:
    assert resolve_country("gErMaNy").iso2 == "DE"


@pytest.mark.parametrize("bad", ["", "   ", "Atlantis", "Republic of Nowhere", "XX"])
def test_unresolved_identity_is_fatal(bad: str) -> None:
    """An unknown market must never be silently accepted."""
    with pytest.raises(CountryResolutionError):
        resolve_country(bad)


def test_every_source_country_resolves(source_payload: dict) -> None:
    names = (
        [m["country"] for m in source_payload["markets"]]
        + [source_payload["home_market_benchmark"]["country"]]
        + [
            m["country"]
            for m in source_payload["excluded_from_top50_but_in_normalization_universe"]
        ]
    )
    assert len(names) == EXPECTED_UNIVERSE_SIZE
    resolved = {resolve_country(n).iso2 for n in names}
    assert len(resolved) == EXPECTED_UNIVERSE_SIZE
