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
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.domain.models import CompanyClaim
from boro_gtm.research.domain.models import (
    OperationalResearchAttempt,
    OperationalResearchProfile,
    OperationalResearchRun,
    ResearchArtifactDerivation,
    ResearchFetchEvent,
    ResearchSource,
    ResearchSourceDiscovery,
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
from boro_gtm.research.services import (
    acquisition,
    artifacts,
    claims,
    gaps,
    identity,
    inference,
    profiles,
    review,
)
from boro_gtm.research.services.evidence import EvidenceContext, create_evidence_item
from boro_gtm.research.services.extraction import (
    Observation,
    extractors_for,
    observation_fingerprint,
    run_extraction,
)
from boro_gtm.research.services.staleness import stale_required_attributes

#: Queries the fixture plan issues. Two of them share a result, on purpose.
PLAN_SEARCH_QUERIES: tuple[str, ...] = tuple(corpus.SEARCH_RESULTS)


class Transport(Protocol):
    """What the pipeline needs from the outside world, and nothing more."""

    def fetch(
        self, url: str, *, if_none_match: str | None = None,
        robots_disallowed: frozenset[str] | None = None,
    ) -> Any: ...


class DiscoveryProvider(Protocol):
    """Where to look, and what to record about having looked there.

    Two implementations: `FixtureDiscoveryProvider` for deterministic tests and
    QA, and `BoRoFirstPartyDiscoveryProvider` for real account research. The
    pipeline holds one or the other and cannot tell which — which is the point,
    because the alternative was the fixture corpus being named inside the
    pipeline itself (M3-ADR-071).

    Not a registry, not a plugin system, not configurable by a third party.
    Selection is explicit at the call site.
    """

    def plan(self) -> list[tuple[DiscoveredLocator, str | None]]: ...

    def seed_inputs(self) -> dict[str, object]: ...

#: Below this extractor confidence an observation becomes a review candidate
#: rather than a claim. A policy number nobody has calibrated, named as such —
#: an uncalibrated threshold that looks authoritative is worse than one that
#: admits it.
REVIEW_CONFIDENCE_THRESHOLD = 0.60


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
    policy_version: str = RESEARCH_POLICY_VERSION,
) -> tuple[OperationalResearchRun, bool]:
    """A logical question. Asking it twice does not create two questions.

    ``policy_version`` is a parameter rather than a read of the module
    constant so that "a policy change is a different question" can be
    exercised directly, instead of by monkeypatching global state in a test.
    """
    now = now or _now()
    digest = research_plan_hash(
        company_id=company_id, policy_version=policy_version,
        vertical_id=vertical_id, target_attribute_keys=target_attribute_keys,
        plan_inputs=plan_inputs,
    )
    result = session.execute(
        pg_insert(OperationalResearchRun)
        .values(
            id=uuid.uuid4(), company_id=company_id,
            research_policy_version=policy_version, vertical_id=vertical_id,
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


class AttemptAlreadyLiveError(GtmError):
    """A second attempt was requested while one is still running.

    The partial unique index `uq_attempt_live` already makes this
    unrepresentable. Raising here turns that into a sentence the caller can act
    on, and a documented 409, rather than an `IntegrityError` naming an index
    (G15).
    """

    code = "RESEARCH_ATTEMPT_ALREADY_LIVE"
    http_status = 409


def start_attempt(
    session: Session, *, run: OperationalResearchRun, now: datetime | None = None,
    seed_inputs: dict | None = None, allow_partial_assertion: bool = True,
) -> OperationalResearchAttempt:
    """Open attempt n+1 and freeze its seeds.

    Seeds are frozen because an execution whose inputs changed mid-flight is
    not reproducible, and reproducibility is the only reason to record them.
    """
    now = now or _now()
    live = session.scalars(
        select(OperationalResearchAttempt).where(
            OperationalResearchAttempt.run_id == run.id,
            OperationalResearchAttempt.status.not_in(("COMPLETED", "PARTIAL", "FAILED")),
        )
    ).first()
    if live is not None:
        raise AttemptAlreadyLiveError(
            f"attempt {live.attempt_number} of run {run.id} is still "
            f"{live.status}; one question is executed by one worker at a time"
        )
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
    gaps_by_kind: dict[str, int] = field(default_factory=dict)
    inferences_created: int = 0
    low_confidence_deferred: list[str] = field(default_factory=list)
    signals_raised: int = 0
    edges_created: int = 0
    failed_sources: list[str] = field(default_factory=list)
    sampled_pending_review: list[uuid.UUID] = field(default_factory=list)
    #: Set when a crawl budget, rather than the website, ended exploration. The
    #: difference matters: "we stopped looking" is not "there was nothing there"
    #: (M3-ADR-072).
    budget_stopped_at: str | None = None
    #: Sources discovered and deliberately not retrieved, because the budget ran
    #: out. They stay in the discovery record, so a later attempt can take them.
    unretrieved_sources: list[str] = field(default_factory=list)

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
    transport: Transport | None = None,
    provider: DiscoveryProvider | None = None,
    now: datetime | None = None,
    vertical_id: uuid.UUID | None = None,
    conditional: bool = True,
    max_retrievals: int | None = None,
) -> PipelineResult:
    """Discover, retrieve, read, evidence and assert — once.

    ``transport`` and ``provider`` default to the fixture pair, so every
    existing test and the QA path are unchanged and no caller can reach the
    internet by forgetting an argument. Live research passes both explicitly;
    see `boro_gtm.research.live`.

    ``conditional`` sends ``If-None-Match`` from the last successful retrieval,
    which is what a polite crawler does and what makes a re-run cheap. Setting
    it to ``False`` forces a full re-read — the operational case where a
    validator is not trusted, and the only way to observe what happens when the
    same bytes genuinely arrive again.

    ``max_retrievals`` stops the fetch stage after that many attempts and
    records that a budget, rather than the site, ended exploration.
    """
    now = now or _now()
    transport = transport if transport is not None else FixtureTransport()
    if provider is None:
        # The fixture pair is the only one that may be assembled implicitly.
        # A live transport with a fixture provider would fetch the corpus's
        # invented addresses from the real internet, so it must be spelled out.
        if not isinstance(transport, FixtureTransport):
            raise ValueError(
                f"{type(transport).__name__} needs an explicit provider: only "
                "the fixture pair may be assembled by default, because a "
                "fixture provider names addresses that do not exist and a live "
                "transport would go and ask for them"
            )
        provider = FixtureDiscoveryProvider(transport)

    run, _ = get_or_create_run(
        session, company_id=company_id, now=now, vertical_id=vertical_id
    )
    attempt = start_attempt(
        session, run=run, now=now, seed_inputs=provider.seed_inputs()
    )
    result = PipelineResult(attempt=attempt)

    # -- DISCOVERING -------------------------------------------------------
    _advance(session, attempt, "DISCOVERING", None, now)
    candidates = _discover(session, provider, attempt, now, result)
    _advance(session, attempt, "FETCHING", "discovery_completed_at", now)

    # -- FETCHING ----------------------------------------------------------
    fetched = _retrieve(
        session, transport, attempt, candidates, now, result,
        conditional=conditional, max_retrievals=max_retrievals,
    )
    _advance(session, attempt, "EXTRACTING", "fetch_completed_at", now)

    # -- EXTRACTING --------------------------------------------------------
    observations = _extract(
        session, run, attempt, company_id, fetched, now, result
    )
    _relate_sources(session, fetched, now, result)
    _advance(session, attempt, "ASSERTING", "extraction_completed_at", now)

    # -- ASSERTING ---------------------------------------------------------
    _assert(session, run, attempt, company_id, observations, now, result)
    _infer(session, company_id, now, result)
    # The company profile is rebuilt *before* gaps, because a CONTRADICTED gap
    # is derived from the profile's contradiction state. Rebuilding afterwards
    # made contradictions appear only on the *next* run, which looked like a
    # gap that took two attempts to notice a disagreement already on record.
    profiles.rebuild_company_profile(session, company_id)
    _raise_gaps(session, run, attempt, company_id, observations, now, result)
    _raise_identity_signals(session, attempt, company_id, fetched, now, result)
    # The plan profile is last: its open_gap_count reads the gaps just raised.
    profiles.rebuild_plan_profile(session, run.id)

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
    session: Session, provider: DiscoveryProvider,
    attempt: OperationalResearchAttempt, now: datetime, result: PipelineResult,
) -> dict[str, tuple[ResearchSource, list[DiscoveredLocator]]]:
    """Every method, and every observation each one makes.

    One address found by a sitemap, a crawl link and two search queries is one
    source and four discovery rows. Collapsing them would lose the only record
    of *how* the system found what it found.

    The provider decides *what* to look for and this decides *how it is
    recorded*. The two were tangled: the fixture corpus's home page and query
    list were written into this function, so there was no way to run the
    pipeline against a real website without editing it (M3-ADR-071).
    """
    batches = provider.plan()

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
    session: Session, transport: Transport,
    attempt: OperationalResearchAttempt,
    candidates: dict[str, tuple[ResearchSource, list[DiscoveredLocator]]],
    now: datetime, result: PipelineResult, conditional: bool = True,
    max_retrievals: int | None = None,
) -> list[_Retrieved]:
    retrieved: list[_Retrieved] = []
    #: Best candidates first, so a budget that runs out spends what it had on
    #: the pages most likely to describe the operation. Ties fall back to the
    #: locator, so the order is deterministic.
    ordered = sorted(
        candidates.items(),
        key=lambda kv: (-max((c.relevance_hint or 0.0) for c in kv[1][1]), kv[0]),
    )
    for locator, (source, _) in ordered:
        if max_retrievals is not None and len(transport_requests(transport)) >= max_retrievals:
            result.budget_stopped_at = "MAX_RETRIEVALS"
            result.unretrieved_sources.append(locator)
            continue
        previous = acquisition.last_successful_fetch(session, source.id)
        fetch = transport.fetch(
            locator,
            if_none_match=previous.etag if (previous and conditional) else None,
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


def transport_requests(transport: Transport) -> list[str]:
    """How many retrievals this transport has already made.

    Both transports keep the list; the live one keeps it on `stats` because it
    also counts bytes and refusals.
    """
    stats = getattr(transport, "stats", None)
    if stats is not None:
        return stats.requests
    return getattr(transport, "requests", [])


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

    # Mirrors: one semantic document, two registrable domains. Grouped over
    # the retrieval list rather than a body-keyed dict, because two URLs
    # serving *byte-identical* content share one body -- and a dict keyed by
    # body id silently dropped one of them, so the clearest mirror of all was
    # the one case that produced no edge.
    artifact_of = dict(session.execute(
        select(
            ResearchArtifactDerivation.body_id,
            ResearchArtifactDerivation.artifact_id,
        ).where(
            ResearchArtifactDerivation.body_id.in_({item.body_id for item in fetched})
        )
    ).all())
    by_artifact: dict[uuid.UUID, list[_Retrieved]] = {}
    for item in fetched:
        artifact_id = artifact_of.get(item.body_id)
        if artifact_id is not None:
            by_artifact.setdefault(artifact_id, []).append(item)

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
    session: Session, run: OperationalResearchRun,
    attempt: OperationalResearchAttempt, company_id: uuid.UUID,
    fetched: list[_Retrieved], now: datetime, result: PipelineResult,
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
                # The raw document, so a locator can carry a structural path.
                # It never enters the extraction contract hash: how a reading
                # was produced is unchanged by being able to describe where.
                context={"raw": item.raw},
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
                weak = (
                    extractor.extractor_confidence is not None
                    and extractor.extractor_confidence < REVIEW_CONFIDENCE_THRESHOLD
                    and extractor.determinism != "SAMPLED"
                )
                if weak:
                    # Too weak to assert, but *observed* — a different state
                    # from finding nothing. The evidence is created and a
                    # durable candidate records why it is waiting, so a
                    # reviewer arriving after the process ended can still find
                    # it (D3, M3-ADR-061).
                    result.low_confidence_deferred.append(observation.attribute_key)
                    review.raise_candidate(
                        session, evidence_item_id=evidence.id,
                        attribute_key=observation.attribute_key,
                        observation_fingerprint=observation_fingerprint(observation),
                        reason=review.LOW_CONFIDENCE_REASON, now=now,
                        extractor_confidence=extractor.extractor_confidence,
                    )
                    continue
                if extractor.determinism == "SAMPLED":
                    # Sampled output may create evidence and may not assert.
                    # It waits for a human, and the wait is durable.
                    result.sampled_pending_review.append(evidence.id)
                    review.raise_candidate(
                        session, evidence_item_id=evidence.id,
                        attribute_key=observation.attribute_key,
                        observation_fingerprint=observation_fingerprint(observation),
                        reason=review.SAMPLED_REASON, now=now,
                        extractor_confidence=extractor.extractor_confidence,
                    )
                    continue
                observations.append((observation, evidence.id))

    return observations


def _assert(
    session: Session, run: OperationalResearchRun,
    attempt: OperationalResearchAttempt, company_id: uuid.UUID,
    observations: list[tuple[Observation, uuid.UUID]], now: datetime,
    result: PipelineResult,
) -> None:
    for pending in claims.group_observations(session, observations):
        outcome = claims.assert_claim(
            session, company_id=company_id, pending=pending, now=now
        )
        if outcome.created:
            result.claims_created += 1
        result.evidence_links_created += outcome.links_created


#: Failures that will not resolve by trying again. A timeout might; a 410 will
#: not, and calling them the same thing would make "we could not reach it"
#: indistinguishable from "it is not there".
PERMANENT_FETCH_FAILURES = frozenset({
    "NOT_FOUND", "GONE", "DENIED", "LOGIN_WALL", "ROBOTS_DENIED", "MIME_MISMATCH",
})


def _unreachable_attributes(
    session: Session, run: OperationalResearchRun, attempt_id: uuid.UUID,
) -> set[str]:
    """Attributes every pursued source failed permanently for.

    An earlier version attached `UNRESOLVABLE_SOURCE` to `targets[0]` whenever
    *any* source failed. That is not evidence semantics: the first target has
    no relationship to the failed source, and the fixture duly reported
    `branch_count` unreachable while `branch_count` had three good claims.

    A source is only unreachable *for a question* if the plan was pursuing that
    question when it found the source — which is why discovery now records
    `pursued_for`.
    """
    rows = session.execute(
        select(
            ResearchSourceDiscovery.source_id,
            ResearchSourceDiscovery.discovery_context,
        ).where(ResearchSourceDiscovery.attempt_id == attempt_id)
    ).all()
    pursued: dict[str, set[uuid.UUID]] = {}
    for source_id, context in rows:
        for key in (context or {}).get("pursued_for", []):
            pursued.setdefault(key, set()).add(source_id)
    if not pursued:
        return set()

    outcomes: dict[uuid.UUID, set[str]] = {}
    for source_id, outcome in session.execute(
        select(ResearchFetchEvent.source_id, ResearchFetchEvent.fetch_outcome)
        .where(ResearchFetchEvent.attempt_id == attempt_id)
    ).all():
        outcomes.setdefault(source_id, set()).add(outcome)

    unreachable = set()
    for key, source_ids in pursued.items():
        seen = [outcomes.get(sid, set()) for sid in source_ids]
        if not seen or not all(seen):
            continue
        if all(o <= PERMANENT_FETCH_FAILURES for o in seen):
            unreachable.add(key)
    return unreachable


def _raise_gaps(
    session: Session, run: OperationalResearchRun,
    attempt: OperationalResearchAttempt, company_id: uuid.UUID,
    observations: list[tuple[Observation, uuid.UUID]], now: datetime,
    result: PipelineResult,
) -> None:
    """Every targeted attribute we do not hold becomes exactly one kind of gap.

    The three "we do not have it" kinds are **mutually exclusive**, because they
    are different states and an attribute cannot be in two of them:

    * `UNRESOLVABLE_SOURCE` — every source pursued for it failed permanently;
    * `INSUFFICIENT_EVIDENCE` — something relevant *was* observed but did not
      meet the assertion bar;
    * `NO_EVIDENCE` — nothing relevant was observed at all.

    An earlier version raised `NO_EVIDENCE` and `INSUFFICIENT_EVIDENCE` for the
    same attribute in the same breath, which contradicts both definitions.

    `CONTRADICTED` and `STALE_EVIDENCE` are separate and may coexist with each
    other: they describe evidence we *do* hold, and holding contradictory stale
    evidence is a real state.

    "Evidenced" spans the whole question, not just this attempt. A second run
    that gets 304 for every page extracts nothing, and an attempt-local view
    would declare every attribute a gap — turning "nothing changed" into "we
    know nothing".
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

    def raise_kind(key: str, kind: str, note: str) -> None:
        outcome = gaps.raise_gap(
            session, run_id=run.id, company_id=company_id, attribute_key=key,
            gap_kind=kind, attempt_id=attempt.id, now=now, note=note,
        )
        if outcome.created:
            result.gaps_raised += 1
            result.gaps_by_kind[kind] = result.gaps_by_kind.get(kind, 0) + 1
        else:
            gaps.record_attempt(
                session, gap_id=outcome.gap.id, attempt_id=attempt.id, now=now
            )

    targets = list(run.target_attribute_keys)
    weak = set(result.low_confidence_deferred)
    unreachable = _unreachable_attributes(session, run, attempt.id)

    for key in targets:
        if key in evidenced:
            continue
        # Exactly one of the three, in order of how much it explains.
        if key in unreachable:
            raise_kind(key, "UNRESOLVABLE_SOURCE",
                       "every source pursued for this attribute failed permanently")
        elif key in weak:
            raise_kind(key, "INSUFFICIENT_EVIDENCE",
                       "observed below the review confidence threshold; awaiting review")
        else:
            raise_kind(key, "NO_EVIDENCE", "no evidence found in this attempt")

    for key in stale_required_attributes(session, company_id, targets, now.date()):
        raise_kind(key, "STALE_EVIDENCE", "freshest evidence is past its horizon")

    profile = session.get(OperationalResearchProfile, company_id)
    if profile is not None:
        for key, state in (profile.contradictions or {}).items():
            if key in targets and state.get("contradiction"):
                raise_kind(key, "CONTRADICTED", "sources disagree on this attribute")


def _infer(
    session: Session, company_id: uuid.UUID, now: datetime, result: PipelineResult,
) -> None:
    """Derived claims, by stated rule, never FACT."""
    for outcome in inference.apply_all(session, company_id=company_id, now=now):
        if outcome.created:
            result.inferences_created += 1


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
