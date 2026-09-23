"""M2 API — discovery, companies, claims, resolution review."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.core.db import get_db
from boro_gtm.core.errors import NotFoundError, ValidationError
from boro_gtm.discovery.api import schemas as s
from boro_gtm.discovery.domain.models import (
    AttributeDefinition,
    Company,
    CompanyClaim,
    CompanyLocation,
    CompanyMarketPresence,
    CompanyProfile,
    CompanyRelationship,
    CompanyVertical,
    DiscoveryProvider,
    DiscoveryQuery,
    DiscoveryRun,
    EntityResolutionDecision,
    EntityResolutionHead,
    ProjectionRun,
    ProviderEntity,
    ProviderRecordBody,
    ProviderRecordSighting,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import ResolutionDecision
from boro_gtm.discovery.registry import ATTRIBUTE_REGISTRY_VERSION
from boro_gtm.discovery.services import projection, resolution

router = APIRouter()


def _company_or_404(db: Session, company_id: uuid.UUID) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError(f"No company {company_id}", details={"company_id": str(company_id)})
    return company


# ---------------------------------------------------------------------------
# Providers and registry
# ---------------------------------------------------------------------------


@router.get("/discovery-providers", response_model=list[s.ProviderOut], tags=["discovery"])
def list_providers(db: Session = Depends(get_db)) -> Any:
    return db.scalars(
        select(DiscoveryProvider).order_by(DiscoveryProvider.provider_key)
    ).all()


@router.get(
    "/attribute-registry", response_model=list[s.AttributeDefinitionOut], tags=["discovery"]
)
def get_attribute_registry(
    db: Session = Depends(get_db),
    version: str = Query(ATTRIBUTE_REGISTRY_VERSION),
) -> Any:
    return db.scalars(
        select(AttributeDefinition)
        .where(AttributeDefinition.registry_version == version)
        .order_by(AttributeDefinition.attribute_key)
    ).all()


# ---------------------------------------------------------------------------
# Discovery runs
# ---------------------------------------------------------------------------


@router.get("/discovery-runs", response_model=list[s.DiscoveryRunOut], tags=["discovery"])
def list_runs(
    db: Session = Depends(get_db),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    stmt = select(DiscoveryRun).order_by(DiscoveryRun.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(DiscoveryRun.status == status.upper())
    return db.scalars(stmt).all()


@router.get(
    "/discovery-runs/{run_id}", response_model=s.DiscoveryRunOut, tags=["discovery"]
)
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise NotFoundError(f"No discovery run {run_id}")
    return run


@router.get(
    "/discovery-runs/{run_id}/queries",
    response_model=list[s.DiscoveryQueryOut],
    tags=["discovery"],
)
def get_run_queries(run_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    return db.scalars(
        select(DiscoveryQuery)
        .where(DiscoveryQuery.discovery_run_id == run_id)
        .order_by(DiscoveryQuery.page_number)
    ).all()


@router.get(
    "/discovery-runs/{run_id}/versions",
    response_model=list[s.RecordVersionOut],
    tags=["discovery"],
)
def get_run_versions(run_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    versions = db.scalars(
        select(ProviderRecordVersion)
        .join(DiscoveryQuery, DiscoveryQuery.id == ProviderRecordVersion.discovery_query_id)
        .where(DiscoveryQuery.discovery_run_id == run_id)
        .order_by(ProviderRecordVersion.retrieved_at)
    ).all()
    return [_version_out(db, v) for v in versions]


def _version_out(db: Session, version: ProviderRecordVersion) -> s.RecordVersionOut:
    out = s.RecordVersionOut.model_validate(version)
    out.body_count = db.scalar(
        select(func.count()).select_from(ProviderRecordBody)
        .where(ProviderRecordBody.provider_record_version_id == version.id)
    ) or 0
    out.sighting_count = db.scalar(
        select(func.count()).select_from(ProviderRecordSighting)
        .where(ProviderRecordSighting.provider_record_version_id == version.id)
    ) or 0
    return out


@router.get(
    "/provider-entities/{entity_id}/versions",
    response_model=list[s.RecordVersionOut],
    tags=["discovery"],
)
def get_entity_versions(entity_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    versions = db.scalars(
        select(ProviderRecordVersion)
        .where(ProviderRecordVersion.provider_entity_id == entity_id)
        .order_by(ProviderRecordVersion.retrieved_at)
    ).all()
    return [_version_out(db, v) for v in versions]


@router.get(
    "/provider-entities/{entity_id}/resolution-chain",
    response_model=list[s.DecisionOut],
    tags=["resolution"],
)
def get_resolution_chain(entity_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    """Root → head, in order. The chain is linear by construction."""
    decisions = db.scalars(
        select(EntityResolutionDecision)
        .where(EntityResolutionDecision.provider_entity_id == entity_id)
    ).all()
    by_parent = {d.supersedes_decision_id: d for d in decisions}
    chain, node = [], by_parent.get(None)
    while node is not None:
        chain.append(node)
        node = by_parent.get(node.id)
    return chain


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


@router.get("/companies", response_model=list[s.CompanyOut], tags=["companies"])
def list_companies(
    db: Session = Depends(get_db),
    market_id: uuid.UUID | None = Query(None),
    vertical_id: uuid.UUID | None = Query(None),
    lifecycle_status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Any:
    stmt = select(Company)
    if lifecycle_status:
        stmt = stmt.where(Company.lifecycle_status == lifecycle_status.upper())
    if market_id:
        stmt = stmt.where(
            Company.id.in_(
                select(CompanyMarketPresence.company_id)
                .where(CompanyMarketPresence.market_id == market_id)
            )
        )
    if vertical_id:
        stmt = stmt.where(
            Company.id.in_(
                select(CompanyVertical.company_id)
                .where(CompanyVertical.vertical_id == vertical_id)
            )
        )
    companies = db.scalars(
        stmt.order_by(Company.created_at, Company.id).limit(limit).offset(offset)
    ).all()
    return [_company_out(db, c) for c in companies]


@router.get("/companies/{company_id}", response_model=s.CompanyOut, tags=["companies"])
def get_company(company_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    return _company_out(db, _company_or_404(db, company_id))


def _company_out(db: Session, company: Company) -> s.CompanyOut:
    profile = db.get(CompanyProfile, company.id)
    return s.CompanyOut(
        id=company.id,
        lifecycle_status=company.lifecycle_status,
        identity_policy_version=company.identity_policy_version,
        merged_into_company_id=company.merged_into_company_id,
        created_at=company.created_at,
        canonical_name=profile.canonical_name if profile else None,
        primary_domain=profile.primary_domain if profile else None,
        legal_form=profile.legal_form if profile else None,
        founded_year=profile.founded_year if profile else None,
        employee_count_min=profile.employee_count_min if profile else None,
        employee_count_max=profile.employee_count_max if profile else None,
        projection_conflict=profile.projection_conflict if profile else False,
    )


@router.get(
    "/companies/{company_id}/claims", response_model=list[s.ClaimOut], tags=["companies"]
)
def get_company_claims(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    attribute_key: str | None = Query(None),
    include_superseded: bool = Query(
        False, description="Include claims whose provider entity now resolves elsewhere."
    ),
) -> Any:
    """Claims attributed through the *effective* decision, never a stored id."""
    _company_or_404(db, company_id)
    effective = (
        select(CompanyClaim)
        .join(ProviderRecordVersion,
              ProviderRecordVersion.id == CompanyClaim.provider_record_version_id)
        .join(EntityResolutionHead,
              EntityResolutionHead.provider_entity_id
              == ProviderRecordVersion.provider_entity_id)
        .where(EntityResolutionHead.company_id == company_id)
    )
    direct = select(CompanyClaim).where(CompanyClaim.subject_company_id == company_id)

    if include_superseded:
        historical = (
            select(CompanyClaim)
            .join(EntityResolutionDecision,
                  EntityResolutionDecision.id == CompanyClaim.resolution_decision_id)
            .where(EntityResolutionDecision.company_id == company_id)
        )
        stmt = effective.union(direct, historical)
    else:
        stmt = effective.union(direct)

    claims = db.scalars(select(CompanyClaim).from_statement(stmt)).all()
    if attribute_key:
        claims = [c for c in claims if c.attribute_key == attribute_key]
    return sorted(claims, key=lambda c: (c.attribute_key, c.created_at))


@router.get(
    "/companies/{company_id}/locations", response_model=list[s.LocationOut],
    tags=["companies"],
)
def get_company_locations(company_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    _company_or_404(db, company_id)
    return db.scalars(
        select(CompanyLocation).where(CompanyLocation.company_id == company_id)
    ).all()


@router.get(
    "/companies/{company_id}/market-presences",
    response_model=list[s.MarketPresenceOut], tags=["companies"],
)
def get_company_presences(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    as_of: str | None = Query(None, description="ISO date; defaults to all intervals."),
    current_only: bool = Query(False),
) -> Any:
    """Intervals are stored; the date filter is applied here, never persisted."""
    _company_or_404(db, company_id)
    rows = db.scalars(
        select(CompanyMarketPresence)
        .where(CompanyMarketPresence.company_id == company_id)
        .order_by(CompanyMarketPresence.effective_from)
    ).all()
    if as_of or current_only:
        from datetime import date as _date

        moment = _date.fromisoformat(as_of) if as_of else _date.today()
        rows = [
            r for r in rows
            if r.effective_from <= moment and (r.effective_to is None or r.effective_to > moment)
        ]
    return rows


@router.get(
    "/companies/{company_id}/verticals", response_model=list[s.VerticalOut],
    tags=["companies"],
)
def get_company_verticals(company_id: uuid.UUID, db: Session = Depends(get_db)) -> Any:
    _company_or_404(db, company_id)
    return db.scalars(
        select(CompanyVertical).where(CompanyVertical.company_id == company_id)
    ).all()


@router.get(
    "/companies/{company_id}/relationships", response_model=list[s.RelationshipOut],
    tags=["companies"],
)
def get_company_relationships(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    as_of: str | None = Query(None),
    current_only: bool = Query(False),
) -> Any:
    _company_or_404(db, company_id)
    rows = db.scalars(
        select(CompanyRelationship).where(
            (CompanyRelationship.from_company_id == company_id)
            | (CompanyRelationship.to_company_id == company_id)
        ).order_by(CompanyRelationship.effective_from)
    ).all()
    if as_of or current_only:
        from datetime import date as _date

        moment = _date.fromisoformat(as_of) if as_of else _date.today()
        rows = [
            r for r in rows
            if r.effective_from <= moment and (r.effective_to is None or r.effective_to > moment)
        ]
    return rows


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@router.get(
    "/entity-resolution/decisions", response_model=list[s.DecisionOut], tags=["resolution"]
)
def list_decisions(
    db: Session = Depends(get_db),
    decision: str | None = Query(None),
    method: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> Any:
    stmt = select(EntityResolutionDecision).order_by(
        EntityResolutionDecision.decided_at.desc()
    ).limit(limit)
    if decision:
        stmt = stmt.where(EntityResolutionDecision.decision == decision.upper())
    if method:
        stmt = stmt.where(EntityResolutionDecision.method == method.upper())
    return db.scalars(stmt).all()


@router.get(
    "/entity-resolution/ambiguous", response_model=list[s.DecisionOut], tags=["resolution"]
)
def list_ambiguous(
    db: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)
) -> Any:
    """The human-review queue: effective AMBIGUOUS heads only."""
    superseder = EntityResolutionDecision.__table__.alias("s")
    return db.scalars(
        select(EntityResolutionDecision)
        .where(EntityResolutionDecision.decision == ResolutionDecision.AMBIGUOUS.value)
        .where(
            ~select(1).select_from(superseder)
            .where(superseder.c.supersedes_decision_id == EntityResolutionDecision.id)
            .exists()
        )
        .order_by(EntityResolutionDecision.decided_at.desc())
        .limit(limit)
    ).all()


@router.post(
    "/entity-resolution/decisions", response_model=s.DecisionOut, status_code=201,
    tags=["resolution"],
)
def record_human_decision(
    payload: s.HumanReviewRequest, db: Session = Depends(get_db)
) -> Any:
    """Append a human decision superseding the current head.

    Additive: the superseded decision is never modified.
    """
    valid = {d.value for d in ResolutionDecision}
    if payload.decision.upper() not in valid:
        raise ValidationError(
            f"Invalid decision {payload.decision!r}", details={"valid": sorted(valid)}
        )
    entity = db.get(ProviderEntity, payload.provider_entity_id)
    if entity is None:
        raise NotFoundError(f"No provider entity {payload.provider_entity_id}")
    decision = resolution.append_human_decision(
        db, entity,
        decision=payload.decision.upper(),
        company_id=payload.company_id,
        decided_by=payload.decided_by,
        rationale=payload.rationale,
    )
    db.commit()
    db.refresh(decision)
    return decision


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


@router.post(
    "/projections/rebuild", response_model=s.ProjectionRunOut, status_code=201,
    tags=["projections"],
)
def rebuild(db: Session = Depends(get_db), triggered_by: str = Query("api")) -> Any:
    """Truncate every derived projection and rebuild it from evidence."""
    run = projection.rebuild_projections(db, triggered_by=triggered_by)
    db.commit()
    db.refresh(run)
    return run


@router.get(
    "/projections/runs", response_model=list[s.ProjectionRunOut], tags=["projections"]
)
def list_projection_runs(
    db: Session = Depends(get_db), limit: int = Query(20, ge=1, le=100)
) -> Any:
    return db.scalars(
        select(ProjectionRun).order_by(ProjectionRun.started_at.desc()).limit(limit)
    ).all()
