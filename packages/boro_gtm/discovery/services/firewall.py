"""The M1/M2 firewall (design §11).

Non-negotiable: **a discovery run never writes to M1.** Provider-discovered
company counts are not market-density facts. Coverage, indexing depth, language
handling and query phrasing all bias them — typically toward large
English-language markets, precisely the direction that would flatter a
predetermined conclusion.

This module holds no promotion path on purpose. There is nothing here that
could accidentally be called.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError

#: Every M0/M1 table. M2 reads them and writes none of them. The list is
#: exhaustive rather than a hand-picked subset, so a table added to M0/M1
#: later is protected by default, and the guard test fails if it is not.
M1_WRITE_PROTECTED_TABLES = (
    "market_observations",
    "observation_sources",
    "market_scores",
    "market_score_components",
    "score_runs",
    "market_size_estimates",
    "market_deep_dives",
    "market_competition_assessments",
    "market_vertical_profiles",
    "market_snapshots",
    "market_snapshot_categories",
    "market_categories",
    "markets",
    "sources",
    "scoring_models",
    "scoring_model_components",
    "research_gaps",
    "verticals",
    "icps",
    "offers",
    "channels",
)


class DensityPromotionNotImplementedError(GtmError):
    """Raised if anything attempts to promote discovery counts into M1.

    Promotion is explicit, human-authorised, PROXY-typed and
    methodology-bearing. None of that is implemented in M2, and a discovery run
    must never reach for it.
    """

    code = "DENSITY_PROMOTION_NOT_IMPLEMENTED"
    http_status = 501


def promote_discovery_count_to_market_observation(*args, **kwargs):  # noqa: ANN002, ANN003
    """Deliberately unimplemented (design §11, M2-ADR-005)."""
    raise DensityPromotionNotImplementedError(
        "Discovery counts are provider-biased and are not market density. "
        "Promotion to an M1 observation must be explicit, human-authorised, "
        "typed PROXY and carry methodology, provider, query definition, "
        "retrieval date and calibration. That path is not implemented in M2.",
        details={"milestone": "M2", "see": "docs/M2_COMPANY_DISCOVERY_DESIGN.md §11"},
    )


def m1_write_fingerprint(session: Session) -> dict[str, int]:
    """Row counts of every write-protected M1 table.

    Tests take this before and after a discovery run and assert it is
    unchanged, which is a stronger guarantee than inspecting call sites.
    """
    from sqlalchemy import text

    counts: dict[str, int] = {}
    for table in M1_WRITE_PROTECTED_TABLES:
        counts[table] = session.execute(
            text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed allowlist
        ).scalar_one()
    return counts
