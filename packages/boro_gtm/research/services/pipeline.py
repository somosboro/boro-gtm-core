"""The internal M3 pipeline: one research question, one execution, end to end.

Two things make this an orchestrator rather than a script.

**Durable stage timestamps are the gate, not the status column.** `status =
'EXTRACTING'` says where a worker believes it is; `fetch_completed_at` says
retrieval actually finished. A crash between the two is exactly the case where
they disagree, so every stage boundary asserts on the timestamp.

**A failed source must not erase a successful one.** Nine good pages and one
timeout is nine pages of evidence plus a recorded failure — not a lost attempt.
The attempt lands `PARTIAL`, which is a real outcome and not a euphemism.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    OperationalResearchAttempt,
    OperationalResearchRun,
    ResearchArtifactDerivation,
    ResearchSource,
)
from boro_gtm.research.fixtures import corpus
from boro_gtm.research.fixtures.transport import (
    DiscoveredLocator,
    FixtureDiscoveryProvider,
    FixtureTransport,
)
from boro_gtm.research.policies import (
    CLASSIFIER_POLICY_VERSION,
    normalize_locator,
    sha256_json,
)
from boro_gtm.research.registry import (
    DEFAULT_TARGET_ATTRIBUTES,
    RESEARCH_POLICY_VERSION,
    RESEARCH_REGISTRY_VERSION,
)
from boro_gtm.research.services import acquisition, artifacts, claims, gaps, identity
from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
from boro_gtm.research.services.extraction import (
    HUMAN_EXTRACTOR,
    Observation,
    extractors_for,
    run_extraction,
)

#: Queries the fixture plan issues. Two of them share a result, on purpose.
PLAN_SEARCH_QUERIES: tuple[str, ...] = tuple(corpus.SEARCH_RESULTS)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Run and attempt
# ---------------------------------------------------------------------------


def research_plan_hash(
    *, company_id: uuid.UUID, policy_version: str, vertical_id: uuid.UUID | None,
    target_attribute_keys: tuple[str, ...], plan_inputs: dict | None,
) -> str:
    """Identity of the *question*, covering everything that changes it."""
    return sha256_json({
        "company_id": str(company_id),
        "research_policy_version": policy_version,
        "vertical_id": str(vertical_id) if vertical_id else None,
        "target_attribute_keys": sorted(target_attribute_keys),
        "plan_inputs": plan_inputs,
    })


def get_or_create_run(
    session: Session,
    *,
    company_id: uuid.UUID,
    now: datetime | None = None,
    vertical_id: uuid.UUID | None = None,
    target_attribute_keys: tuple[str, ...] = DEFAULT_TARGET_ATTRIBUTES,
    plan_inputs: dict | None = None,
    created_by: str = "m3-pipeline",
) -> tuple[OperationalResearchRun, bool]:
    """A logical question. Asking it twice does not create two questions."""
    now = now or _now()
    digest = research_plan_hash(
        company_id=company_id, policy_version=RESEARCH_POLICY_VERSION,
        vertical_id=vertical_id, target_attribute_keys=target_attribute_keys,
        plan_inputs=plan_inputs,
    )
    result = session.execute(
        pg_insert(OperationalResearchRun)
        .values(
            id=uuid.uuid4(), company_id=company_id,
            research_policy_version=RESEARCH_POLICY_VERSION, vertical_id=vertical_id,
            target_attribute_keys=list(target_attribute_keys), plan_inputs=plan_inputs,
            research_plan_hash=digest, created_by=created_by, created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_run_plan_hash")
        .returning(OperationalResearchRun.id)
    )
    row = result.first()
    if row is not None:
        return session.get(OperationalResearchRun, row[0]), True
    return session.scalars(
        select(OperationalResearchRun).where(
            OperationalResearchRun.research_plan_hash == digest
        )
    ).one(), False


def start_attempt(
    session: Session, *, run: OperationalResearchRun, now: datetime | None = None,
    seed_inputs: dict | None = None, allow_partial_assertion: bool = True,
) -> OperationalResearchAttempt:
    """Open attempt n+1 and freeze its seeds.

    Seeds are frozen because an execution whose inputs changed mid-flight is
    not reproducible, and reproducibility is the only reason to record them.
    """
    now = now or _now()
    last = session.scalar(
        select(func.max(OperationalResearchAttempt.attempt_number)).where(
            OperationalResearchAttempt.run_id == run.id
        )
    )
    seeds = seed_inputs or {
        "human_seeds": list(corpus.HUMAN_SEEDS),
        "search_queries": list(PLAN_SEARCH_QUERIES),
    }
    attempt = OperationalResearchAttempt(
        id=uuid.uuid4(), run_id=run.id, attempt_number=(last or 0) + 1,
        status="PENDING", allow_partial_assertion=allow_partial_assertion,
        attempt_seed_inputs=seeds, attempt_seed_inputs_hash=sha256_json(seeds),
        created_at=now, started_at=now,
    )
    session.add(attempt)
    session.flush()
    return attempt


def _advance(
    session: Session, attempt: OperationalResearchAttempt, status: str,
    stamp: str | None, now: datetime,
) -> None:
    attempt.status = status
    if stamp:
        setattr(attempt, stamp, now)
    session.flush()


# ---------------------------------------------------------------------------
# Result of one execution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PipelineResult:
    attempt: OperationalResearchAttempt
    sources_seen: int = 0
    discoveries_created: int = 0
    fetch_outcomes: dict[str, int] = field(default_factory=dict)
    bodies_created: int = 0
    derivations_created: int = 0
    text_derivations_created: int = 0
    extractions_created: int = 0
    extractions_reused: int = 0
    evidence_created: int = 0
    claims_created: int = 0
    evidence_links_created: int = 0
    gaps_raised: int = 0
    signals_raised: int = 0
    edges_created: int = 0
    failed_sources: list[str] = field(default_factory=list)
    sampled_pending_review: list[uuid.UUID] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.attempt.status


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    session: Session,
    *,
    company_id: uuid.UUID,
    transport: FixtureTransport | None = None,
    now: datetime | None = None,
    confirm_sampled: bool = False,
    vertical_id: uuid.UUID | None = None,
) -> PipelineResult:
    """Discover, retrieve, read, evidence and assert — once."""
    now = now or _now()
    transport = transport or FixtureTransport()
    provider = FixtureDiscoveryProvider(transport)

    run, _ = get_or_create_run(
        session, company_id=company_id, now=now, vertical_id=vertical_id
    )
    attempt = start_attempt(session, run=run, now=now)
    result = PipelineResult(attempt=attempt)

    # -- DISCOVERING -------------------------------------------------------
    _advance(session, attempt, "DISCOVERING", None, now)
    candidates = _discover(session, provider, attempt, now, result)
    _advance(session, attempt, "FETCHING", "discovery_completed_at", now)

    # -- FETCHING ----------------------------------------------------------
    fetched = _retrieve(session, transport, attempt, candidates, now, result)
    _advance(session, attempt, "EXTRACTING", "fetch_completed_at", now)

    # -- EXTRACTING --------------------------------------------------------
    observations = _extract(session, attempt, fetched, now, result, confirm_sampled)
    _relate_sources(session, fetched, now, result)
    _advance(session, attempt, "ASSERTING", "extraction_completed_at", now)

    # -- ASSERTING ---------------------------------------------------------
    _assert(session, run, attempt, company_id, observations, now, result)
    _raise_gaps(session, run, attempt, company_id, observations, now, result)
    _raise_identity_signals(session, attempt, company_id, fetched, now, result)

    terminal = "PARTIAL" if result.failed_sources else "COMPLETED"
    attempt.status = terminal
    attempt.completed_at = now
    if result.failed_sources:
        attempt.failure_stage = "FETCHING"
        attempt.error = (
            f"{len(result.failed_sources)} source(s) did not yield bytes; "
            "successful evidence retained"
        )
    session.flush()
    return result


# -- stages -----------------------------------------------------------------


def _discover(
    session: Session, provider: FixtureDiscoveryProvider,
    attempt: OperationalResearchAttempt, now: datetime, result: PipelineResult,
) -> dict[str, tuple[ResearchSource, list[DiscoveredLocator]]]:
    """Every method, and every observation each one makes.

    One address found by a sitemap, a crawl link and two search queries is one
    source and four discovery rows. Collapsing them would lose the only record
    of *how* the system found what it found.
    """
    batches: list[tuple[DiscoveredLocator, str | None]] = []
    for candidate in provider.human_seeds():
        batches.append((candidate, None))
    for candidate in provider.sitemap(corpus.HOME):
        batches.append((candidate, corpus.HOME))
    for candidate in provider.crawl_links(corpus.HOME):
        batches.append((candidate, corpus.HOME))
    for query in PLAN_SEARCH_QUERIES:
        for candidate in provider.search(query):
            batches.append((candidate, None))
    for candidate in provider.job_board():
        batches.append((candidate, None))
    for candidate in provider.registry():
        batches.append((candidate, None))
    for candidate in provider.api():
        batches.append((candidate, None))
    # The mirror and the third-party directory are seeded directly, standing in
    # for a link the fixture crawler has no page to find them on.
    for url in (corpus.ABOUT_MIRROR_URL, corpus.DIRECTORY_URL):
        batches.append((
            DiscoveredLocator(url, "HUMAN_SEED", {"operator": "fixture-analyst"},
                              relevance_hint=0.7),
            None,
        ))

    found: dict[str, tuple[ResearchSource, list[DiscoveredLocator]]] = {}
    for candidate, parent_url in batches:
        source = acquisition.get_or_create_source(session, candidate.url, now)
        parent = (
            acquisition.get_or_create_source(session, parent_url, now)
            if parent_url and normalize_locator(parent_url) != source.normalized_locator
            else None
        )
        discovery = acquisition.record_discovery(
            session, source=source, attempt_id=attempt.id, candidate=candidate,
            now=now, parent=parent,
        )
        if discovery is not None:
            result.discoveries_created += 1
        entry = found.setdefault(source.normalized_locator, (source, []))
        entry[1].append(candidate)
    result.sources_seen = len(found)
    return found


@dataclass(slots=True)
class _Retrieved:
    source: ResearchSource
    fetch_event_id: uuid.UUID
    body_id: uuid.UUID
    raw: bytes
    media_type: str


def _retrieve(
    session: Session, transport: FixtureTransport,
    attempt: OperationalResearchAttempt,
    candidates: dict[str, tuple[ResearchSource, list[DiscoveredLocator]]],
    now: datetime, result: PipelineResult,
) -> list[_Retrieved]:
    retrieved: list[_Retrieved] = []
    for locator, (source, _) in sorted(candidates.items()):
        previous = acquisition.last_successful_fetch(session, source.id)
        fetch = transport.fetch(
            locator, if_none_match=previous.etag if previous else None
        )
        event, body, created = acquisition.record_fetch(
            session, source=source, attempt_id=attempt.id, result=fetch, now=now,
            validated_body_id=previous.body_id if fetch.outcome == "NOT_MODIFIED" else None,
        )
        result.fetch_outcomes[fetch.outcome] = result.fetch_outcomes.get(fetch.outcome, 0) + 1
        if created:
            result.bodies_created += 1

        if fetch.redirected_from is not None and fetch.final_url:
            target = acquisition.get_or_create_source(session, fetch.final_url, now)
            if acquisition.record_edge(
                session, from_source=source, to_source=target,
                relation_type="REDIRECTS_TO", edge_origin="FETCH_OBSERVED",
                observed_by_fetch_event_id=event.id, now=now,
            ):
                result.edges_created += 1

        if fetch.outcome == "OK" and body is not None:
            classification = artifacts.classify_body(
                session, body_id=body.id, raw=fetch.body, now=now,
                classifier_policy_version=CLASSIFIER_POLICY_VERSION,
            )
            retrieved.append(_Retrieved(
                source=source, fetch_event_id=event.id, body_id=body.id,
                raw=fetch.body, media_type=classification.sniffed_media_type,
            ))
        elif fetch.outcome == "NOT_MODIFIED" and previous is not None:
            # No new bytes, so nothing to re-read. The event alone records that
            # the document is still current.
            continue
        else:
            result.failed_sources.append(locator)
    return retrieved


_CANONICAL_LINK = re.compile(
    rb"""<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']""", re.I
)
_ALTERNATE_LINK = re.compile(
    rb"""<link[^>]+rel=["']alternate["'][^>]+hreflang=["']([^"']+)["']"""
    rb"""[^>]+href=["']([^"']+)["']""",
    re.I,
)


def _relate_sources(
    session: Session, fetched: list[_Retrieved], now: datetime, result: PipelineResult,
) -> None:
    """Record what pages say about each other, and what we infer across them.

    A declared canonical and an hreflang alternate are things a single page
    states, so one retrieval witnesses each. A mirror is not: no single fetch
    can see that two documents on two domains are the same, so it is DERIVED
    and carries both observations. That distinction is why the edge table has
    an origin column at all.
    """
    by_body: dict[uuid.UUID, _Retrieved] = {item.body_id: item for item in fetched}

    for item in fetched:
        canonical = _CANONICAL_LINK.search(item.raw)
        if canonical:
            target_url = canonical.group(1).decode()
            if normalize_locator(target_url) != item.source.normalized_locator:
                target = acquisition.get_or_create_source(session, target_url, now)
                if acquisition.record_edge(
                    session, from_source=item.source, to_source=target,
                    relation_type="DECLARES_CANONICAL", edge_origin="FETCH_OBSERVED",
                    observed_by_fetch_event_id=item.fetch_event_id, now=now,
                ):
                    result.edges_created += 1

        for _, href in _ALTERNATE_LINK.findall(item.raw):
            target_url = href.decode()
            if normalize_locator(target_url) == item.source.normalized_locator:
                continue
            target = acquisition.get_or_create_source(session, target_url, now)
            if acquisition.record_edge(
                session, from_source=item.source, to_source=target,
                relation_type="LANGUAGE_VARIANT_OF", edge_origin="FETCH_OBSERVED",
                observed_by_fetch_event_id=item.fetch_event_id, now=now,
            ):
                result.edges_created += 1

    # Mirrors: one semantic document, two registrable domains.
    rows = session.execute(
        select(
            ResearchArtifactDerivation.artifact_id,
            ResearchArtifactDerivation.body_id,
        ).where(ResearchArtifactDerivation.body_id.in_(list(by_body)))
    ).all()
    by_artifact: dict[uuid.UUID, list[_Retrieved]] = {}
    for artifact_id, body_id in rows:
        by_artifact.setdefault(artifact_id, []).append(by_body[body_id])

    for members in by_artifact.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda item: item.source.normalized_locator)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if left.source.registrable_domain == right.source.registrable_domain:
                    continue
                if acquisition.record_edge(
                    session, from_source=left.source, to_source=right.source,
                    relation_type="MIRROR_CANDIDATE", edge_origin="DERIVED",
                    observed_by_fetch_event_id=left.fetch_event_id,
                    corroborating_fetch_event_id=right.fetch_event_id, now=now,
                ):
                    result.edges_created += 1


def _extract(
    session: Session, attempt: OperationalResearchAttempt,
    fetched: list[_Retrieved], now: datetime, result: PipelineResult,
    confirm_sampled: bool,
) -> list[tuple[Observation, uuid.UUID]]:
    observations: list[tuple[Observation, uuid.UUID]] = []
    for item in fetched:
        try:
            derivation, _, derivation_created = artifacts.derive_artifact(
                session, body_id=item.body_id, raw=item.raw,
                media_type=item.media_type, now=now,
            )
            text_derivation, text_created = artifacts.derive_text(
                session, body_id=item.body_id, raw=item.raw,
                media_type=item.media_type, now=now,
            )
        except artifacts.CanonicalizationMismatchError:
            result.failed_sources.append(item.source.normalized_locator)
            continue
        if derivation_created:
            result.derivations_created += 1
        if text_created:
            result.text_derivations_created += 1

        for extractor in extractors_for(item.media_type):
            sample_slot = (
                f"{attempt.id}:{extractor.extractor_id}"
                if extractor.determinism == "SAMPLED" else None
            )
            outcome = run_extraction(
                session, extractor=extractor, text_derivation=text_derivation,
                attempt_id=attempt.id, now=now, sample_execution_id=sample_slot,
            )
            if outcome.created:
                result.extractions_created += 1
            else:
                result.extractions_reused += 1

            context = EvidenceContext(
                extraction_id=outcome.extraction.id,
                fetch_event_id=item.fetch_event_id,
                artifact_derivation_id=derivation.id,
                body_id=item.body_id,
                source=item.source,
            )
            for observation in outcome.observations:
                evidence, created = create_evidence_item(
                    session, context=context, observation=observation, now=now
                )
                if created:
                    result.evidence_created += 1
                if extractor.determinism == "SAMPLED" and not confirm_sampled:
                    # Sampled output may create evidence, but may not on its own
                    # assert a company claim. It waits for a human.
                    result.sampled_pending_review.append(evidence.id)
                    continue
                observations.append((observation, evidence.id))

            if extractor.determinism == "SAMPLED" and confirm_sampled:
                observations.extend(_confirm_sampled(
                    session, attempt=attempt, text_derivation=text_derivation,
                    item=item, derivation_id=derivation.id, now=now, result=result,
                ))
    return observations


def _confirm_sampled(
    session: Session, *, attempt: OperationalResearchAttempt, text_derivation,
    item: _Retrieved, derivation_id: uuid.UUID, now: datetime,
    result: PipelineResult,
) -> list[tuple[Observation, uuid.UUID]]:
    """A human confirming a sampled read is a separate, deterministic extraction.

    The sampled evidence stays exactly as it was; confirmation does not edit it.
    What changes is that an assertable lineage now exists.
    """
    outcome = run_extraction(
        session, extractor=HUMAN_EXTRACTOR, text_derivation=text_derivation,
        attempt_id=attempt.id, now=now,
    )
    if outcome.created:
        result.extractions_created += 1
    else:
        result.extractions_reused += 1
    context = EvidenceContext(
        extraction_id=outcome.extraction.id, fetch_event_id=item.fetch_event_id,
        artifact_derivation_id=derivation_id, body_id=item.body_id, source=item.source,
    )
    confirmed: list[tuple[Observation, uuid.UUID]] = []
    for observation in outcome.observations:
        evidence, created = create_evidence_item(
            session, context=context, observation=observation, now=now
        )
        if created:
            result.evidence_created += 1
        confirmed.append((observation, evidence.id))
    return confirmed


def _assert(
    session: Session, run: OperationalResearchRun,
    attempt: OperationalResearchAttempt, company_id: uuid.UUID,
    observations: list[tuple[Observation, uuid.UUID]], now: datetime,
    result: PipelineResult,
) -> None:
    for pending in claims.group_observations(observations):
        outcome = claims.assert_claim(
            session, company_id=company_id, pending=pending, now=now
        )
        if outcome.created:
            result.claims_created += 1
        result.evidence_links_created += outcome.links_created


def _raise_gaps(
    session: Session, run: OperationalResearchRun,
    attempt: OperationalResearchAttempt, company_id: uuid.UUID,
    observations: list[tuple[Observation, uuid.UUID]], now: datetime,
    result: PipelineResult,
) -> None:
    """Every targeted attribute nobody evidenced becomes a gap, not a false.

    "Evidenced" spans the whole question, not just this attempt. A second run
    that gets 304 for every page extracts nothing, and an attempt-local view
    would then declare every attribute a gap — turning "nothing changed" into
    "we know nothing", which is the inverse of what a 304 means.
    """
    evidenced = {observation.attribute_key for observation, _ in observations}
    evidenced |= {
        key for key in session.scalars(
            select(CompanyClaim.attribute_key).where(
                CompanyClaim.subject_company_id == company_id,
                CompanyClaim.attribute_registry_version == RESEARCH_REGISTRY_VERSION,
                CompanyClaim.availability == "OBSERVED",
            )
        ).all()
    }
    for key in run.target_attribute_keys:
        if key in evidenced:
            continue
        outcome = gaps.raise_gap(
            session, run_id=run.id, company_id=company_id, attribute_key=key,
            gap_kind="NO_EVIDENCE", attempt_id=attempt.id, now=now,
            note="no evidence found in this attempt",
        )
        if outcome.created:
            result.gaps_raised += 1
        else:
            gaps.record_attempt(
                session, gap_id=outcome.gap.id, attempt_id=attempt.id, now=now
            )


def _raise_identity_signals(
    session: Session, attempt: OperationalResearchAttempt, company_id: uuid.UUID,
    fetched: list[_Retrieved], now: datetime, result: PipelineResult,
) -> None:
    """A registry filing naming a parent entity is a question, not a merge."""
    import json

    from boro_gtm.research.domain.models import ResearchEvidenceItem

    for item in fetched:
        if item.media_type != "application/json":
            continue
        try:
            record = json.loads(item.raw)
        except ValueError:  # pragma: no cover - fixture contract
            continue
        for related in record.get("related_entities", []):
            if related.get("relationship") != "PARENT":
                continue
            evidence_ids = session.scalars(
                select(ResearchEvidenceItem.id).where(
                    ResearchEvidenceItem.source_id == item.source.id
                )
            ).all()
            outcome = identity.raise_signal(
                session, company_id=company_id, signal_kind="POSSIBLE_PARENT",
                concern=related["name"], attempt_id=attempt.id,
                evidence_item_ids=list(evidence_ids), now=now,
            )
            if outcome.occurrence_created:
                result.signals_raised += 1
