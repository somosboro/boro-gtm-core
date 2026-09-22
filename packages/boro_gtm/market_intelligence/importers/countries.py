"""Deterministic country-name to ISO 3166-1 resolution.

The importer never invents a code. An unmapped market name raises
:class:`~boro_gtm.core.errors.CountryResolutionError` and aborts the import, so a
new economy entering the universe is a conscious mapping decision rather than
a silently created duplicate registry row.
"""

from __future__ import annotations

from dataclasses import dataclass

from boro_gtm.core.errors import CountryResolutionError


@dataclass(frozen=True, slots=True)
class CountryIdentity:
    """A resolved market identity."""

    iso2: str
    iso3: str
    canonical_name: str


#: canonical name -> (alpha-2, alpha-3)
#: Covers the full 63-economy normalization universe of the 2026 snapshot.
_CANONICAL: dict[str, tuple[str, str]] = {
    "Australia": ("AU", "AUS"),
    "Austria": ("AT", "AUT"),
    "Bahrain": ("BH", "BHR"),
    "Belgium": ("BE", "BEL"),
    "Brazil": ("BR", "BRA"),
    "Bulgaria": ("BG", "BGR"),
    "Canada": ("CA", "CAN"),
    "Chile": ("CL", "CHL"),
    "China": ("CN", "CHN"),
    "Colombia": ("CO", "COL"),
    "Costa Rica": ("CR", "CRI"),
    "Croatia": ("HR", "HRV"),
    "Czechia": ("CZ", "CZE"),
    "Denmark": ("DK", "DNK"),
    "Estonia": ("EE", "EST"),
    "Finland": ("FI", "FIN"),
    "France": ("FR", "FRA"),
    "Germany": ("DE", "DEU"),
    "Greece": ("GR", "GRC"),
    "Hungary": ("HU", "HUN"),
    "India": ("IN", "IND"),
    "Indonesia": ("ID", "IDN"),
    "Ireland": ("IE", "IRL"),
    "Israel": ("IL", "ISR"),
    "Italy": ("IT", "ITA"),
    "Japan": ("JP", "JPN"),
    "Kazakhstan": ("KZ", "KAZ"),
    "Kuwait": ("KW", "KWT"),
    "Lithuania": ("LT", "LTU"),
    "Luxembourg": ("LU", "LUX"),
    "Malaysia": ("MY", "MYS"),
    "Mexico": ("MX", "MEX"),
    "Morocco": ("MA", "MAR"),
    "Netherlands": ("NL", "NLD"),
    "New Zealand": ("NZ", "NZL"),
    "Norway": ("NO", "NOR"),
    "Oman": ("OM", "OMN"),
    "Panama": ("PA", "PAN"),
    "Peru": ("PE", "PER"),
    "Philippines": ("PH", "PHL"),
    "Poland": ("PL", "POL"),
    "Portugal": ("PT", "PRT"),
    "Qatar": ("QA", "QAT"),
    "Romania": ("RO", "ROU"),
    "Russia": ("RU", "RUS"),
    "Saudi Arabia": ("SA", "SAU"),
    "Serbia": ("RS", "SRB"),
    "Singapore": ("SG", "SGP"),
    "Slovakia": ("SK", "SVK"),
    "Slovenia": ("SI", "SVN"),
    "South Africa": ("ZA", "ZAF"),
    "South Korea": ("KR", "KOR"),
    "Spain": ("ES", "ESP"),
    "Sweden": ("SE", "SWE"),
    "Switzerland": ("CH", "CHE"),
    # Taiwan: ISO 3166-1 assigns TW/TWN to "Taiwan, Province of China".
    # We use TW/TWN purely as an application identifier (03 — Country mapping).
    "Taiwan": ("TW", "TWN"),
    "Thailand": ("TH", "THA"),
    "Turkey": ("TR", "TUR"),
    "United Arab Emirates": ("AE", "ARE"),
    "United Kingdom": ("GB", "GBR"),
    "United States": ("US", "USA"),
    "Uruguay": ("UY", "URY"),
    "Vietnam": ("VN", "VNM"),
}

#: Accepted spelling variants -> canonical name. Deterministic, not fuzzy.
_ALIASES: dict[str, str] = {
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
    "korea (south)": "South Korea",
    "czech republic": "Czechia",
    "slovak republic": "Slovakia",
    "holland": "Netherlands",
    "russian federation": "Russia",
    "viet nam": "Vietnam",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "taiwan, province of china": "Taiwan",
    "chinese taipei": "Taiwan",
    "hellenic republic": "Greece",
    "state of israel": "Israel",
}

_BY_NORMALIZED: dict[str, str] = {name.casefold(): name for name in _CANONICAL}


def _normalize(raw: str) -> str:
    return " ".join(raw.strip().split()).casefold()


def resolve_country(name: str) -> CountryIdentity:
    """Resolve a source country name to a stable ISO identity.

    Raises:
        CountryResolutionError: if the name is unknown. The importer treats
            this as fatal — silently accepting an unresolved identity would
            corrupt the market registry.
    """
    if not name or not name.strip():
        raise CountryResolutionError("Empty market name cannot be resolved")

    normalized = _normalize(name)
    canonical = _BY_NORMALIZED.get(normalized) or _ALIASES.get(normalized)
    if canonical is None:
        raise CountryResolutionError(
            f"Unresolved market identity: {name!r}. "
            "Add an explicit entry to app/market_intelligence/importers/countries.py.",
            details={"country": name},
        )

    iso2, iso3 = _CANONICAL[canonical]
    return CountryIdentity(iso2=iso2, iso3=iso3, canonical_name=canonical)


def known_countries() -> dict[str, tuple[str, str]]:
    """Return a copy of the canonical mapping (for tests/diagnostics)."""
    return dict(_CANONICAL)
