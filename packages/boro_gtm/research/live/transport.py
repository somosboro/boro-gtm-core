"""One real HTTP transport, satisfying the existing M3 acquisition contract.

It returns the same `FetchResult` the fixture transport returns and nothing
else, so `acquisition.record_fetch` is still the only way a retrieval becomes a
row. There is no second acquisition model and no path around
`ResearchFetchEvent` (M3-ADR-071).

Every condition the pipeline already knows how to record is produced here from
a real server: redirects, 404, 410, 403, timeouts, connection failures,
oversized bodies, unreadable media types, conditional GET and 304. What the
fixture transport could only simulate, this one observes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from boro_gtm.research.fixtures.transport import FetchResult
from boro_gtm.research.live.policy import (
    ACCEPTED_MEDIA_TYPES,
    USER_AGENT,
    CrawlBudget,
)
from boro_gtm.research.live.safety import UnsafeTargetError, resolve_safely
from boro_gtm.research.policies import host_of, normalize_locator

#: Status codes that mean "there is a document, but not for you".
_DENIED_STATUSES = frozenset({401, 402, 403, 405, 406, 423, 429, 451})
#: Bodies that are a sign-in page dressed as a success.
_LOGIN_MARKERS = (b"name=\"password\"", b"name='password'", b"type=\"password\"",
                  b"type='password'")


@dataclass
class HostState:
    """Per-host politeness and robots state, fetched once per attempt."""

    robots: RobotFileParser | None = None
    robots_fetched: bool = False
    sitemaps: tuple[str, ...] = ()
    #: None until the first request. Not 0.0: a monotonic clock may legitimately
    #: read 0.0, and testing the timestamp for truthiness skipped the delay.
    last_request_at: float | None = None
    crawl_delay: float | None = None


@dataclass
class TransportStats:
    """What the operator needs to see afterwards, counted as it happens."""

    requests: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)
    bytes_downloaded: int = 0
    refused_unsafe: list[tuple[str, str]] = field(default_factory=list)
    robots_denied: list[str] = field(default_factory=list)


class ProductionWebTransport:
    """HTTPS/HTTP over httpx, with the safety rules applied at every hop.

    Redirects are followed **manually**. `follow_redirects=True` would let the
    client chase a `Location` this process never inspected, which is precisely
    how a public host becomes a request to `169.254.169.254`.
    """

    def __init__(
        self,
        *,
        budget: CrawlBudget | None = None,
        client: httpx.Client | None = None,
        resolver=None,
        allow_private: bool = False,
        respect_robots: bool = True,
        sleeper=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.budget = budget or CrawlBudget()
        self.respect_robots = respect_robots
        self.stats = TransportStats()
        self._hosts: dict[str, HostState] = {}
        self._resolver = resolver
        self._allow_private = allow_private
        self._sleep = sleeper
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self.budget.timeout_seconds),
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/pdf,"
                          "text/plain,application/json;q=0.8,*/*;q=0.1",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ProductionWebTransport:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- robots ------------------------------------------------------------

    def host_state(self, url: str) -> HostState:
        host = host_of(normalize_locator(url))
        state = self._hosts.get(host)
        if state is None:
            state = HostState()
            self._hosts[host] = state
        if self.respect_robots and not state.robots_fetched:
            self._load_robots(url, state)
        return state

    def _load_robots(self, url: str, state: HostState) -> None:
        """Fetch `/robots.txt` once per host.

        A missing or unreadable `robots.txt` means *no stated preference*, which
        is permission by convention — not a reason to stop, and not a reason to
        assume everything is allowed if the server said 403 to robots itself.
        """
        state.robots_fetched = True
        parts = urlsplit(normalize_locator(url))
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser = RobotFileParser()
        try:
            target = resolve_safely(robots_url, resolver=self._resolver,
                                    allow_private=self._allow_private)
            self._wait(state)
            response = self._client.get(target.url)
        except (UnsafeTargetError, httpx.HTTPError, OSError):
            parser.parse([])                    # no stated preference
            state.robots = parser
            return
        if response.status_code >= 400:
            parser.parse([])
            state.robots = parser
            return
        text = response.text
        parser.parse(text.splitlines())
        state.robots = parser
        state.sitemaps = tuple(
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.lower().startswith("sitemap:") and ":" in line
        )
        delay = parser.crawl_delay(USER_AGENT)
        if delay is not None:
            state.crawl_delay = float(delay)

    def robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        state = self.host_state(url)
        if state.robots is None:                        # pragma: no cover
            return True
        return state.robots.can_fetch(USER_AGENT, url)

    def sitemaps_for(self, url: str) -> tuple[str, ...]:
        return self.host_state(url).sitemaps

    # -- politeness --------------------------------------------------------

    def _wait(self, state: HostState) -> None:
        delay = max(self.budget.delay_seconds, state.crawl_delay or 0.0)
        if state.last_request_at is not None:
            elapsed = self._clock() - state.last_request_at
            if elapsed < delay:
                self._sleep(delay - elapsed)
        state.last_request_at = self._clock()

    # -- retrieval ---------------------------------------------------------

    def fetch(
        self, url: str, *, if_none_match: str | None = None,
        robots_disallowed: frozenset[str] | None = None,
    ) -> FetchResult:
        """One retrieval of a **document**, in the vocabulary the event stores.

        Signature matches the fixture transport exactly, including the unused
        `robots_disallowed` — the pipeline passes nothing, and a real crawler
        reads the site's own `robots.txt` rather than a caller's list.
        """
        return self._fetch(url, if_none_match=if_none_match,
                           accepted=ACCEPTED_MEDIA_TYPES, counts_as_retrieval=True)

    def fetch_auxiliary(self, url: str) -> FetchResult:
        """A sitemap or similar: read to find documents, never a document itself.

        Two differences from `fetch`, both of which were bugs when this was one
        method. A sitemap is `application/xml`, which is not a media type the
        extraction pipeline reads, so the document gate correctly refused it and
        sitemap discovery silently found nothing. And a sitemap is not a page we
        researched, so counting it against the crawl budget would spend the
        operator's page allowance on plumbing.
        """
        return self._fetch(url, if_none_match=None, accepted=None,
                           counts_as_retrieval=False)

    def _fetch(
        self, url: str, *, if_none_match: str | None,
        accepted: frozenset[str] | None, counts_as_retrieval: bool,
    ) -> FetchResult:
        locator = normalize_locator(url)
        if counts_as_retrieval:
            self.stats.requests.append(locator)

        if self.respect_robots and not self.robots_allows(locator):
            self.stats.robots_denied.append(locator)
            return self._record(FetchResult(
                outcome="ROBOTS_DENIED", error_class="RobotsDisallowed",
                error_detail="disallowed by the site's robots.txt",
                final_url=locator,
            ))

        current = locator
        redirected_from: str | None = None
        for hop in range(self.budget.max_redirects + 1):
            try:
                target = resolve_safely(current, resolver=self._resolver,
                                        allow_private=self._allow_private)
            except UnsafeTargetError as exc:
                self.stats.refused_unsafe.append((current, exc.reason))
                return self._record(FetchResult(
                    outcome="DENIED", error_class="UnsafeTarget",
                    error_detail=exc.reason, final_url=current,
                    redirected_from=redirected_from,
                ))

            state = self.host_state(current)
            if self.respect_robots and redirected_from and not self.robots_allows(current):
                # A redirect crossing into a disallowed path is still disallowed.
                self.stats.robots_denied.append(current)
                return self._record(FetchResult(
                    outcome="ROBOTS_DENIED", error_class="RobotsDisallowed",
                    error_detail="redirect target disallowed by robots.txt",
                    final_url=current, redirected_from=redirected_from,
                ))

            headers = {}
            if if_none_match and hop == 0:
                headers["If-None-Match"] = if_none_match

            self._wait(state)
            try:
                response = self._client.get(target.url, headers=headers)
            except httpx.TimeoutException as exc:
                return self._record(FetchResult(
                    outcome="TIMEOUT", error_class=type(exc).__name__,
                    error_detail=str(exc)[:500], final_url=current,
                    redirected_from=redirected_from,
                ))
            except (httpx.HTTPError, OSError) as exc:
                return self._record(FetchResult(
                    outcome="TRANSPORT_ERROR", error_class=type(exc).__name__,
                    error_detail=str(exc)[:500], final_url=current,
                    redirected_from=redirected_from,
                ))

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return self._record(FetchResult(
                        outcome="TRANSPORT_ERROR", http_status=response.status_code,
                        error_class="RedirectWithoutLocation", final_url=current,
                        redirected_from=redirected_from,
                    ))
                if redirected_from is None:
                    redirected_from = locator
                current = normalize_locator(urljoin(current, location))
                continue

            return self._record(self._interpret(
                response, requested=current, redirected_from=redirected_from,
                accepted=accepted,
            ))

        return self._record(FetchResult(
            outcome="TRANSPORT_ERROR", error_class="TooManyRedirects",
            error_detail=f"exceeded {self.budget.max_redirects} redirects",
            final_url=current, redirected_from=redirected_from,
        ))

    # -- response interpretation -------------------------------------------

    def _interpret(
        self, response: httpx.Response, *, requested: str,
        redirected_from: str | None, accepted: frozenset[str] | None,
    ) -> FetchResult:
        status = response.status_code
        declared = response.headers.get("content-type")
        media_type = (declared or "").split(";")[0].strip().lower()
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        common = {
            "http_status": status, "declared_content_type": declared,
            "final_url": requested, "redirected_from": redirected_from,
            "etag": etag, "last_modified": last_modified,
        }

        if status == 304:
            # No bytes. The event must name the validator that proved it, and
            # `If-None-Match` is the only one this transport sends.
            return FetchResult(
                outcome="NOT_MODIFIED", body=None, validated_by="ETAG",
                validator_value=etag or response.request.headers.get("If-None-Match"),
                **common,
            )
        if status == 404:
            return FetchResult(outcome="NOT_FOUND", **common)
        if status == 410:
            return FetchResult(outcome="GONE", **common)
        if status in _DENIED_STATUSES:
            return FetchResult(
                outcome="DENIED", error_class=f"HTTP{status}",
                error_detail=response.reason_phrase or None, **common,
            )
        if status >= 400:
            return FetchResult(
                outcome="TRANSPORT_ERROR", error_class=f"HTTP{status}",
                error_detail=response.reason_phrase or None, **common,
            )

        body = response.content
        declared_length = response.headers.get("content-length")
        length = int(declared_length) if (declared_length or "").isdigit() else len(body)

        if len(body) > self.budget.max_bytes or length > self.budget.max_bytes:
            # The bytes are discarded rather than stored: the point of the limit
            # is not to keep them.
            return FetchResult(
                outcome="TOO_LARGE", content_length=max(length, len(body)),
                error_class="ContentTooLarge",
                error_detail=f"{max(length, len(body))} bytes exceeds "
                             f"{self.budget.max_bytes}",
                **common,
            )
        if accepted is not None and media_type and media_type not in accepted:
            return FetchResult(
                outcome="MIME_MISMATCH", content_length=len(body),
                error_class="UnsupportedMediaType", error_detail=media_type,
                **common,
            )
        if (accepted is not None
                and media_type in ("text/html", "application/xhtml+xml")
                and _looks_like_login(body)):
            # Recorded as what it is. Signing in would be bypassing access
            # control, which this fetcher does not do.
            return FetchResult(
                outcome="LOGIN_WALL", content_length=len(body),
                error_class="AuthenticationRequired", **common,
            )

        self.stats.bytes_downloaded += len(body)
        return FetchResult(outcome="OK", body=body, content_length=len(body), **common)

    def _record(self, result: FetchResult) -> FetchResult:
        self.stats.outcomes[result.outcome] = self.stats.outcomes.get(result.outcome, 0) + 1
        return result


def _looks_like_login(body: bytes) -> bool:
    """A password field and almost nothing else.

    Deliberately narrow: a contractor's careers page can carry a login link in
    its footer, and calling that a login wall would throw away real evidence.
    """
    if not any(marker in body.lower() for marker in _LOGIN_MARKERS):
        return False
    return len(body) < 40_000
