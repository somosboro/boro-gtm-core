"""API integration: clean DB -> migrate -> import -> score -> query."""

from __future__ import annotations

import pytest

from tests.conftest import SOURCE_JSON
from tests.fixtures.golden import (
    EXPECTED_SOURCES,
    EXPECTED_UNIVERSE_SIZE,
    GOLDEN_SCORES,
    INITIAL_PORTFOLIO,
    SCORE_TOLERANCE,
)

pytestmark = pytest.mark.integration

SNAPSHOT_KEY = "MI-2026-09-21-V1"
PREFIX = "/api/v1"


@pytest.fixture(scope="module")
def _unused():  # pragma: no cover - placeholder to keep module import cheap
    return None


@pytest.fixture
def loaded_client(api_client):
    """Full pipeline executed through the public entry points."""
    from boro_gtm.core.db import session_scope
    from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
    from boro_gtm.strategy.seeds.loader import seed_all

    with session_scope() as session:
        session.execute(_truncate_all())
    with session_scope() as session:
        SnapshotImporter(session).import_file(SOURCE_JSON)
    with session_scope() as session:
        seed_all(session)

    response = api_client.post(
        f"{PREFIX}/score-runs/base",
        json={"snapshot_key": SNAPSHOT_KEY, "mode": "reference_reproduction"},
    )
    assert response.status_code == 201, response.text
    api_client.base_run_id = response.json()["id"]
    return api_client


def _truncate_all():
    from sqlalchemy import text

    tables = [
        "market_score_components", "market_scores", "score_runs",
        "observation_sources", "market_observations", "market_snapshot_categories",
        "market_competition_assessments", "market_size_estimates",
        "market_deep_dives", "market_vertical_profiles", "research_gaps",
        "scoring_model_components", "scoring_models", "market_snapshots",
        "markets", "sources", "verticals", "icps", "offers", "channels",
    ]
    return text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")


# --- health ---------------------------------------------------------------


def test_health(api_client) -> None:
    body = api_client.get(f"{PREFIX}/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


def test_openapi_is_generated(api_client) -> None:
    spec = api_client.get(f"{PREFIX}/openapi.json").json()
    assert spec["openapi"].startswith("3.")
    assert f"{PREFIX}/contextual-rankings" in spec["paths"]


# --- M0 reads -------------------------------------------------------------


def test_snapshot_endpoints(loaded_client) -> None:
    listing = loaded_client.get(f"{PREFIX}/market-intelligence/snapshots").json()
    assert len(listing) == 1
    assert listing[0]["key"] == SNAPSHOT_KEY
    assert len(listing[0]["sha256"]) == 64

    detail = loaded_client.get(
        f"{PREFIX}/market-intelligence/snapshots/{SNAPSHOT_KEY}"
    ).json()
    assert detail["universe_size"] == EXPECTED_UNIVERSE_SIZE
    assert detail["counts"]["markets"] == EXPECTED_UNIVERSE_SIZE
    assert detail["counts"]["sources"] == EXPECTED_SOURCES
    assert any(r["kind"] == "imported_reference" for r in detail["score_runs"])


def test_markets_listing_and_filters(loaded_client) -> None:
    body = loaded_client.get(f"{PREFIX}/markets", params={"limit": 200}).json()
    assert body["total"] == EXPECTED_UNIVERSE_SIZE
    assert len(body["results"]) == EXPECTED_UNIVERSE_SIZE

    home = loaded_client.get(f"{PREFIX}/markets", params={"home_market": True}).json()
    assert home["total"] == 1
    assert home["results"][0]["iso2"] == "CL"

    giant = loaded_client.get(
        f"{PREFIX}/markets", params={"category": "GIANT_MARKET", "limit": 200}
    ).json()
    assert giant["total"] > 0
    assert all("GIANT_MARKET" in m["categories"] for m in giant["results"])

    region = loaded_client.get(
        f"{PREFIX}/markets", params={"region": "North America"}
    ).json()
    assert {m["iso2"] for m in region["results"]} == {"US", "CA"}


def test_markets_pagination_does_not_lose_or_duplicate(loaded_client) -> None:
    seen = []
    for offset in range(0, EXPECTED_UNIVERSE_SIZE, 10):
        page = loaded_client.get(
            f"{PREFIX}/markets", params={"limit": 10, "offset": offset}
        ).json()
        seen.extend(m["iso2"] for m in page["results"])
    assert len(seen) == EXPECTED_UNIVERSE_SIZE
    assert len(set(seen)) == EXPECTED_UNIVERSE_SIZE


def test_min_score_filter_requires_a_run(loaded_client) -> None:
    body = loaded_client.get(
        f"{PREFIX}/markets",
        params={"min_score": 70, "score_run": loaded_client.base_run_id, "limit": 200},
    ).json()
    assert body["total"] >= 3
    for market in body["results"]:
        assert market["latest_score"]["score"] >= 70


def test_market_detail_and_observations(loaded_client) -> None:
    market = loaded_client.get(f"{PREFIX}/markets/us").json()
    assert market["iso2"] == "US"
    assert market["iso3"] == "USA"
    assert market["competition"]["level"] == "VERY_HIGH"
    assert market["competition"]["included_in_score"] is False

    observations = loaded_client.get(f"{PREFIX}/markets/US/observations").json()
    assert observations
    by_metric = {o["metric_key"]: o for o in observations}
    assert by_metric["gdp_nominal_usd_bn"]["value_numeric"] == pytest.approx(30770)
    assert by_metric["gdp_nominal_usd_bn"]["period_label"] == "2025"
    assert by_metric["gdp_nominal_usd_bn"]["sources"]


def test_null_observation_is_served_as_null(loaded_client) -> None:
    """Singapore has no observed software-spending figure in this snapshot."""
    observations = loaded_client.get(
        f"{PREFIX}/markets/SG/observations",
        params={"metric_key": "software_spending_pct_gdp"},
    ).json()
    assert len(observations) == 1
    assert observations[0]["value_numeric"] is None
    assert observations[0]["fact_type"] == "N/D"


def test_observation_filters(loaded_client) -> None:
    filtered = loaded_client.get(
        f"{PREFIX}/markets/US/observations", params={"fact_type": "FACT"}
    ).json()
    assert filtered
    assert {o["fact_type"] for o in filtered} == {"FACT"}


def test_market_sources_expose_attribution(loaded_client) -> None:
    body = loaded_client.get(f"{PREFIX}/markets/US/sources").json()
    assert body["sources"]
    assert "attribution_legend" in body
    attributions = {
        m["attribution"] for s in body["sources"] for m in s["metrics"]
    }
    assert attributions <= {"EXPLICIT", "METRIC_HINT", "MARKET_LEVEL"}


def test_market_size_present_and_absent(loaded_client) -> None:
    present = loaded_client.get(f"{PREFIX}/markets/US/market-size").json()
    assert len(present) == 1
    assert present[0]["sam_min"] == pytest.approx(50000)
    assert present[0]["warning"]

    absent = loaded_client.get(f"{PREFIX}/markets/PL/market-size").json()
    assert absent == []


def test_deep_dive_present_and_absent(loaded_client) -> None:
    present = loaded_client.get(f"{PREFIX}/markets/US/deep-dive").json()
    assert len(present) == 1
    assert "Commercial HVAC/mechanical" in present[0]["priority_verticals"]
    assert loaded_client.get(f"{PREFIX}/markets/PL/deep-dive").json() == []


def test_sources_endpoints(loaded_client) -> None:
    listing = loaded_client.get(f"{PREFIX}/sources").json()
    assert len(listing) == EXPECTED_SOURCES
    detail = loaded_client.get(f"{PREFIX}/sources/IMF_WEO_2026_04").json()
    assert detail["title"].startswith("IMF World Economic Outlook")
    assert detail["used_for"]


def test_scoring_model_endpoints(loaded_client) -> None:
    models = loaded_client.get(f"{PREFIX}/scoring-models").json()
    keys = {m["key"] for m in models}
    assert {"market-attractiveness", "contextual-market-fit"} <= keys

    detail = loaded_client.get(
        f"{PREFIX}/scoring-models/market-attractiveness/1.0"
    ).json()
    assert sum(c["weight"] for c in detail["components"]) == 100.0
    assert detail["definition"]["percentile_method"] == "midrank_over_n_minus_1"


# --- scoring --------------------------------------------------------------


def test_ranking_reproduces_golden_values(loaded_client) -> None:
    body = loaded_client.get(
        f"{PREFIX}/score-runs/{loaded_client.base_run_id}/ranking",
        params={"limit": 100},
    ).json()
    assert body["total"] == 50
    by_iso = {r["market"]["iso2"]: r for r in body["results"]}
    for iso2, (score, rank) in GOLDEN_SCORES.items():
        assert by_iso[iso2]["score"] == pytest.approx(score, abs=SCORE_TOLERANCE)
        assert by_iso[iso2]["rank"] == rank


def test_ranking_pagination_preserves_order(loaded_client) -> None:
    first = loaded_client.get(
        f"{PREFIX}/score-runs/{loaded_client.base_run_id}/ranking",
        params={"limit": 10, "offset": 0},
    ).json()
    second = loaded_client.get(
        f"{PREFIX}/score-runs/{loaded_client.base_run_id}/ranking",
        params={"limit": 10, "offset": 10},
    ).json()
    assert [r["rank"] for r in first["results"]] == list(range(1, 11))
    assert [r["rank"] for r in second["results"]] == list(range(11, 21))


def test_ranking_excludes_unranked_by_default(loaded_client) -> None:
    default = loaded_client.get(
        f"{PREFIX}/score-runs/{loaded_client.base_run_id}/ranking",
        params={"limit": 200},
    ).json()
    everything = loaded_client.get(
        f"{PREFIX}/score-runs/{loaded_client.base_run_id}/ranking",
        params={"limit": 200, "include_unranked": True},
    ).json()
    assert default["total"] == 50
    assert everything["total"] == 51
    assert "CL" in {r["market"]["iso2"] for r in everything["results"]}


def test_market_score_detail_has_components(loaded_client) -> None:
    body = loaded_client.get(
        f"{PREFIX}/score-runs/{loaded_client.base_run_id}/markets/US"
    ).json()
    assert body["score"] == pytest.approx(81.908, abs=SCORE_TOLERANCE)
    assert body["rank"] == 1
    assert len(body["components"]) == 7
    assert all(c["explanation"] for c in body["components"])


def test_score_runs_listing_and_filters(loaded_client) -> None:
    runs = loaded_client.get(f"{PREFIX}/score-runs").json()
    kinds = {r["kind"] for r in runs}
    assert {"imported_reference", "reference_reproduction"} <= kinds

    filtered = loaded_client.get(
        f"{PREFIX}/score-runs", params={"kind": "reference_reproduction"}
    ).json()
    assert all(r["kind"] == "reference_reproduction" for r in filtered)


def test_native_run_via_api(loaded_client) -> None:
    response = loaded_client.post(
        f"{PREFIX}/score-runs/base",
        json={"snapshot_key": SNAPSHOT_KEY, "mode": "native_recalculation"},
    )
    assert response.status_code == 201
    run_id = response.json()["id"]
    ranking = loaded_client.get(
        f"{PREFIX}/score-runs/{run_id}/ranking", params={"limit": 200}
    ).json()
    assert all(r["coverage"] == pytest.approx(0.8) for r in ranking["results"])


# --- M1 -------------------------------------------------------------------


def test_strategy_registries(loaded_client) -> None:
    assert len(loaded_client.get(f"{PREFIX}/verticals").json()) == 9
    assert len(loaded_client.get(f"{PREFIX}/icps").json()) == 1
    assert len(loaded_client.get(f"{PREFIX}/offers").json()) == 4
    assert len(loaded_client.get(f"{PREFIX}/channels").json()) == 6


def test_market_vertical_profiles(loaded_client) -> None:
    listing = loaded_client.get(f"{PREFIX}/markets/US/verticals").json()
    assert listing
    keys = {p["vertical_key"] for p in listing}
    assert "commercial_hvac" in keys

    detail = loaded_client.get(f"{PREFIX}/markets/US/verticals/commercial_hvac").json()
    assert detail["evidence"]["derived_from"] == "snapshot_deep_dive.priority_verticals"
    assert detail["coverage"] is not None

    missing = loaded_client.get(f"{PREFIX}/markets/PL/verticals/commercial_hvac")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_contextual_ranking_endpoint(loaded_client) -> None:
    response = loaded_client.post(
        f"{PREFIX}/contextual-rankings",
        json={
            "snapshot_key": SNAPSHOT_KEY,
            "vertical_key": "commercial_hvac",
            "icp_key": "boro_field_service_midmarket_v1",
            "offer_key": "operations_architecture_sprint",
            "channel_key": "multichannel",
            "ticket_usd": 3000,
            "market_iso2": INITIAL_PORTFOLIO,
            "allow_low_coverage": False,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["model"] == "contextual-market-fit:1.0"
    assert len(body["results"]) == len(INITIAL_PORTFOLIO)
    assert body["coverage_policy"]["missing_data_policy"] == "renormalize_to_covered_weight"

    for result in body["results"]:
        # The three dimensions are returned separately and never collapsed.
        assert {"score", "confidence", "coverage"} <= set(result)
        assert result["confidence"] != result["coverage"] or result["coverage"] in (0.0, 1.0)
        assert result["components"]

    ranked = [r for r in body["results"] if r["rank"] is not None]
    assert [r["rank"] for r in ranked] == sorted(r["rank"] for r in ranked)


def test_contextual_missing_evidence_surfaces_gaps(loaded_client) -> None:
    body = loaded_client.post(
        f"{PREFIX}/contextual-rankings",
        json={
            "snapshot_key": SNAPSHOT_KEY,
            "vertical_key": "commercial_hvac",
            "channel_key": "multichannel",
            "ticket_usd": 3000,
            "market_iso2": ["US", "PL"],
        },
    ).json()
    by_iso = {r["market"]["iso2"]: r for r in body["results"]}
    assert by_iso["PL"]["coverage"] < by_iso["US"]["coverage"]
    assert by_iso["PL"]["score"] > 0
    assert by_iso["PL"]["research_gaps"]
    assert by_iso["PL"]["notes"]


def test_insufficient_coverage_is_an_error_contract(loaded_client) -> None:
    response = loaded_client.post(
        f"{PREFIX}/contextual-rankings",
        json={
            "snapshot_key": SNAPSHOT_KEY,
            "vertical_key": "commercial_hvac",
            "channel_key": "multichannel",
            "market_iso2": ["PL"],
            "min_coverage": 0.99,
            "allow_low_coverage": False,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_COVERAGE"

    allowed = loaded_client.post(
        f"{PREFIX}/contextual-rankings",
        json={
            "snapshot_key": SNAPSHOT_KEY,
            "vertical_key": "commercial_hvac",
            "channel_key": "multichannel",
            "market_iso2": ["PL"],
            "min_coverage": 0.99,
            "allow_low_coverage": True,
        },
    )
    assert allowed.status_code == 201
    assert allowed.json()["results"][0]["rank"] == 1


def test_research_gap_endpoints(loaded_client) -> None:
    loaded_client.post(
        f"{PREFIX}/contextual-rankings",
        json={
            "snapshot_key": SNAPSHOT_KEY,
            "vertical_key": "commercial_hvac",
            "channel_key": "multichannel",
            "ticket_usd": 3000,
            "market_iso2": ["PL"],
        },
    )
    gaps = loaded_client.get(f"{PREFIX}/research-gaps").json()
    assert gaps
    assert {g["status"] for g in gaps} == {"OPEN"}

    filtered = loaded_client.get(
        f"{PREFIX}/research-gaps", params={"market": "PL", "status": "OPEN"}
    ).json()
    assert filtered
    assert all(g["market_iso2"] == "PL" for g in filtered)

    updated = loaded_client.post(
        f"{PREFIX}/research-gaps/{filtered[0]['id']}/status", json={"status": "RESOLVED"}
    ).json()
    assert updated["status"] == "RESOLVED"


# --- error contract -------------------------------------------------------


def test_error_contract_shape(loaded_client) -> None:
    missing_market = loaded_client.get(f"{PREFIX}/markets/ZZ")
    assert missing_market.status_code == 404
    payload = missing_market.json()
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == "MARKET_NOT_FOUND"

    missing_snapshot = loaded_client.get(
        f"{PREFIX}/market-intelligence/snapshots/NOPE"
    )
    assert missing_snapshot.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"

    missing_model = loaded_client.get(f"{PREFIX}/scoring-models/nope/9.9")
    assert missing_model.json()["error"]["code"] == "MODEL_NOT_FOUND"

    bad_request = loaded_client.post(f"{PREFIX}/score-runs/base", json={})
    assert bad_request.status_code == 422
    assert bad_request.json()["error"]["code"] == "VALIDATION_ERROR"
