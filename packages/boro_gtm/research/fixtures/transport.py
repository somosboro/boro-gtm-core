"""A fixture transport and fixture discovery providers. No network, ever.

The transport is the only place in M3 that pretends to be the outside world,
so it is also the only place that must be able to produce every retrieval
outcome — including the unpleasant ones. A pipeline only tested against
HTTP 200 is a pipeline whose error handling has never run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from boro_gtm.research.fixtures import corpus
from boro_gtm.research.policies import normalize_locator


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What one retrieval observed. Mirrors a fetch event, minus the ids."""

    outcome: str
    http_status: int | None = None
    body: bytes | None = None
    final_url: str | None = None
    declared_content_type: str | None = None
    content_length: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    validated_by: str | None = None
    validator_value: str | None = None
    error_class: str | None = None
    error_detail: str | None = None
    redirected_from: str | None = None


class FixtureTransport:
    """Serves the corpus, honours conditional requests, and can be mutated.

    ``apply_semantic_change`` switches one resource to its changed body, which
    is how the changed-source scenario is driven without editing a file
    mid-test.
    """

    #: Anything larger than this reports TOO_LARGE instead of returning bytes.
    byte_limit = 2_000_000

    def __init__(self, web: dict[str, corpus.FixtureResource] | None = None) -> None:
        self._web = dict(web or corpus.build_web())
        self._changed: set[str] = set()
        self._cosmetic: set[str] = set()
        #: Every locator requested, in order. Tests assert on this rather than
        #: on database rows when they mean "did we go out again?".
        self.requests: list[str] = []

    # -- corpus mutation ---------------------------------------------------

    def apply_semantic_change(self, url: str) -> None:
        self._changed.add(normalize_locator(url))

    def apply_cosmetic_change(self, url: str) -> None:
        self._cosmetic.add(normalize_locator(url))

    def set_etag(self, url: str, etag: str) -> None:
        """A page that really changed would also change its validator.

        Without this the fixture would answer 304 for a document whose bytes
        it had just replaced, which no real server does.
        """
        locator = normalize_locator(url)
        self._web[locator] = replace(self._web[locator], etag=etag)

    # -- retrieval ---------------------------------------------------------

    def fetch(
        self, url: str, *, if_none_match: str | None = None,
        robots_disallowed: frozenset[str] | None = None,
    ) -> FetchResult:
        locator = normalize_locator(url)
        self.requests.append(locator)

        disallowed = robots_disallowed or frozenset(
            normalize_locator(u) for u in corpus.ROBOTS_DISALLOWED
        )
        if locator in disallowed:
            return FetchResult(outcome="ROBOTS_DENIED", error_class="RobotsDisallowed")

        resource = self._web.get(locator)
        if resource is None:
            return FetchResult(outcome="NOT_FOUND", http_status=404)

        if resource.redirect_to is not None:
            target = self.fetch(
                resource.redirect_to, if_none_match=if_none_match,
                robots_disallowed=disallowed,
            )
            return FetchResult(
                outcome=target.outcome,
                http_status=target.http_status,
                body=target.body,
                final_url=normalize_locator(resource.redirect_to),
                declared_content_type=target.declared_content_type,
                content_length=target.content_length,
                etag=target.etag,
                last_modified=target.last_modified,
                # A 304 reached through a redirect is still a 304, and the
                # database requires it to name the validator that proved it.
                validated_by=target.validated_by,
                validator_value=target.validator_value,
                error_class=target.error_class,
                redirected_from=locator,
            )

        if resource.outcome != "OK":
            return FetchResult(
                outcome=resource.outcome,
                http_status=resource.http_status,
                declared_content_type=resource.declared_content_type,
                error_class=resource.error_class,
                error_detail=None,
            )

        body = resource.body
        if body is not None and locator in self._changed and resource.changed_body:
            body = resource.changed_body
        if body is not None and locator in self._cosmetic:
            body = corpus.cosmetic_variant(body)

        if body is None:
            # An OK with no bytes is a corpus bug, not a network condition.
            raise AssertionError(f"fixture {locator} is OK but has no body")

        if len(body) > self.byte_limit or resource.byte_limit_exceeded:
            return FetchResult(outcome="TOO_LARGE", http_status=200,
                               content_length=len(body))

        if if_none_match is not None and resource.etag and if_none_match == resource.etag:
            # 304 returns no bytes. The caller keeps the body it already has;
            # the event records which validator proved it.
            return FetchResult(
                outcome="NOT_MODIFIED", http_status=304, body=None,
                etag=resource.etag, last_modified=resource.last_modified,
                validated_by="ETAG", validator_value=resource.etag,
                declared_content_type=resource.declared_content_type,
                final_url=locator,
            )

        return FetchResult(
            outcome="OK",
            http_status=resource.http_status or 200,
            body=body,
            final_url=locator,
            declared_content_type=resource.declared_content_type,
            content_length=len(body),
            etag=resource.etag,
            last_modified=resource.last_modified,
        )


# ---------------------------------------------------------------------------
# Discovery providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredLocator:
    """A candidate address and how it was found. Candidates are never facts."""

    url: str
    method: str
    context: dict[str, object] | None = None
    discovered_from: str | None = None
    relevance_hint: float | None = None


class FixtureDiscoveryProvider:
    """Deterministic candidate generation for all seven discovery methods."""

    def __init__(self, transport: FixtureTransport | None = None) -> None:
        self.transport = transport or FixtureTransport()

    def human_seeds(self) -> list[DiscoveredLocator]:
        return [
            DiscoveredLocator(url, "HUMAN_SEED", {"operator": "fixture"}, relevance_hint=1.0)
            for url in corpus.HUMAN_SEEDS
        ]

    def sitemap(self, site: str) -> list[DiscoveredLocator]:
        return [
            DiscoveredLocator(url, "SITEMAP", {"sitemap": f"{site}sitemap.xml"},
                              discovered_from=site, relevance_hint=0.6)
            for url in corpus.SITEMAP
        ]

    def crawl_links(self, from_url: str) -> list[DiscoveredLocator]:
        resource = self.transport._web.get(normalize_locator(from_url))
        if resource is None:
            return []
        return [
            DiscoveredLocator(url, "CRAWL_LINK", {"anchor_from": normalize_locator(from_url)},
                              discovered_from=from_url, relevance_hint=0.5)
            for url in resource.links
        ]

    def search(self, query: str) -> list[DiscoveredLocator]:
        return [
            DiscoveredLocator(url, "SEARCH", {"query": query, "rank": rank},
                              relevance_hint=round(1.0 - rank * 0.1, 4))
            for rank, url in enumerate(corpus.SEARCH_RESULTS.get(query, ()))
        ]

    def job_board(self) -> list[DiscoveredLocator]:
        return [
            DiscoveredLocator(url, "JOB_BOARD", {"board": "hvacjobsboard.example"},
                              relevance_hint=0.8)
            for url in corpus.JOB_BOARD_RESULTS
        ]

    def registry(self) -> list[DiscoveredLocator]:
        return [
            DiscoveredLocator(url, "REGISTRY", {"jurisdiction": "OH"}, relevance_hint=0.9)
            for url in corpus.REGISTRY_RESULTS
        ]

    def api(self) -> list[DiscoveredLocator]:
        return [
            DiscoveredLocator(url, "API", {"endpoint": "partner-feed"}, relevance_hint=0.3)
            for url in corpus.API_RESULTS
        ]
