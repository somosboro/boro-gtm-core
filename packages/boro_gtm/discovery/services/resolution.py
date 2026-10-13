"""Entity resolution (design §6).

Three tiers with deliberately different authority. The load-bearing rule:
**fuzzy name similarity may retrieve candidates, never decide a merge.**

Concurrency is handled declaratively. Two partial unique indexes plus a
composite self-FK make the decision graph a linear chain per provider entity,
so the only thing application code must do is catch the conflict and re-read —
never hold a lock, never trust a protocol.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from boro_gtm.core import public_suffix
from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.domain.models import (
    Company,
    CompanyDomain,
    CompanyName,
    DiscoveryProvider,
    EntityResolutionCandidate,
    EntityResolutionDecision,
    EntityResolutionHead,
    ProviderEntity,
    ProviderRecordNormalization,
    ProviderRecordVersion,
)
from boro_gtm.discovery.enums import (
    CompanyLifecycle,
    ExternalIdKind,
    ResolutionDecision,
    ResolutionMethod,
)
from boro_gtm.discovery.providers.base import CandidateCompany
from boro_gtm.discovery.registry import IDENTITY_POLICY_VERSION

logger = logging.getLogger(__name__)

#: Above this, a candidate match is auto-accepted. Configuration, not truth.
AUTO_MATCH_THRESHOLD = 0.80
#: Between the two, the record is parked as AMBIGUOUS for human review.
AMBIGUOUS_THRESHOLD = 0.45

#: Domains that never carry identity: thousands of unrelated firms share them.
DOMAIN_BLOCKLIST = frozenset({
    # Shared site builders and hosts: where a company lives, not who it is.
    "wixsite.com", "business.site", "weebly.com", "squarespace.com",
    "wordpress.com", "blogspot.com", "godaddysites.com", "godaddy.com",
    "myshopify.com", "sites.google.com", "square.site", "webnode.com",
    "jimdosite.com", "netlify.app", "vercel.app", "github.io",
    # Social and video. A listing whose "website" is a Facebook page tells us
    # the company has a Facebook page.
    "google.com", "facebook.com", "linkedin.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "tiktok.com", "pinterest.com",
    "nextdoor.com",
    # Directories, review sites and lead brokers. A contractor's Places listing
    # often points at one of these instead of a first-party site, and treating
    # it as identity would merge every contractor on that directory into one
    # company (M2-ADR-046).
    "yelp.com", "yellowpages.com", "bbb.org", "angi.com", "angieslist.com",
    "homeadvisor.com", "thumbtack.com", "houzz.com", "porch.com",
    "buildzoom.com", "manta.com", "bizapedia.com", "mapquest.com",
    "chamberofcommerce.com", "dandb.com", "opencorporates.com",
    "thebluebook.com", "procore.com", "networx.com", "hometownlocal.com",
    # Job boards. A careers URL is not a company's identity domain.
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "linkedin.cn",
    # Booking and scheduling platforms operated for the contractor.
    "housecallpro.com", "servicetitan.com", "calendly.com", "acuityscheduling.com",
    "setmore.com", "schedulicity.com",
})

#: Signal weights for candidate scoring (design §6.5).
SIGNAL_WEIGHTS: dict[str, float] = {
    "legal_name_exact_same_market": 0.45,
    "name_and_city": 0.40,
    "shared_phone": 0.25,
    "shared_address": 0.25,
    "shared_group_domain": 0.10,
    "fuzzy_name": 0.10,
    "same_country": 0.05,
}


class IdentityIndexInvariantError(GtmError):
    """One resolve pass accepted the same identity domain for two companies.

    Tier 1 consults the index before anything can create or match, so this is
    unreachable unless that ordering is broken. It stops the pass: choosing
    either company would be a merge nobody decided.
    """

    code = "IDENTITY_INDEX_INVARIANT"
    http_status = 500


@dataclass(slots=True)
class RunIdentityIndex:
    """Identity domains accepted earlier in *this* resolve pass.

    The deterministic match reads `company_domains`, a projection rebuilt when a
    run finishes. Within one pass the second listing of a domain therefore could
    not see the first, and a company found twice in a run became two companies
    (M2-ADR-056). This index is that missing visibility and nothing more: the
    rule is still `identity_domain_exact`, and only a domain that the identity
    policy accepts for the candidate itself is ever put in it.

    It lives for one `resolve_run` call and is passed in explicitly. It is not
    module state, and it writes no row.
    """

    _accepted: dict[str, uuid.UUID] = field(default_factory=dict)

    def get(self, domain: str) -> uuid.UUID | None:
        return self._accepted.get(domain)

    def record(self, domain: str, company_id: uuid.UUID) -> None:
        held = self._accepted.setdefault(domain, company_id)
        if held != company_id:
            raise IdentityIndexInvariantError(
                f"{domain} was accepted as the identity of two companies in one "
                "resolve pass.",
                details={"domain": domain, "company_ids": sorted([str(held),
                                                                  str(company_id)])},
            )

    def __len__(self) -> int:
        return len(self._accepted)


def identity_key(candidate: CandidateCompany) -> str | None:
    """The domain this candidate may be identified by, or `None`.

    The whole identity policy for a candidate, in one place, so the lookup and
    the index can never disagree about what counts: the record must hold the
    domain alone, and the domain must be one an organisation registers for
    itself. A deep path, a hosting tenant, a platform and a directory all
    return `None` (M2-ADR-053, -054, -055).
    """
    if candidate.registrable_domain_shared:
        return None
    domain = normalize_domain(candidate.registrable_domain)
    if not domain or domain_role(domain) != "IDENTITY":
        return None
    return domain


@dataclass(frozen=True, slots=True)
class _IdentityMatch:
    company_id: uuid.UUID
    domain: str
    #: `durable_projection` or `same_run`. Where the fact was read from, not a
    #: different rule.
    source: str


@dataclass(frozen=True, slots=True)
class _IdentityConflict:
    domain: str
    durable_company_id: uuid.UUID
    same_run_company_id: uuid.UUID


@dataclass(slots=True)
class ResolutionOutcome:
    decision_id: uuid.UUID
    company_id: uuid.UUID | None
    decision: str
    method: str
    signals: dict[str, object] = field(default_factory=dict)
    created_company: bool = False
    #: Whether *this* call appended a decision. False when the entity was
    #: already resolved, when nothing changed, and when another worker won the
    #: race — so callers can count what happened rather than what the head says.
    written: bool = True


def _bare_host(domain: str | None) -> str | None:
    """The host inside whatever a provider handed over: a URL, or a bare name."""
    if not domain:
        return None
    value = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.rsplit("@", 1)[-1].split(":")[0].rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    return value or None


def normalize_domain(domain: str | None) -> str | None:
    """Reduce to the registrable domain under the Public Suffix List.

    Subdomains of a company's own domain are dropped: `shop.acme.de` is
    `acme.de`. A *tenant* of a hosting platform is not a subdomain in that
    sense. `someone.lovable.app` stays `someone.lovable.app`, because the list's
    private section says other people register beneath `lovable.app`, and
    collapsing to it would hand every tenant the same identity (M2-ADR-053).
    """
    return public_suffix.registrable_domain(_bare_host(domain))


def normalize_name(name: str | None) -> str | None:
    """Case-fold and strip common legal suffixes, for *retrieval* only."""
    if not name:
        return None
    value = " ".join(name.strip().casefold().split())
    suffixes = (
        " gmbh & co. kg", " gmbh", " ltd.", " ltd", " limited", " s.l.u.",
        " s.l.", " s.a.", " inc.", " inc", " llc", " b.v.", " n.v.", " plc",
        " oy", " ab", " a/s", " sp. z o.o.", " pty ltd", " srl", " s.r.l.",
    )
    for suffix in suffixes:
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
            break
    return value or None


def domain_role(domain: str | None) -> str | None:
    """The strongest role M2 may give a domain: IDENTITY, ALTERNATE or GROUP.

    `IDENTITY` — a domain an organisation registered for itself. The only role
        that takes part in `identity_domain_exact`.
    `ALTERNATE` — a tenant hostname on somebody else's platform
        (`someone.lovable.app`). It is this company's site and nobody else's,
        so it is not shared; but the company does not hold the registration and
        the platform can reassign the name, so it never decides identity
        (M2-ADR-054).
    `GROUP` — a host many unrelated companies sit behind: the platform's own
        domain, a directory, a social network.
    """
    parts = public_suffix.split(_bare_host(domain))
    if parts is None:
        return None
    if parts.private:
        return "ALTERNATE" if parts.registrable else "GROUP"
    if parts.registrable is None:
        return None
    if parts.registrable in DOMAIN_BLOCKLIST:
        return "GROUP"
    return "IDENTITY"


def is_identity_domain(domain: str | None) -> bool:
    """Whether a domain may stand alone as identity (design §8)."""
    return domain_role(domain) == "IDENTITY"


def has_identity_collision(session: Session, entity: ProviderEntity) -> bool:
    """Whether a derived key has been shown not to identify a single object.

    Design §4.2: two genuinely different organizations can derive one key (two
    "Schmidt GmbH" on one corporate domain). The evidence for that is more than
    one version under the entity whose **non-key** normalized identity fields
    disagree — here, distinct normalized legal names.

    This is computed, not stored. A collision only becomes visible when the
    second version arrives, and ``provider_entities`` is append-only, so a
    stored flag could never be set at the moment it becomes true (M2-ADR-034).

    Only ``DERIVED`` entities can collide this way. A native id is the
    provider's own assertion of identity, and a content hash makes every
    differing payload a separate entity by construction.
    """
    if entity.external_id_kind != ExternalIdKind.DERIVED.value:
        return False
    rows = session.execute(
        select(ProviderRecordNormalization.normalized_payload)
        .join(
            ProviderRecordVersion,
            ProviderRecordVersion.id
            == ProviderRecordNormalization.provider_record_version_id,
        )
        .where(
            ProviderRecordVersion.provider_entity_id == entity.id,
            ProviderRecordNormalization.error.is_(None),
        )
    ).scalars().all()
    names = {
        normalize_name((payload or {}).get("legal_name"))
        for payload in rows
    }
    names.discard(None)
    return len(names) > 1


def resolve(
    session: Session,
    entity: ProviderEntity,
    version: ProviderRecordVersion,
    candidate: CandidateCompany,
    provider: DiscoveryProvider,
    now: datetime | None = None,
    decided_by: str = "system:resolver",
    identity_index: RunIdentityIndex | None = None,
) -> ResolutionOutcome:
    """Resolve one provider entity to a canonical company.

    `identity_index` carries the identity domains this resolve pass has already
    accepted, so resolving within a run is the same as resolving across runs.
    """
    outcome = _resolve(session, entity, version, candidate, provider,
                       now or datetime.now(UTC), decided_by, identity_index)
    if identity_index is not None and outcome.written and outcome.company_id is not None:
        # Only a decision this pass wrote enters the index. An entity resolved
        # by an earlier run is already in the projection, and re-asserting it
        # here could contradict that projection where history left two anchors
        # on one domain.
        key = identity_key(candidate)
        if key is not None:
            identity_index.record(key, outcome.company_id)
    return outcome


def _resolve(
    session: Session,
    entity: ProviderEntity,
    version: ProviderRecordVersion,
    candidate: CandidateCompany,
    provider: DiscoveryProvider,
    now: datetime,
    decided_by: str,
    identity_index: RunIdentityIndex | None,
) -> ResolutionOutcome:
    existing = current_head(session, entity.id)
    if existing is not None and existing.decision not in (
        ResolutionDecision.AMBIGUOUS.value,
    ):
        # Already resolved and not awaiting review: nothing to decide.
        return ResolutionOutcome(
            decision_id=existing.id, company_id=existing.company_id,
            decision=existing.decision, method=existing.method,
            signals=existing.signals or {}, written=False,
        )

    # A record carrying no identifying attribute at all — typically one the
    # adapter could not interpret — must not mint a canonical company. It is
    # parked, and the raw evidence stays on file for a later normalizer.
    if not normalize_name(candidate.legal_name) and not normalize_domain(
        candidate.registrable_domain
    ):
        return _write_decision(
            session, entity, version, None,
            ResolutionDecision.AMBIGUOUS.value,
            ResolutionMethod.CANDIDATE_AUTO.value,
            {"reason": "no_identifying_attributes"},
            now, decided_by, supersedes=existing,
        )

    collision = has_identity_collision(session, entity)

    # --- tier 1: deterministic ------------------------------------------
    if not collision:
        deterministic = _deterministic_match(session, candidate, identity_index)
        if isinstance(deterministic, _IdentityConflict):
            # The projection and this pass name different companies for one
            # identity domain — another writer got there first. Neither is
            # chosen: a wrong-company merge is worse than an unmerged record.
            return _write_decision(
                session, entity, version, None,
                ResolutionDecision.AMBIGUOUS.value,
                ResolutionMethod.CANDIDATE_AUTO.value,
                {"reason": "identity_domain_conflict",
                 "domain": deterministic.domain,
                 "durable_company_id": str(deterministic.durable_company_id),
                 "same_run_company_id": str(deterministic.same_run_company_id)},
                now, decided_by, supersedes=existing,
            )
        if deterministic is not None:
            return _write_decision(
                session, entity, version, deterministic.company_id,
                ResolutionDecision.MATCHED.value,
                ResolutionMethod.DETERMINISTIC.value,
                {"rule": "identity_domain_exact",
                 "domain": deterministic.domain,
                 "source": deterministic.source},
                now, decided_by, supersedes=existing,
            )

    # --- tier 2: scored candidates ---------------------------------------
    scored = _score_candidates(session, candidate, provider)
    for company_id, score, signals in scored:
        session.add(EntityResolutionCandidate(
            provider_record_version_id=version.id,
            candidate_company_id=company_id,
            match_signals=signals, score=min(score, 1.0),
            tier="CANDIDATE", created_at=now,
        ))

    best = scored[0] if scored else None

    # An entity whose derived key has been shown to cover more than one
    # organization may neither auto-match nor create. Creating would mint an
    # identity for whichever of the two firms happened to be asked about.
    if collision:
        return _write_decision(
            session, entity, version, None,
            ResolutionDecision.AMBIGUOUS.value,
            ResolutionMethod.CANDIDATE_AUTO.value,
            {"reason": "identity_collision",
             "best_score": best[1] if best else None},
            now, decided_by, supersedes=existing,
        )

    if best is not None and best[1] >= AUTO_MATCH_THRESHOLD:
        return _write_decision(
            session, entity, version, best[0],
            ResolutionDecision.MATCHED.value,
            ResolutionMethod.CANDIDATE_AUTO.value,
            {"score": best[1], **best[2]}, now, decided_by, supersedes=existing,
        )

    if best is not None and best[1] >= AMBIGUOUS_THRESHOLD:
        # Creates nothing and merges nothing.
        return _write_decision(
            session, entity, version, None,
            ResolutionDecision.AMBIGUOUS.value,
            ResolutionMethod.CANDIDATE_AUTO.value,
            {"score": best[1], "candidate_company_id": str(best[0]), **best[2]},
            now, decided_by, supersedes=existing,
        )

    # --- tier 3: new identity ---------------------------------------------
    company = Company(
        created_at=now,
        identity_policy_version=IDENTITY_POLICY_VERSION,
        lifecycle_status=CompanyLifecycle.ACTIVE.value,
    )
    session.add(company)
    session.flush()
    outcome = _write_decision(
        session, entity, version, company.id,
        ResolutionDecision.CREATED_NEW.value,
        ResolutionMethod.DETERMINISTIC.value if not scored
        else ResolutionMethod.CANDIDATE_AUTO.value,
        {"best_score": best[1] if best else None}, now, decided_by,
        supersedes=existing, speculative_company_id=company.id,
    )
    outcome.created_company = outcome.company_id == company.id
    return outcome


def current_head(session: Session, provider_entity_id: uuid.UUID
                 ) -> EntityResolutionDecision | None:
    """The decision nothing supersedes.

    The chain is linear by construction (design §6.3), so this returns at most
    one row — no ``ORDER BY ... LIMIT 1`` is needed for correctness.
    """
    superseder = EntityResolutionDecision.__table__.alias("s")
    return session.scalars(
        select(EntityResolutionDecision)
        .where(EntityResolutionDecision.provider_entity_id == provider_entity_id)
        .where(
            ~select(1)
            .select_from(superseder)
            .where(superseder.c.supersedes_decision_id == EntityResolutionDecision.id)
            .exists()
        )
    ).first()


def _deterministic_match(
    session: Session, candidate: CandidateCompany,
    identity_index: RunIdentityIndex | None = None,
) -> _IdentityMatch | _IdentityConflict | None:
    """Exact identity-domain match, subject to the domain policy.

    One rule, read from two places: the durable projection, and the domains
    this pass has accepted and the projection does not show yet. They are the
    same signal. If they name different companies, that is reported rather than
    resolved.
    """
    domain = identity_key(candidate)
    if domain is None:
        # Includes a page on a domain the record does not hold alone — a
        # franchisor's location page, a parent's brand page. Sharing that
        # domain is not evidence of being the same company (M2-ADR-055).
        return None
    durable = session.scalar(
        select(CompanyDomain.company_id)
        .join(Company, Company.id == CompanyDomain.company_id)
        .where(
            CompanyDomain.domain_normalized == domain,
            CompanyDomain.domain_role == "IDENTITY",
            Company.lifecycle_status == CompanyLifecycle.ACTIVE.value,
        )
    )
    same_run = identity_index.get(domain) if identity_index is not None else None
    if durable is not None and same_run is not None and durable != same_run:
        return _IdentityConflict(domain=domain, durable_company_id=durable,
                                 same_run_company_id=same_run)
    if durable is not None:
        return _IdentityMatch(company_id=durable, domain=domain,
                              source="durable_projection")
    if same_run is not None:
        return _IdentityMatch(company_id=same_run, domain=domain, source="same_run")
    return None


def _score_candidates(session: Session, candidate: CandidateCompany,
                      provider: DiscoveryProvider
                      ) -> list[tuple[uuid.UUID, float, dict]]:
    """Retrieve and score plausible companies.

    Trigram similarity participates only in *retrieval* and contributes a low
    weight; it can never reach the auto-match threshold alone.
    """
    name = normalize_name(candidate.legal_name)
    domain = normalize_domain(candidate.registrable_domain)
    if not name and not domain:
        return []

    signals_by_company: dict[uuid.UUID, dict] = {}

    if name:
        exact = session.execute(
            select(CompanyName.company_id, CompanyName.name_normalized)
            .join(Company, Company.id == CompanyName.company_id)
            .where(
                CompanyName.name_normalized == name,
                Company.lifecycle_status == CompanyLifecycle.ACTIVE.value,
            )
        ).all()
        for company_id, _ in exact:
            signals_by_company.setdefault(company_id, {})["legal_name_exact_same_market"] = True

        fuzzy = session.execute(
            select(CompanyName.company_id, CompanyName.name_normalized)
            .join(Company, Company.id == CompanyName.company_id)
            .where(
                func.similarity(CompanyName.name_normalized, name) > 0.55,
                Company.lifecycle_status == CompanyLifecycle.ACTIVE.value,
            )
            .limit(25)
        ).all()
        for company_id, _ in fuzzy:
            signals_by_company.setdefault(company_id, {})["fuzzy_name"] = True

    if domain:
        shared = session.execute(
            select(CompanyDomain.company_id)
            .join(Company, Company.id == CompanyDomain.company_id)
            .where(
                CompanyDomain.domain_normalized == domain,
                CompanyDomain.domain_role == "GROUP",
                Company.lifecycle_status == CompanyLifecycle.ACTIVE.value,
            )
        ).all()
        for (company_id,) in shared:
            signals_by_company.setdefault(company_id, {})["shared_group_domain"] = True

    scored: list[tuple[uuid.UUID, float, dict]] = []
    for company_id, signals in signals_by_company.items():
        score = sum(SIGNAL_WEIGHTS.get(k, 0.0) for k, v in signals.items() if v)
        scored.append((company_id, round(min(score, 1.0), 4), signals))
    # Deterministic ordering: best score first, then company id.
    scored.sort(key=lambda t: (-t[1], str(t[0])))
    return scored


def _write_decision(session, entity, version, company_id, decision, method,
                    signals, now, decided_by, supersedes=None,
                    merged_company_id=None,
                    speculative_company_id: uuid.UUID | None = None
                    ) -> ResolutionOutcome:
    """Append a decision, tolerating both concurrency races.

    A conflict means another worker won; we re-read rather than retrying
    blindly, so no ``IntegrityError`` escapes to the caller.

    ``speculative_company_id`` is a company this attempt minted on the way in.
    If the attempt loses the race that company was never referenced by anyone,
    so it is removed rather than left behind as an orphan identity.
    """
    if (supersedes is not None
            and supersedes.decision == decision
            and supersedes.company_id == company_id):
        # Nothing changed, so appending another decision would only grow the
        # chain. Re-running resolution over an unchanged AMBIGUOUS entity is
        # routine; it must be a no-op.
        return ResolutionOutcome(
            decision_id=supersedes.id, company_id=supersedes.company_id,
            decision=supersedes.decision, method=supersedes.method,
            signals=supersedes.signals or {}, written=False,
        )

    values = {
        "id": uuid.uuid4(),
        "provider_entity_id": entity.id,
        "provider_record_version_id": version.id if version is not None else None,
        "company_id": company_id,
        "merged_company_id": merged_company_id,
        "decision": decision,
        "method": method,
        "supersedes_decision_id": supersedes.id if supersedes is not None else None,
        "identity_policy_version": IDENTITY_POLICY_VERSION,
        "signals": signals,
        "decided_by": decided_by,
        "decided_at": now,
    }
    try:
        with session.begin_nested():
            session.execute(pg_insert(EntityResolutionDecision).values(**values))
    except IntegrityError:
        # Either uq_resolution_root (another worker created the chain) or
        # uq_resolution_supersedes (another worker superseded this head first).
        head = current_head(session, entity.id)
        if head is None:  # pragma: no cover - defensive
            raise
        if speculative_company_id is not None:
            session.execute(
                delete(Company).where(Company.id == speculative_company_id)
            )
        logger.info(
            "Resolution race resolved by re-reading the head",
            extra={"provider_entity_id": str(entity.id), "decision_id": str(head.id)},
        )
        _upsert_head(session, entity.id, head)
        return ResolutionOutcome(
            decision_id=head.id, company_id=head.company_id,
            decision=head.decision, method=head.method,
            signals=head.signals or {}, written=False,
        )

    written = session.get(EntityResolutionDecision, values["id"])
    _upsert_head(session, entity.id, written)
    return ResolutionOutcome(
        decision_id=written.id, company_id=written.company_id,
        decision=written.decision, method=written.method, signals=signals,
    )


def _upsert_head(session: Session, provider_entity_id: uuid.UUID,
                 decision: EntityResolutionDecision) -> None:
    """Maintain the O(1) cache. It is a cache, never the invariant."""
    stmt = (
        pg_insert(EntityResolutionHead)
        .values(
            provider_entity_id=provider_entity_id,
            current_decision_id=decision.id,
            company_id=decision.company_id,
        )
        .on_conflict_do_update(
            index_elements=["provider_entity_id"],
            set_={"current_decision_id": decision.id, "company_id": decision.company_id},
        )
    )
    session.execute(stmt)


def append_human_decision(
    session: Session,
    entity: ProviderEntity,
    *,
    decision: str,
    company_id: uuid.UUID | None,
    decided_by: str,
    rationale: str | None = None,
    now: datetime | None = None,
) -> EntityResolutionDecision:
    """Append a reviewer's decision on top of the current head.

    Strictly additive: the superseded decision is never modified, and no claim
    is rewritten. Reassignment is expressed by the chain, not by mutation
    (design §6.3, M2-ADR-006).
    """
    now = now or datetime.now(UTC)
    head = current_head(session, entity.id)
    row = EntityResolutionDecision(
        provider_entity_id=entity.id,
        provider_record_version_id=head.provider_record_version_id if head else None,
        company_id=company_id,
        decision=decision,
        method=ResolutionMethod.HUMAN_REVIEW.value,
        supersedes_decision_id=head.id if head is not None else None,
        identity_policy_version=IDENTITY_POLICY_VERSION,
        signals={"source": "human_review"},
        rationale=rationale,
        decided_by=decided_by,
        decided_at=now,
    )
    session.add(row)
    session.flush()
    _upsert_head(session, entity.id, row)
    return row


def rebuild_heads(session: Session) -> int:
    """Rebuild the head cache from decisions alone."""
    session.query(EntityResolutionHead).delete()
    session.flush()
    s = EntityResolutionDecision.__table__.alias("s")
    heads = session.scalars(
        select(EntityResolutionDecision).where(
            ~select(1).select_from(s)
            .where(s.c.supersedes_decision_id == EntityResolutionDecision.id)
            .exists()
        )
    ).all()
    for decision in heads:
        session.add(EntityResolutionHead(
            provider_entity_id=decision.provider_entity_id,
            current_decision_id=decision.id,
            company_id=decision.company_id,
        ))
    session.flush()
    return len(heads)
