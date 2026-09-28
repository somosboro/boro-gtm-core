"""The application layer: run/attempt services, and M3's four job types.

Two boundaries this module exists to keep.

**A run is a question; an attempt is an execution.** No run DTO carries a
status, because a question does not have one — its executions do. No attempt
carries question identity, because that would let mutable execution state
redefine what was asked.

**The queue is M2's.** `discovery_jobs` takes a free-text `job_type`, so M3
adds four values and no infrastructure: no Redis, no broker, no second table.
The queue shares a transaction with the domain writes it triggers, which is the
property a separate datastore would need an outbox to match.

`REBUILD_RESEARCH_PROFILE` is deliberately **not** a job type. A projection
rebuilt by a worker minutes later is a window in which the API answers from
stale derived state and nothing in the row says so, so it runs synchronously at
the completion point and on explicit request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError, NotFoundError
from boro_gtm.discovery.domain.models import Company, DiscoveryJob
from boro_gtm.discovery.services import jobs as job_queue
from boro_gtm.research.domain.models import (
    OperationalResearchAttempt,
    OperationalResearchRun,
)
from boro_gtm.research.fixtures.transport import FixtureTransport
from boro_gtm.research.registry import DEFAULT_TARGET_ATTRIBUTES, RESEARCH_POLICY_VERSION
from boro_gtm.research.services import profiles
from boro_gtm.research.services.pipeline import (
    AttemptAlreadyLiveError,
    get_or_create_run,
    run_pipeline,
    start_attempt,
)

#: **One** job type, because one is what actually runs (M3-ADR-058).
#:
#: The design declared four — DISCOVER_SOURCES, FETCH_ARTIFACT,
#: EXTRACT_ARTIFACT, ASSERT_CLAIMS — on the stated grounds that each has a
#: different failure and retry profile: "a robots denial must not be retried
#: like a timeout". That reasoning is sound and it is about a **production**
#: provider. M3 ships fixture adapters only: every outcome is deterministic and
#: reproducible from local files, so per-stage retry has nothing to retry, and
#: three of the four names were implemented as "skip and succeed".
#:
#: Four names where three mean nothing is worse than one name that is true. The
#: split returns with the provider that needs it.
EXECUTE_RESEARCH = "M3_EXECUTE_RESEARCH"

JOB_TYPES: tuple[str, ...] = (EXECUTE_RESEARCH,)

#: One retry. A fixture execution that failed will fail identically, so the
#: second attempt exists to survive an infrastructure hiccup, not a bad source.
MAX_ATTEMPTS: dict[str, int] = {EXECUTE_RESEARCH: 2}


class RunNotFoundError(NotFoundError):
    """No research question with that id."""

    code = "RESEARCH_RUN_NOT_FOUND"


class AttemptNotFoundError(NotFoundError):
    """No execution with that id."""

    code = "RESEARCH_ATTEMPT_NOT_FOUND"


class AttemptNotTerminalError(GtmError):
    """A retry was requested for an attempt that is still running."""

    code = "RESEARCH_ATTEMPT_NOT_TERMINAL"
    http_status = 409


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Runs — the question
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunView:
    """A question. Deliberately carries no status."""

    id: uuid.UUID
    company_id: uuid.UUID
    research_policy_version: str
    vertical_id: uuid.UUID | None
    target_attribute_keys: list[str]
    research_plan_hash: str
    plan_inputs: dict[str, Any] | None
    created_by: str
    created_at: datetime
    attempt_count: int


def _run_view(session: Session, run: OperationalResearchRun) -> RunView:
    count = session.scalar(
        select(func.count()).select_from(OperationalResearchAttempt).where(
            OperationalResearchAttempt.run_id == run.id
        )
    ) or 0
    return RunView(
        id=run.id, company_id=run.company_id,
        research_policy_version=run.research_policy_version,
        vertical_id=run.vertical_id,
        target_attribute_keys=list(run.target_attribute_keys),
        research_plan_hash=run.research_plan_hash,
        plan_inputs=run.plan_inputs, created_by=run.created_by,
        created_at=run.created_at, attempt_count=count,
    )


def list_runs(
    session: Session,
    *,
    company_id: uuid.UUID | None = None,
    policy_version: str | None = None,
    vertical_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RunView]:
    """Deterministically ordered and bounded. No endpoint returns everything."""
    statement: Select = select(OperationalResearchRun)
    if company_id is not None:
        statement = statement.where(OperationalResearchRun.company_id == company_id)
    if policy_version is not None:
        statement = statement.where(
            OperationalResearchRun.research_policy_version == policy_version
        )
    if vertical_id is not None:
        statement = statement.where(OperationalResearchRun.vertical_id == vertical_id)
    statement = statement.order_by(
        OperationalResearchRun.created_at.desc(), OperationalResearchRun.id
    ).limit(limit).offset(offset)
    return [_run_view(session, run) for run in session.scalars(statement).all()]


def get_run(session: Session, run_id: uuid.UUID) -> RunView:
    run = session.get(OperationalResearchRun, run_id)
    if run is None:
        raise RunNotFoundError(f"no research run {run_id}")
    return _run_view(session, run)


def create_or_reuse_run(
    session: Session,
    *,
    company_id: uuid.UUID,
    vertical_id: uuid.UUID | None = None,
    target_attribute_keys: tuple[str, ...] = DEFAULT_TARGET_ATTRIBUTES,
    plan_inputs: dict[str, Any] | None = None,
    policy_version: str = RESEARCH_POLICY_VERSION,
    created_by: str = "api",
) -> tuple[RunView, bool]:
    """Asking the same question twice does not create two questions."""
    if session.get(Company, company_id) is None:
        raise NotFoundError(f"no company {company_id}")
    run, created = get_or_create_run(
        session, company_id=company_id, vertical_id=vertical_id,
        target_attribute_keys=tuple(target_attribute_keys),
        plan_inputs=plan_inputs, created_by=created_by,
        policy_version=policy_version, now=_now(),
    )
    return _run_view(session, run), created


# ---------------------------------------------------------------------------
# Attempts — the execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptView:
    """An execution. Carries the state a question must not."""

    id: uuid.UUID
    run_id: uuid.UUID
    attempt_number: int
    status: str
    error: str | None
    failure_stage: str | None
    allow_partial_assertion: bool
    attempt_seed_inputs: dict[str, Any] | None
    attempt_seed_inputs_hash: str | None
    created_at: datetime
    started_at: datetime | None
    discovery_completed_at: datetime | None
    fetch_completed_at: datetime | None
    extraction_completed_at: datetime | None
    completed_at: datetime | None


def _attempt_view(attempt: OperationalResearchAttempt) -> AttemptView:
    return AttemptView(
        id=attempt.id, run_id=attempt.run_id, attempt_number=attempt.attempt_number,
        status=attempt.status, error=attempt.error,
        failure_stage=attempt.failure_stage,
        allow_partial_assertion=attempt.allow_partial_assertion,
        attempt_seed_inputs=attempt.attempt_seed_inputs,
        attempt_seed_inputs_hash=attempt.attempt_seed_inputs_hash,
        created_at=attempt.created_at, started_at=attempt.started_at,
        discovery_completed_at=attempt.discovery_completed_at,
        fetch_completed_at=attempt.fetch_completed_at,
        extraction_completed_at=attempt.extraction_completed_at,
        completed_at=attempt.completed_at,
    )


def list_attempts(
    session: Session,
    *,
    run_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AttemptView]:
    statement: Select = select(OperationalResearchAttempt)
    if run_id is not None:
        statement = statement.where(OperationalResearchAttempt.run_id == run_id)
    if company_id is not None:
        statement = statement.join(
            OperationalResearchRun,
            OperationalResearchRun.id == OperationalResearchAttempt.run_id,
        ).where(OperationalResearchRun.company_id == company_id)
    if status is not None:
        statement = statement.where(OperationalResearchAttempt.status == status)
    statement = statement.order_by(
        OperationalResearchAttempt.created_at.desc(), OperationalResearchAttempt.id
    ).limit(limit).offset(offset)
    return [_attempt_view(a) for a in session.scalars(statement).all()]


def get_attempt(session: Session, attempt_id: uuid.UUID) -> AttemptView:
    attempt = session.get(OperationalResearchAttempt, attempt_id)
    if attempt is None:
        raise AttemptNotFoundError(f"no research attempt {attempt_id}")
    return _attempt_view(attempt)


TERMINAL = frozenset({"COMPLETED", "PARTIAL", "FAILED"})


def execute_attempt(
    session: Session,
    *,
    run_id: uuid.UUID,
    transport: FixtureTransport | None = None,
    confirm_sampled: bool = False,
    conditional: bool = True,
) -> AttemptView:
    """Start and run one execution of an existing question.

    A second live attempt raises `AttemptAlreadyLiveError`, which the API maps
    to 409 — never a raw `IntegrityError` naming an index.
    """
    run = session.get(OperationalResearchRun, run_id)
    if run is None:
        raise RunNotFoundError(f"no research run {run_id}")
    result = run_pipeline(
        session, company_id=run.company_id, transport=transport,
        vertical_id=run.vertical_id, confirm_sampled=confirm_sampled,
        conditional=conditional, now=_now(),
    )
    return _attempt_view(result.attempt)


def retry_attempt(
    session: Session, attempt_id: uuid.UUID, *,
    transport: FixtureTransport | None = None,
) -> AttemptView:
    """Retry a terminal attempt as n+1 on the same question."""
    attempt = session.get(OperationalResearchAttempt, attempt_id)
    if attempt is None:
        raise AttemptNotFoundError(f"no research attempt {attempt_id}")
    if attempt.status not in TERMINAL:
        raise AttemptNotTerminalError(
            f"attempt {attempt.attempt_number} is {attempt.status}; only a "
            "terminal attempt can be retried, and retry never reopens one"
        )
    return execute_attempt(session, run_id=attempt.run_id, transport=transport)


def open_attempt(
    session: Session, *, run_id: uuid.UUID, seed_inputs: dict[str, Any] | None = None
) -> AttemptView:
    """Open an attempt without executing it, for staged or queued work."""
    run = session.get(OperationalResearchRun, run_id)
    if run is None:
        raise RunNotFoundError(f"no research run {run_id}")
    return _attempt_view(
        start_attempt(session, run=run, now=_now(), seed_inputs=seed_inputs)
    )


# ---------------------------------------------------------------------------
# Projections — synchronous, never queued
# ---------------------------------------------------------------------------


def rebuild_profiles(
    session: Session, *, company_id: uuid.UUID, run_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Explicit rebuild. Deriving is cheap; being stale is not."""
    company, plan = profiles.rebuild_all(
        session, company_id=company_id, run_id=run_id
    )
    return {
        "company_id": str(company_id),
        "attributes": company.attributes,
        "contradicted": company.contradicted,
        "plan_coverage": float(plan.coverage) if plan is not None else None,
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def enqueue_research(session: Session, *, run_id: uuid.UUID) -> DiscoveryJob:
    """Queue one execution of a research question."""
    if session.get(OperationalResearchRun, run_id) is None:
        raise RunNotFoundError(f"no research run {run_id}")
    return job_queue.enqueue(
        session, EXECUTE_RESEARCH, {"run_id": str(run_id)},
        max_attempts=MAX_ATTEMPTS[EXECUTE_RESEARCH],
    )


def run_worker_once(
    session: Session, *, transport: FixtureTransport | None = None
) -> dict[str, Any] | None:
    """Claim one M3 job and run it, translating domain errors.

    Returns `None` when the queue holds nothing runnable. A domain conflict —
    an attempt already live for that question — fails the job with its message
    rather than letting an exception escape the worker loop.

    The fixture adapter is the only research adapter M3 has: a worker running
    this is exercising the pipeline, not the internet.
    """
    claimed = job_queue.claim(session, job_types=list(JOB_TYPES), limit=1)
    if not claimed:
        return None
    job = claimed[0]
    payload = job.payload or {}
    try:
        view = execute_attempt(
            session, run_id=uuid.UUID(payload["run_id"]), transport=transport
        )
    except (AttemptAlreadyLiveError, RunNotFoundError) as exc:
        job_queue.fail(session, job, str(exc))
        return {"job_type": job.job_type, "job_id": job.id, "failed": str(exc)}
    job_queue.complete(session, job)
    return {
        "job_type": job.job_type, "job_id": job.id,
        "attempt_id": str(view.id), "status": view.status,
    }
