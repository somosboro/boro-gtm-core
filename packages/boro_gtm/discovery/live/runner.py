"""Execute a bounded live discovery plan through the existing M2 machinery.

This replaces exactly one thing: the page-chunking loop in
`discovery.services.runs.fetch`, which assumes a run is one query paginated by
storage. A plan is many queries across many metros, and each provider page must
stay its own `DiscoveryQuery` so every stored record can name the intent, metro
and page that found it (§9).

Everything else is M2's, unchanged: `ingest_record` writes the entity, the
version, the body, the sighting and the normalization; `normalize_run` and
`resolve_run` do the rest. There is no parallel dedupe here and no canonical
write (M2-ADR-041).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import (
    Company,
    CompanyDomain,
    DiscoveryProvider,
    DiscoveryQuery,
    DiscoveryRun,
    EntityResolutionDecision,
    ProviderEntity,
)
from boro_gtm.discovery.enums import DiscoveryRunStatus
from boro_gtm.discovery.live.plan import PlannedRun, PlanStep
from boro_gtm.discovery.providers.google_places import (
    GooglePlacesAdapter,
    MissingCredentialError,
    ProviderQuotaError,
    api_key,
)
from boro_gtm.discovery.services.ingestion import ingest_record
from boro_gtm.discovery.services.projection import rebuild_projections
from boro_gtm.discovery.services.runs import create_run, normalize_run, resolve_run

__all__ = ["LiveDiscoveryReport", "preflight_credential", "run_live_discovery"]


@dataclass
class LiveDiscoveryReport:
    """What the operator needs to judge one discovery run."""

    run_id: uuid.UUID | None = None
    provider: str = "google_places"
    query_plan_version: str = ""
    metros: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)

    status: str = "PENDING"
    queries_planned: int = 0
    queries_issued: int = 0
    pages_issued: int = 0
    records_fetched: int = 0
    record_errors: int = 0
    normalization_errors: int = 0
    provider_entities: int = 0
    budget_stopped_at: str | None = None
    provider_error: str | None = None

    normalized: int = 0
    canonical_companies: int = 0
    new_companies: int = 0
    matched_companies: int = 0
    ambiguous_entities: int = 0
    companies_with_identity_domain: int = 0
    companies_without_domain: int = 0
    fetch_complete: bool = False

    def as_dict(self) -> dict:
        return {
            "run_id": str(self.run_id) if self.run_id else None,
            "provider": self.provider,
            "query_plan_version": self.query_plan_version,
            "metros": self.metros,
            "intents": self.intents,
            "status": self.status,
            "fetch_complete": self.fetch_complete,
            "queries_planned": self.queries_planned,
            "queries_issued": self.queries_issued,
            "pages_issued": self.pages_issued,
            "records_fetched": self.records_fetched,
            "record_errors": self.record_errors,
            "normalization_errors": self.normalization_errors,
            "provider_entities": self.provider_entities,
            "budget_stopped_at": self.budget_stopped_at,
            "provider_error": self.provider_error,
            "normalized": self.normalized,
            "canonical_companies": self.canonical_companies,
            "new_companies": self.new_companies,
            "matched_companies": self.matched_companies,
            "ambiguous_entities": self.ambiguous_entities,
            "companies_with_identity_domain": self.companies_with_identity_domain,
            "companies_without_domain": self.companies_without_domain,
        }


def preflight_credential(explicit: str | None = None) -> None:
    """Fail before any network call when the credential is absent (§23)."""
    api_key(explicit)


def run_live_discovery(
    session: Session,
    *,
    provider: DiscoveryProvider,
    adapter: GooglePlacesAdapter,
    planned: PlannedRun,
    market_id: uuid.UUID | None = None,
    vertical_id: uuid.UUID | None = None,
    icp_id: uuid.UUID | None = None,
    channel_id: uuid.UUID | None = None,
    commit: object | None = None,
) -> LiveDiscoveryReport:
    """Fetch the plan, then normalize and resolve through existing M2.

    A provider or network failure part-way through leaves the run
    `PARTIAL_FETCH` with `fetch_completed_at` unset, so canonical writes stay
    blocked unless the run was explicitly created to allow partial resolution.
    Evidence already paid for is kept.
    """
    report = LiveDiscoveryReport(
        query_plan_version=planned.query_plan_version,
        metros=list(planned.metros),
        intents=list(planned.intents),
        queries_planned=planned.planned_first_page_queries,
    )
    run = create_run(
        session, provider, market_id=market_id, vertical_id=vertical_id,
        icp_id=icp_id, channel_id=channel_id,
        adapter_version=adapter.capabilities().normalizer_version,
    )
    report.run_id = run.id
    run.status = DiscoveryRunStatus.FETCHING.value
    run.started_at = datetime.now(UTC)
    session.flush()

    entity_ids: set[uuid.UUID] = set()
    page_number = 0
    budget = planned.budget

    try:
        for step in planned.steps:
            token: str | None = None
            for page_index in range(budget.max_pages_per_query):
                if report.queries_issued >= budget.max_queries:
                    report.budget_stopped_at = "MAX_QUERIES"
                    break
                if report.records_fetched >= budget.max_results:
                    report.budget_stopped_at = "MAX_RESULTS"
                    break

                paged = PlanStep(step.intent, step.metro, page=page_index,
                                 page_token=token)
                params = paged.as_query(limit=budget.page_size)

                result = adapter.search_page(params)
                report.queries_issued += 1
                report.pages_issued += 1

                # The query row is written **after** the page returns, with its
                # count already set. `discovery_queries` is append-only, so
                # there is no second write to fill the count in later — and a
                # row that records what the query returned is the more honest
                # one anyway.
                query = _query_row(session, run, params, page_number,
                                   result_count=len(result.records))
                page_number += 1

                for raw in result.records:
                    try:
                        ingested = ingest_record(session, adapter, provider, raw, query)
                    except Exception:               # noqa: BLE001 - counted
                        report.record_errors += 1
                        continue
                    report.records_fetched += 1
                    if ingested.normalization_error:
                        report.normalization_errors += 1
                    entity_ids.add(ingested.provider_entity_id)

                if commit is not None:
                    commit()
                token = result.next_page_token
                if not token:
                    break
            if report.budget_stopped_at:
                break
    except (ProviderQuotaError, MissingCredentialError, Exception) as exc:  # noqa: BLE001
        # The provider refused, or the network failed. Truthfully partial: the
        # evidence stays, and `fetch_completed_at` is never set.
        run.status = DiscoveryRunStatus.PARTIAL_FETCH.value
        run.error = str(exc)[:4000]
        report.status = run.status
        report.provider_error = str(exc)[:500]
        report.provider_entities = len(entity_ids)
        session.flush()
        _fill_resolution_counts(session, run, report)
        return report

    run.status = DiscoveryRunStatus.FETCHED.value
    run.fetch_completed_at = datetime.now(UTC)
    report.fetch_complete = True
    report.provider_entities = len(entity_ids)
    session.flush()

    report.normalized = normalize_run(session, run, adapter)
    resolve_run(session, run, adapter)
    session.flush()
    # Names, domains and profiles are projections, rebuilt from the claim
    # ledger. Without this an operator sees canonical companies with no name and
    # no domain, and M3 has no domain to research (§12).
    rebuild_projections(session, triggered_by=f"discovery:{run.id}")
    session.flush()
    run.status = DiscoveryRunStatus.COMPLETED.value
    run.completed_at = datetime.now(UTC)
    report.status = run.status
    session.flush()
    _fill_resolution_counts(session, run, report)
    return report


def _query_row(session: Session, run: DiscoveryRun, params: dict,
               page_number: int, *, result_count: int) -> DiscoveryQuery:
    """One row per provider page, which is the unit of query provenance."""
    query = DiscoveryQuery(
        id=uuid.uuid4(), discovery_run_id=run.id, parameters=params,
        cursor=str(params.get("page_token") or params.get("page")),
        page_number=page_number, result_count=result_count,
        issued_at=datetime.now(UTC),
    )
    session.add(query)
    session.flush()
    return query


def _fill_resolution_counts(
    session: Session, run: DiscoveryRun, report: LiveDiscoveryReport
) -> None:
    """Counts an operator reads, derived from what M2 actually decided."""
    decisions = session.execute(
        select(EntityResolutionDecision.decision,
               func.count(EntityResolutionDecision.id))
        .join(ProviderEntity,
              ProviderEntity.id == EntityResolutionDecision.provider_entity_id)
        .where(ProviderEntity.provider_id == run.provider_id)
        .group_by(EntityResolutionDecision.decision)
    ).all()
    by_decision = {d: n for d, n in decisions}
    report.new_companies = by_decision.get("CREATED_NEW", 0)
    report.matched_companies = by_decision.get("MATCHED", 0)
    report.ambiguous_entities = by_decision.get("AMBIGUOUS", 0)

    report.canonical_companies = session.scalar(
        select(func.count()).select_from(Company)
    ) or 0
    report.companies_with_identity_domain = session.scalar(
        select(func.count(func.distinct(CompanyDomain.company_id)))
        .where(CompanyDomain.domain_role == "IDENTITY")
    ) or 0
    report.companies_without_domain = max(
        report.canonical_companies - report.companies_with_identity_domain, 0
    )
