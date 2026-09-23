"""Closed-vocabulary filters refuse a value they do not contain.

Before this, ``?status=NOPE`` returned ``200 []``. On a review queue that
reads as "nothing to review", which is the wrong answer to give quietly: a
typo and an empty queue looked identical.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

INVALID = "DEFINITELY_NOT_A_VALID_VALUE"

VOCABULARY_FILTERS = [
    ("/api/v1/companies", "lifecycle_status"),
    ("/api/v1/discovery-runs", "status"),
    ("/api/v1/entity-resolution/decisions", "decision"),
    ("/api/v1/entity-resolution/decisions", "method"),
    ("/api/v1/research-gaps", "status"),
    ("/api/v1/research-gaps", "priority"),
    ("/api/v1/score-runs", "kind"),
    ("/api/v1/markets/DE/observations", "fact_type"),
]


@pytest.mark.parametrize(("path", "field"), VOCABULARY_FILTERS)
def test_an_invalid_filter_value_is_refused_by_name(api_client, path, field):
    response = api_client.get(path, params={field: INVALID})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["field"] == field
    assert error["details"]["valid"], "the refusal must say what is allowed"
    assert INVALID not in error["details"]["valid"]


@pytest.fixture
def imported_api(api_client, migrated_engine):
    """The API over a committed snapshot, so market-scoped paths resolve."""
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from boro_gtm.market_intelligence.importers.snapshot_importer import SnapshotImporter
    from tests.conftest import _ALL_TABLES, SOURCE_JSON

    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    db = factory()
    db.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    SnapshotImporter(db).import_file(SOURCE_JSON)
    db.commit()
    db.close()
    try:
        yield api_client
    finally:
        cleanup = factory()
        cleanup.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
        cleanup.commit()
        cleanup.close()


@pytest.mark.parametrize(("path", "field"), VOCABULARY_FILTERS)
def test_every_documented_value_is_accepted_in_either_case(imported_api, path, field):
    """The vocabularies are not uniformly upper-case, so both spellings work.

    ``ScoreRunKind`` is lower-case while the rest are upper. A helper that
    simply upper-cased the input would reject a value its own vocabulary
    contains — which is how this test earned its place.
    """
    allowed = imported_api.get(path, params={field: INVALID}).json()["error"][
        "details"
    ]["valid"]
    for value in allowed:
        for spelling in (value, value.lower(), value.upper()):
            response = imported_api.get(path, params={field: spelling})
            assert response.status_code == 200, (
                f"{field}={spelling!r} was refused although {value!r} is valid: "
                f"{response.text}"
            )


def test_an_omitted_filter_is_not_treated_as_a_value(api_client):
    unfiltered = api_client.get("/api/v1/companies")
    empty_string = api_client.get("/api/v1/companies", params={"lifecycle_status": ""})
    assert unfiltered.status_code == empty_string.status_code == 200
    assert unfiltered.json() == empty_string.json()


def test_the_helper_returns_the_vocabularys_own_spelling():
    from boro_gtm.core.enums import ResearchGapStatus, ScoreRunKind, coerce_vocabulary
    from boro_gtm.core.errors import ValidationError

    assert coerce_vocabulary("native_recalculation", ScoreRunKind, "kind") == (
        ScoreRunKind.NATIVE_RECALCULATION.value
    )
    assert coerce_vocabulary("NATIVE_RECALCULATION", ScoreRunKind, "kind") == (
        ScoreRunKind.NATIVE_RECALCULATION.value
    )
    assert coerce_vocabulary("open", ResearchGapStatus, "status") == (
        ResearchGapStatus.OPEN.value
    )
    assert coerce_vocabulary(None, ScoreRunKind, "kind") is None
    with pytest.raises(ValidationError) as exc:
        coerce_vocabulary("nope", ScoreRunKind, "kind")
    assert exc.value.details["field"] == "kind"
