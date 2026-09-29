"""The production transport, against a mocked HTTP layer. No network.

Every condition the fetch event vocabulary can record is driven here, because a
transport whose error paths have never run is a transport that will meet them
for the first time against a real customer's website.

`httpx.MockTransport` is used rather than monkeypatching the client, so the
request objects, header handling and redirect statuses are the library's real
ones and only the socket is replaced.
"""

from __future__ import annotations

import httpx
import pytest

from boro_gtm.research.live.policy import USER_AGENT, CrawlBudget
from boro_gtm.research.live.transport import ProductionWebTransport

HOST = "bigmechanical.com"
BASE = f"https://{HOST}"


def _resolver(mapping):
    def resolve(host: str, port: int) -> list[str]:
        try:
            return mapping[host]
        except KeyError:
            raise OSError(f"no such host {host}") from None
    return resolve


def _transport(handler, *, budget=None, respect_robots=False, resolver=None,
               mapping=None) -> ProductionWebTransport:
    """A transport whose socket is a function. Sleeps are a no-op.

    `mapping` is passed through as-is, including `{}`: an empty mapping means
    "nothing resolves", which is a case worth testing.
    """
    return ProductionWebTransport(
        budget=budget or CrawlBudget(delay_seconds=0.0),
        client=httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        ),
        resolver=resolver or _resolver(mapping if mapping is not None
                                      else {HOST: ["93.184.216.34"]}),
        respect_robots=respect_robots,
        sleeper=lambda _seconds: None,
    )


def _html(body: str = "<html><body><p>Commercial service</p></body></html>",
          **headers) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(),
        headers={"content-type": "text/html; charset=utf-8", **headers},
    )


# --- the happy path ---------------------------------------------------------


def test_a_successful_fetch_reports_what_the_event_stores():
    def handler(request):
        assert request.headers["user-agent"] == USER_AGENT
        return _html(etag='W/"abc"', **{"last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"})

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/services/commercial")

    assert result.outcome == "OK"
    assert result.http_status == 200
    assert b"Commercial service" in result.body
    assert result.declared_content_type.startswith("text/html")
    assert result.content_length == len(result.body)
    assert result.etag == 'W/"abc"'
    assert result.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert result.final_url == f"{BASE}/services/commercial"
    assert result.error_class is None


def test_an_honest_user_agent_identifies_us_and_says_how_to_reach_us():
    assert "BoRoGTMResearchBot" in USER_AGENT
    assert "+https://" in USER_AGENT
    assert "@" in USER_AGENT


# --- conditional GET --------------------------------------------------------


def test_a_conditional_request_sends_the_validator_and_304_carries_no_bytes():
    seen = {}

    def handler(request):
        seen["inm"] = request.headers.get("if-none-match")
        return httpx.Response(304, headers={"etag": 'W/"abc"'})

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/about", if_none_match='W/"abc"')

    assert seen["inm"] == 'W/"abc"'
    assert result.outcome == "NOT_MODIFIED"
    assert result.body is None
    # The event requires a named validator; ETAG is the only one we send.
    assert result.validated_by == "ETAG"
    assert result.validator_value == 'W/"abc"'


def test_a_validator_is_not_replayed_across_a_redirect():
    """`If-None-Match` belongs to the document we asked for, not its successor."""
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": f"{BASE}/new"})
        return _html()

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/old", if_none_match='W/"abc"')

    assert result.outcome == "OK"
    assert seen == ['W/"abc"', None]


# --- status handling --------------------------------------------------------


@pytest.mark.parametrize(("status", "outcome"), [
    (404, "NOT_FOUND"),
    (410, "GONE"),
    (401, "DENIED"),
    (403, "DENIED"),
    (429, "DENIED"),
    (451, "DENIED"),
    (500, "TRANSPORT_ERROR"),
    (503, "TRANSPORT_ERROR"),
])
def test_statuses_map_to_the_existing_outcome_vocabulary(status, outcome):
    with _transport(lambda request: httpx.Response(status)) as transport:
        result = transport.fetch(f"{BASE}/services")
    assert result.outcome == outcome
    assert result.http_status == status
    assert result.body is None


def test_a_denied_source_is_recorded_not_worked_around():
    """403 is an answer. The next move is a gap, not another attempt.

    Asserted at the transport because this is where the temptation lives: a
    retry with a different User-Agent would be evasion.
    """
    calls = []

    def handler(request):
        calls.append(request.headers.get("user-agent"))
        return httpx.Response(403)

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/services")

    assert result.outcome == "DENIED"
    assert result.error_class == "HTTP403"
    assert len(calls) == 1, "a denial is not retried"
    assert set(calls) == {USER_AGENT}, "and never under a different identity"


# --- redirects --------------------------------------------------------------


def test_redirects_are_followed_and_the_final_url_is_recorded():
    def handler(request):
        if request.url.path == "/service":
            return httpx.Response(301, headers={"location": "/services/"})
        if request.url.path == "/services":
            return httpx.Response(302, headers={"location": f"{BASE}/services/commercial"})
        return _html()

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/service")

    assert result.outcome == "OK"
    assert result.final_url == f"{BASE}/services/commercial"
    assert result.redirected_from == f"{BASE}/service"


def test_a_redirect_chain_is_bounded():
    def handler(request):
        n = int(request.url.path.rsplit("/", 1)[-1] or 0)
        return httpx.Response(302, headers={"location": f"{BASE}/hop/{n + 1}"})

    budget = CrawlBudget(delay_seconds=0.0, max_redirects=3)
    with _transport(handler, budget=budget) as transport:
        result = transport.fetch(f"{BASE}/hop/0")

    assert result.outcome == "TRANSPORT_ERROR"
    assert result.error_class == "TooManyRedirects"
    assert len(transport.stats.requests) == 1


def test_a_redirect_to_a_private_address_is_refused_at_the_new_hop():
    """The defence has to be per hop.

    A public page redirecting to `169.254.169.254` is the textbook SSRF, and a
    client with `follow_redirects=True` would have made that request before this
    process ever saw the Location header.
    """
    def handler(request):
        if request.url.host == HOST:
            return httpx.Response(302, headers={"location": "http://metadata.internal/latest/"})
        return _html("secrets")                         # pragma: no cover

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/about")

    assert result.outcome == "DENIED"
    assert result.error_class == "UnsafeTarget"
    assert transport.stats.refused_unsafe
    assert "metadata.internal" in transport.stats.refused_unsafe[0][0]


def test_a_redirect_to_a_public_host_that_resolves_privately_is_refused():
    mapping = {HOST: ["93.184.216.34"], "cdn.othersite.com": ["10.1.2.3"]}

    def handler(request):
        if request.url.host == HOST:
            return httpx.Response(301, headers={"location": "https://cdn.othersite.com/x"})
        return _html("secrets")                         # pragma: no cover

    with _transport(handler, mapping=mapping) as transport:
        result = transport.fetch(f"{BASE}/about")

    assert result.outcome == "DENIED"
    assert "private address" in transport.stats.refused_unsafe[0][1]


def test_a_redirect_without_a_location_is_a_transport_error():
    with _transport(lambda r: httpx.Response(302)) as transport:
        result = transport.fetch(f"{BASE}/about")
    assert result.outcome == "TRANSPORT_ERROR"
    assert result.error_class == "RedirectWithoutLocation"


# --- failures ---------------------------------------------------------------


def test_a_timeout_is_a_timeout_and_not_a_generic_error():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/services")

    assert result.outcome == "TIMEOUT"
    assert result.error_class == "ReadTimeout"
    assert result.error_detail


def test_a_connection_failure_is_a_transport_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/services")

    assert result.outcome == "TRANSPORT_ERROR"
    assert result.error_class == "ConnectError"


def test_an_unresolvable_host_is_denied_before_any_request():
    calls = []

    def handler(request):                               # pragma: no cover
        calls.append(request)
        return _html()

    with _transport(handler, mapping={}) as transport:
        result = transport.fetch(f"{BASE}/services")

    assert result.outcome == "DENIED"
    assert result.error_class == "UnsafeTarget"
    assert calls == []


# --- content limits --------------------------------------------------------


def test_a_document_over_the_byte_limit_reports_too_large_and_keeps_nothing():
    budget = CrawlBudget(delay_seconds=0.0, max_bytes=1000)

    def handler(request):
        return _html("x" * 5000)

    with _transport(handler, budget=budget) as transport:
        result = transport.fetch(f"{BASE}/services")

    assert result.outcome == "TOO_LARGE"
    assert result.body is None, "the point of the limit is not to keep the bytes"
    assert result.content_length > 1000
    assert transport.stats.bytes_downloaded == 0


def test_a_declared_length_over_the_limit_is_refused_too():
    budget = CrawlBudget(delay_seconds=0.0, max_bytes=1000)

    def handler(request):
        return httpx.Response(200, content=b"small",
                              headers={"content-type": "text/html",
                                       "content-length": "9999999"})

    with _transport(handler, budget=budget) as transport:
        result = transport.fetch(f"{BASE}/services")
    assert result.outcome == "TOO_LARGE"


@pytest.mark.parametrize("media_type", [
    "image/jpeg", "video/mp4", "font/woff2", "text/css",
    "application/javascript", "application/zip", "application/msword",
])
def test_a_media_type_the_pipeline_cannot_read_is_a_mime_mismatch(media_type):
    """Recorded as a retrieval outcome, so the attribute becomes an honest gap.

    Silently skipping it would look identical to the page not existing.
    """
    def handler(request):
        return httpx.Response(200, content=b"\x00\x01\x02",
                              headers={"content-type": media_type})

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/brochure")

    assert result.outcome == "MIME_MISMATCH"
    assert result.error_detail == media_type
    assert result.body is None


@pytest.mark.parametrize("media_type", [
    "text/html", "application/pdf", "text/plain", "application/json",
])
def test_the_media_types_the_pipeline_reads_are_accepted(media_type):
    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4 x",
                              headers={"content-type": f"{media_type}; charset=utf-8"})

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/brochure")
    assert result.outcome == "OK"


def test_a_sign_in_page_is_a_login_wall_not_a_document():
    def handler(request):
        return _html('<form><input type="password" name="password"></form>')

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/careers/apply")

    assert result.outcome == "LOGIN_WALL"
    assert result.error_class == "AuthenticationRequired"


def test_a_long_page_with_a_footer_login_link_is_still_a_document():
    """Narrow on purpose: a careers page is evidence even with a login in it."""
    body = "<p>Commercial preventive maintenance</p>" * 2000
    def handler(request):
        return _html(body + '<input type="password">')

    with _transport(handler) as transport:
        result = transport.fetch(f"{BASE}/careers")
    assert result.outcome == "OK"


# --- robots -----------------------------------------------------------------


def _robots(rules: str):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=rules.encode(),
                                  headers={"content-type": "text/plain"})
        return _html()
    return handler


def test_a_disallowed_path_is_not_fetched():
    handler = _robots("User-agent: *\nDisallow: /private/\n")
    with _transport(handler, respect_robots=True) as transport:
        result = transport.fetch(f"{BASE}/private/rates")

    assert result.outcome == "ROBOTS_DENIED"
    assert result.error_class == "RobotsDisallowed"
    assert f"{BASE}/private/rates" in transport.stats.robots_denied
    assert not any(r.endswith("/private/rates")
                   for r in [str(x) for x in transport.stats.requests[1:]])


def test_an_allowed_path_is_fetched_under_the_same_robots_file():
    handler = _robots("User-agent: *\nDisallow: /private/\n")
    with _transport(handler, respect_robots=True) as transport:
        assert transport.fetch(f"{BASE}/services").outcome == "OK"


def test_a_blanket_disallow_stops_everything():
    handler = _robots("User-agent: *\nDisallow: /\n")
    with _transport(handler, respect_robots=True) as transport:
        assert transport.fetch(f"{BASE}/services").outcome == "ROBOTS_DENIED"


def test_a_rule_naming_our_agent_specifically_is_obeyed():
    handler = _robots(f"User-agent: {USER_AGENT.split('/')[0]}\nDisallow: /\n\n"
                      "User-agent: *\nAllow: /\n")
    with _transport(handler, respect_robots=True) as transport:
        assert transport.fetch(f"{BASE}/services").outcome == "ROBOTS_DENIED"


def test_a_missing_robots_file_is_no_stated_preference():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return _html()

    with _transport(handler, respect_robots=True) as transport:
        assert transport.fetch(f"{BASE}/services").outcome == "OK"


def test_robots_is_fetched_once_per_host():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nAllow: /\n")
        return _html()

    with _transport(handler, respect_robots=True) as transport:
        for path in ("/", "/services", "/about"):
            transport.fetch(f"{BASE}{path}")

    assert sum(1 for r in transport.stats.requests if r.endswith("robots.txt")) == 0, (
        "the robots fetch is not a research retrieval and is not counted as one"
    )


def test_declared_sitemaps_are_read_from_robots():
    handler = _robots("User-agent: *\nAllow: /\n"
                      f"Sitemap: {BASE}/sitemap_index.xml\n"
                      f"Sitemap: {BASE}/news-sitemap.xml\n")
    with _transport(handler, respect_robots=True) as transport:
        assert transport.sitemaps_for(BASE) == (
            f"{BASE}/sitemap_index.xml", f"{BASE}/news-sitemap.xml",
        )


def test_a_redirect_into_a_disallowed_path_is_refused():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /portal/\n")
        if request.url.path == "/services":
            return httpx.Response(302, headers={"location": f"{BASE}/portal/x"})
        return _html("secrets")                         # pragma: no cover

    with _transport(handler, respect_robots=True) as transport:
        result = transport.fetch(f"{BASE}/services")

    assert result.outcome == "ROBOTS_DENIED"
    assert "redirect target" in result.error_detail


def test_a_crawl_delay_is_honoured_when_the_site_asks_for_one():
    slept: list[float] = []
    handler = _robots("User-agent: *\nCrawl-delay: 7\nAllow: /\n")
    transport = ProductionWebTransport(
        budget=CrawlBudget(delay_seconds=1.0),
        client=httpx.Client(transport=httpx.MockTransport(handler),
                            follow_redirects=False,
                            headers={"User-Agent": USER_AGENT}),
        resolver=_resolver({HOST: ["93.184.216.34"]}), respect_robots=True,
        sleeper=slept.append, clock=lambda: 0.0,
    )
    try:
        transport.fetch(f"{BASE}/services")
        transport.fetch(f"{BASE}/about")
    finally:
        transport.close()

    assert slept, "the second request waited"
    assert max(slept) == pytest.approx(7.0), "the site's delay wins over ours"
