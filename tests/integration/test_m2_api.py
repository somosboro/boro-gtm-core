"""M2 read API and the two write endpoints it exposes.

Covers acceptance H1–H10. The API commits, so this module seeds through its
own committed session and truncates afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select, text

from boro_gtm.discovery import seeds
from boro_gtm.discovery.domain.models import (
    DiscoveryProvider,
)
from boro_gtm.discovery.providers.fixtures import (
    FixtureJsonDirectoryAdapter,
    json_record,
)
from boro_gtm.discovery.services import runs
from boro_gtm.discovery.services.projection import rebuild_projections
from tests.conftest import _ALL_TABLES

pytestmark = pytest.mark.integration

RECORDS = [
    json_record({"legal_name": "Schmidt Kältetechnik GmbH",
                 "website_domain": "schmidt-kaelte.de",
                 "employees": {"min": 40, "max": 60}, "founded": 1994,
                 "location": {"country": "DE", "city": "München"}},
                external_id="A"),
    json_record({"legal_name": "Nowak Elektro Sp. z o.o.",
                 "website_domain": "nowak-elektro.pl",
                 "location": {"country": "PL", "city": "Kraków"}},
                external_id="B"),
    # Same normalized name as the first, no domain: parks as AMBIGUOUS.
    json_record({"legal_name": "Schmidt Kältetechnik GmbH",
                 "location": {"country": "AT", "city": "Wien"}},
                external_id="C"),
]


@pytest.fixture
def seeded_api(api_client, committed_sessions) -> Iterator[tuple]:
    db = committed_sessions()
    db.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    seeds.seed_all(db)
    db.flush()
    provider = db.scalar(select(DiscoveryProvider).where(
        DiscoveryProvider.provider_key == FixtureJsonDirectoryAdapter.PROVIDER_KEY))

    adapter = FixtureJsonDirectoryAdapter(RECORDS[:2])
    run = runs.create_run(db, provider)
    runs.fetch(db, run, adapter, {"q": "test"})
    runs.normalize_run(db, run, adapter)
    runs.resolve_run(db, run, adapter)
    rebuild_projections(db)

    # A later run, against projections that now exist: the third record shares
    # a normalized name with the first and carries no domain, so it parks.
    later_adapter = FixtureJsonDirectoryAdapter(RECORDS[2:])
    later = runs.create_run(db, provider)
    runs.fetch(db, later, later_adapter, {"q": "test-2"})
    runs.normalize_run(db, later, later_adapter)
    runs.resolve_run(db, later, later_adapter)
    rebuild_projections(db)
    db.commit()
    run_id = str(run.id)
    db.close()

    yield api_client, run_id


def test_provider_listing_marks_fixtures_as_fixtures(seeded_api):
    client, _ = seeded_api
    body = client.get("/api/v1/discovery-providers").json()
    assert body
    assert all(p["is_fixture"] for p in body), (
        "no production provider has been selected; nothing may claim to be one"
    )


def test_attribute_registry_is_served_with_its_version(seeded_api):
    client, _ = seeded_api
    body = client.get("/api/v1/attribute-registry").json()
    assert body
    keys = {a["attribute_key"] for a in body}
    assert {"legal_name", "domain", "employee_count", "location"} <= keys
    assert {a["registry_version"] for a in body} == {"1.0"}, (
        "every definition states the registry version it belongs to"
    )


def test_company_listing_and_detail(seeded_api):
    client, _ = seeded_api
    companies = client.get("/api/v1/companies").json()
    assert len(companies) == 2, "the ambiguous record created no company"

    detail = client.get(f"/api/v1/companies/{companies[0]['id']}").json()
    assert detail["id"] == companies[0]["id"]
    assert detail["lifecycle_status"] == "ACTIVE"
    assert detail["canonical_name"], "the projection is joined onto the anchor"
    assert detail["primary_domain"]


def test_a_company_response_separates_value_from_confidence(seeded_api):
    client, _ = seeded_api
    companies = client.get("/api/v1/companies").json()
    claims = client.get(f"/api/v1/companies/{companies[0]['id']}/claims").json()
    assert claims
    for claim in claims:
        assert "fact_type" in claim and "confidence" in claim
        assert "availability" in claim
        if claim["availability"] == "NOT_AVAILABLE":
            assert claim["value_jsonb"] is None
            assert claim["fact_type"] is None


def test_claims_expose_their_provenance_chain(seeded_api):
    client, _ = seeded_api
    companies = client.get("/api/v1/companies").json()
    claims = client.get(f"/api/v1/companies/{companies[0]['id']}/claims").json()
    sourced = [c for c in claims if c.get("provider_record_version_id")]
    assert sourced, "a provider-derived claim must name the record version"
    version_id = sourced[0]["provider_record_version_id"]

    entities = client.get("/api/v1/discovery-runs").json()
    assert entities  # the run is listed
    chain_source = sourced[0]["resolution_decision_id"]
    assert chain_source, "a claim must name the decision that attributed it"
    assert version_id != chain_source


def test_ambiguous_queue_is_exposed_for_review(seeded_api):
    client, _ = seeded_api
    queue = client.get("/api/v1/entity-resolution/ambiguous").json()
    assert len(queue) == 1
    assert queue[0]["decision"] == "AMBIGUOUS"
    assert queue[0]["company_id"] is None


def test_a_human_decision_appends_to_the_chain(seeded_api):
    client, _ = seeded_api
    queue = client.get("/api/v1/entity-resolution/ambiguous").json()
    entity_id = queue[0]["provider_entity_id"]
    company_id = client.get("/api/v1/companies").json()[0]["id"]

    response = client.post("/api/v1/entity-resolution/decisions", json={
        "provider_entity_id": entity_id,
        "company_id": company_id,
        "decision": "MATCHED",
        "decided_by": "reviewer:test",
        "rationale": "Austrian branch of the same firm",
    })
    assert response.status_code == 201
    created = response.json()
    assert created["method"] == "HUMAN_REVIEW"
    assert created["supersedes_decision_id"] == queue[0]["id"]

    chain = client.get(
        f"/api/v1/provider-entities/{entity_id}/resolution-chain").json()
    assert [d["id"] for d in chain][-1] == created["id"]
    assert chain[0]["supersedes_decision_id"] is None
    # The superseded decision is unchanged, not rewritten.
    assert chain[0]["decision"] == "AMBIGUOUS"
    assert client.get("/api/v1/entity-resolution/ambiguous").json() == []


def test_an_invalid_decision_is_rejected_with_the_vocabulary(seeded_api):
    client, _ = seeded_api
    queue_entity = client.get(
        "/api/v1/entity-resolution/ambiguous").json()[0]["provider_entity_id"]
    response = client.post("/api/v1/entity-resolution/decisions", json={
        "provider_entity_id": queue_entity,
        "company_id": None,
        "decision": "PROBABLY",
        "decided_by": "reviewer:test",
        "rationale": "typo",
    })
    assert response.status_code in (400, 422)
    body = response.json()
    assert "MATCHED" in str(body)


def test_rebuild_endpoint_is_idempotent_over_the_api(seeded_api):
    client, _ = seeded_api
    before = client.get("/api/v1/companies").json()
    first = client.post("/api/v1/projections/rebuild").json()
    second = client.post("/api/v1/projections/rebuild").json()

    assert first["content_digests"] == second["content_digests"]
    assert first["row_counts"] == second["row_counts"]
    assert client.get("/api/v1/companies").json() == before

    listed = client.get("/api/v1/projections/runs").json()
    assert len(listed) >= 2


def test_run_evidence_is_navigable_from_run_to_bytes(seeded_api):
    client, run_id = seeded_api
    run = client.get(f"/api/v1/discovery-runs/{run_id}").json()
    assert run["status"] == "COMPLETED"

    queries = client.get(f"/api/v1/discovery-runs/{run_id}/queries").json()
    assert queries and queries[0]["page_number"] == 0

    versions = client.get(f"/api/v1/discovery-runs/{run_id}/versions").json()
    assert len(versions) == 2
    for version in versions:
        assert version["canonicalization_strategy"]
        assert version["canonicalization_version"]
        assert version["canonical_payload_hash"]
        assert version["body_count"] >= 1


def test_unknown_ids_return_404_not_an_empty_object(seeded_api):
    client, _ = seeded_api
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/v1/companies/{missing}").status_code == 404
    assert client.get(f"/api/v1/discovery-runs/{missing}").status_code == 404


def test_m0_endpoints_still_answer_unchanged(seeded_api):
    client, _ = seeded_api
    assert client.get("/api/v1/health").status_code == 200
    # M2 seeding wrote nothing into M1, so its collections stay empty here.
    markets = client.get("/api/v1/markets").json()
    assert markets["total"] == 0
    assert markets["results"] == []
