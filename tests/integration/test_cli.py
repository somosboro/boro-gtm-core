"""The CLI answers a user's mistake with a message, never a traceback.

A stack trace through the ``json`` module tells someone who mistyped a path
nothing they can act on, and it makes a routine mistake look like a crash.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from boro_gtm.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _run(*args: str, env: dict[str, str] | None = None):
    return runner.invoke(app, list(args), env=env or {}, catch_exceptions=False)


def test_version_reports_the_package_version():
    from boro_gtm import __version__

    result = _run("version")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["version"] == __version__


def test_importing_a_file_that_is_not_json_is_a_message_not_a_traceback(tmp_path):
    bad = tmp_path / "not-a-snapshot.md"
    bad.write_text("# This is markdown, not JSON\n", encoding="utf-8")

    result = runner.invoke(app, ["market-intelligence", "import", str(bad)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert bad.name in payload["error"]["message"]
    assert payload["error"]["details"]["line"] == 1


def test_importing_a_json_array_is_refused_by_shape(tmp_path):
    bad = tmp_path / "array.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")

    result = runner.invoke(app, ["market-intelligence", "import", str(bad)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "JSON object" in payload["error"]["message"]


def test_importing_non_utf8_bytes_is_refused(tmp_path):
    bad = tmp_path / "binary.json"
    bad.write_bytes(b"\xff\xfe\x00\x01not text")

    result = runner.invoke(app, ["market-intelligence", "import", str(bad)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_a_missing_file_is_refused_by_the_argument_parser(tmp_path):
    result = runner.invoke(
        app, ["market-intelligence", "import", str(tmp_path / "absent.json")]
    )
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_an_unknown_fixture_provider_names_the_known_ones(tmp_path):
    fixture = tmp_path / "records.json"
    fixture.write_text("[]", encoding="utf-8")
    result = runner.invoke(
        app,
        ["discovery", "run", "--provider", "nope", "--fixture", str(fixture)],
    )
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "fixture_json_directory" in result.output


def test_an_unknown_market_is_reported_as_not_found(tmp_path, database_url, monkeypatch):
    """It used to surface as INTERNAL_ERROR, which is what a bug looks like."""
    fixture = tmp_path / "records.json"
    fixture.write_text(
        json.dumps([{"external_id": "X", "payload": {"legal_name": "X GmbH"}}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("GTM_DATABASE_URL", database_url)

    import boro_gtm.core.db as core_db
    from boro_gtm.core.config import get_settings

    core_db.reset_engine()
    get_settings.cache_clear()
    try:
        result = runner.invoke(
            app,
            [
                "discovery", "run",
                "--provider", "fixture_json_directory",
                "--fixture", str(fixture),
                "--market", "ZZ",
            ],
        )
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "NOT_FOUND" in result.output
        assert "INTERNAL_ERROR" not in result.output
    finally:
        core_db.reset_engine()
        get_settings.cache_clear()


def test_no_command_prints_help_rather_than_failing():
    result = runner.invoke(app, [])
    assert "Traceback" not in result.output
    assert "discovery" in result.output and "market-intelligence" in result.output
