"""The M3 API surface: contracts, errors, filters, and what it must not emit.

Every exclusion here is checked at the serialization boundary rather than in the
service layer, because that is where a leak would actually reach a client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.domain import models as m
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

API = "/api/v1"
NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


@pytest.fixture
def seeded(api_client, database_url):
    """A researched company, committed so the API can read it."""
    from sqlalchemy import text as sql

    from tests.conftest import _ALL_TABLES

    engine = create_engine(database_url)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    # The shared truncate list, rather than a hand-ordered DELETE. Writing the
    # order by hand got it wrong immediately -- evidence items before the
    # identity-review rows that reference them -- and it would go wrong again
    # every time a table is added.
    session.execute(sql(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    session.commit()

    seed_all(session)
    company = Company(created_at=NOW, identity_policy_version="1.0",
                      lifecycle_status="ACTIVE")
    session.add(company)
    session.flush()
    result = run_pipeline(
        session, company_id=company.id, transport=FixtureTransport(), now=NOW
    )
    session.commit()
    context = {
        "client": api_client,
        "company_id": str(company.id),
        "run_id": str(result.attempt.run_id),
        "attempt_id": str(result.attempt.id),
        "session": session,
    }
    yield context

    # Leave the database as it was found. This fixture commits, so it owns the
    # cleanup: without it the committed rows leak into later files and the
    # concurrency tests -- which assert absolute counts -- fail for reasons
    # that have nothing to do with concurrency.
    session.rollback()
    session.execute(sql(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    session.commit()
    session.close()
    engine.dispose()


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


# --- runs and attempts ------------------------------------------------------


def test_a_run_carries_no_execution_status(seeded):
    """A question does not have a status; its executions do."""
    body = _ok(seeded["client"].get(f"{API}/operational-research/runs/{seeded['run_id']}"))
    assert "status" not in body
    assert "error" not in body
    assert "completed_at" not in body
    assert body["research_plan_hash"]
    assert body["attempt_count"] >= 1


def test_an_attempt_carries_execution_state(seeded):
    body = _ok(seeded["client"].get(
        f"{API}/operational-research/attempts/{seeded['attempt_id']}"
    ))
    assert body["status"] in {"COMPLETED", "PARTIAL", "FAILED"}
    assert body["fetch_completed_at"]
    assert body["extraction_completed_at"]
    assert body["attempt_seed_inputs_hash"]


def test_runs_filter_by_company_policy_and_vertical(seeded):
    client = seeded["client"]
    assert len(_ok(client.get(
        f"{API}/operational-research/runs", params={"company": seeded["company_id"]}
    ))) >= 1
    assert _ok(client.get(
        f"{API}/operational-research/runs", params={"company": str(uuid.uuid4())}
    )) == []
    assert len(_ok(client.get(
        f"{API}/operational-research/runs", params={"policy": "1.0"}
    ))) >= 1
    assert _ok(client.get(
        f"{API}/operational-research/runs", params={"policy": "9.9"}
    )) == []


def test_attempts_filter_by_status_run_and_company(seeded):
    client = seeded["client"]
    assert len(_ok(client.get(
        f"{API}/operational-research/attempts", params={"run": seeded["run_id"]}
    ))) == 1
    assert len(_ok(client.get(
        f"{API}/operational-research/attempts", params={"status": "PARTIAL"}
    ))) >= 1
    assert _ok(client.get(
        f"{API}/operational-research/attempts", params={"status": "PENDING"}
    )) == []


def test_pagination_is_bounded(seeded):
    client = seeded["client"]
    assert client.get(
        f"{API}/operational-research/runs", params={"limit": 10_000}
    ).status_code == 422
    assert client.get(
        f"{API}/operational-research/runs", params={"offset": -1}
    ).status_code == 422
    assert len(_ok(client.get(
        f"{API}/operational-research/runs", params={"limit": 1}
    ))) <= 1


def test_creating_a_run_twice_reuses_the_question(seeded):
    client = seeded["client"]
    payload = {"company_id": seeded["company_id"],
               "target_attribute_keys": ["erp", "crm"]}
    first = client.post(f"{API}/operational-research/runs", json=payload)
    second = client.post(f"{API}/operational-research/runs", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


# --- errors -----------------------------------------------------------------


def test_unknown_ids_are_404_in_the_documented_envelope(seeded):
    client = seeded["client"]
    missing = str(uuid.uuid4())
    for path in (
        f"{API}/operational-research/runs/{missing}",
        f"{API}/operational-research/attempts/{missing}",
        f"{API}/research-sources/{missing}",
        f"{API}/research-sources/{missing}/fetch-events",
        f"{API}/research-artifacts/{missing}",
        f"{API}/research-bodies/{missing}",
        f"{API}/research-extractions/{missing}",
        f"{API}/research-evidence-items/{missing}",
        f"{API}/companies/{missing}/research",
        f"{API}/operational-research/runs/{missing}/coverage",
        f"{API}/operational-research/runs/{missing}/gaps",
        f"{API}/identity-review-signals/{missing}",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        body = response.json()
        assert body["error"]["code"]
        assert body["error"]["message"]


def test_a_second_live_attempt_is_409_not_an_integrity_error(seeded):
    """The index is the backstop; the client gets a domain error.

    The live attempt is *opened*, not faked by rewriting a terminal one -- the
    transition trigger refuses `PARTIAL -> PENDING`, and rightly so.
    """
    from boro_gtm.research.services.application import open_attempt

    session = seeded["session"]
    run_id = uuid.UUID(seeded["run_id"])
    open_attempt(session, run_id=run_id)
    session.commit()

    response = seeded["client"].post(
        f"{API}/operational-research/runs/{seeded['run_id']}/attempts", json={}
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "RESEARCH_ATTEMPT_ALREADY_LIVE"
    assert "IntegrityError" not in response.text
    assert "psycopg" not in response.text


def test_retrying_a_live_attempt_is_409(seeded):
    """Only a terminal attempt can be retried, and retry never reopens one."""
    from boro_gtm.research.services.application import open_attempt

    session = seeded["session"]
    live = open_attempt(session, run_id=uuid.UUID(seeded["run_id"]))
    session.commit()

    response = seeded["client"].post(
        f"{API}/operational-research/attempts/{live.id}/retry"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESEARCH_ATTEMPT_NOT_TERMINAL"


def test_an_unknown_attribute_key_is_422(seeded):
    response = seeded["client"].post(f"{API}/operational-research/runs", json={
        "company_id": seeded["company_id"],
        "target_attribute_keys": ["not_an_attribute"],
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"]


def test_an_unknown_gap_kind_is_422(seeded):
    response = seeded["client"].get(
        f"{API}/operational-research/runs/{seeded['run_id']}/gaps",
        params={"kind": "NOT_A_KIND"},
    )
    assert response.status_code == 422


def test_no_postgres_error_text_reaches_a_client(seeded):
    """A database message can carry schema and query internals."""
    response = seeded["client"].get(f"{API}/research-bodies/{uuid.uuid4()}")
    for leak in ("psycopg", "SELECT ", "relation ", "sqlalchemy"):
        assert leak not in response.text


# --- evidence ---------------------------------------------------------------


def test_the_evidence_dto_answers_the_whole_provenance_walk(seeded):
    session = seeded["session"]
    item_id = session.scalar(select(m.ResearchEvidenceItem.id).limit(1))
    body = _ok(seeded["client"].get(f"{API}/research-evidence-items/{item_id}"))

    assert body["source"]["normalized_locator"]
    assert body["fetch_event"]["fetch_outcome"] == "OK"
    assert body["body"]["raw_body_sha256"]
    assert body["body"]["byte_length"] > 0
    assert body["artifact_derivation"]["canonical_content_hash"]
    assert body["artifact"]["canonicalization_strategy"]
    assert body["extraction"]["extractor_id"]
    assert body["locator"]["resolved"] is True
    assert body["quote"] and body["quote_sha256"]
    assert body["publisher_key"] and body["publisher_policy_version"]


def test_the_api_never_serves_raw_bytes_text_or_model_output(seeded):
    """Retention-limited internal payloads are not part of the contract."""
    session = seeded["session"]
    client = seeded["client"]
    body_id = session.scalar(select(m.ResearchArtifactBody.id).limit(1))
    extraction_id = session.scalar(select(m.ResearchExtraction.id).limit(1))
    item_id = session.scalar(select(m.ResearchEvidenceItem.id).limit(1))

    for path in (f"{API}/research-bodies/{body_id}",
                 f"{API}/research-extractions/{extraction_id}",
                 f"{API}/research-evidence-items/{item_id}"):
        payload = client.get(path).json()
        text = str(payload)
        # The *payload* keys, not any key containing the word: `raw_output_sha256`
        # and `raw_output_retention` are metadata and are meant to be there.
        for node in (payload, payload.get("extraction", {}), payload.get("body", {})):
            assert "raw_body" not in node
            assert "raw_output" not in node
            assert "extracted_text" not in node
        assert "<!DOCTYPE" not in text
        assert "%PDF" not in text
        assert "dispatch@meridianmechanical.com" not in text


def test_a_body_reports_hash_length_and_retention_only(seeded):
    session = seeded["session"]
    body_id = session.scalar(select(m.ResearchArtifactBody.id).limit(1))
    payload = _ok(seeded["client"].get(f"{API}/research-bodies/{body_id}"))
    assert set(payload) == {
        "id", "raw_body_sha256", "byte_length", "body_retention",
        "pruned_at", "first_seen_at",
    }


# --- company-global ---------------------------------------------------------


def test_company_research_exposes_the_projection_with_read_time_staleness(seeded):
    body = _ok(seeded["client"].get(
        f"{API}/companies/{seeded['company_id']}/research",
        params={"as_of": "2026-09-27"},
    ))
    assert body["as_of"] == "2026-09-27"
    assert body["attributes"]
    keys = {a["attribute_key"] for a in body["attributes"]}
    assert "emergency_service" in keys
    states = {a["staleness"] for a in body["attributes"]}
    assert states & {"FRESH", "AGING", "STALE", "UNKNOWN_AGE"}

    contradicted = [a for a in body["attributes"] if a["contradiction"]]
    assert contradicted
    assert all(a["contradicting_claim_ids"] for a in contradicted)


def test_company_research_carries_no_coverage(seeded):
    """Coverage belongs to a question, not to a company."""
    body = _ok(seeded["client"].get(
        f"{API}/companies/{seeded['company_id']}/research"
    ))
    for forbidden in ("coverage", "contradiction_rate", "confidence_summary"):
        assert forbidden not in body


def test_the_q2_projection_has_exactly_seven_fields_and_no_hypothesis(seeded):
    records = _ok(seeded["client"].get(
        f"{API}/companies/{seeded['company_id']}/research/evidence"
    ))
    assert records
    for record in records:
        assert set(record) == {
            "source", "source_type", "date_observed", "fact", "confidence",
            "inference_allowed", "related_hypothesis",
        }
        assert record["related_hypothesis"] is None
    dated = [r for r in records if r["date_observed"]]
    assert dated
    assert all(r["date_observed"] != "2026-09-27" for r in dated)


def test_claims_expose_their_evidence_ids(seeded):
    claims = _ok(seeded["client"].get(
        f"{API}/companies/{seeded['company_id']}/research/claims"
    ))
    assert claims
    assert all(c["evidence_item_ids"] for c in claims)
    assert all(c["attribute_registry_version"] == RESEARCH_REGISTRY_VERSION
               for c in claims)


# --- plan-scoped ------------------------------------------------------------


def test_coverage_exposes_three_separate_numbers(seeded):
    body = _ok(seeded["client"].get(
        f"{API}/operational-research/runs/{seeded['run_id']}/coverage"
    ))
    assert 0.0 <= body["coverage"] <= 1.0
    assert body["required_attribute_count"] > 0
    assert "contradiction_rate" in body
    assert "confidence_summary" in body
    for forbidden in ("score", "fit", "priority", "grade", "tier", "sales_ready"):
        assert forbidden not in body


def test_gaps_state_insufficient_evidence_and_never_absence(seeded):
    rows = _ok(seeded["client"].get(
        f"{API}/operational-research/runs/{seeded['run_id']}/gaps"
    ))
    assert rows
    kinds = {r["gap_kind"] for r in rows}
    assert kinds <= {
        "NO_EVIDENCE", "INSUFFICIENT_EVIDENCE", "STALE_EVIDENCE",
        "CONTRADICTED", "UNRESOLVABLE_SOURCE", "NO_CRAWLABLE_SEED",
    }
    for row in rows:
        assert row["current_status"] in {"RAISED", "ATTEMPTED", "RESOLVED", "ABANDONED"}
        assert "value" not in row
        assert "false" not in str(row).lower() or row["gap_kind"]


def test_gaps_filter_by_kind(seeded):
    rows = _ok(seeded["client"].get(
        f"{API}/operational-research/runs/{seeded['run_id']}/gaps",
        params={"kind": "NO_EVIDENCE"},
    ))
    assert rows
    assert {r["gap_kind"] for r in rows} == {"NO_EVIDENCE"}


# --- identity review --------------------------------------------------------


def test_an_identity_signal_exposes_its_concern_and_occurrence_history(seeded):
    signals = _ok(seeded["client"].get(f"{API}/identity-review-signals"))
    assert signals
    signal = signals[0]
    assert signal["normalized_concern"]
    assert signal["signal_kind"]
    assert signal["occurrences"]
    assert signal["occurrences"][0]["current_status"] == "OPEN"
    assert signal["occurrences"][0]["is_open"] is True


def test_identity_signals_filter_by_company_kind_and_open(seeded):
    client = seeded["client"]
    assert _ok(client.get(f"{API}/identity-review-signals",
                          params={"company": seeded["company_id"]}))
    assert _ok(client.get(f"{API}/identity-review-signals",
                          params={"kind": "POSSIBLE_PARENT"}))
    assert _ok(client.get(f"{API}/identity-review-signals", params={"open": True}))
    assert _ok(client.get(f"{API}/identity-review-signals",
                          params={"open": False})) == []


def test_identity_signal_evidence_is_first_class(seeded):
    signals = _ok(seeded["client"].get(f"{API}/identity-review-signals"))
    items = _ok(seeded["client"].get(
        f"{API}/identity-review-signals/{signals[0]['id']}/evidence"
    ))
    assert items
    assert all(i["quote_sha256"] for i in items)
    assert all(i["source"]["normalized_locator"] for i in items)


def test_a_review_decision_appends_and_respects_the_transition_graph(seeded):
    client = seeded["client"]
    signal = _ok(client.get(f"{API}/identity-review-signals"))[0]
    base = f"{API}/identity-review-signals/{signal['id']}/occurrences/1/status"

    assert client.post(base, json={"status": "ACTIONED", "actor": "a"}
                       ).status_code == 409, "OPEN cannot jump to ACTIONED"

    assert client.post(base, json={"status": "ACKNOWLEDGED", "actor": "a"}
                       ).status_code == 201
    assert client.post(base, json={"status": "ACTIONED", "actor": "a"}
                       ).status_code == 201

    refused = client.post(base, json={"status": "OPEN", "actor": "a"})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "SIGNAL_TRANSITION_ILLEGAL"


def test_the_identity_review_path_writes_no_m2_identity(seeded):
    """M3-owned end to end: no ProviderEntity, no resolution decision."""
    from sqlalchemy import func as sql_func

    from boro_gtm.discovery.domain.models import (
        CompanyDomain,
        EntityResolutionDecision,
        ProviderEntity,
    )

    session = seeded["session"]
    client = seeded["client"]

    def fingerprint():
        session.expire_all()
        return tuple(
            session.scalar(select(sql_func.count()).select_from(model)) or 0
            for model in (ProviderEntity, EntityResolutionDecision, CompanyDomain,
                          Company)
        )

    before = fingerprint()
    signal = _ok(client.get(f"{API}/identity-review-signals"))[0]
    _ok(client.get(f"{API}/identity-review-signals/{signal['id']}"))
    _ok(client.get(f"{API}/identity-review-signals/{signal['id']}/evidence"))
    client.post(
        f"{API}/identity-review-signals/{signal['id']}/occurrences/1/status",
        json={"status": "ACKNOWLEDGED", "actor": "reviewer"},
    )
    assert fingerprint() == before


# --- human evidence review --------------------------------------------------


def _pending_item(seeded) -> dict:
    items = _ok(seeded["client"].get(f"{API}/research-reviews/pending"))
    assert items, "the corpus leaves a sampled reading awaiting review"
    return items[0]


def test_the_review_queue_lists_sampled_readings_with_no_claim(seeded):
    """The state a claim-keyed route could not address at all."""
    item = _pending_item(seeded)
    assert item["evidence_item_id"]
    assert item["extraction_id"]
    assert item["source"].startswith("https://")
    assert item["locator"]["kind"] == "PDF_SPAN"

    session = seeded["session"]
    linked = session.scalars(select(m.ClaimEvidenceLink.id).where(
        m.ClaimEvidenceLink.evidence_item_id == uuid.UUID(item["evidence_item_id"])
    )).all()
    assert linked == []


def test_confirming_an_observation_appends_a_human_lineage(seeded):
    item = _pending_item(seeded)
    response = seeded["client"].post(
        f"{API}/research-evidence-items/{item['evidence_item_id']}/review",
        json={"decision": "CONFIRM", "actor": "analyst",
              "company_id": seeded["company_id"]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision"] == "CONFIRM"
    assert body["human_extraction_id"]
    assert body["model_extraction_unchanged"] is True
    assert body["reviewed_at"]

    session = seeded["session"]
    session.expire_all()
    row = session.get(m.ResearchEvidenceReview, uuid.UUID(body["review_id"]))
    assert row is not None and row.decision == "CONFIRM"


def test_rejecting_an_observation_persists_and_asserts_nothing(seeded):
    """The defect: a rejection used to leave no trace whatsoever."""
    session = seeded["session"]
    item = _pending_item(seeded)
    claims_before = session.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(CompanyClaim)
    )

    response = seeded["client"].post(
        f"{API}/research-evidence-items/{item['evidence_item_id']}/review",
        json={"decision": "REJECT", "actor": "analyst", "note": "not convinced"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision"] == "REJECT"
    assert body["human_extraction_id"] is None
    assert body["resulting_claim_id"] is None
    assert body["note"] == "not convinced"

    session.expire_all()
    row = session.get(m.ResearchEvidenceReview, uuid.UUID(body["review_id"]))
    assert row is not None, "the decision survives the request"
    assert row.actor == "analyst"
    assert row.note == "not convinced"
    assert session.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(CompanyClaim)
    ) == claims_before


def test_reviews_are_listed_for_an_observation(seeded):
    item = _pending_item(seeded)
    seeded["client"].post(
        f"{API}/research-evidence-items/{item['evidence_item_id']}/review",
        json={"decision": "REJECT", "actor": "first"},
    )
    rows = _ok(seeded["client"].get(
        f"{API}/research-evidence-items/{item['evidence_item_id']}/reviews"
    ))
    assert len(rows) == 1
    assert rows[0]["actor"] == "first"
    assert rows[0]["decision"] == "REJECT"


def test_an_invalid_review_decision_is_422(seeded):
    item = _pending_item(seeded)
    response = seeded["client"].post(
        f"{API}/research-evidence-items/{item['evidence_item_id']}/review",
        json={"decision": "MAYBE", "actor": "analyst"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"]


def test_reviewing_an_unknown_observation_is_404(seeded):
    response = seeded["client"].post(
        f"{API}/research-evidence-items/{uuid.uuid4()}/review",
        json={"decision": "REJECT", "actor": "analyst"},
    )
    assert response.status_code == 404


def test_the_claim_keyed_review_route_is_gone(seeded):
    """Superseded, not aliased: nothing is released, so nothing depends on it."""
    session = seeded["session"]
    claim_id = session.scalar(select(CompanyClaim.id).limit(1))
    response = seeded["client"].post(
        f"{API}/research-claims/{claim_id}/review",
        json={"decision": "CONFIRM", "actor": "analyst"},
    )
    assert response.status_code == 404


# --- the commercial firewall, at the boundary -------------------------------


FORBIDDEN = (
    "qualification_score", "qualification_route", "budget_band", "buyer_role",
    "problem_class", "operational_hypotheses", "implementation_mode_candidates",
    "commercial_level_candidate", "commercial_level_final",
    "selected_capabilities", "price_floor_usd", "quoted_price_usd",
    "normalized_delivery_cost_usd", "normalized_gross_margin", "CAP-", "EV-",
)


def test_no_m3_response_carries_a_commercial_field(seeded):
    client = seeded["client"]
    session = seeded["session"]
    item_id = session.scalar(select(m.ResearchEvidenceItem.id).limit(1))
    signals = _ok(client.get(f"{API}/identity-review-signals"))

    paths = [
        f"{API}/operational-research/runs",
        f"{API}/operational-research/runs/{seeded['run_id']}",
        f"{API}/operational-research/runs/{seeded['run_id']}/coverage",
        f"{API}/operational-research/runs/{seeded['run_id']}/gaps",
        f"{API}/operational-research/attempts",
        f"{API}/operational-research/attempts/{seeded['attempt_id']}",
        f"{API}/research-evidence-items/{item_id}",
        f"{API}/companies/{seeded['company_id']}/research",
        f"{API}/companies/{seeded['company_id']}/research/claims",
        f"{API}/companies/{seeded['company_id']}/research/evidence",
        f"{API}/identity-review-signals/{signals[0]['id']}",
    ]
    for path in paths:
        text = client.get(path).text
        for token in FORBIDDEN:
            assert token not in text, f"{token} leaked from {path}"


def test_the_openapi_document_carries_no_m4_concept_or_raw_payload(api_client):
    spec = _ok(api_client.get(f"{API}/openapi.json"))
    import json

    blob = json.dumps(spec)
    for token in FORBIDDEN:
        assert token not in blob, token
    for token in ('"raw_body"', '"raw_output"', '"extracted_text"'):
        assert token not in blob, token


def test_the_openapi_document_is_structurally_sound(api_client):
    import re

    spec = _ok(api_client.get(f"{API}/openapi.json"))
    import json

    blob = json.dumps(spec)
    schemas = set(spec.get("components", {}).get("schemas", {}))
    referenced = set(re.findall(r"#/components/schemas/(\w+)", blob))
    assert referenced <= schemas, f"broken $refs: {referenced - schemas}"

    methods = ("get", "post", "put", "patch", "delete")
    ids = [
        item[method]["operationId"]
        for item in spec["paths"].values() for method in item if method in methods
    ]
    assert len(ids) == len(set(ids)), "duplicate operation ids"

    m3_paths = [
        p for p in spec["paths"]
        if any(p.startswith(f"{API}{prefix}") for prefix in (
            "/operational-research", "/research-sources", "/research-artifacts",
            "/research-bodies", "/research-extractions", "/research-evidence-items",
            "/research-claims", "/identity-review-signals",
        )) or p.endswith("/research") or "/research/" in p
    ]
    assert len(m3_paths) >= 26, sorted(m3_paths)

    # The run schema must not have acquired execution state.
    run_schema = spec["components"]["schemas"]["RunOut"]["properties"]
    assert "status" not in run_schema
    attempt_schema = spec["components"]["schemas"]["AttemptOut"]["properties"]
    assert "status" in attempt_schema


def test_every_m3_operation_documents_the_error_envelope(api_client):
    spec = _ok(api_client.get(f"{API}/openapi.json"))
    methods = ("get", "post", "put", "patch", "delete")
    for path, item in spec["paths"].items():
        # M3's own paths only. M0/M1 owns `/research-gaps`, which is a market
        # research gap and nothing to do with this milestone.
        if not any(path.startswith(f"{API}{prefix}") for prefix in (
            "/operational-research", "/research-sources", "/research-artifacts",
            "/research-bodies", "/research-extractions", "/research-evidence-items",
            "/research-claims", "/identity-review-signals",
        )):
            continue
        has_path_param = "{" in path
        for method in item:
            if method not in methods:
                continue
            responses = item[method]["responses"]
            # Every operation can fail unexpectedly...
            assert "500" in responses, f"{method} {path} is missing 500"
            # ...but only one addressing an entity can 404. A collection
            # endpoint has nothing to be missing, and claiming otherwise would
            # document a response it never returns.
            if has_path_param:
                assert "404" in responses, f"{method} {path} is missing 404"
