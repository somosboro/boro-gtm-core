"""First-party source discovery for one canonical M2 company.

The scope question comes first and is answered by M2, not by this module: which
domains may be researched *as* this company. Everything else — sitemaps, links,
relevance — only narrows what is already in scope. A crawler that decides for
itself which site belongs to a company will eventually attach one contractor's
evidence to another's account, and there is no provenance repair for that
(M3-ADR-074).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.discovery.domain.models import CompanyDomain
from boro_gtm.discovery.services.resolution import is_identity_domain
from boro_gtm.research.fixtures.transport import DiscoveredLocator
from boro_gtm.research.live.policy import (
    FIRST_PARTY_POLICY_VERSION,
    CrawlBudget,
    is_relevant,
    relevance_hint,
    strip_presentation_params,
)
from boro_gtm.research.policies import host_of, normalize_locator, registrable_domain

#: Roles M2 may have recorded for a domain, and whether research may treat that
#: domain as *this company speaking about itself*.
#:
#: `IDENTITY` — M2's partial unique index guarantees at most one company holds
#:   it, so it is the company.
#: `ALTERNATE`, `REDIRECT`, `COUNTRY_TLD` — the same organisation under another
#:   address. In scope, because M2 already decided they are the same company.
#: `GROUP` — a shared host (`*.wixsite.com`, a franchise portal). M2 records it
#:   precisely because it does **not** identify a company. Never in scope.
#: `DEFUNCT` — historical. Out of scope: whatever is served there now is
#:   somebody else's, or nobody's.
RESEARCHABLE_ROLES = frozenset({"IDENTITY", "ALTERNATE", "REDIRECT", "COUNTRY_TLD"})

_LINK = re.compile(rb"""<a\b[^>]*?href\s*=\s*["']([^"'>\s]+)["']""", re.I)
_SITEMAP_LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_INDEX = re.compile(rb"<sitemapindex", re.I)


class NoResearchableDomainError(Exception):
    """M2 knows this company but not a domain research may speak for."""


@dataclass
class DomainScope:
    """The hosts this company's research may visit, and why each is allowed."""

    company_id: uuid.UUID
    #: Registrable domains, lower-cased. Membership is by registrable domain so
    #: `www.` and a `service.` subdomain are in scope, and `notthem.com` is not.
    registrable: dict[str, str] = field(default_factory=dict)
    #: The address research starts from.
    primary: str | None = None
    #: Domains M2 knows but research refuses, with the role that excluded them.
    excluded: dict[str, str] = field(default_factory=dict)

    def allows(self, url: str) -> bool:
        host = host_of(normalize_locator(url))
        return bool(host) and registrable_domain(host) in self.registrable

    def reason_for(self, url: str) -> str | None:
        host = host_of(normalize_locator(url))
        return self.registrable.get(registrable_domain(host)) if host else None


def domain_scope(session: Session, company_id: uuid.UUID) -> DomainScope:
    """Ask M2 which domains are this company, using M2's own rules.

    Never derived from the company's *name*: two contractors called "Allied
    Mechanical" are two companies, and a name lookup would silently research
    whichever one ranks better.
    """
    rows = session.scalars(
        select(CompanyDomain)
        .where(CompanyDomain.company_id == company_id)
        .order_by(CompanyDomain.domain_normalized)
    ).all()

    scope = DomainScope(company_id=company_id)
    identity: str | None = None
    for row in rows:
        domain = (row.domain_normalized or "").lower().strip().rstrip(".")
        if not domain:
            continue
        if row.domain_role not in RESEARCHABLE_ROLES:
            scope.excluded[domain] = row.domain_role
            continue
        if not is_identity_domain(domain):
            # The shared-host blocklist M2 applies when it writes the
            # projection, applied again here. A domain that cannot carry
            # identity cannot be crawled as a company either, whatever role a
            # row happens to say.
            scope.excluded[domain] = "BLOCKLISTED_HOST"
            continue
        scope.registrable[registrable_domain(domain)] = row.domain_role
        if row.domain_role == "IDENTITY" and identity is None:
            identity = domain

    if not scope.registrable:
        raise NoResearchableDomainError(
            f"company {company_id} has no domain research may speak for; "
            f"M2 recorded {dict(scope.excluded) or 'no domains at all'}"
        )
    if identity is None:
        # No IDENTITY row, but M2 accepted an alternate. Deterministic choice so
        # two runs start from the same place.
        identity = sorted(scope.registrable)[0]
    scope.primary = f"https://{identity}/"
    return scope


class BoRoFirstPartyDiscoveryProvider:
    """Homepage, robots-declared sitemaps, and bounded in-scope links.

    Implements the same seven methods as the fixture provider so the pipeline
    does not have to know which one it holds. The four this version does not do
    — `SEARCH`, `JOB_BOARD`, `REGISTRY`, `API` — return nothing rather than
    guessing: third-party search is where wrong-company evidence comes from, and
    it is not needed until first-party research proves insufficient.
    """

    def __init__(
        self, transport, *, scope: DomainScope, budget: CrawlBudget | None = None,
        human_seed_urls: tuple[str, ...] = (),
    ) -> None:
        self.transport = transport
        self.scope = scope
        self.budget = budget or CrawlBudget()
        self._human_seeds = human_seed_urls
        #: Recorded so the operator can see what was refused and why, instead of
        #: wondering why a page they expected never appeared.
        self.rejected_out_of_scope: list[str] = []
        self.rejected_irrelevant: list[str] = []
        #: Set when a budget stopped exploration, so the attempt can say that
        #: the picture is incomplete *because we stopped*, not because the site
        #: had nothing (M3-ADR-072).
        self.budget_exhausted_at: str | None = None
        #: Bodies already read during discovery, within this attempt only.
        #: Discovery reads a page to find links; the pipeline reads it again to
        #: record evidence. Both are legitimate, but asking the server twice for
        #: the same bytes in the same attempt is not, and it doubled the request
        #: count against every site in the first pilot.
        self._pages: dict[str, bytes | None] = {}

    # -- helpers -----------------------------------------------------------

    def _context(self, **extra) -> dict[str, object]:
        return {"policy_version": FIRST_PARTY_POLICY_VERSION,
                "provider": "boro_first_party", **extra}

    def _admit(self, url: str, *, allow_any_path: bool = False) -> str | None:
        """In scope and worth fetching, or a recorded reason why not."""
        try:
            locator = normalize_locator(strip_presentation_params(url))
        except ValueError:                              # pragma: no cover
            return None
        if urlsplit(locator).scheme not in ("http", "https"):
            return None
        if not self.scope.allows(locator):
            self.rejected_out_of_scope.append(locator)
            return None
        if not allow_any_path and not is_relevant(locator):
            self.rejected_irrelevant.append(locator)
            return None
        return locator

    def seed_inputs(self) -> dict[str, object]:
        """The scope this attempt ran under, recorded on the attempt.

        Includes the domains that were *excluded* and why. An operator asking
        "why did this account find nothing?" should not have to guess whether a
        domain was missing or refused.
        """
        return {
            "provider": "boro_first_party",
            "policy_version": FIRST_PARTY_POLICY_VERSION,
            "primary": self.scope.primary,
            "in_scope_domains": sorted(self.scope.registrable),
            "excluded_domains": dict(sorted(self.scope.excluded.items())),
            "human_seeds": list(self._human_seeds),
            "budget": {
                "max_pages": self.budget.max_pages,
                "max_depth": self.budget.max_depth,
                "max_bytes": self.budget.max_bytes,
                "max_retrievals": self.budget.max_retrievals,
            },
        }

    def plan(self) -> list[tuple[DiscoveredLocator, str | None]]:
        """Identity address → robots/sitemap → bounded in-scope links.

        Depth is bounded by walking one level of links from each page the first
        level found, and no further. Two hops from the homepage reaches
        "/services/commercial/", which is where contractors describe what they
        actually do; past that it is blog archives.
        """
        batches: list[tuple[DiscoveredLocator, str | None]] = []
        #: Addresses discovered, by any method. A repeat is still recorded —
        #: *how* a page was found is provenance — but it does not widen the
        #: frontier twice.
        seen: set[str] = set()
        #: Pages whose links have already been read. Kept apart from `seen`:
        #: the sitemap discovers `/services/commercial` at depth 0, and treating
        #: that as "already expanded" meant depth 2 was never reached at all.
        expanded: set[str] = set()

        def add(candidate: DiscoveredLocator, parent: str | None) -> None:
            batches.append((candidate, parent))
            seen.add(candidate.url)

        seeds = self.human_seeds()
        for candidate in seeds:
            add(candidate, None)
        site = self.scope.primary
        if site is None:                                # pragma: no cover
            return batches

        for candidate in self.sitemap(site):
            add(candidate, site)

        frontier = [c.url for c in seeds]
        for depth in range(1, self.budget.max_depth + 1):
            next_frontier: list[str] = []
            for parent in frontier:
                if parent in expanded:
                    continue
                expanded.add(parent)
                for candidate in self.crawl_links(parent):
                    context = dict(candidate.context or {})
                    context["depth"] = depth
                    add(replace(candidate, context=context), parent)
                    if candidate.url not in expanded:
                        next_frontier.append(candidate.url)
                if len(seen) >= self.budget.max_retrievals:
                    self.budget_exhausted_at = "DISCOVERY_FRONTIER"
                    return batches
            # Best pages first, so a narrow budget expands the ones most likely
            # to describe the operation.
            frontier = sorted(
                dict.fromkeys(next_frontier), key=lambda u: -relevance_hint(u)
            )[: self.budget.max_pages]
            if not frontier:
                break
        return batches

    # -- the seven methods -------------------------------------------------

    def human_seeds(self) -> list[DiscoveredLocator]:
        """The M2 identity address, plus anything an operator named explicitly.

        The homepage is a `HUMAN_SEED` because that is what it is: M2 asserted
        this domain is the company, and research takes that assertion as given
        rather than rediscovering it.
        """
        found: list[DiscoveredLocator] = []
        seen: set[str] = set()
        for url, why in [(self.scope.primary, "m2_identity_domain"),
                         *[(u, "operator") for u in self._human_seeds]]:
            if not url:
                continue
            locator = self._admit(url, allow_any_path=True)
            if locator is None or locator in seen:
                continue
            seen.add(locator)
            found.append(DiscoveredLocator(
                locator, "HUMAN_SEED",
                self._context(origin=why,
                              domain_role=self.scope.reason_for(locator)),
                relevance_hint=1.0,
            ))
        return found

    def sitemap(self, site: str) -> list[DiscoveredLocator]:
        """Sitemaps `robots.txt` declares, plus the conventional location.

        One level of sitemap index is followed, which is what contractor sites
        on WordPress actually publish. Deeper nesting is not chased: the
        homepage links reach the same pages.
        """
        if self._admit(site, allow_any_path=True) is None:
            return []
        declared = list(self.transport.sitemaps_for(site))
        parts = urlsplit(normalize_locator(site))
        conventional = f"{parts.scheme}://{parts.netloc}/sitemap.xml"
        if conventional not in declared:
            declared.append(conventional)

        urls: list[tuple[str, str]] = []
        for sitemap_url in declared[:3]:
            if self.scope.allows(sitemap_url) is False:
                self.rejected_out_of_scope.append(sitemap_url)
                continue
            for nested, listed in self._read_sitemap(sitemap_url, follow_index=True):
                urls.append((listed, nested))

        found: list[DiscoveredLocator] = []
        seen: set[str] = set()
        for listed, sitemap_url in urls:
            locator = self._admit(listed)
            if locator is None or locator in seen:
                continue
            seen.add(locator)
            found.append(DiscoveredLocator(
                locator, "SITEMAP", self._context(sitemap=sitemap_url),
                discovered_from=site, relevance_hint=relevance_hint(locator),
            ))
        return found

    def _page(self, locator: str) -> bytes | None:
        """Read a page for its links, once per attempt.

        `fetch_auxiliary`, not `fetch`: this read exists to find addresses. The
        retrieval that produces evidence is the pipeline's, and it records its
        own fetch event with its own conditional request.
        """
        if locator in self._pages:
            return self._pages[locator]
        result = self.transport.fetch_auxiliary(locator)
        body = result.body if result.outcome == "OK" else None
        self._pages[locator] = body
        return body

    def _read_sitemap(
        self, sitemap_url: str, *, follow_index: bool
    ) -> list[tuple[str, str]]:
        # `fetch_auxiliary`: a sitemap is `application/xml`, which the document
        # media-type gate refuses, and it is not a page we researched so it must
        # not spend the crawl budget.
        result = self.transport.fetch_auxiliary(sitemap_url)
        if result.outcome != "OK" or not result.body:
            return []
        body = result.body
        locs = [loc.decode("utf-8", "replace") for loc in _SITEMAP_LOC.findall(body)]
        if _SITEMAP_INDEX.search(body) and follow_index:
            nested: list[tuple[str, str]] = []
            for child in locs[:3]:
                if self.scope.allows(child):
                    nested.extend(self._read_sitemap(child, follow_index=False))
            return nested
        return [(sitemap_url, loc) for loc in locs]

    def crawl_links(self, from_url: str) -> list[DiscoveredLocator]:
        """In-scope, relevant links from one already-fetched page.

        Refetches the page rather than being handed its bytes, because the
        pipeline's discovery stage runs before its fetch stage. The transport's
        per-host delay applies, so this is one extra polite request for the
        homepage and nothing more.
        """
        locator = self._admit(from_url, allow_any_path=True)
        if locator is None:
            return []
        body = self._page(locator)
        if not body:
            return []
        base = locator

        found: list[DiscoveredLocator] = []
        seen: set[str] = set()
        for raw in _LINK.findall(body):
            href = raw.decode("utf-8", "replace").strip()
            if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            candidate = self._admit(urljoin(base, href))
            if candidate is None or candidate in seen or candidate == locator:
                continue
            seen.add(candidate)
            found.append(DiscoveredLocator(
                candidate, "CRAWL_LINK",
                self._context(anchor_from=locator, depth=1),
                discovered_from=locator, relevance_hint=relevance_hint(candidate),
            ))
        return found

    # The rest are deliberately empty in v1. Returning nothing is a decision,
    # recorded here rather than left as an unimplemented method that raises.
    def search(self, query: str) -> list[DiscoveredLocator]:
        """Not used: third-party search is how another company's page arrives."""
        return []

    def job_board(self) -> list[DiscoveredLocator]:
        """Not used: the company's own careers pages are first-party already."""
        return []

    def registry(self) -> list[DiscoveredLocator]:
        """Not used: M2 owns registry identity, and M3 does not re-derive it."""
        return []

    def api(self) -> list[DiscoveredLocator]:
        """Not used: BoRo has no partner feed for this cohort."""
        return []
