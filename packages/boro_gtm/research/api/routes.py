"""M3 API — operational evidence, projections, identity review, human review.

Read-mostly. The three write endpoints create a research question, execute one,
and append a review decision; none of them mutates anything the schema declares
append-only.

What is deliberately not exposed: the job queue, raw bodies, raw model output,
unredacted extracted text, the private canonical commercial files, and every
M4 concept. Each exclusion has a test at the serialization boundary rather than
a comment asking future readers to be careful.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.core.db import get_db
from boro_gtm.core.enums import coerce_vocabulary
from boro_gtm.core.errors import NotFoundError, ValidationError
from boro_gtm.discovery.domain.models import Company, CompanyClaim
from boro_gtm.research.api import schemas as s
from boro_gtm.research.domain.models import (
    ClaimEvidenceLink,
    IdentityReviewSignal,
    IdentityReviewSignalEvidence,
    IdentityReviewSignalOccurrence,
    OperationalResearchGap,
    OperationalResearchPlanProfile,
    OperationalResearchProfile,
    OperationalResearchRun,
    ResearchArtifact,
    ResearchArtifactBody,
    ResearchArtifactDerivation,
    ResearchEvidenceItem,
    ResearchExtraction,
    ResearchFetchEvent,
    ResearchSource,
    ResearchSourceDiscovery,
    ResearchSourceEdge,
)
from boro_gtm.research.enums import ReviewReason as _ReviewReason
from boro_gtm.research.enums import SignalKind, SignalStatus
from boro_gtm.research.registry import RESEARCH_REGISTRY_VERSION
from boro_gtm.research.services import application, gaps, review
from boro_gtm.research.services.projection import project_claim
from boro_gtm.research.services.staleness import company_staleness

router = APIRouter()

MAX_PAGE = 200


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Bounded pagination. There is no "return all rows forever" endpoint."""
    if limit < 1 or limit > MAX_PAGE:
        raise ValidationError(
            f"limit must be between 1 and {MAX_PAGE}", {"limit": limit}
        )
    if offset < 0:
        raise ValidationError("offset must not be negative", {"offset": offset})
    return limit, offset


def _get_or_404(db: Session, model: type, entity_id: uuid.UUID, what: str) -> Any:
    """A subresource of something that does not exist is 404, not an empty list."""
    row = db.get(model, entity_id)
    if row is None:
        raise NotFoundError(f"no {what} {entity_id}")
    return row


# ---------------------------------------------------------------------------
# Runs and attempts
# ---------------------------------------------------------------------------


@router.get("/operational-research/runs", response_model=list[s.RunOut],
            tags=["operational-research"])
def list_runs(
    company: uuid.UUID | None = Query(None, description="Filter by canonical company"),
    policy: str | None = Query(None, description="Filter by research policy version"),
    vertical: uuid.UUID | None = Query(None, description="Filter by vertical"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.RunOut]:
    limit, offset = _page(limit, offset)
    return [
        s.RunOut(**dataclasses.asdict(view)) for view in application.list_runs(
            db, company_id=company, policy_version=policy, vertical_id=vertical,
            limit=limit, offset=offset,
        )
    ]


@router.get("/operational-research/runs/{run_id}", response_model=s.RunOut,
            tags=["operational-research"])
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> s.RunOut:
    return s.RunOut(**dataclasses.asdict(application.get_run(db, run_id)))


@router.post("/operational-research/runs", response_model=s.RunOut, status_code=201,
             tags=["operational-research"])
def create_run(body: s.RunCreate, db: Session = Depends(get_db)) -> s.RunOut:
    """Create or reuse. The same question asked twice is one question."""
    from boro_gtm.research.registry import DEFAULT_TARGET_ATTRIBUTES, get_attribute

    keys = tuple(body.target_attribute_keys or DEFAULT_TARGET_ATTRIBUTES)
    for key in keys:
        get_attribute(key)          # unknown attribute is a 422, not a 500
    view, _ = application.create_or_reuse_run(
        db, company_id=body.company_id, vertical_id=body.vertical_id,
        target_attribute_keys=keys, plan_inputs=body.plan_inputs,
    )
    db.commit()
    return s.RunOut(**dataclasses.asdict(view))


@router.post("/operational-research/runs/{run_id}/attempts",
             response_model=s.AttemptOut, status_code=201,
             tags=["operational-research"])
def create_attempt(
    run_id: uuid.UUID, body: s.AttemptCreate | None = None,
    db: Session = Depends(get_db),
) -> s.AttemptOut:
    """Execute the question once, against the fixture adapter.

    M3 ships no production research provider, so this is an operational and QA
    entry point rather than a live crawl.
    """
    options = body or s.AttemptCreate()
    view = application.execute_attempt(
        db, run_id=run_id, conditional=options.conditional
    )
    db.commit()
    return s.AttemptOut(**dataclasses.asdict(view))


@router.get("/operational-research/attempts", response_model=list[s.AttemptOut],
            tags=["operational-research"])
def list_attempts(
    run: uuid.UUID | None = Query(None, description="Filter by research question"),
    company: uuid.UUID | None = Query(None, description="Filter by company"),
    status: str | None = Query(None, description="Filter by execution status"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.AttemptOut]:
    from boro_gtm.research.enums import AttemptStatus

    limit, offset = _page(limit, offset)
    if status is not None:
        status = coerce_vocabulary(status, AttemptStatus, "status")
    return [
        s.AttemptOut(**dataclasses.asdict(view)) for view in application.list_attempts(
            db, run_id=run, company_id=company, status=status,
            limit=limit, offset=offset,
        )
    ]


@router.get("/operational-research/attempts/{attempt_id}",
            response_model=s.AttemptOut, tags=["operational-research"])
def get_attempt(attempt_id: uuid.UUID, db: Session = Depends(get_db)) -> s.AttemptOut:
    return s.AttemptOut(**dataclasses.asdict(application.get_attempt(db, attempt_id)))


@router.post("/operational-research/attempts/{attempt_id}/retry",
             response_model=s.AttemptOut, status_code=201,
             tags=["operational-research"])
def retry_attempt(
    attempt_id: uuid.UUID, db: Session = Depends(get_db)
) -> s.AttemptOut:
    """Retry advances the question as attempt n+1; it never reopens one."""
    view = application.retry_attempt(db, attempt_id)
    db.commit()
    return s.AttemptOut(**dataclasses.asdict(view))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _source_out(row: ResearchSource) -> s.SourceOut:
    return s.SourceOut.model_validate(row, from_attributes=True)


@router.get("/research-sources/{source_id}", response_model=s.SourceOut,
            tags=["operational-research"])
def get_source(source_id: uuid.UUID, db: Session = Depends(get_db)) -> s.SourceOut:
    return _source_out(_get_or_404(db, ResearchSource, source_id, "research source"))


@router.get("/research-sources/{source_id}/fetch-events",
            response_model=list[s.FetchEventOut], tags=["operational-research"])
def list_fetch_events(
    source_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.FetchEventOut]:
    _get_or_404(db, ResearchSource, source_id, "research source")
    limit, offset = _page(limit, offset)
    rows = db.scalars(
        select(ResearchFetchEvent)
        .where(ResearchFetchEvent.source_id == source_id)
        .order_by(ResearchFetchEvent.retrieved_at.desc(), ResearchFetchEvent.id)
        .limit(limit).offset(offset)
    ).all()
    return [s.FetchEventOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/research-sources/{source_id}/discoveries",
            response_model=list[s.DiscoveryOut], tags=["operational-research"])
def list_discoveries(
    source_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.DiscoveryOut]:
    _get_or_404(db, ResearchSource, source_id, "research source")
    limit, offset = _page(limit, offset)
    rows = db.scalars(
        select(ResearchSourceDiscovery)
        .where(ResearchSourceDiscovery.source_id == source_id)
        .order_by(ResearchSourceDiscovery.discovered_at, ResearchSourceDiscovery.id)
        .limit(limit).offset(offset)
    ).all()
    return [s.DiscoveryOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/research-sources/{source_id}/edges",
            response_model=list[s.SourceEdgeOut], tags=["operational-research"])
def list_edges(
    source_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[s.SourceEdgeOut]:
    _get_or_404(db, ResearchSource, source_id, "research source")
    rows = db.scalars(
        select(ResearchSourceEdge)
        .where(
            (ResearchSourceEdge.from_source_id == source_id)
            | (ResearchSourceEdge.to_source_id == source_id)
        )
        .order_by(ResearchSourceEdge.observed_at, ResearchSourceEdge.id)
        .limit(MAX_PAGE)
    ).all()
    return [s.SourceEdgeOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/research-artifacts/{artifact_id}", response_model=s.ArtifactOut,
            tags=["operational-research"])
def get_artifact(artifact_id: uuid.UUID, db: Session = Depends(get_db)) -> s.ArtifactOut:
    row = _get_or_404(db, ResearchArtifact, artifact_id, "research artifact")
    return s.ArtifactOut.model_validate(row, from_attributes=True)


@router.get("/research-artifacts/{artifact_id}/derivations",
            response_model=list[s.DerivationOut], tags=["operational-research"])
def list_derivations(
    artifact_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[s.DerivationOut]:
    _get_or_404(db, ResearchArtifact, artifact_id, "research artifact")
    rows = db.scalars(
        select(ResearchArtifactDerivation)
        .where(ResearchArtifactDerivation.artifact_id == artifact_id)
        .order_by(ResearchArtifactDerivation.derived_at, ResearchArtifactDerivation.id)
        .limit(MAX_PAGE)
    ).all()
    return [s.DerivationOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/research-bodies/{body_id}", response_model=s.BodyOut,
            tags=["operational-research"])
def get_body(body_id: uuid.UUID, db: Session = Depends(get_db)) -> s.BodyOut:
    """Hash, length and retention state. The bytes are never served."""
    row = _get_or_404(db, ResearchArtifactBody, body_id, "research body")
    return s.BodyOut.model_validate(row, from_attributes=True)


@router.get("/research-extractions/{extraction_id}", response_model=s.ExtractionOut,
            tags=["operational-research"])
def get_extraction(
    extraction_id: uuid.UUID, db: Session = Depends(get_db)
) -> s.ExtractionOut:
    """Provenance of a reading. `raw_output` is not serialized."""
    row = _get_or_404(db, ResearchExtraction, extraction_id, "research extraction")
    return s.ExtractionOut.model_validate(row, from_attributes=True)


def _evidence_out(db: Session, item: ResearchEvidenceItem) -> s.EvidenceItemOut:
    derivation = db.get(ResearchArtifactDerivation, item.artifact_derivation_id)
    return s.EvidenceItemOut(
        id=item.id,
        source=_source_out(db.get(ResearchSource, item.source_id)),
        fetch_event=s.FetchEventOut.model_validate(
            db.get(ResearchFetchEvent, item.fetch_event_id), from_attributes=True
        ),
        artifact_derivation=s.DerivationOut.model_validate(
            derivation, from_attributes=True
        ),
        artifact=s.ArtifactOut.model_validate(
            db.get(ResearchArtifact, derivation.artifact_id), from_attributes=True
        ),
        body=s.BodyOut.model_validate(
            db.get(ResearchArtifactBody, item.body_id), from_attributes=True
        ),
        extraction=s.ExtractionOut.model_validate(
            db.get(ResearchExtraction, item.extraction_id), from_attributes=True
        ),
        locator=item.locator, locator_hash=item.locator_hash,
        quote=item.quote, quote_sha256=item.quote_sha256,
        publisher_key=item.publisher_key,
        publisher_policy_version=item.publisher_policy_version,
        created_at=item.created_at,
    )


@router.get("/research-evidence-items/{item_id}", response_model=s.EvidenceItemOut,
            tags=["operational-research"])
def get_evidence_item(
    item_id: uuid.UUID, db: Session = Depends(get_db)
) -> s.EvidenceItemOut:
    """The whole provenance walk, so a client need not reconstruct the joins."""
    item = _get_or_404(db, ResearchEvidenceItem, item_id, "research evidence item")
    return _evidence_out(db, item)


# ---------------------------------------------------------------------------
# Company-global research
# ---------------------------------------------------------------------------


@router.get("/companies/{company_id}/research", response_model=s.CompanyResearchOut,
            tags=["operational-research"])
def get_company_research(
    company_id: uuid.UUID,
    as_of: date | None = Query(None, description="Date to judge staleness against"),
    db: Session = Depends(get_db),
) -> s.CompanyResearchOut:
    """Company-global knowledge, with staleness computed at read time.

    There is no coverage here. Coverage belongs to a research question, and
    putting it on a company would make it look like a property of the company.
    """
    _get_or_404(db, Company, company_id, "company")
    profile = db.get(OperationalResearchProfile, company_id)
    if profile is None:
        raise NotFoundError(f"no operational research profile for company {company_id}")
    judged = as_of or datetime.now(UTC).date()
    verdicts = company_staleness(db, company_id, judged)

    attributes: list[s.AttributeStateOut] = []
    for key in sorted(profile.facts or {}):
        state = (profile.facts or {})[key]
        contradiction = (profile.contradictions or {}).get(key, {})
        attributes.append(s.AttributeStateOut(
            attribute_key=key,
            availability=state.get("availability", "OBSERVED"),
            envelope=state.get("envelope"),
            best=state.get("best"),
            best_claim_id=state.get("best_claim_id"),
            best_fact_type=state.get("best_fact_type"),
            unit=state.get("unit"),
            confidence=state.get("confidence"),
            source_published_at=state.get("source_published_at"),
            source_published_granularity=state.get("source_published_granularity"),
            observed_at=state.get("observed_at"),
            period_granularity=state.get("period_granularity"),
            contradiction=bool(contradiction.get("contradiction")),
            contradicting_claim_ids=contradiction.get("claim_ids", []),
            corroborating_publisher_count=(
                profile.corroborating_publisher_counts or {}
            ).get(key, 0),
            claim_ids=state.get("claim_ids", []),
            staleness=verdicts[key].state if key in verdicts else None,
        ))
    return s.CompanyResearchOut(
        company_id=company_id, attributes=attributes,
        assertion_policy_version=profile.assertion_policy_version,
        publisher_policy_version=profile.publisher_policy_version,
        derived_from_claim_ids=list(profile.derived_from_claim_ids or []),
        as_of=judged,
    )


@router.get("/companies/{company_id}/research/claims",
            response_model=list[s.ResearchClaimOut], tags=["operational-research"])
def list_company_research_claims(
    company_id: uuid.UUID,
    attribute: str | None = Query(None, description="Filter by attribute key"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.ResearchClaimOut]:
    _get_or_404(db, Company, company_id, "company")
    limit, offset = _page(limit, offset)
    statement = select(CompanyClaim).where(
        CompanyClaim.subject_company_id == company_id,
        CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
    )
    if attribute is not None:
        statement = statement.where(CompanyClaim.attribute_key == attribute)
    rows = db.scalars(
        statement.order_by(CompanyClaim.attribute_key, CompanyClaim.id)
        .limit(limit).offset(offset)
    ).all()
    out = []
    for claim in rows:
        evidence = db.scalars(
            select(ClaimEvidenceLink.evidence_item_id).where(
                ClaimEvidenceLink.claim_id == claim.id
            ).order_by(ClaimEvidenceLink.evidence_item_id)
        ).all()
        out.append(s.ResearchClaimOut(
            id=claim.id, attribute_key=claim.attribute_key,
            attribute_registry_version=claim.attribute_registry_version,
            value=claim.value_jsonb, unit=claim.unit, fact_type=claim.fact_type,
            availability=claim.availability,
            confidence=float(claim.confidence) if claim.confidence is not None else None,
            observed_at=claim.observed_at,
            period_granularity=claim.period_granularity,
            assertion_fingerprint=claim.assertion_fingerprint,
            evidence_item_ids=list(evidence), created_at=claim.created_at,
        ))
    return out


@router.get("/companies/{company_id}/research/evidence",
            response_model=list[s.Q2EvidenceOut], tags=["operational-research"])
def list_company_research_evidence(
    company_id: uuid.UUID,
    attribute: str | None = Query(None, description="Filter by attribute key"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.Q2EvidenceOut]:
    """The canonical seven-field projection. A read, never a second store.

    A claim whose evidence no longer resolves is **omitted**, not serialized
    with an empty provenance list.
    """
    from boro_gtm.research.services.projection import UnsupportedClaimError

    _get_or_404(db, Company, company_id, "company")
    limit, offset = _page(limit, offset)
    statement = select(CompanyClaim).where(
        CompanyClaim.subject_company_id == company_id,
        CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
        CompanyClaim.availability == "OBSERVED",
    )
    if attribute is not None:
        statement = statement.where(CompanyClaim.attribute_key == attribute)
    claims = db.scalars(
        statement.order_by(CompanyClaim.attribute_key, CompanyClaim.id)
    ).all()
    records: list[s.Q2EvidenceOut] = []
    for claim in claims:
        try:
            for record in project_claim(db, claim):
                records.append(s.Q2EvidenceOut(**record.as_dict()))
        except UnsupportedClaimError:
            continue
    return records[offset:offset + limit]


# ---------------------------------------------------------------------------
# Plan-scoped
# ---------------------------------------------------------------------------


@router.get("/operational-research/runs/{run_id}/coverage",
            response_model=s.CoverageOut, tags=["operational-research"])
def get_coverage(run_id: uuid.UUID, db: Session = Depends(get_db)) -> s.CoverageOut:
    """Coverage, confidence and contradiction rate — three separate numbers."""
    _get_or_404(db, OperationalResearchRun, run_id, "research run")
    profile = db.get(OperationalResearchPlanProfile, run_id)
    if profile is None:
        raise NotFoundError(f"no plan profile for research run {run_id}")
    return s.CoverageOut(
        run_id=profile.run_id, company_id=profile.company_id,
        coverage=float(profile.coverage),
        confidence_summary=(
            float(profile.confidence_summary)
            if profile.confidence_summary is not None else None
        ),
        contradiction_rate=float(profile.contradiction_rate),
        required_attribute_count=profile.required_attribute_count,
        covered_attribute_count=profile.covered_attribute_count,
        not_applicable_attribute_count=profile.not_applicable_attribute_count,
        open_gap_count=profile.open_gap_count,
        assertion_policy_version=profile.assertion_policy_version,
        publisher_policy_version=profile.publisher_policy_version,
    )


@router.get("/operational-research/runs/{run_id}/gaps",
            response_model=list[s.GapOut], tags=["operational-research"])
def list_gaps(
    run_id: uuid.UUID,
    kind: str | None = Query(None, description="Filter by gap kind"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.GapOut]:
    """A gap states insufficient evidence. It never asserts a company lacks it."""
    from boro_gtm.research.enums import GapKind

    _get_or_404(db, OperationalResearchRun, run_id, "research run")
    limit, offset = _page(limit, offset)
    statement = select(OperationalResearchGap).where(
        OperationalResearchGap.run_id == run_id
    )
    if kind is not None:
        statement = statement.where(
            OperationalResearchGap.gap_kind
            == coerce_vocabulary(kind, GapKind, "kind")
        )
    rows = db.scalars(
        statement.order_by(
            OperationalResearchGap.gap_kind, OperationalResearchGap.attribute_key
        ).limit(limit).offset(offset)
    ).all()
    return [
        s.GapOut(
            id=row.id, run_id=row.run_id, company_id=row.company_id,
            attribute_key=row.attribute_key, gap_kind=row.gap_kind,
            current_status=gaps.current_status(db, row.id),
            attempt_count=gaps.attempt_count(db, row.id),
            first_raised_at=row.first_raised_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Identity review — M3-owned end to end
# ---------------------------------------------------------------------------


def _signal_out(db: Session, signal: IdentityReviewSignal) -> s.IdentitySignalOut:
    occurrences = db.scalars(
        select(IdentityReviewSignalOccurrence)
        .where(IdentityReviewSignalOccurrence.signal_id == signal.id)
        .order_by(IdentityReviewSignalOccurrence.occurrence_number)
    ).all()
    return s.IdentitySignalOut(
        id=signal.id, company_id=signal.company_id,
        related_company_id=signal.related_company_id,
        signal_kind=signal.signal_kind,
        normalized_concern=signal.normalized_concern,
        signal_policy_version=signal.signal_policy_version,
        signal_fingerprint=signal.signal_fingerprint,
        first_raised_at=signal.first_raised_at,
        occurrences=[
            s.SignalOccurrenceOut(
                id=o.id, occurrence_number=o.occurrence_number,
                raised_by_attempt_id=o.raised_by_attempt_id,
                supersedes_occurrence_id=o.supersedes_occurrence_id,
                is_open=o.is_open,
                current_status=review.occurrence_status(db, o.id),
                raised_at=o.raised_at,
            )
            for o in occurrences
        ],
    )


@router.get("/identity-review-signals", response_model=list[s.IdentitySignalOut],
            tags=["identity-review"])
def list_identity_signals(
    company: uuid.UUID | None = Query(None, description="Filter by company"),
    kind: str | None = Query(None, description="Filter by signal kind"),
    open: bool | None = Query(None, description="Only signals with an open episode"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.IdentitySignalOut]:
    limit, offset = _page(limit, offset)
    statement = select(IdentityReviewSignal)
    if company is not None:
        statement = statement.where(IdentityReviewSignal.company_id == company)
    if kind is not None:
        statement = statement.where(
            IdentityReviewSignal.signal_kind
            == coerce_vocabulary(kind, SignalKind, "kind")
        )
    if open is not None:
        live = select(IdentityReviewSignalOccurrence.signal_id).where(
            IdentityReviewSignalOccurrence.is_open.is_(True)
        )
        statement = statement.where(
            IdentityReviewSignal.id.in_(live) if open
            else IdentityReviewSignal.id.not_in(live)
        )
    rows = db.scalars(
        statement.order_by(
            IdentityReviewSignal.first_raised_at.desc(), IdentityReviewSignal.id
        ).limit(limit).offset(offset)
    ).all()
    return [_signal_out(db, row) for row in rows]


@router.get("/identity-review-signals/{signal_id}",
            response_model=s.IdentitySignalOut, tags=["identity-review"])
def get_identity_signal(
    signal_id: uuid.UUID, db: Session = Depends(get_db)
) -> s.IdentitySignalOut:
    """The durable concern, plus every review episode it has had."""
    signal = _get_or_404(db, IdentityReviewSignal, signal_id, "identity review signal")
    return _signal_out(db, signal)


@router.get("/identity-review-signals/{signal_id}/evidence",
            response_model=list[s.EvidenceItemOut], tags=["identity-review"])
def list_identity_signal_evidence(
    signal_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[s.EvidenceItemOut]:
    """First-class evidence. A signal needs no claim to be justified."""
    _get_or_404(db, IdentityReviewSignal, signal_id, "identity review signal")
    items = db.scalars(
        select(ResearchEvidenceItem)
        .join(IdentityReviewSignalEvidence,
              IdentityReviewSignalEvidence.evidence_item_id == ResearchEvidenceItem.id)
        .join(IdentityReviewSignalOccurrence,
              IdentityReviewSignalOccurrence.id
              == IdentityReviewSignalEvidence.signal_occurrence_id)
        .where(IdentityReviewSignalOccurrence.signal_id == signal_id)
        .order_by(ResearchEvidenceItem.id)
        .limit(MAX_PAGE)
    ).all()
    return [_evidence_out(db, item) for item in items]


@router.post("/identity-review-signals/{signal_id}/occurrences/{number}/status",
             response_model=s.SignalEventOut, status_code=201,
             tags=["identity-review"])
def append_signal_status(
    signal_id: uuid.UUID, number: int, body: s.SignalStatusIn,
    db: Session = Depends(get_db),
) -> s.SignalEventOut:
    """Append a review decision. Terminal stays terminal, by trigger."""
    _get_or_404(db, IdentityReviewSignal, signal_id, "identity review signal")
    status = coerce_vocabulary(body.status, SignalStatus, "status")
    event = review.append_signal_status(
        db, signal_id=signal_id, occurrence_number=number, status=status,
        actor=body.actor, note=body.note,
    )
    db.commit()
    return s.SignalEventOut.model_validate(event, from_attributes=True)


# ---------------------------------------------------------------------------
# Human evidence review
# ---------------------------------------------------------------------------


# Its own path rather than `/research-evidence-items/pending-review`: that
# would be shadowed by `/research-evidence-items/{item_id}`, which is declared
# first and would reject the literal segment as an invalid UUID.
@router.get("/research-reviews/pending",
            response_model=list[s.PendingReviewOut], tags=["operational-research"])
def list_pending_reviews(
    company: uuid.UUID | None = Query(None, description="Filter by company"),
    reason: str | None = Query(None, description="Filter by why it is waiting"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    db: Session = Depends(get_db),
) -> list[s.PendingReviewOut]:
    """The review queue: sampled readings nobody has decided on yet.

    This exists because a sampled reading cannot assert a claim on its own, so
    there is a real interval in which it is reviewable and no claim exists. A
    review route keyed by claim could not address that interval at all.
    """
    if reason is not None:
        reason = coerce_vocabulary(reason, _ReviewReason, "reason")
    out = []
    for candidate in review.pending_candidates(
        db, company_id=company, reason=reason, limit=limit
    ):
        item = db.get(ResearchEvidenceItem, candidate.evidence_item_id)
        out.append(s.PendingReviewOut(
            candidate_id=candidate.id, evidence_item_id=item.id,
            extraction_id=item.extraction_id, company_id=candidate.company_id,
            run_id=candidate.run_id, attribute_key=candidate.attribute_key,
            reason=candidate.reason,
            extractor_confidence=(
                float(candidate.extractor_confidence)
                if candidate.extractor_confidence is not None else None
            ),
            source=db.get(ResearchSource, item.source_id).normalized_locator,
            quote=item.quote, locator=item.locator, raised_at=candidate.raised_at,
        ))
    return out


@router.post("/research-evidence-items/{item_id}/review",
             response_model=s.EvidenceReviewOut, status_code=201,
             tags=["operational-research"])
def review_evidence_item(
    item_id: uuid.UUID, body: s.EvidenceReviewIn, db: Session = Depends(get_db)
) -> s.EvidenceReviewOut:
    """Record a human decision about one observation. Both outcomes persist.

    **Confirm** appends a HUMAN extraction and its own evidence, and may assert
    a claim. **Reject** records the decision, the actor, the time and the
    rationale, and asserts nothing — a reviewer's disbelief is not evidence
    that the opposite is true. Neither touches the model's extraction.
    """
    outcome = review.review_evidence(
        db, evidence_item_id=item_id, decision=body.decision, actor=body.actor,
        note=body.note,
    )
    db.commit()
    return s.EvidenceReviewOut(**outcome.as_dict())


@router.get("/research-evidence-items/{item_id}/reviews",
            response_model=list[s.EvidenceReviewOut], tags=["operational-research"])
def list_evidence_reviews(
    item_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[s.EvidenceReviewOut]:
    """Every decision recorded about this observation, oldest first."""
    _get_or_404(db, ResearchEvidenceItem, item_id, "research evidence item")
    # `created_evidence_item_ids` is reconstructed from the persisted human
    # extraction, not invented. The previous version emitted an empty list for
    # every historical row, which is false for every confirmation.
    return [
        s.EvidenceReviewOut(
            review_id=row.id, evidence_item_id=row.evidence_item_id,
            company_id=review.company_of_evidence(db, row.evidence_item_id),
            decision=row.decision, actor=row.actor, note=row.note,
            reviewed_at=row.reviewed_at,
            human_extraction_id=row.human_extraction_id,
            resulting_claim_id=row.resulting_claim_id,
            created_evidence_item_ids=review.evidence_created_by(db, row),
            model_extraction_unchanged=True,
        )
        for row in review.reviews_for(db, item_id)
    ]


# ---------------------------------------------------------------------------
# Signal event history helper endpoint count guard
# ---------------------------------------------------------------------------


@router.get("/operational-research/attempts/{attempt_id}/extractions",
            response_model=list[s.ExtractionOut], tags=["operational-research"])
def list_attempt_extractions(
    attempt_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[s.ExtractionOut]:
    """Which readings an execution created or reused."""
    from boro_gtm.research.domain.models import (
        OperationalResearchAttempt,
        ResearchAttemptExtraction,
    )

    _get_or_404(db, OperationalResearchAttempt, attempt_id, "research attempt")
    limit, offset = _page(limit, offset)
    rows = db.scalars(
        select(ResearchExtraction)
        .join(ResearchAttemptExtraction,
              ResearchAttemptExtraction.extraction_id == ResearchExtraction.id)
        .where(ResearchAttemptExtraction.attempt_id == attempt_id)
        .order_by(ResearchExtraction.created_at, ResearchExtraction.id)
        .limit(limit).offset(offset)
    ).all()
    return [s.ExtractionOut.model_validate(r, from_attributes=True) for r in rows]


__all__ = ["router", "func"]
