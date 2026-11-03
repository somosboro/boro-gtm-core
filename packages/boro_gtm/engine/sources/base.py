"""What every source adapter returns, and the fetcher they share.

Adapters are pure translators: given a cursor and a fetcher, they return what
they saw, where they stopped, and whether there is anything left. They never
write rows; `sourcing.run_source` does, so dedupe, expiry and exhaustion are
recorded the same way for every source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from boro_gtm.research.fixtures.transport import FetchResult

ENGINE_USER_AGENT = ("BoRoGTMEngine/1.0 (+https://somosboro.com/bot; B2B partner and project "
                     "discovery; contact abourgeoisroa@somosboro.com)")


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


@dataclass
class SignalDraft:
    external_id: str
    kind: str
    lane: str
    url: str | None
    title: str | None
    excerpt: str | None
    company_name: str | None
    company_domain: str | None
    country: str | None
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Set by the adapter when it already knows this item is not a target, so
    #: the run can count *why* it was rejected instead of silently dropping it.
    reject_reason: str | None = None


@dataclass
class SourcePage:
    items: list[SignalDraft]
    query: dict[str, Any]
    cursor_after: dict[str, Any]
    exhausted: bool
    pages_fetched: int = 1
    error: str | None = None


class SourceAdapter(Protocol):
    key: str
    lane: str
    family: str

    def next_page(self, fetcher: Fetcher, cursor: dict[str, Any]) -> SourcePage: ...


class TransportFetcher:
    """The M3 production transport: SSRF-checked at every hop, robots-aware, polite."""

    def __init__(self, delay_seconds: float = 2.0, max_retrievals: int = 400) -> None:
        import httpx

        from boro_gtm.research.live.policy import CrawlBudget
        from boro_gtm.research.live.transport import ProductionWebTransport

        budget = CrawlBudget(max_pages=max_retrievals, max_retrievals=max_retrievals,
                             delay_seconds=delay_seconds)
        client = httpx.Client(timeout=httpx.Timeout(budget.timeout_seconds),
                              follow_redirects=False,
                              headers={"User-Agent": ENGINE_USER_AGENT,
                                       "Accept": "text/html,application/json;q=0.9,*/*;q=0.1"})
        self._transport = ProductionWebTransport(budget=budget, client=client)

    def get(self, url: str) -> FetchResult:
        return self._transport.fetch_auxiliary(url)

    def close(self) -> None:
        self._transport.close()
