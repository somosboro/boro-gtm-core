"""The API answers in its documented envelope, including when it fails.

Before this file, a database whose schema had drifted produced a bare
``Internal Server Error`` body: a 500 with no code a client could branch on,
from an application that documents one error shape everywhere else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _envelope(body) -> dict:
    assert isinstance(body, dict), body
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) >= {"code", "message", "details"}, error
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    return error


def test_not_found_uses_the_envelope(api_client):
    missing = "00000000-0000-0000-0000-000000000000"
    response = api_client.get(f"/api/v1/companies/{missing}")
    assert response.status_code == 404
    assert _envelope(response.json())["code"] == "NOT_FOUND"


def test_malformed_uuid_uses_the_envelope(api_client):
    response = api_client.get("/api/v1/companies/not-a-uuid")
    assert response.status_code == 422
    assert _envelope(response.json())["code"] == "VALIDATION_ERROR"


def test_an_unexpected_failure_still_uses_the_envelope(
    migrated_engine, database_url, monkeypatch
):
    """A 500 must carry a code, and must not leak internals.

    Built with its own client because the shared ``api_client`` re-raises
    server exceptions, which is the right default everywhere else.
    """
    from fastapi.testclient import TestClient

    import boro_gtm.core.db as core_db
    import boro_gtm.discovery.api.routes as routes
    from boro_gtm.api.main import create_app
    from boro_gtm.core.config import get_settings

    def explode(*_args, **_kwargs):
        raise RuntimeError("psycopg: column widgets.secret_column does not exist")

    monkeypatch.setattr(routes, "_company_or_404", explode)
    monkeypatch.setenv("GTM_DATABASE_URL", database_url)
    core_db.reset_engine()
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/companies/00000000-0000-0000-0000-000000000000/claims"
            )
        assert response.status_code == 500
        error = _envelope(response.json())
        assert error["code"] == "INTERNAL_ERROR"
        assert "secret_column" not in response.text, (
            "internals must not reach the client"
        )
    finally:
        core_db.reset_engine()
        get_settings.cache_clear()


def test_health_reports_schema_parity(api_client):
    body = api_client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["schema"] == "ok"


def test_health_reports_drift_rather_than_claiming_ok(api_client, migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(text("ALTER TABLE companies DROP COLUMN identity_policy_version"))
    try:
        body = api_client.get("/api/v1/health").json()
        assert body["schema"] == "drift"
        assert body["status"] == "degraded"
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE companies ADD COLUMN identity_policy_version "
                    "VARCHAR(16) NOT NULL DEFAULT '1.0'"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE companies ALTER COLUMN identity_policy_version "
                    "DROP DEFAULT"
                )
            )


def test_the_app_refuses_to_start_against_a_drifted_database(
    migrated_engine, database_url, monkeypatch
):
    """Booting into guaranteed 500s is worse than refusing with a message."""
    from fastapi.testclient import TestClient

    import boro_gtm.core.db as core_db
    from boro_gtm.api.main import create_app
    from boro_gtm.core.config import get_settings
    from boro_gtm.core.schema_check import SchemaDriftError

    with migrated_engine.begin() as conn:
        conn.execute(text("ALTER TABLE discovery_runs DROP COLUMN fetch_completed_at"))
    monkeypatch.setenv("GTM_DATABASE_URL", database_url)
    core_db.reset_engine()
    get_settings.cache_clear()
    try:
        with pytest.raises(SchemaDriftError) as exc:
            with TestClient(create_app()):
                pass
        assert any(
            "fetch_completed_at" in p for p in exc.value.details["problems"]
        )
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE discovery_runs "
                    "ADD COLUMN fetch_completed_at TIMESTAMPTZ"
                )
            )
        core_db.reset_engine()
        get_settings.cache_clear()


def test_the_startup_guard_can_be_disabled(
    migrated_engine, database_url, monkeypatch
):
    """An operator who knows better must be able to boot anyway."""
    from fastapi.testclient import TestClient

    import boro_gtm.core.db as core_db
    from boro_gtm.api.main import create_app
    from boro_gtm.core.config import get_settings

    with migrated_engine.begin() as conn:
        conn.execute(text("ALTER TABLE discovery_runs DROP COLUMN fetch_completed_at"))
    monkeypatch.setenv("GTM_DATABASE_URL", database_url)
    monkeypatch.setenv("GTM_SCHEMA_CHECK_ON_STARTUP", "false")
    core_db.reset_engine()
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            assert client.get("/api/v1/health").json()["schema"] == "drift"
            response = client.get("/api/v1/discovery-runs")
            assert response.status_code == 500
            assert _envelope(response.json())["code"] == "INTERNAL_ERROR"
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE discovery_runs "
                    "ADD COLUMN fetch_completed_at TIMESTAMPTZ"
                )
            )
        core_db.reset_engine()
        get_settings.cache_clear()
