"""PostgreSQL-backed job queue (M2-ADR-010).

``SELECT ... FOR UPDATE SKIP LOCKED``. No Redis, no broker: the queue shares a
transaction with the domain writes it triggers, which is the property a
separate datastore would need an outbox pattern to match.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import DiscoveryJob
from boro_gtm.discovery.enums import JobStatus

logger = logging.getLogger(__name__)


def enqueue(session: Session, job_type: str, payload: dict[str, Any],
            run_after: datetime | None = None, max_attempts: int = 3) -> DiscoveryJob:
    """Enqueue in the caller's transaction, so the job and the writes that
    justify it commit together or not at all."""
    now = datetime.now(UTC)
    job = DiscoveryJob(
        job_type=job_type, payload=payload, status=JobStatus.QUEUED.value,
        attempts=0, max_attempts=max_attempts,
        run_after=run_after or now, created_at=now,
    )
    session.add(job)
    session.flush()
    return job


def claim(session: Session, job_types: list[str] | None = None,
          limit: int = 1) -> list[DiscoveryJob]:
    """Claim runnable jobs, skipping rows another worker holds."""
    now = datetime.now(UTC)
    stmt = (
        select(DiscoveryJob)
        .where(
            DiscoveryJob.status == JobStatus.QUEUED.value,
            DiscoveryJob.run_after <= now,
        )
        .order_by(DiscoveryJob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if job_types:
        stmt = stmt.where(DiscoveryJob.job_type.in_(job_types))

    jobs = list(session.scalars(stmt).all())
    for job in jobs:
        job.status = JobStatus.RUNNING.value
        job.attempts += 1
        job.locked_at = now
    session.flush()
    return jobs


def complete(session: Session, job: DiscoveryJob) -> None:
    job.status = JobStatus.DONE.value
    job.locked_at = None
    session.flush()


def fail(session: Session, job: DiscoveryJob, error: str,
         retry_in: timedelta | None = None) -> None:
    """Retry until ``max_attempts``, then park as FAILED."""
    job.last_error = error[:4000]
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED.value
    else:
        job.status = JobStatus.QUEUED.value
        job.run_after = datetime.now(UTC) + (retry_in or timedelta(minutes=1))
    session.flush()


def reset_stale(session: Session, older_than: timedelta = timedelta(minutes=30)) -> int:
    """Requeue jobs whose worker died holding them."""
    cutoff = datetime.now(UTC) - older_than
    result = session.execute(
        update(DiscoveryJob)
        .where(DiscoveryJob.status == JobStatus.RUNNING.value,
               DiscoveryJob.locked_at < cutoff)
        .values(status=JobStatus.QUEUED.value, locked_at=None)
    )
    session.flush()
    return result.rowcount or 0
