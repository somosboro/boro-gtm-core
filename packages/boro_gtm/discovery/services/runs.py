"""Discovery run lifecycle (design §7).

The rule that makes partial failure safe:

> A run that has not reached ``FETCHED`` does not write to the canonical
> registry, unless it was created with ``allow_partial_resolution = True``.

Raw evidence already paid for is always retained; the canonical registry is
never touched by an incomplete fetch, because a truncated result set is not
evidence of absence.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.domain.models import (
    CompanyClaim,
    DiscoveryProvider,
    DiscoveryQuery,
    DiscoveryRun,
    ProviderEntity,
    ProviderRecordNormalization,
    ProviderRecordSighting,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import DiscoveryRunStatus
from boro_gtm.discovery.providers.base import CandidateCompany, ProviderAdapter
from boro_gtm.discovery.services.claims import write_claims_for_candidate
from boro_gtm.discovery.services.ingestion import ingest_record
from boro_gtm.discovery.services.resolution import resolve

logger = logging.getLogger(__name__)


class PartialFetchNotResolvableError(GtmError):
    """Raised when a run that never reached FETCHED is asked to resolve."""

    code = "PARTIAL_FETCH_NOT_RESOLVABLE"
    http_status = 409


@dataclass(slots=True)
class FetchReport:
    run_id: uuid.UUID
    status: str
    pages: int = 0
    records: int = 0
    versions_created: int = 0
    bodies_created: int = 0
    error: str | None = None
    #: Records the provider returned that could not be stored at all.
    record_errors: int = 0
    #: Records stored whose payload the adapter could not interpret.
    normalization_errors: int = 0
    entity_ids: list[uuid.UUID] = field(default_factory=list)


def create_run(session: Session, provider: DiscoveryProvider, *,
               market_id: uuid.UUID | None = None,
               vertical_id: uuid.UUID | None = None,
               icp_id: uuid.UUID | None = None,
               channel_id: uuid.UUID | None = None,
               allow_partial_resolution: bool = False,
               adapter_version: str | None = None) -> DiscoveryRun:
    now = datetime.now(UTC)
    run = DiscoveryRun(
        provider_id=provider.id, market_id=market_id, vertical_id=vertical_id,
        icp_id=icp_id, channel_id=channel_id,
        status=DiscoveryRunStatus.PENDING.value,
        adapter_version=adapter_version,
        allow_partial_resolution=allow_partial_resolution,
        created_at=now,
    )
    session.add(run)
    session.flush()
    return run


def fetch(session: Session, run: DiscoveryRun, adapter: ProviderAdapter,
          query_params: dict[str, Any], *, page_size: int = 50,
          start_page: int = 0, commit_page: Any = None) -> FetchReport:
    """Fetch and persist raw evidence, one committed transaction per page.

    Pages commit incrementally on purpose: evidence already paid for must not
    be lost to a later failure.
    """
    provider = session.get(DiscoveryProvider, run.provider_id)
    report = FetchReport(run_id=run.id, status=run.status)
    run.status = DiscoveryRunStatus.FETCHING.value
    run.started_at = run.started_at or datetime.now(UTC)
    session.flush()

    page = start_page
    try:
        records = list(adapter.search({**query_params, "limit": page_size}))
        for offset in range(0, max(len(records), 1), page_size):
            chunk = records[offset:offset + page_size]
            if not chunk and offset:
                break
            # A page may be fetched again after an interrupted run, so the
            # query row is looked up before it is created.
            query = session.scalar(
                select(DiscoveryQuery).where(
                    DiscoveryQuery.discovery_run_id == run.id,
                    DiscoveryQuery.page_number == page,
                )
            )
            if query is None:
                # Insert-or-ignore, then re-read: two workers fetching the same
                # page must not race each other into a unique violation.
                session.execute(
                    pg_insert(DiscoveryQuery)
                    .values(
                        id=uuid.uuid4(), discovery_run_id=run.id,
                        parameters=query_params, cursor=str(page),
                        page_number=page, result_count=len(chunk),
                        issued_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["discovery_run_id", "page_number"]
                    )
                )
                query = session.scalar(
                    select(DiscoveryQuery).where(
                        DiscoveryQuery.discovery_run_id == run.id,
                        DiscoveryQuery.page_number == page,
                    )
                )

            for raw in chunk:
                try:
                    with session.begin_nested():
                        result = ingest_record(session, adapter, provider, raw, query)
                except Exception as exc:  # noqa: BLE001 - one record, not the page
                    report.record_errors += 1
                    logger.warning(
                        "Discovery record could not be ingested; the rest of "
                        "the page is unaffected",
                        extra={"discovery_run_id": str(run.id),
                               "error": str(exc)[:500]},
                    )
                    continue
                report.records += 1
                report.versions_created += int(result.version_created)
                report.bodies_created += int(result.body_created)
                if result.normalization_error:
                    report.normalization_errors += 1
                if result.provider_entity_id not in report.entity_ids:
                    report.entity_ids.append(result.provider_entity_id)
            report.pages += 1
            page += 1
            if commit_page is not None:
                commit_page()
    except Exception as exc:  # noqa: BLE001 - recorded on the run
        run.status = DiscoveryRunStatus.PARTIAL_FETCH.value
        run.error = str(exc)[:4000]
        report.status = run.status
        report.error = str(exc)
        session.flush()
        logger.warning(
            "Discovery fetch failed; raw evidence retained",
            extra={"discovery_run_id": str(run.id), "pages": report.pages},
        )
        return report

    run.status = DiscoveryRunStatus.FETCHED.value
    # Durable: a later stage may move ``status`` on, but it can never make an
    # incomplete fetch look complete.
    run.fetch_completed_at = datetime.now(UTC)
    report.status = run.status
    session.flush()
    return report


def normalize_run(session: Session, run: DiscoveryRun, adapter: ProviderAdapter) -> int:
    """Re-derive normalization over stored versions. No provider contact."""
    if run.fetch_completed_at is not None:
        run.status = DiscoveryRunStatus.NORMALIZING.value
    # A partial fetch keeps saying so; normalization does not launder it.
    session.flush()
    caps = adapter.capabilities()
    versions = _run_versions(session, run)
    count = 0
    for version in versions:
        existing = session.scalar(
            select(ProviderRecordNormalization).where(
                ProviderRecordNormalization.provider_record_version_id == version.id,
                ProviderRecordNormalization.normalizer_version == caps.normalizer_version,
            )
        )
        if existing is not None:
            continue
        try:
            candidate = adapter.normalize(version.parsed_payload)
            payload: dict[str, Any] | None = candidate.__dict__.copy()
            error = None
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the run
            payload, error = None, str(exc)[:2000]
        session.add(ProviderRecordNormalization(
            provider_record_version_id=version.id,
            normalizer_version=caps.normalizer_version,
            normalized_payload=payload, error=error,
            created_at=datetime.now(UTC),
        ))
        count += 1
    session.flush()
    return count


def resolve_run(session: Session, run: DiscoveryRun, adapter: ProviderAdapter,
                *, vertical_ids: dict[str, uuid.UUID] | None = None) -> dict[str, int]:
    """Resolve every entity this run touched, then write claims.

    Refuses to touch the canonical registry unless the run reached ``FETCHED``
    or explicitly opted into partial resolution.
    """
    # The gate is the durable completion timestamp, not the status column,
    # which later stages overwrite (design §10.4).
    if run.fetch_completed_at is None and not run.allow_partial_resolution:
        raise PartialFetchNotResolvableError(
            f"Run {run.id} never completed its fetch (status {run.status!r}). "
            "A run that has not reached FETCHED writes nothing canonical "
            "unless it was created with allow_partial_resolution=true.",
            details={"status": run.status, "run_id": str(run.id),
                     "allow_partial_resolution": run.allow_partial_resolution},
        )

    run.status = DiscoveryRunStatus.RESOLVING.value
    session.flush()

    provider = session.get(DiscoveryProvider, run.provider_id)
    stats = {"entities": 0, "matched": 0, "created": 0, "ambiguous": 0,
             "unchanged": 0, "claims": 0}

    for version in _run_versions(session, run):
        entity = session.get(ProviderEntity, version.provider_entity_id)
        candidate = _candidate_from_version(session, adapter, version)
        outcome = resolve(session, entity, version, candidate, provider)
        stats["entities"] += 1
        # Count what this run *did*, not what the head happens to say: an
        # already-resolved entity has a CREATED_NEW head but created nothing.
        if not outcome.written:
            stats["unchanged"] += 1
        elif outcome.decision == "MATCHED":
            stats["matched"] += 1
        elif outcome.decision == "CREATED_NEW":
            stats["created"] += 1
        elif outcome.decision == "AMBIGUOUS":
            stats["ambiguous"] += 1

        if outcome.company_id is not None and not _already_claimed(
            session, version.id, outcome.decision_id
        ):
            # Claims belong to a (version, decision) pair. Re-running a run
            # over unchanged evidence must not append the same claims again:
            # nothing new was observed, so there is nothing new to assert.
            claims = write_claims_for_candidate(
                session, candidate, version.id, outcome.decision_id,
                vertical_ids=vertical_ids, market_id=run.market_id,
            )
            stats["claims"] += len(claims)

    run.status = DiscoveryRunStatus.COMPLETED.value
    run.completed_at = datetime.now(UTC)
    session.flush()
    return stats


def _already_claimed(session: Session, version_id: uuid.UUID,
                     decision_id: uuid.UUID) -> bool:
    """Whether this (version, decision) pair has already produced claims."""
    return session.scalar(
        select(CompanyClaim.id).where(
            CompanyClaim.provider_record_version_id == version_id,
            CompanyClaim.resolution_decision_id == decision_id,
        ).limit(1)
    ) is not None


def _run_versions(session: Session, run: DiscoveryRun) -> list[ProviderRecordVersion]:
    """Every version this run *saw*, not only the ones it first created.

    Attribution runs through ``provider_record_sightings``, which is what that
    table is for. Keying off ``ProviderRecordVersion.discovery_query_id`` would
    mean a second run over unchanged data saw nothing — every version already
    existed, so the ``ON CONFLICT DO NOTHING`` ingest left them pointing at the
    first run's query, and a re-run would silently resolve zero entities.
    """
    return list(session.scalars(
        select(ProviderRecordVersion)
        .join(
            ProviderRecordSighting,
            ProviderRecordSighting.provider_record_version_id
            == ProviderRecordVersion.id,
        )
        .join(
            DiscoveryQuery,
            DiscoveryQuery.id == ProviderRecordSighting.discovery_query_id,
        )
        .where(DiscoveryQuery.discovery_run_id == run.id)
        .distinct()
        .order_by(ProviderRecordVersion.id)
    ).all())


def _candidate_from_version(session: Session, adapter: ProviderAdapter,
                            version: ProviderRecordVersion) -> CandidateCompany:
    """Prefer stored normalization; fall back to re-deriving it purely."""
    caps = adapter.capabilities()
    stored = session.scalar(
        select(ProviderRecordNormalization).where(
            ProviderRecordNormalization.provider_record_version_id == version.id,
            ProviderRecordNormalization.normalizer_version == caps.normalizer_version,
        )
    )
    if stored is not None:
        if stored.error is not None:
            # Already known to be uninterpretable at this normalizer version.
            # Re-running the adapter would only raise the same error again;
            # an empty candidate parks the record instead.
            return CandidateCompany()
        known = set(CandidateCompany.__dataclass_fields__)
        return CandidateCompany(
            **{k: v for k, v in (stored.normalized_payload or {}).items() if k in known}
        )
    return adapter.normalize(version.parsed_payload)
