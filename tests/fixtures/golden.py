"""Golden values from the supplied research artifact.

These live in tests only. Production logic must never contain them
(06 — Golden tests).
"""

from __future__ import annotations

#: iso2 -> (score, rank) for the published international top 50.
GOLDEN_SCORES: dict[str, tuple[float, int]] = {
    "US": (81.908, 1),
    "GB": (72.401, 2),
    "DE": (72.115, 3),
    "AU": (69.741, 4),
    "FR": (69.626, 5),
    "ES": (67.183, 9),
    "AE": (63.093, 17),
    "PL": (60.138, 23),
}

#: The home-market benchmark is scored but deliberately unranked.
GOLDEN_HOME_BENCHMARK: tuple[str, float] = ("CL", 42.616)

#: Required ISO mappings (03 — Country mapping).
REQUIRED_ISO_MAPPINGS: dict[str, tuple[str, str]] = {
    "United States": ("US", "USA"),
    "United Kingdom": ("GB", "GBR"),
    "Germany": ("DE", "DEU"),
    "Australia": ("AU", "AUS"),
    "Canada": ("CA", "CAN"),
    "France": ("FR", "FRA"),
    "Spain": ("ES", "ESP"),
    "Italy": ("IT", "ITA"),
    "United Arab Emirates": ("AE", "ARE"),
    "Poland": ("PL", "POL"),
    "Chile": ("CL", "CHL"),
    "Czechia": ("CZ", "CZE"),
    "South Korea": ("KR", "KOR"),
    "Taiwan": ("TW", "TWN"),
}

SCORE_TOLERANCE = 0.005

EXPECTED_UNIVERSE_SIZE = 63
EXPECTED_RANKED_MARKETS = 50
EXPECTED_HOME_BENCHMARKS = 1
EXPECTED_UNIVERSE_ONLY = 12
EXPECTED_SOURCES = 15

#: The 10-market initial commercial portfolio (00 — BoRo Studio context).
INITIAL_PORTFOLIO = ["US", "GB", "DE", "AU", "CA", "FR", "ES", "IT", "AE", "PL"]
