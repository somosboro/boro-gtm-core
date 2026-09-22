"""A-5 — snapshot identity is canonical and entry-point independent."""

from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import func, select

from boro_gtm.core.errors import ImportConflictError
from boro_gtm.market_intelligence.domain.models import MarketObservation, MarketSnapshot
from boro_gtm.market_intelligence.importers.contract import canonical_bytes
from boro_gtm.market_intelligence.importers.snapshot_importer import (
    SnapshotImporter,
    compute_sha256,
    snapshot_identity,
)
from tests.conftest import SOURCE_JSON

pytestmark = pytest.mark.integration


def _reordered(payload: dict) -> dict:
    """Same document, different key order and whitespace."""
    text = json.dumps(payload, indent=4, sort_keys=False)
    reparsed = json.loads(text)
    # Reverse top-level key order so ordering genuinely differs.
    return {k: reparsed[k] for k in reversed(list(reparsed))}


# --- pure identity function -------------------------------------------------


def test_canonical_bytes_ignore_formatting(source_payload) -> None:
    assert canonical_bytes(source_payload) == canonical_bytes(_reordered(source_payload))


def test_identity_ignores_formatting(source_payload) -> None:
    assert snapshot_identity(source_payload) == snapshot_identity(_reordered(source_payload))


def test_identity_changes_with_semantics(source_payload) -> None:
    mutated = copy.deepcopy(source_payload)
    mutated["markets"][0]["why_it_matters"] = "changed"
    assert snapshot_identity(mutated) != snapshot_identity(source_payload)


def test_identity_is_not_the_file_byte_digest(source_payload, source_bytes) -> None:
    """The two digests are different concepts and both are retained."""
    assert snapshot_identity(source_payload) != compute_sha256(source_bytes)


# --- cross-entry-point behaviour -------------------------------------------


def test_file_then_payload_is_idempotent(session, source_payload) -> None:
    """The A-5 regression: identical content through either door is a no-op."""
    first = SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()
    second = SnapshotImporter(session).import_payload(source_payload)
    session.flush()

    assert first.created is True
    assert second.created is False
    assert second.snapshot_id == first.snapshot_id
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 1


def test_payload_then_file_is_idempotent(session, source_payload) -> None:
    first = SnapshotImporter(session).import_payload(source_payload)
    session.flush()
    second = SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()

    assert first.created is True
    assert second.created is False
    assert second.snapshot_id == first.snapshot_id


def test_reordered_and_reindented_payload_is_a_no_op(session, source_payload) -> None:
    SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()
    observations_before = session.scalar(
        select(func.count()).select_from(MarketObservation)
    )

    result = SnapshotImporter(session).import_payload(_reordered(source_payload))
    session.flush()

    assert result.created is False
    assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 1
    assert (
        session.scalar(select(func.count()).select_from(MarketObservation))
        == observations_before
    )


def test_mutated_payload_under_same_key_conflicts(session, source_payload) -> None:
    SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()

    mutated = copy.deepcopy(source_payload)
    mutated["markets"][0]["why_it_matters"] = "TAMPERED AFTER PUBLICATION"
    with pytest.raises(ImportConflictError) as exc:
        SnapshotImporter(session).import_payload(mutated)

    assert exc.value.code == "IMPORT_CONFLICT"
    assert exc.value.details["existing_sha256"] != exc.value.details["incoming_sha256"]


def test_stored_payload_is_not_mutated_by_a_rejected_import(
    session, source_payload
) -> None:
    SnapshotImporter(session).import_file(SOURCE_JSON)
    session.flush()

    mutated = copy.deepcopy(source_payload)
    mutated["markets"][0]["why_it_matters"] = "TAMPERED AFTER PUBLICATION"
    # The conflict is raised before any write, so the session stays usable.
    with pytest.raises(ImportConflictError):
        SnapshotImporter(session).import_payload(mutated)

    snapshot = session.scalar(select(MarketSnapshot))
    assert "TAMPERED" not in snapshot.raw_payload["markets"][0]["why_it_matters"]


def test_byte_digest_is_recorded_only_for_file_imports(session, source_payload) -> None:
    result = SnapshotImporter(session).import_payload(source_payload)
    session.flush()
    snapshot = session.get(MarketSnapshot, __import__("uuid").UUID(result.snapshot_id))
    assert snapshot.sha256 == snapshot_identity(source_payload)
    assert snapshot.source_file_sha256 is None
    assert snapshot.source_filename is None
