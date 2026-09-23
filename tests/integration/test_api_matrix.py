"""An API smoke matrix generated from OpenAPI, not hand-picked.

Two things this protects:

* **No undocumented 500.** Every implemented operation is probed with a valid
  request, a well-formed-but-absent identifier and a malformed one.
* **Referential consistency.** An identifier a list endpoint just returned
  must work on the detail endpoint and on every subresource. A list that
  hands out ids its own detail route rejects is worse than an empty list.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration

ABSENT_UUID = "00000000-0000-0000-0000-000000000000"
MALFORMED = "not-a-uuid"
COMPANY_SUBRESOURCES = (
    "claims", "locations", "market-presences", "verticals", "relationships",
)


@pytest.fixture
def populated(api_client, migrated_engine):
    """A committed database holding M0, M1 and M2 data for the matrix."""
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from boro_gtm.discovery import seeds as discovery_seeds
    from boro_gtm.discovery.domain.models import DiscoveryProvider
    from boro_gtm.discovery.providers.fixtures import (
        FixtureJsonDirectoryAdapter,
        json_record,
    )
    from boro_gtm.discovery.services import runs
    from boro_gtm.discovery.services.projection import rebuild_projections
    from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
    from boro_gtm.strategy.seeds.loader import seed_all as seed_strategy
    from tests.conftest import _ALL_TABLES, SOURCE_JSON

    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    db = factory()
    db.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    seed_strategy(db)
    SnapshotImporter(db).import_file(SOURCE_JSON)
    discovery_seeds.seed_all(db)
    db.flush()
    provider = db.scalar(
        select(DiscoveryProvider).where(
            DiscoveryProvider.provider_key == FixtureJsonDirectoryAdapter.PROVIDER_KEY
        )
    )
    records = [
        json_record(
            {
                "legal_name": f"Matrix {i} GmbH",
                "website_domain": f"matrix{i}.de",
                "location": {"country": "DE", "city": "Berlin"},
            },
            external_id=f"MX-{i}",
        )
        for i in range(3)
    ]
    adapter = FixtureJsonDirectoryAdapter(records)
    run = runs.create_run(db, provider)
    runs.fetch(db, run, adapter, {"q": "matrix"})
    runs.normalize_run(db, run, adapter)
    runs.resolve_run(db, run, adapter)
    rebuild_projections(db)
    db.commit()
    db.close()

    try:
        yield api_client
    finally:
        cleanup = factory()
        cleanup.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
        cleanup.commit()
        cleanup.close()


# --- referential consistency ----------------------------------------------


def test_every_listed_company_is_fully_reachable(populated):
    """The invariant behind the reported 404: an id from /companies must work.

    Detail must be 200, and every subresource must be 200 — with data or with
    an empty list, but never 404.
    """
    companies = populated.get("/api/v1/companies").json()
    assert companies, "the fixture must produce companies or this proves nothing"

    failures = []
    for company in companies:
        cid = company["id"]
        detail = populated.get(f"/api/v1/companies/{cid}")
        if detail.status_code != 200:
            failures.append(("detail", cid, detail.status_code))
        for sub in COMPANY_SUBRESOURCES:
            response = populated.get(f"/api/v1/companies/{cid}/{sub}")
            if response.status_code != 200:
                failures.append((sub, cid, response.status_code))
            elif not isinstance(response.json(), list):
                failures.append((sub, cid, "not a list"))
    assert failures == []


def test_every_listed_run_is_fully_reachable(populated):
    runs_listed = populated.get("/api/v1/discovery-runs").json()
    assert runs_listed
    failures = []
    for run in runs_listed:
        rid = run["id"]
        for suffix in ("", "/queries", "/versions"):
            response = populated.get(f"/api/v1/discovery-runs/{rid}{suffix}")
            if response.status_code != 200:
                failures.append((suffix or "detail", rid, response.status_code))
    assert failures == []


def test_every_provider_entity_from_a_run_is_fully_reachable(populated):
    runs_listed = populated.get("/api/v1/discovery-runs").json()
    versions = populated.get(
        f"/api/v1/discovery-runs/{runs_listed[0]['id']}/versions"
    ).json()
    assert versions
    failures = []
    for entity_id in {v["provider_entity_id"] for v in versions}:
        for suffix in ("versions", "resolution-chain"):
            response = populated.get(f"/api/v1/provider-entities/{entity_id}/{suffix}")
            if response.status_code != 200:
                failures.append((suffix, entity_id, response.status_code))
    assert failures == []


def test_every_listed_market_is_fully_reachable(populated):
    markets = populated.get("/api/v1/markets?limit=200").json()["results"]
    assert len(markets) > 50
    failures = []
    for market in markets:
        iso2 = market["iso2"]
        for suffix in ("", "/observations", "/sources", "/verticals"):
            response = populated.get(f"/api/v1/markets/{iso2}{suffix}")
            if response.status_code != 200:
                failures.append((suffix or "detail", iso2, response.status_code))
    assert failures == []


# --- absent and malformed identifiers -------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/companies/{id}",
        "/api/v1/companies/{id}/claims",
        "/api/v1/companies/{id}/locations",
        "/api/v1/companies/{id}/market-presences",
        "/api/v1/companies/{id}/verticals",
        "/api/v1/companies/{id}/relationships",
        "/api/v1/discovery-runs/{id}",
        "/api/v1/discovery-runs/{id}/queries",
        "/api/v1/discovery-runs/{id}/versions",
        "/api/v1/provider-entities/{id}/versions",
        "/api/v1/provider-entities/{id}/resolution-chain",
    ],
)
def test_absent_uuid_is_404_not_an_empty_list(populated, path):
    """A subresource of something that does not exist must say so.

    Returning ``[]`` would make a typo indistinguishable from a real but
    empty collection.
    """
    response = populated.get(path.format(id=ABSENT_UUID))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"].endswith("NOT_FOUND")


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/companies/{id}",
        "/api/v1/companies/{id}/claims",
        "/api/v1/discovery-runs/{id}",
        "/api/v1/discovery-runs/{id}/versions",
        "/api/v1/provider-entities/{id}/resolution-chain",
    ],
)
def test_malformed_uuid_is_422(populated, path):
    response = populated.get(path.format(id=MALFORMED))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- the generated matrix --------------------------------------------------


def _path_values(client) -> dict[str, str]:
    companies = client.get("/api/v1/companies").json()
    runs_listed = client.get("/api/v1/discovery-runs").json()
    versions = (
        client.get(f"/api/v1/discovery-runs/{runs_listed[0]['id']}/versions").json()
        if runs_listed
        else []
    )
    models = client.get("/api/v1/scoring-models").json()
    score_runs = client.get("/api/v1/score-runs").json()
    snapshots = client.get("/api/v1/market-intelligence/snapshots").json()
    sources = client.get("/api/v1/sources").json()
    verticals = client.get("/api/v1/verticals").json()
    return {
        "company_id": companies[0]["id"],
        "run_id": runs_listed[0]["id"],
        "entity_id": versions[0]["provider_entity_id"],
        "iso2": "DE",
        "snapshot_key": snapshots[0]["key"],
        "source_key": sources[0]["source_key"],
        "key": models[0]["key"],
        "version": models[0]["version"],
        "vertical_key": verticals[0]["key"],
        "score_run_id": score_runs[0]["id"] if score_runs else ABSENT_UUID,
        "id": score_runs[0]["id"] if score_runs else ABSENT_UUID,
        "gap_id": ABSENT_UUID,
    }


def test_no_get_operation_returns_an_undocumented_500(populated):
    """Every GET in the OpenAPI document, probed three ways."""
    spec = populated.get("/api/v1/openapi.json").json()
    values = _path_values(populated)

    failures: list[str] = []
    probed = 0
    for path, operations in sorted(spec["paths"].items()):
        operation = operations.get("get")
        if operation is None:
            continue
        params = [p for p in operation.get("parameters", []) if p["in"] == "path"]
        cases = [("valid", values)]
        if params:
            cases += [("absent", None), ("malformed", None)]

        for label, table in cases:
            url = path
            for param in params:
                name = param["name"]
                if label == "valid":
                    if name not in table:
                        url = None
                        break
                    url = url.replace(f"{{{name}}}", str(table[name]))
                else:
                    is_uuid = param.get("schema", {}).get("format") == "uuid"
                    if label == "absent":
                        value = ABSENT_UUID if is_uuid else "zz-absent"
                    else:
                        value = MALFORMED if is_uuid else "!!"
                    url = url.replace(f"{{{name}}}", value)
            if url is None:
                continue
            response = populated.get(url)
            probed += 1
            if response.status_code >= 500:
                failures.append(f"{label} GET {url} -> {response.status_code}")

    assert probed >= 30, f"the matrix only probed {probed} operations"
    assert failures == [], "\n".join(failures)


def test_every_error_response_uses_the_documented_envelope(populated):
    spec = populated.get("/api/v1/openapi.json").json()
    offenders = []
    for path, operations in sorted(spec["paths"].items()):
        if "get" not in operations:
            continue
        params = [p for p in operations["get"].get("parameters", []) if p["in"] == "path"]
        if not params:
            continue
        url = path
        for param in params:
            is_uuid = param.get("schema", {}).get("format") == "uuid"
            placeholder = "{" + param["name"] + "}"
            url = url.replace(
                placeholder, ABSENT_UUID if is_uuid else "zz-absent"
            )
        response = populated.get(url)
        if response.status_code < 400:
            continue
        body = response.json()
        if not (isinstance(body, dict) and set(body) == {"error"}):
            offenders.append(f"{url} -> {response.status_code} {body}")
            continue
        error = body["error"]
        if not {"code", "message", "details"} <= set(error):
            offenders.append(f"{url} -> malformed envelope {error}")
    assert offenders == []


def test_uuid_paths_reject_a_malformed_identifier_consistently(populated):
    spec = populated.get("/api/v1/openapi.json").json()
    offenders = []
    for path, operations in sorted(spec["paths"].items()):
        if "get" not in operations:
            continue
        params = [p for p in operations["get"].get("parameters", []) if p["in"] == "path"]
        uuid_params = [
            p for p in params if p.get("schema", {}).get("format") == "uuid"
        ]
        if len(uuid_params) != len(params) or not params:
            continue
        url = path
        for param in params:
            url = url.replace("{" + param["name"] + "}", MALFORMED)
        response = populated.get(url)
        if response.status_code != 422:
            offenders.append(f"{url} -> {response.status_code}")
    assert offenders == []


def test_uuid_of_the_right_shape_but_no_row_is_never_confused_with_success(populated):
    """A well-formed UUID that names nothing must not read as an empty success."""
    assert uuid.UUID(ABSENT_UUID)
    for path in (
        "/api/v1/companies/{id}/claims",
        "/api/v1/discovery-runs/{id}/versions",
        "/api/v1/provider-entities/{id}/versions",
    ):
        response = populated.get(path.format(id=ABSENT_UUID))
        assert response.status_code == 404, path


# --- the OpenAPI document as a contract ------------------------------------


def test_openapi_documents_the_error_envelope_everywhere_it_can_occur(populated):
    """A generated client must know the failure cases are structured.

    25 operations could return 404 without declaring it, so a client built
    from the document would treat a 404 body as undefined.
    """
    spec = populated.get("/api/v1/openapi.json").json()
    assert "ErrorEnvelope" in spec["components"]["schemas"]

    missing_404, missing_500 = [], []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = operation.get("responses", {})
            if "500" not in responses:
                missing_500.append(f"{method.upper()} {path}")
            has_path_param = any(
                p.get("in") == "path" for p in operation.get("parameters", [])
            )
            if has_path_param and "404" not in responses:
                missing_404.append(f"{method.upper()} {path}")
    assert missing_404 == []
    assert missing_500 == []


def test_openapi_has_unique_operation_ids_and_no_broken_refs(populated):
    spec = populated.get("/api/v1/openapi.json").json()
    schemas = spec.get("components", {}).get("schemas", {})

    ids = [
        operation["operationId"]
        for operations in spec["paths"].values()
        for method, operation in operations.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(ids) == len(set(ids)), "duplicate operationId"

    def refs(node):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                yield node["$ref"]
            for value in node.values():
                yield from refs(value)
        elif isinstance(node, list):
            for value in node:
                yield from refs(value)

    broken = [
        ref
        for ref in refs(spec)
        if ref.startswith("#/components/schemas/")
        and ref.rsplit("/", 1)[1] not in schemas
    ]
    assert broken == []


def test_the_documented_404_body_is_the_body_actually_returned(populated):
    """Documenting a shape the server does not send would be worse than silence."""
    spec = populated.get("/api/v1/openapi.json").json()
    declared = spec["components"]["schemas"]["ErrorEnvelope"]
    required = set(declared["properties"]["error"]["required"])

    response = populated.get(f"/api/v1/companies/{ABSENT_UUID}")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert required <= set(body["error"])
