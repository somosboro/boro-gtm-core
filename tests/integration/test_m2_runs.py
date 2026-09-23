"""M2 run lifecycle, job queue and the M1 firewall.

Covers acceptance F1–F9 and G1–G6.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import (
    Company,
    DiscoveryJob,
    ProviderRecordBody,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import DiscoveryRunStatus
from boro_gtm.discovery.providers.fixtures import (
    FixtureJsonDirectoryAdapter,
    json_record,
)
from boro_gtm.discovery.services import firewall, jobs, runs
from boro_gtm.discovery.services.projection import rebuild_projections
from tests.integration.conftest_m2 import run_discovery

pytestmark = pytest.mark.integration


def _records(n: int):
    return [json_record({"legal_name": f"Firm {i} GmbH",
                         "website_domain": f"firm{i}.de",
                         "location": {"country": "DE", "city": "Berlin"}},
                        external_id=f"E{i}") for i in range(n)]


class _ExplodingAdapter(FixtureJsonDirectoryAdapter):
    """Yields some records, then fails — as a rate-limited API does."""

    def __init__(self, records, fail_after: int) -> None:
        super().__init__(records)
        self._fail_after = fail_after

    def search(self, query):
        for index, record in enumerate(self._records):
            if index >= self._fail_after:
                raise RuntimeError("provider returned 429")
            yield record


# --- F1-F4: partial fetch --------------------------------------------------


def test_a_failed_fetch_keeps_the_evidence_it_paid_for(m2_session, provider_json):
    adapter = _ExplodingAdapter(_records(5), fail_after=3)
    run = runs.create_run(m2_session, provider_json)
    report = runs.fetch(m2_session, run, adapter, {"q": "test"})

    assert run.status == DiscoveryRunStatus.PARTIAL_FETCH.value
    assert "429" in (run.error or "")
    assert report.records == 0 or report.records <= 3
    # Nothing canonical was written.
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 0


def test_a_partial_run_refuses_to_write_anything_canonical(m2_session, provider_json):
    adapter = _ExplodingAdapter(_records(5), fail_after=3)
    run = runs.create_run(m2_session, provider_json)
    runs.fetch(m2_session, run, adapter, {"q": "test"})
    runs.normalize_run(m2_session, run, adapter)

    with pytest.raises(runs.PartialFetchNotResolvableError) as exc:
        runs.resolve_run(m2_session, run, adapter)
    assert exc.value.http_status == 409
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 0


def test_partial_resolution_is_possible_only_when_explicitly_opted_into(
    m2_session, provider_json
):
    adapter = _ExplodingAdapter(_records(5), fail_after=3)
    run = runs.create_run(m2_session, provider_json, allow_partial_resolution=True)
    runs.fetch(m2_session, run, adapter, {"q": "test"})
    runs.normalize_run(m2_session, run, adapter)
    stats = runs.resolve_run(m2_session, run, adapter)
    assert stats["entities"] >= 0  # the point is that it did not raise


def test_a_resumed_fetch_does_not_duplicate_evidence(m2_session, provider_json):
    """Re-fetching the same page is a no-op, so resumption is safe."""
    records = _records(3)
    run = runs.create_run(m2_session, provider_json)
    first = runs.fetch(m2_session, run, FixtureJsonDirectoryAdapter(records),
                       {"q": "test"})
    versions_after_first = m2_session.scalar(
        select(func.count()).select_from(ProviderRecordVersion))
    bodies_after_first = m2_session.scalar(
        select(func.count()).select_from(ProviderRecordBody))

    second = runs.fetch(m2_session, run, FixtureJsonDirectoryAdapter(records),
                        {"q": "test"})
    assert first.versions_created == 3
    assert second.versions_created == 0
    assert m2_session.scalar(
        select(func.count()).select_from(ProviderRecordVersion)) == versions_after_first
    assert m2_session.scalar(
        select(func.count()).select_from(ProviderRecordBody)) == bodies_after_first


def test_one_provider_failing_does_not_affect_another(
    m2_session, provider_json, provider_csv
):
    from boro_gtm.discovery.providers.fixtures import FixtureCsvExportAdapter, csv_record

    failing = runs.create_run(m2_session, provider_json)
    runs.fetch(m2_session, failing, _ExplodingAdapter(_records(4), fail_after=0),
               {"q": "test"})
    assert failing.status == DiscoveryRunStatus.PARTIAL_FETCH.value

    _, _, stats = run_discovery(
        m2_session, FixtureCsvExportAdapter(), provider_csv,
        [csv_record({"company": "Healthy GmbH", "domain": "healthy.de",
                     "country": "DE", "city": "Berlin", "zip": "",
                     "staff_min": "", "staff_max": ""})])
    assert stats["created"] == 1
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 1


def test_a_bad_record_does_not_abort_normalization_of_the_rest(
    m2_session, provider_json
):
    from boro_gtm.discovery.domain.models import ProviderRecordNormalization

    class PickyAdapter(FixtureJsonDirectoryAdapter):
        def normalize(self, parsed):
            if parsed.get("legal_name") == "Firm 1 GmbH":
                raise ValueError("unparseable")
            return super().normalize(parsed)

    adapter = PickyAdapter(_records(3))
    run = runs.create_run(m2_session, provider_json)
    report = runs.fetch(m2_session, run, adapter, {"q": "test"})

    assert run.status == DiscoveryRunStatus.FETCHED.value
    assert report.records == 3, "all three records were stored as evidence"
    assert report.normalization_errors == 1

    rows = m2_session.scalars(select(ProviderRecordNormalization)).all()
    assert len(rows) == 3
    failed = [r for r in rows if r.error]
    assert len(failed) == 1
    assert failed[0].normalized_payload is None
    assert "unparseable" in failed[0].error

    stats = runs.resolve_run(m2_session, run, adapter)
    assert stats["entities"] == 3
    # The two interpretable records become companies; the third is parked
    # rather than minting an anonymous identity.
    assert stats["created"] == 2
    assert stats["ambiguous"] == 1
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 2


# --- F5-F7: job queue ------------------------------------------------------


def test_a_claimed_job_is_not_handed_to_a_second_worker(m2_session):
    jobs.enqueue(m2_session, "FETCH", {"n": 1})
    jobs.enqueue(m2_session, "FETCH", {"n": 2})
    first = jobs.claim(m2_session, limit=1)
    second = jobs.claim(m2_session, limit=1)
    assert len(first) == len(second) == 1
    assert first[0].id != second[0].id
    assert jobs.claim(m2_session, limit=5) == []


def test_a_failed_job_retries_then_parks(m2_session):
    job = jobs.enqueue(m2_session, "FETCH", {"n": 1}, max_attempts=2)
    claimed = jobs.claim(m2_session)[0]
    jobs.fail(m2_session, claimed, "boom", retry_in=timedelta(seconds=-1))
    assert claimed.status == "QUEUED"

    again = jobs.claim(m2_session)[0]
    assert again.id == job.id
    jobs.fail(m2_session, again, "boom again")
    assert again.status == "FAILED"
    assert jobs.claim(m2_session) == []


def test_a_job_held_by_a_dead_worker_is_requeued(m2_session):
    jobs.enqueue(m2_session, "FETCH", {"n": 1})
    claimed = jobs.claim(m2_session)[0]
    claimed.locked_at = datetime.now(UTC) - timedelta(hours=2)
    m2_session.flush()

    assert jobs.reset_stale(m2_session, older_than=timedelta(minutes=30)) == 1
    assert m2_session.get(DiscoveryJob, claimed.id).status == "QUEUED"


# --- G1-G6: the M1 firewall -----------------------------------------------


def test_a_discovery_run_writes_nothing_into_m1(imported_session, provider_json):
    """The strong form: M1 row counts are identical before and after."""
    from boro_gtm.discovery import seeds

    seeds.seed_all(imported_session)
    imported_session.flush()
    provider = imported_session.scalars(
        select(__import__("boro_gtm.discovery.domain.models", fromlist=["x"])
               .DiscoveryProvider)).first()

    before = firewall.m1_write_fingerprint(imported_session)
    assert any(count > 0 for count in before.values()), "M1 must hold real data"

    run_discovery(imported_session, FixtureJsonDirectoryAdapter(), provider,
                  _records(4))
    rebuild_projections(imported_session)
    after = firewall.m1_write_fingerprint(imported_session)

    assert after == before
    assert imported_session.scalar(select(func.count()).select_from(Company)) == 4


def test_promoting_discovery_counts_to_market_density_is_refused(m2_session):
    with pytest.raises(firewall.DensityPromotionNotImplementedError) as exc:
        firewall.promote_discovery_count_to_market_observation(
            market_id=None, count=137)
    assert exc.value.http_status == 501
    assert "not market density" in str(exc.value)


def test_the_firewall_watches_every_m1_evidence_table(imported_session):
    """A table added to M1 later must be added here, or this test fails."""
    from boro_gtm.market_intelligence.domain import models as mi

    evidence_tables = {
        mi.MarketObservation.__tablename__,
        mi.ObservationSource.__tablename__,
        mi.MarketScore.__tablename__,
        mi.MarketScoreComponent.__tablename__,
        mi.MarketSizeEstimate.__tablename__,
        mi.MarketSnapshot.__tablename__,
    }
    assert evidence_tables <= set(firewall.M1_WRITE_PROTECTED_TABLES)


# --- F8/F9: a second run over unchanged evidence ---------------------------


def test_a_second_run_sees_the_same_records_and_changes_nothing(
    m2_session, provider_json
):
    """Sighting attribution, honest counters and no duplicated claims."""
    from boro_gtm.discovery.domain.models import CompanyClaim, EntityResolutionDecision

    records = _records(3)
    _, _, first = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json, records)
    claims_after_first = m2_session.scalar(
        select(func.count()).select_from(CompanyClaim))
    decisions_after_first = m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision))

    _, report, second = run_discovery(
        m2_session, FixtureJsonDirectoryAdapter(), provider_json, records)

    assert first["created"] == 3
    assert report.versions_created == 0, "nothing new was observed"
    # The run still sees the records it fetched, through their sightings.
    assert second["entities"] == 3
    assert second["unchanged"] == 3
    assert second["created"] == 0, "the head says CREATED_NEW; this run created nothing"
    assert second["claims"] == 0

    assert m2_session.scalar(
        select(func.count()).select_from(CompanyClaim)) == claims_after_first
    assert m2_session.scalar(
        select(func.count()).select_from(EntityResolutionDecision)
    ) == decisions_after_first
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 3


def test_a_run_that_died_before_resolving_can_be_resolved_later(
    m2_session, provider_json
):
    """The reason attribution goes through sightings: a first run that fetched
    but never resolved must not strand its evidence."""
    records = _records(2)
    adapter = FixtureJsonDirectoryAdapter(records)
    first = runs.create_run(m2_session, provider_json)
    runs.fetch(m2_session, first, adapter, {"q": "test"})
    # ... and the worker dies here, before resolve_run.
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 0

    second = runs.create_run(m2_session, provider_json)
    runs.fetch(m2_session, second, FixtureJsonDirectoryAdapter(records), {"q": "test"})
    stats = runs.resolve_run(m2_session, second, FixtureJsonDirectoryAdapter())

    assert stats["entities"] == 2, "the second run sees the first run's versions"
    assert stats["created"] == 2
    assert m2_session.scalar(select(func.count()).select_from(Company)) == 2
