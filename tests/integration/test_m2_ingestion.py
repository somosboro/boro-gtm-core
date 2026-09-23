"""M2 ingestion: version identity, body cardinality, provider capabilities.

Covers acceptance A1–A20.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import (
    ProviderEntity,
    ProviderRecordBody,
    ProviderRecordSighting,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import ExternalIdKind
from boro_gtm.discovery.providers.fixtures import (
    FixtureCsvExportAdapter,
    FixtureJsonDirectoryAdapter,
    FixtureScrapeAdapter,
    csv_record,
    json_record,
)
from boro_gtm.discovery.services.ingestion import ingest_record
from tests.integration.conftest_m2 import make_query

pytestmark = pytest.mark.integration

PAYLOAD = {
    "legal_name": "Schmidt Kältetechnik GmbH",
    "website_domain": "schmidt-kaelte.de",
    "location": {"country": "DE", "city": "München", "postal_code": "80331"},
    "employees": {"min": 40, "max": 60},
}


def _counts(session):
    return (
        session.scalar(select(func.count()).select_from(ProviderRecordVersion)),
        session.scalar(select(func.count()).select_from(ProviderRecordBody)),
        session.scalar(select(func.count()).select_from(ProviderRecordSighting)),
    )


# --- A1/A2: version idempotency --------------------------------------------


def test_identical_payload_creates_no_new_version(m2_session, provider_json):
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)
    raw = json_record(PAYLOAD, external_id="P-1")

    first = ingest_record(m2_session, adapter, provider_json, raw, query)
    second = ingest_record(m2_session, adapter, provider_json, raw, query)

    assert first.version_created is True
    assert second.version_created is False
    assert first.version_id == second.version_id
    versions, bodies, _ = _counts(m2_session)
    assert versions == 1
    assert bodies == 1, "identical bytes must not duplicate a body"


def test_semantic_change_creates_new_version(m2_session, provider_json):
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)

    first = ingest_record(m2_session, adapter, provider_json,
                          json_record(PAYLOAD, external_id="P-1"), query)
    mutated = {**PAYLOAD, "employees": {"min": 45, "max": 60}}
    second = ingest_record(m2_session, adapter, provider_json,
                           json_record(mutated, external_id="P-1"), query)

    assert second.version_created is True
    assert first.version_id != second.version_id
    assert first.provider_entity_id == second.provider_entity_id, "same entity"
    assert _counts(m2_session)[0] == 2


# --- A5/A5a-d: body cardinality --------------------------------------------


def test_reformatted_payload_is_one_version_two_bodies(m2_session, provider_json):
    """Identity is semantic; bytes are fidelity (ADR M2-026)."""
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)

    compact = json_record(PAYLOAD, external_id="P-1")
    indented = json_record(PAYLOAD, external_id="P-1", indent=4)
    assert compact.body != indented.body, "fixture must produce different bytes"

    first = ingest_record(m2_session, adapter, provider_json, compact, query)
    second = ingest_record(m2_session, adapter, provider_json, indented, query)

    assert first.version_id == second.version_id
    assert second.version_created is False
    assert second.body_created is True
    versions, bodies, _ = _counts(m2_session)
    assert (versions, bodies) == (1, 2)


def test_byte_digest_lives_on_the_body_not_the_version(m2_session):
    version_cols = {c.name for c in ProviderRecordVersion.__table__.columns}
    body_cols = {c.name for c in ProviderRecordBody.__table__.columns}
    assert "raw_body_sha256" not in version_cols
    assert "raw_body_sha256" in body_cols
    assert "raw_body" in body_cols


def test_pruning_bodies_preserves_version_identity(m2_session, provider_json):
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)
    result = ingest_record(m2_session, adapter, provider_json,
                           json_record(PAYLOAD, external_id="P-1"), query)
    version = m2_session.get(ProviderRecordVersion, result.version_id)
    hash_before = version.canonical_payload_hash

    m2_session.query(ProviderRecordBody).delete()
    m2_session.flush()

    still = m2_session.get(ProviderRecordVersion, result.version_id)
    assert still is not None
    assert still.canonical_payload_hash == hash_before
    assert still.parsed_payload == version.parsed_payload


# --- A16-A20: canonicalization participates in identity --------------------


def test_version_identity_includes_canonicalization_contract(m2_session):
    constraint = next(
        c for c in ProviderRecordVersion.__table__.constraints
        if getattr(c, "name", None) == "uq_version_identity"
    )
    assert {c.name for c in constraint.columns} == {
        "provider_entity_id", "canonicalization_strategy",
        "canonicalization_version", "canonical_payload_hash",
    }


def test_different_strategy_version_yields_distinct_version(m2_session, provider_json):
    """The A19 regression: even an identical hash must not collapse versions."""
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)
    first = ingest_record(m2_session, adapter, provider_json,
                          json_record(PAYLOAD, external_id="P-1"), query)
    original = m2_session.get(ProviderRecordVersion, first.version_id)

    class BumpedAdapter(FixtureJsonDirectoryAdapter):
        def capabilities(self):
            return replace(super().capabilities(), canonicalization_version="2")

    bumped = ingest_record(m2_session, BumpedAdapter(), provider_json,
                           json_record(PAYLOAD, external_id="P-1"), query)

    assert bumped.version_created is True
    assert bumped.version_id != first.version_id
    second = m2_session.get(ProviderRecordVersion, bumped.version_id)
    # Identical hash, different contract -> two distinct versions.
    assert second.canonical_payload_hash == original.canonical_payload_hash
    assert second.canonicalization_version != original.canonicalization_version


def test_historical_versions_remain_interpretable(m2_session, provider_json):
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)
    ingest_record(m2_session, adapter, provider_json,
                  json_record(PAYLOAD, external_id="P-1"), query)
    for version in m2_session.scalars(select(ProviderRecordVersion)).all():
        assert version.canonicalization_strategy
        assert version.canonicalization_version


# --- A8-A12: provider identity capability ----------------------------------


def test_native_external_id_is_used_verbatim(m2_session, provider_json):
    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)
    result = ingest_record(m2_session, adapter, provider_json,
                           json_record(PAYLOAD, external_id="P-42"), query)
    entity = m2_session.get(ProviderEntity, result.provider_entity_id)
    assert entity.external_id_kind == ExternalIdKind.NATIVE.value
    assert entity.provider_external_id == "P-42"


def test_derived_key_is_labelled_derived(m2_session, provider_csv):
    adapter = FixtureCsvExportAdapter()
    _, query = make_query(m2_session, provider_csv)
    row = {"company": "Acme", "domain": "acme.de", "country": "DE",
           "city": "Berlin", "zip": "10115", "staff_min": "40", "staff_max": "60"}
    result = ingest_record(m2_session, adapter, provider_csv, csv_record(row), query)
    entity = m2_session.get(ProviderEntity, result.provider_entity_id)
    assert entity.external_id_kind == ExternalIdKind.DERIVED.value
    assert entity.key_algorithm_version == "1"
    # Never presented as a provider-issued id.
    assert entity.provider_external_id != "acme.de"


def test_changed_key_fields_create_a_new_entity(m2_session, provider_csv):
    adapter = FixtureCsvExportAdapter()
    _, query = make_query(m2_session, provider_csv)
    base = {"company": "Acme", "domain": "acme.de", "country": "DE",
            "city": "Berlin", "zip": "10115", "staff_min": "40", "staff_max": "60"}
    first = ingest_record(m2_session, adapter, provider_csv, csv_record(base), query)
    moved = {**base, "domain": "acme-group.de"}
    second = ingest_record(m2_session, adapter, provider_csv, csv_record(moved), query)

    assert first.provider_entity_id != second.provider_entity_id
    # The old entity is untouched; no mutable pointer is invented.
    assert m2_session.get(ProviderEntity, first.provider_entity_id) is not None


def test_content_only_provider_cannot_track_change(m2_session, provider_scrape):
    adapter = FixtureScrapeAdapter()
    _, query = make_query(m2_session, provider_scrape)
    first = ingest_record(
        m2_session, adapter, provider_scrape,
        json_record({"title": "Acme", "domain": "acme.de", "city": "Berlin"}), query)
    second = ingest_record(
        m2_session, adapter, provider_scrape,
        json_record({"title": "Acme", "domain": "acme.de", "city": "Hamburg"}), query)

    entity = m2_session.get(ProviderEntity, first.provider_entity_id)
    assert entity.external_id_kind == ExternalIdKind.CONTENT.value
    # A payload change produces a new entity: such a provider cannot express
    # "the same object changed".
    assert first.provider_entity_id != second.provider_entity_id


def test_derived_key_falls_back_rather_than_inventing_a_partial_key(
    m2_session, provider_csv
):
    adapter = FixtureCsvExportAdapter()
    _, query = make_query(m2_session, provider_csv)
    incomplete = {"company": "NoDomain Ltd", "domain": "", "country": "DE",
                  "city": "Berlin", "zip": "", "staff_min": "", "staff_max": ""}
    result = ingest_record(m2_session, adapter, provider_csv,
                           csv_record(incomplete), query)
    entity = m2_session.get(ProviderEntity, result.provider_entity_id)
    assert entity.external_id_kind == ExternalIdKind.CONTENT.value


# --- A13/A14: canonicalization declaration ---------------------------------


def test_non_json_provider_may_not_borrow_the_json_canonicalizer():
    from boro_gtm.core.errors import ValidationError
    from boro_gtm.discovery.providers.base import ProviderCapabilities

    with pytest.raises(ValidationError):
        ProviderCapabilities(
            provider_key="bad", name="Bad", identity_capability="NATIVE_EXTERNAL_ID",
            canonicalization_strategy="JSON_CANONICAL_V1",
            canonicalization_version="1", media_type="text/csv",
            normalizer_version="1",
        )


def test_each_provider_declares_its_own_strategy(m2_session, provider_json, provider_csv):
    assert provider_json.canonicalization_strategy == "JSON_CANONICAL_V1"
    assert provider_csv.canonicalization_strategy == "CSV_ROW_V1"
    assert provider_json.is_fixture and provider_csv.is_fixture


# --- A3: raw evidence is append-only ---------------------------------------


def test_versions_and_bodies_cannot_be_updated(m2_session, provider_json):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    adapter = FixtureJsonDirectoryAdapter()
    _, query = make_query(m2_session, provider_json)
    ingest_record(m2_session, adapter, provider_json,
                  json_record(PAYLOAD, external_id="P-1"), query)

    for table, column in (("provider_record_versions", "canonical_payload_hash"),
                          ("provider_record_bodies", "raw_body_sha256")):
        savepoint = m2_session.begin_nested()
        with pytest.raises(DBAPIError) as exc:
            m2_session.execute(text(f"UPDATE {table} SET {column} = 'x'"))
        assert "append-only" in str(exc.value)
        savepoint.rollback()
