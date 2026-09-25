"""M3 database-level invariants, against real PostgreSQL.

These are the guarantees the design says must hold *by construction* rather
than by the care of whoever writes a row. Each test tries to violate one and
asserts the database refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from boro_gtm.discovery.domain.models import AttributeDefinition, Company
from boro_gtm.research.domain import models as m
from boro_gtm.research.registry import RESEARCH_ATTRIBUTES, RESEARCH_REGISTRY_VERSION
from boro_gtm.research.seeds import seed_all

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def m3(session):
    seed_all(session)
    session.flush()
    return session


@pytest.fixture
def company(m3):
    row = Company(created_at=NOW, identity_policy_version="1.0", lifecycle_status="ACTIVE")
    m3.add(row)
    m3.flush()
    return row


@pytest.fixture
def run(m3, company):
    row = m.OperationalResearchRun(
        company_id=company.id, research_policy_version="1.0",
        target_attribute_keys=["emergency_service"], plan_inputs={},
        research_plan_hash="a" * 64, created_by="test", created_at=NOW,
    )
    m3.add(row)
    m3.flush()
    return row


@pytest.fixture
def attempt(m3, run):
    row = m.OperationalResearchAttempt(
        run_id=run.id, attempt_number=1, status="PENDING",
        allow_partial_assertion=False, created_at=NOW,
    )
    m3.add(row)
    m3.flush()
    return row


def _body(m3, payload: bytes = b"hello") -> m.ResearchArtifactBody:
    import hashlib

    row = m.ResearchArtifactBody(
        raw_body_sha256=hashlib.sha256(payload).hexdigest(), raw_body=payload,
        byte_length=len(payload), body_retention="RETAINED", first_seen_at=NOW,
    )
    m3.add(row)
    m3.flush()
    return row


def _source(m3, locator="https://acme.de/services") -> m.ResearchSource:
    row = m.ResearchSource(
        normalized_locator=locator, locator_policy_version="1",
        host="acme.de", registrable_domain="acme.de", first_seen_at=NOW,
    )
    m3.add(row)
    m3.flush()
    return row


# --- N. registry fidelity --------------------------------------------------


def test_seeded_registry_matches_the_design_table_exactly(m3):
    """N1: a hand-written count drifted once; this is the mechanical check."""
    seeded = set(
        m3.scalars(
            select(AttributeDefinition.attribute_key).where(
                AttributeDefinition.registry_version == RESEARCH_REGISTRY_VERSION
            )
        ).all()
    )
    assert seeded == {a.key for a in RESEARCH_ATTRIBUTES}
    assert len(seeded) == 31

    owners = set(
        m3.scalars(
            select(AttributeDefinition.owner_milestone).where(
                AttributeDefinition.registry_version == RESEARCH_REGISTRY_VERSION
            )
        ).all()
    )
    assert owners == {"M3"}


def test_seeding_twice_creates_no_duplicates(m3):
    before = m3.scalar(
        select(func.count()).select_from(AttributeDefinition).where(
            AttributeDefinition.registry_version == RESEARCH_REGISTRY_VERSION
        )
    )
    seed_all(m3)
    m3.flush()
    after = m3.scalar(
        select(func.count()).select_from(AttributeDefinition).where(
            AttributeDefinition.registry_version == RESEARCH_REGISTRY_VERSION
        )
    )
    assert before == after == 31


# --- G. attempt lifecycle --------------------------------------------------


def test_a_terminal_attempt_can_never_be_reopened(m3, attempt):
    for status in ("DISCOVERING", "FETCHING", "EXTRACTING", "ASSERTING", "COMPLETED"):
        attempt.status = status
        m3.flush()
    assert attempt.status == "COMPLETED"

    savepoint = m3.begin_nested()
    attempt.status = "DISCOVERING"
    with pytest.raises(DBAPIError) as exc:
        m3.flush()
    assert "illegal attempt transition" in str(exc.value)
    savepoint.rollback()


def test_an_illegal_transition_is_rejected(m3, attempt):
    savepoint = m3.begin_nested()
    attempt.status = "ASSERTING"  # skipping DISCOVERING/FETCHING/EXTRACTING
    with pytest.raises(DBAPIError) as exc:
        m3.flush()
    assert "illegal attempt transition" in str(exc.value)
    savepoint.rollback()


def test_attempt_seed_inputs_freeze_once_execution_begins(m3, attempt):
    attempt.attempt_seed_inputs = {"domains": ["acme.de"]}
    attempt.attempt_seed_inputs_hash = "b" * 64
    attempt.status = "DISCOVERING"
    m3.flush()

    savepoint = m3.begin_nested()
    attempt.attempt_seed_inputs = {"domains": ["acme.de", "acme.com"]}
    with pytest.raises(DBAPIError) as exc:
        m3.flush()
    assert "frozen once execution begins" in str(exc.value)
    savepoint.rollback()


def test_only_one_live_attempt_per_run(m3, run, attempt):
    savepoint = m3.begin_nested()
    m3.add(
        m.OperationalResearchAttempt(
            run_id=run.id, attempt_number=2, status="PENDING",
            allow_partial_assertion=False, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "uq_attempt_live" in str(exc.value)
    savepoint.rollback()


def test_a_terminal_attempt_frees_the_run_for_a_retry(m3, run, attempt):
    for status in ("DISCOVERING", "PARTIAL"):
        attempt.status = status
        m3.flush()
    retry = m.OperationalResearchAttempt(
        run_id=run.id, attempt_number=2, status="PENDING",
        allow_partial_assertion=False, created_at=NOW,
    )
    m3.add(retry)
    m3.flush()
    assert retry.attempt_number == 2
    assert attempt.status == "PARTIAL", "the old attempt stays terminal"


def test_the_run_plan_hash_is_unique(m3, company, run):
    savepoint = m3.begin_nested()
    m3.add(
        m.OperationalResearchRun(
            company_id=company.id, research_policy_version="1.0",
            target_attribute_keys=["erp"], plan_inputs={},
            research_plan_hash=run.research_plan_hash, created_by="t", created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        m3.flush()
    savepoint.rollback()


# --- fetch events and 304 --------------------------------------------------


def test_a_successful_fetch_requires_a_body(m3, attempt):
    source = _source(m3)
    savepoint = m3.begin_nested()
    m3.add(
        m.ResearchFetchEvent(
            source_id=source.id, attempt_id=attempt.id, body_id=None,
            fetch_outcome="OK", http_status=200, retrieved_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "ck_fetch_body_semantics" in str(exc.value)
    savepoint.rollback()


def test_a_failed_fetch_must_not_carry_a_body(m3, attempt):
    source, body = _source(m3), _body(m3)
    savepoint = m3.begin_nested()
    m3.add(
        m.ResearchFetchEvent(
            source_id=source.id, attempt_id=attempt.id, body_id=body.id,
            fetch_outcome="NOT_FOUND", http_status=404, retrieved_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "ck_fetch_body_semantics" in str(exc.value)
    savepoint.rollback()


def test_a_304_validates_a_known_body_and_names_its_validator(m3, attempt):
    """M10: a 304 returns no bytes but asserts the cached body is current."""
    source, body = _source(m3), _body(m3)
    ok = m.ResearchFetchEvent(
        source_id=source.id, attempt_id=attempt.id, body_id=body.id,
        fetch_outcome="OK", http_status=200, etag='"v1"', retrieved_at=NOW,
    )
    m3.add(ok)
    m3.flush()

    revalidated = m.ResearchFetchEvent(
        source_id=source.id, attempt_id=attempt.id, body_id=body.id,
        fetch_outcome="NOT_MODIFIED", http_status=304,
        validated_by="ETAG", validator_value='"v1"',
        retrieved_at=NOW + timedelta(days=7),
    )
    m3.add(revalidated)
    m3.flush()

    assert m3.scalar(
        select(func.count()).select_from(m.ResearchArtifactBody)
    ) == 1, "no bytes were fabricated"
    assert m3.scalar(select(func.count()).select_from(m.ResearchFetchEvent)) == 2


def test_a_304_without_a_validator_is_rejected(m3, attempt):
    source, body = _source(m3), _body(m3)
    savepoint = m3.begin_nested()
    m3.add(
        m.ResearchFetchEvent(
            source_id=source.id, attempt_id=attempt.id, body_id=body.id,
            fetch_outcome="NOT_MODIFIED", http_status=304, retrieved_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "ck_fetch_body_semantics" in str(exc.value)
    savepoint.rollback()


def test_the_same_bytes_from_two_sources_are_one_body(m3, attempt):
    """L9/A4: byte identity is global, not per source."""
    a, b = _source(m3, "https://acme.de/doc.pdf"), _source(m3, "https://cdn.acme.de/doc.pdf")
    body = _body(m3, b"identical bytes")
    for source, declared in ((a, "text/plain"), (b, "text/html")):
        m3.add(
            m.ResearchFetchEvent(
                source_id=source.id, attempt_id=attempt.id, body_id=body.id,
                fetch_outcome="OK", http_status=200,
                declared_content_type=declared, retrieved_at=NOW,
            )
        )
    m3.flush()

    assert m3.scalar(select(func.count()).select_from(m.ResearchArtifactBody)) == 1
    declared_types = set(
        m3.scalars(select(m.ResearchFetchEvent.declared_content_type)).all()
    )
    assert declared_types == {"text/plain", "text/html"}, (
        "a per-retrieval fact must not be forced onto the deduplicated body"
    )
    body_columns = {c.name for c in m.ResearchArtifactBody.__table__.columns}
    assert "declared_content_type" not in body_columns
    assert "sniffed_content_type" not in body_columns


# --- append-only and one-way prune ----------------------------------------


@pytest.mark.parametrize(
    "table",
    ["research_source_discoveries", "research_fetch_events", "research_evidence_items",
     "claim_evidence_links", "operational_research_gap_events", "research_sources",
     "research_artifacts", "operational_research_gaps", "identity_review_signals"],
)
def test_evidence_tables_reject_updates(m3, table):
    triggers = m3.execute(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relname = :t AND t.tgname = :n"
        ),
        {"t": table, "n": f"{table}_no_update"},
    ).scalar_one()
    assert triggers == 1, f"{table} has no append-only trigger"


def test_a_body_may_be_pruned_exactly_once_and_nothing_else(m3):
    body = _body(m3, b"prunable")
    m3.execute(
        text(
            "UPDATE research_artifact_bodies SET raw_body = NULL, "
            "body_retention = 'PRUNED', pruned_at = now() WHERE id = :i"
        ),
        {"i": body.id},
    )
    m3.flush()

    savepoint = m3.begin_nested()
    with pytest.raises(DBAPIError) as exc:
        m3.execute(
            text("UPDATE research_artifact_bodies SET byte_length = 99 WHERE id = :i"),
            {"i": body.id},
        )
    assert "one-way prune" in str(exc.value)
    savepoint.rollback()


# --- provenance binding ----------------------------------------------------


def test_evidence_cannot_mix_bodies_across_its_three_paths(m3, attempt):
    """L2: the composite FKs make a mismatched lineage unrepresentable."""
    source = _source(m3)
    body_x, body_y = _body(m3, b"body X"), _body(m3, b"body Y")

    artifact = m.ResearchArtifact(
        canonicalization_strategy="PLAINTEXT_V1", canonicalization_version="1",
        canonical_content_hash="c" * 64, first_seen_at=NOW,
    )
    m3.add(artifact)
    m3.flush()
    derivation = m.ResearchArtifactDerivation(
        body_id=body_x.id, artifact_id=artifact.id,
        canonicalization_strategy="PLAINTEXT_V1", canonicalization_version="1",
        canonical_content_hash="c" * 64, derivation_status="OK",
        source_published_granularity="UNDATED", derived_at=NOW,
    )
    text_derivation = m.ResearchTextDerivation(
        body_id=body_x.id, text_extraction_policy_version="1",
        redaction_policy_version="1", text_derivation_contract_hash="d" * 64,
        extracted_text="body X text", derived_at=NOW,
    )
    m3.add_all([derivation, text_derivation])
    m3.flush()
    extraction = m.ResearchExtraction(
        text_derivation_id=text_derivation.id, body_id=body_x.id,
        extractor_kind="RULE", extractor_id="fixture", extractor_version="1",
        output_schema_version="1", determinism="DETERMINISTIC",
        extraction_contract_hash="e" * 64, created_at=NOW,
    )
    m3.add(extraction)
    m3.flush()
    fetch_y = m.ResearchFetchEvent(
        source_id=source.id, attempt_id=attempt.id, body_id=body_y.id,
        fetch_outcome="OK", http_status=200, retrieved_at=NOW,
    )
    m3.add(fetch_y)
    m3.flush()

    savepoint = m3.begin_nested()
    m3.add(
        m.ResearchEvidenceItem(
            extraction_id=extraction.id,       # over body X
            fetch_event_id=fetch_y.id,         # a retrieval of body Y
            artifact_derivation_id=derivation.id,
            body_id=body_x.id, source_id=source.id,
            locator={"kind": "text"}, locator_hash="f" * 64,
            publisher_key="acme.de", publisher_policy_version="1", created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "fk_evidence_fetch_same_body" in str(exc.value)
    savepoint.rollback()


def test_one_body_supports_two_canonicalization_versions(m3):
    """M4/A9: the derivation join is what makes this representable."""
    body = _body(m3, b"<p>same bytes</p>")
    artifacts = []
    for version, content_hash in (("1", "1" * 64), ("2", "2" * 64)):
        artifact = m.ResearchArtifact(
            canonicalization_strategy="HTML_TEXT_V1", canonicalization_version=version,
            canonical_content_hash=content_hash, first_seen_at=NOW,
        )
        m3.add(artifact)
        m3.flush()
        m3.add(
            m.ResearchArtifactDerivation(
                body_id=body.id, artifact_id=artifact.id,
                canonicalization_strategy="HTML_TEXT_V1",
                canonicalization_version=version,
                canonical_content_hash=content_hash, derivation_status="OK",
                source_published_granularity="UNDATED", derived_at=NOW,
            )
        )
        artifacts.append(artifact)
    m3.flush()

    assert m3.scalar(select(func.count()).select_from(m.ResearchArtifactBody)) == 1
    assert m3.scalar(select(func.count()).select_from(m.ResearchArtifactDerivation)) == 2
    assert artifacts[0].id != artifacts[1].id


def test_publication_metadata_lives_on_the_derivation_not_the_artifact(m3):
    """M5: mirrors with different dates must stay one artifact."""
    artifact_columns = {c.name for c in m.ResearchArtifact.__table__.columns}
    assert "source_published_at" not in artifact_columns
    assert "title" not in artifact_columns
    derivation_columns = {c.name for c in m.ResearchArtifactDerivation.__table__.columns}
    assert {"source_published_at", "source_published_granularity", "title",
            "language"} <= derivation_columns


def test_an_invented_publication_date_is_unrepresentable(m3):
    body = _body(m3, b"dated")
    artifact = m.ResearchArtifact(
        canonicalization_strategy="PLAINTEXT_V1", canonicalization_version="1",
        canonical_content_hash="9" * 64, first_seen_at=NOW,
    )
    m3.add(artifact)
    m3.flush()
    savepoint = m3.begin_nested()
    m3.add(
        m.ResearchArtifactDerivation(
            body_id=body.id, artifact_id=artifact.id,
            canonicalization_strategy="PLAINTEXT_V1", canonicalization_version="1",
            canonical_content_hash="9" * 64, derivation_status="OK",
            source_published_at=None,
            source_published_granularity="DATE",  # a date granularity with no date
            derived_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    # PostgreSQL truncates identifiers at 63 characters, so match the
    # surviving prefix rather than the full generated name.
    assert "ck_research_artifact_derivations_published" in str(exc.value) or (
        "CheckViolation" in str(exc.value)
        and "research_artifact_derivations" in str(exc.value)
    )
    savepoint.rollback()


# --- sampled extraction ----------------------------------------------------


def test_a_sampled_extraction_needs_its_own_execution_slot(m3):
    """M11: a UNIQUE key keeping the first sample is a frozen race."""
    body = _body(m3, b"sampled")
    derivation = m.ResearchTextDerivation(
        body_id=body.id, text_extraction_policy_version="1",
        redaction_policy_version="1", text_derivation_contract_hash="7" * 64,
        extracted_text="text", derived_at=NOW,
    )
    m3.add(derivation)
    m3.flush()

    savepoint = m3.begin_nested()
    m3.add(
        m.ResearchExtraction(
            text_derivation_id=derivation.id, body_id=body.id,
            extractor_kind="MODEL", extractor_id="fixture-model", extractor_version="1",
            output_schema_version="1", determinism="SAMPLED", temperature=0.7,
            sample_execution_id=None, extraction_contract_hash="8" * 64, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "ck_sampled_requires_execution_slot" in str(exc.value)
    savepoint.rollback()

    # With a slot, repeated sampling appends rather than colliding.
    for slot in ("run-1", "run-2"):
        m3.add(
            m.ResearchExtraction(
                text_derivation_id=derivation.id, body_id=body.id,
                extractor_kind="MODEL", extractor_id="fixture-model",
                extractor_version="1", output_schema_version="1",
                determinism="SAMPLED", temperature=0.7, sample_execution_id=slot,
                extraction_contract_hash="8" * 64, created_at=NOW,
            )
        )
    m3.flush()
    assert m3.scalar(select(func.count()).select_from(m.ResearchExtraction)) == 2


# --- signals ---------------------------------------------------------------


def _signal(m3, company, attempt):
    signal = m.IdentityReviewSignal(
        company_id=company.id, signal_kind="POSSIBLE_CEASED_TRADING",
        normalized_concern="ceased", signal_policy_version="1",
        signal_fingerprint="s" * 64, first_raised_at=NOW,
    )
    m3.add(signal)
    m3.flush()
    occurrence = m.IdentityReviewSignalOccurrence(
        signal_id=signal.id, occurrence_number=1, raised_by_attempt_id=attempt.id,
        is_open=True, raised_at=NOW,
    )
    m3.add(occurrence)
    m3.flush()
    m3.add(
        m.IdentityReviewSignalEvent(
            occurrence_id=occurrence.id, status="OPEN", actor="system", occurred_at=NOW
        )
    )
    m3.flush()
    return signal, occurrence


def test_a_signal_with_a_null_related_company_cannot_duplicate(m3, company, attempt):
    """M14: NULLS NOT DISTINCT, or the same concern is raised forever."""
    _signal(m3, company, attempt)
    savepoint = m3.begin_nested()
    m3.add(
        m.IdentityReviewSignal(
            company_id=company.id, signal_kind="POSSIBLE_CEASED_TRADING",
            normalized_concern="ceased", signal_policy_version="1",
            signal_fingerprint="s" * 64, first_raised_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        m3.flush()
    savepoint.rollback()


def test_an_actioned_occurrence_cannot_return_to_open(m3, company, attempt):
    """M16."""
    _, occurrence = _signal(m3, company, attempt)
    for status in ("ACKNOWLEDGED", "ACTIONED"):
        m3.add(
            m.IdentityReviewSignalEvent(
                occurrence_id=occurrence.id, status=status, actor="reviewer",
                occurred_at=NOW + timedelta(minutes=len(status)),
            )
        )
        m3.flush()

    savepoint = m3.begin_nested()
    m3.add(
        m.IdentityReviewSignalEvent(
            occurrence_id=occurrence.id, status="OPEN", actor="reviewer",
            occurred_at=NOW + timedelta(hours=1),
        )
    )
    with pytest.raises(DBAPIError) as exc:
        m3.flush()
    assert "illegal signal transition" in str(exc.value)
    savepoint.rollback()


def test_a_terminal_occurrence_frees_the_concern_for_a_new_episode(m3, company, attempt):
    """M16: revisiting means a new occurrence, not a resurrected one."""
    signal, first = _signal(m3, company, attempt)
    m3.add(
        m.IdentityReviewSignalEvent(
            occurrence_id=first.id, status="DISMISSED", actor="reviewer",
            occurred_at=NOW + timedelta(minutes=5),
        )
    )
    m3.flush()
    m3.refresh(first)
    assert first.is_open is False

    second = m.IdentityReviewSignalOccurrence(
        signal_id=signal.id, occurrence_number=2, raised_by_attempt_id=attempt.id,
        supersedes_occurrence_id=first.id, is_open=True, raised_at=NOW,
    )
    m3.add(second)
    m3.flush()
    assert second.supersedes_occurrence_id == first.id


def test_two_open_occurrences_for_one_concern_are_rejected(m3, company, attempt):
    signal, _ = _signal(m3, company, attempt)
    savepoint = m3.begin_nested()
    m3.add(
        m.IdentityReviewSignalOccurrence(
            signal_id=signal.id, occurrence_number=2,
            raised_by_attempt_id=attempt.id, is_open=True, raised_at=NOW,
        )
    )
    with pytest.raises(IntegrityError) as exc:
        m3.flush()
    assert "uq_occurrence_open" in str(exc.value)
    savepoint.rollback()


# --- gaps ------------------------------------------------------------------


def test_a_gap_is_keyed_by_plan_not_by_company(m3, company, run):
    """M2: two plans lacking the same attribute are two gaps."""
    gap_columns = {c.name for c in m.OperationalResearchGap.__table__.columns}
    assert "run_id" in gap_columns
    assert "attempted_source_count" not in gap_columns
    assert "last_attempt_at" not in gap_columns
    assert "resolved_by_claim_id" not in gap_columns

    other = m.OperationalResearchRun(
        company_id=company.id, research_policy_version="1.0",
        target_attribute_keys=["erp"], plan_inputs={},
        research_plan_hash="z" * 64, created_by="test", created_at=NOW,
    )
    m3.add(other)
    m3.flush()
    for plan in (run, other):
        m3.add(
            m.OperationalResearchGap(
                run_id=plan.id, company_id=company.id, attribute_key="erp",
                gap_kind="NO_EVIDENCE", first_raised_at=NOW,
            )
        )
    m3.flush()
    assert m3.scalar(select(func.count()).select_from(m.OperationalResearchGap)) == 2


def test_a_gap_must_open_with_raised_and_terminates(m3, company, run, attempt):
    gap = m.OperationalResearchGap(
        run_id=run.id, company_id=company.id, attribute_key="erp",
        gap_kind="NO_EVIDENCE", first_raised_at=NOW,
    )
    m3.add(gap)
    m3.flush()

    savepoint = m3.begin_nested()
    m3.add(
        m.OperationalResearchGapEvent(
            gap_id=gap.id, event_kind="RESOLVED", attempt_id=attempt.id, occurred_at=NOW
        )
    )
    with pytest.raises(DBAPIError) as exc:
        m3.flush()
    assert "must open with RAISED" in str(exc.value)
    savepoint.rollback()

    m3.add(
        m.OperationalResearchGapEvent(
            gap_id=gap.id, event_kind="RAISED", attempt_id=attempt.id, occurred_at=NOW
        )
    )
    m3.flush()
    m3.add(
        m.OperationalResearchGapEvent(
            gap_id=gap.id, event_kind="ABANDONED", attempt_id=attempt.id,
            occurred_at=NOW + timedelta(minutes=1),
        )
    )
    m3.flush()

    savepoint = m3.begin_nested()
    m3.add(
        m.OperationalResearchGapEvent(
            gap_id=gap.id, event_kind="ATTEMPTED", attempt_id=attempt.id,
            occurred_at=NOW + timedelta(minutes=2),
        )
    )
    with pytest.raises(DBAPIError) as exc:
        m3.flush()
    assert "illegal gap transition" in str(exc.value)
    savepoint.rollback()


def test_repeated_attempts_on_one_source_append_distinct_events(m3, company, run, attempt):
    """L13: ATTEMPTED names its fetch event, so three fetches are three rows."""
    gap = m.OperationalResearchGap(
        run_id=run.id, company_id=company.id, attribute_key="erp",
        gap_kind="NO_EVIDENCE", first_raised_at=NOW,
    )
    source = _source(m3)
    m3.add(gap)
    m3.flush()
    m3.add(
        m.OperationalResearchGapEvent(
            gap_id=gap.id, event_kind="RAISED", attempt_id=attempt.id, occurred_at=NOW
        )
    )
    m3.flush()

    for minute in (1, 2, 3):
        fetch = m.ResearchFetchEvent(
            source_id=source.id, attempt_id=attempt.id, body_id=None,
            fetch_outcome="DENIED", http_status=403,
            retrieved_at=NOW + timedelta(minutes=minute),
        )
        m3.add(fetch)
        m3.flush()
        m3.add(
            m.OperationalResearchGapEvent(
                gap_id=gap.id, event_kind="ATTEMPTED", attempt_id=attempt.id,
                source_id=source.id, fetch_event_id=fetch.id,
                occurred_at=NOW + timedelta(minutes=minute),
            )
        )
    m3.flush()

    derived = m3.execute(
        text(
            "SELECT attempt_count, attempted_source_count, last_attempt_at "
            "FROM current_operational_research_gaps WHERE id = :i"
        ),
        {"i": gap.id},
    ).one()
    assert derived.attempt_count == 3, "three retrievals, three events"
    assert derived.attempted_source_count == 1
    assert derived.last_attempt_at == NOW + timedelta(minutes=3), (
        "last_attempt_at must be the most recent retrieval, not the first"
    )


# --- M3/M4 firewall --------------------------------------------------------


def test_no_m3_table_carries_a_commercial_judgement_column():
    """H6: the boundary fails a build rather than eroding."""
    forbidden = {
        "lead_score", "qualification_score", "icp_fit_score", "pain_score",
        "priority_score", "recommend_contact", "sales_ready", "tier", "grade",
    }
    offenders = []
    for obj in vars(m).values():
        table = getattr(obj, "__table__", None)
        if table is None or not hasattr(obj, "__tablename__"):
            continue
        for column in table.columns:
            if column.name in forbidden:
                offenders.append(f"{table.name}.{column.name}")
    assert offenders == []
