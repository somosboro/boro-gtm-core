"""Run one real research attempt against one canonical M2 company.

The whole live path in one place, so that reaching the internet is always an
explicit decision made here and never a default reached by omission.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import Company, CompanyClaim, CompanyName
from boro_gtm.research.domain.models import (
    OperationalResearchGap,
    OperationalResearchPlanProfile,
    OperationalResearchProfile,
)
from boro_gtm.research.live.discovery import (
    BoRoFirstPartyDiscoveryProvider,
    DomainScope,
    NoResearchableDomainError,
    domain_scope,
)
from boro_gtm.research.live.policy import FIRST_PARTY_POLICY_VERSION, CrawlBudget
from boro_gtm.research.live.transport import ProductionWebTransport
from boro_gtm.research.services import gaps as gap_service
from boro_gtm.research.services import review as review_service
from boro_gtm.research.services.pipeline import PipelineResult, run_pipeline

__all__ = [
    "LiveRunReport",
    "NoResearchableDomainError",
    "research_company_live",
]


@dataclass
class LiveRunReport:
    """Everything an operator needs to judge one account's research.

    Deliberately flat and countable. The question after a run is "can I trust
    what this found", and that is answered by numbers plus the ability to walk
    from any claim back to a page.
    """

    company_id: uuid.UUID
    company_name: str | None
    primary_url: str | None
    in_scope_domains: list[str] = field(default_factory=list)
    excluded_domains: dict[str, str] = field(default_factory=dict)

    status: str = "UNKNOWN"
    attempt_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None

    sources_discovered: int = 0
    pages_attempted: int = 0
    pages_fetched: int = 0
    fetch_outcomes: dict[str, int] = field(default_factory=dict)
    failed_sources: list[str] = field(default_factory=list)
    bytes_downloaded: int = 0
    refused_unsafe: list[tuple[str, str]] = field(default_factory=list)
    robots_denied: list[str] = field(default_factory=list)
    rejected_out_of_scope: int = 0
    rejected_irrelevant: int = 0
    budget_stopped_at: str | None = None
    unretrieved_sources: int = 0

    claims_created: int = 0
    evidence_created: int = 0
    observed_attributes: list[str] = field(default_factory=list)
    contradicted_attributes: list[str] = field(default_factory=list)
    open_gaps: dict[str, int] = field(default_factory=dict)
    review_candidates: int = 0
    coverage: float | None = None
    required_attributes: int | None = None
    covered_attributes: int | None = None
    confidence_summary: dict | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "company_id": str(self.company_id),
            "company_name": self.company_name,
            "primary_url": self.primary_url,
            "in_scope_domains": self.in_scope_domains,
            "excluded_domains": self.excluded_domains,
            "status": self.status,
            "attempt_id": str(self.attempt_id) if self.attempt_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "sources_discovered": self.sources_discovered,
            "pages_attempted": self.pages_attempted,
            "pages_fetched": self.pages_fetched,
            "fetch_outcomes": self.fetch_outcomes,
            "failed_sources": self.failed_sources,
            "bytes_downloaded": self.bytes_downloaded,
            "refused_unsafe": [list(x) for x in self.refused_unsafe],
            "robots_denied": self.robots_denied,
            "rejected_out_of_scope": self.rejected_out_of_scope,
            "rejected_irrelevant": self.rejected_irrelevant,
            "budget_stopped_at": self.budget_stopped_at,
            "unretrieved_sources": self.unretrieved_sources,
            "claims_created": self.claims_created,
            "evidence_created": self.evidence_created,
            "observed_attributes": self.observed_attributes,
            "contradicted_attributes": self.contradicted_attributes,
            "open_gaps": self.open_gaps,
            "review_candidates": self.review_candidates,
            "coverage": self.coverage,
            "required_attributes": self.required_attributes,
            "covered_attributes": self.covered_attributes,
            "confidence_summary": self.confidence_summary,
            "error": self.error,
            "policy_version": FIRST_PARTY_POLICY_VERSION,
        }


def research_company_live(
    session: Session,
    *,
    company_id: uuid.UUID,
    budget: CrawlBudget | None = None,
    human_seed_urls: tuple[str, ...] = (),
    now: datetime | None = None,
    conditional: bool = True,
    transport: ProductionWebTransport | None = None,
    respect_robots: bool = True,
) -> LiveRunReport:
    """Research one company from its own public website. Reaches the internet.

    Scope is resolved **before** anything is fetched: if M2 has no domain this
    company may speak for, the run does not happen. Guessing from the company
    name is how one contractor's evidence ends up on another's account, and
    there is no way to repair that afterwards (M3-ADR-074).
    """
    budget = budget or CrawlBudget()
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company {company_id}")

    name = session.scalar(
        select(CompanyName.name_raw)
        .where(CompanyName.company_id == company_id)
        .order_by(CompanyName.is_primary.desc(), CompanyName.name_type,
                  CompanyName.name_normalized)
        .limit(1)
    )
    report = LiveRunReport(company_id=company_id, company_name=name, primary_url=None)

    try:
        scope = domain_scope(session, company_id)
    except NoResearchableDomainError as exc:
        report.status = "NO_RESEARCHABLE_DOMAIN"
        report.error = str(exc)
        return report

    report.primary_url = scope.primary
    report.in_scope_domains = sorted(scope.registrable)
    report.excluded_domains = dict(sorted(scope.excluded.items()))

    owns_transport = transport is None
    transport = transport or ProductionWebTransport(
        budget=budget, respect_robots=respect_robots
    )
    provider = BoRoFirstPartyDiscoveryProvider(
        transport, scope=scope, budget=budget, human_seed_urls=human_seed_urls
    )
    try:
        result = run_pipeline(
            session, company_id=company_id, transport=transport, provider=provider,
            now=now, conditional=conditional, max_retrievals=budget.max_retrievals,
        )
    finally:
        if owns_transport:
            transport.close()

    _fill(session, report, result, transport, provider, scope)
    return report


def _fill(
    session: Session, report: LiveRunReport, result: PipelineResult,
    transport: ProductionWebTransport, provider: BoRoFirstPartyDiscoveryProvider,
    scope: DomainScope,
) -> None:
    report.status = result.status
    report.attempt_id = result.attempt.id
    report.run_id = result.attempt.run_id
    report.sources_discovered = result.sources_seen
    report.fetch_outcomes = dict(sorted(result.fetch_outcomes.items()))
    report.pages_attempted = len(transport.stats.requests)
    report.pages_fetched = result.fetch_outcomes.get("OK", 0)
    report.failed_sources = list(result.failed_sources)
    report.bytes_downloaded = transport.stats.bytes_downloaded
    report.refused_unsafe = list(transport.stats.refused_unsafe)
    report.robots_denied = list(transport.stats.robots_denied)
    report.rejected_out_of_scope = len(set(provider.rejected_out_of_scope))
    report.rejected_irrelevant = len(set(provider.rejected_irrelevant))
    report.budget_stopped_at = result.budget_stopped_at or provider.budget_exhausted_at
    report.unretrieved_sources = len(result.unretrieved_sources)
    report.claims_created = result.claims_created
    report.evidence_created = result.evidence_created

    profile = session.get(OperationalResearchProfile, report.company_id)
    if profile is not None:
        facts = profile.facts or {}
        report.observed_attributes = sorted(facts)
        report.contradicted_attributes = sorted(profile.contradictions or {})

    open_gaps: dict[str, int] = {}
    for gap in session.scalars(select(OperationalResearchGap).where(
        OperationalResearchGap.run_id == report.run_id
    )).all():
        if gap_service.current_status(session, gap.id) in gap_service.TERMINAL_GAP_KINDS:
            continue
        open_gaps[gap.gap_kind] = open_gaps.get(gap.gap_kind, 0) + 1
    report.open_gaps = dict(sorted(open_gaps.items()))

    report.review_candidates = len(review_service.pending_candidates(
        session, company_id=report.company_id, limit=500
    ))

    plan = session.get(OperationalResearchPlanProfile, report.run_id)
    if plan is not None:
        report.coverage = float(plan.coverage) if plan.coverage is not None else None
        report.required_attributes = plan.required_attribute_count
        report.covered_attributes = plan.covered_attribute_count
        report.confidence_summary = plan.confidence_summary


def claim_count(session: Session, company_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count()).select_from(CompanyClaim)
        .where(CompanyClaim.subject_company_id == company_id)
    ) or 0
