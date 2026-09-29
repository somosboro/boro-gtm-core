"""The live research path, driven end to end against a mocked website.

Real HTTP code, real pipeline, real database, real provenance — only the socket
is a function. That is the boundary worth drawing: CI must never depend on a
contractor's website being up, and everything above the socket is exactly what
runs against BoRo's real cohort.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import (
    Company,
    CompanyClaim,
    CompanyDomain,
    CompanyName,
)
from boro_gtm.research.domain import models as m
from boro_gtm.research.live.discovery import (
    BoRoFirstPartyDiscoveryProvider,
    NoResearchableDomainError,
    domain_scope,
)
from boro_gtm.research.live.policy import USER_AGENT, CrawlBudget
from boro_gtm.research.live.runner import research_company_live
from boro_gtm.research.live.transport import ProductionWebTransport
from boro_gtm.research.seeds import seed_all
from boro_gtm.research.services import review
from boro_gtm.research.services.pipeline import run_pipeline

pytestmark = pytest.mark.integration

HOST = "bigmechanical.com"
BASE = f"https://{HOST}"
OTHER = "https://alliedmechanical.com"

#: A plausible U.S. commercial mechanical contractor site. The prose carries the
#: same shapes the M3 extractors already read, because the point of this test is
#: the live *path*, not new extraction rules.
PAGES = {
    "/": """<html><head><title>Big Mechanical | Commercial HVAC</title></head>
      <body><h1>Big Mechanical</h1>
      <p>Commercial HVAC and mechanical contracting since 1994.</p>
      <a href="/services/commercial">Commercial Services</a>
      <a href="/about">About Us</a>
      <a href="/locations">Locations</a>
      <a href="/careers">Careers</a>
      <a href="/blog/5-tips">Blog</a>
      <a href="/privacy">Privacy</a>
      <a href="https://www.facebook.com/bigmech">Facebook</a>
      <a href="https://alliedmechanical.com/partners">A different company</a>
      </body></html>""",
    "/services/commercial": """<html><body>
      <h2>Commercial Services</h2>
      <p>We employ 58 field technicians and operate a fleet of 22 service vans.</p>
      <p>Preventive maintenance agreements are available for commercial clients.</p>
      <p>We provide 24/7 emergency service.</p>
      <a href="/services/commercial/chillers">Chillers</a>
      </body></html>""",
    "/services/commercial/chillers": """<html><body>
      <h2>Chiller Service</h2><p>Centrifugal and screw chiller maintenance.</p>
      </body></html>""",
    "/about": """<html><body><h2>About Big Mechanical</h2>
      <p>We employ 58 field technicians across 3 branches.</p>
      <p>Dispatch is coordinated from our Columbus office.</p>
      </body></html>""",
    "/locations": """<html><body><h2>Locations</h2>
      <p>3 branches serving central Ohio.</p></body></html>""",
    "/careers": """<html><body><h2>Careers</h2>
      <p>We are hiring HVAC service technicians. Experience with ServiceTitan
      dispatch software preferred.</p></body></html>""",
    "/blog/5-tips": "<html><body><p>Five tips</p></body></html>",
    "/privacy": "<html><body><p>Privacy policy</p></body></html>",
}

SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BASE}/</loc></url>
  <url><loc>{BASE}/services/commercial</loc></url>
  <url><loc>{BASE}/about</loc></url>
  <url><loc>{BASE}/locations</loc></url>
  <url><loc>{BASE}/careers</loc></url>
  <url><loc>{BASE}/blog/5-tips</loc></url>
  <url><loc>{OTHER}/partners</loc></url>
</urlset>"""

ROBOTS = f"User-agent: *\nDisallow: /private/\nSitemap: {BASE}/sitemap.xml\n"


def _site_handler(record: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(str(request.url))
        host = request.url.host
        path = request.url.path
        if host != HOST:
            # Any other host in this test is a different company's site. If a
            # request reaches it, domain scoping failed.
            return httpx.Response(
                200, content=b"<html><body><p>We employ 900 technicians.</p></body></html>",
                headers={"content-type": "text/html"},
            )
        if path == "/robots.txt":
            return httpx.Response(200, content=ROBOTS.encode(),
                                  headers={"content-type": "text/plain"})
        if path == "/sitemap.xml":
            return httpx.Response(200, content=SITEMAP.encode(),
                                  headers={"content-type": "application/xml"})
        body = PAGES.get(path.rstrip("/") or "/")
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode(),
                              headers={"content-type": "text/html; charset=utf-8",
                                       "etag": f'W/"{abs(hash(path)) % 10**8}"'})
    return handler


def _resolver(host: str, port: int) -> list[str]:
    """Everything resolves to one globally-routable address."""
    return ["93.184.216.34"]


def _transport(handler=None, *, budget=None, record=None) -> ProductionWebTransport:
    return ProductionWebTransport(
        budget=budget or CrawlBudget(delay_seconds=0.0),
        client=httpx.Client(
            transport=httpx.MockTransport(handler or _site_handler(record)),
            follow_redirects=False, headers={"User-Agent": USER_AGENT},
        ),
        resolver=_resolver, respect_robots=True, sleeper=lambda _s: None,
    )


def _company(session, *, name: str, domains: tuple[tuple[str, str], ...]) -> Company:
    company = Company(created_at=None, identity_policy_version="1.0",
                      lifecycle_status="ACTIVE")
    from datetime import UTC, datetime
    company.created_at = datetime(2026, 9, 29, tzinfo=UTC)
    session.add(company)
    session.flush()
    session.add(CompanyName(
        company_id=company.id, name_normalized=name.lower(), name_type="LEGAL",
        name_raw=name, is_primary=True, derived_from_claim_ids=[],
    ))
    for domain, role in domains:
        session.add(CompanyDomain(
            company_id=company.id, domain_normalized=domain, domain_role=role,
            derived_from_claim_ids=[],
        ))
    session.flush()
    return company


@pytest.fixture
def researchable(session):
    seed_all(session)
    session.flush()
    return _company(session, name="Big Mechanical Inc",
                    domains=((HOST, "IDENTITY"), ("bigmech.net", "ALTERNATE")))


# --- §3: scope comes from M2, never from the company name -------------------


def test_scope_is_the_identity_domain_and_the_alternates_m2_accepted(researchable, session):
    scope = domain_scope(session, researchable.id)
    assert scope.primary == f"{BASE}/"
    assert set(scope.registrable) == {HOST, "bigmech.net"}
    assert scope.allows(f"{BASE}/services")
    assert scope.allows("https://www.bigmech.net/about"), "a subdomain is in scope"
    assert not scope.allows(f"{OTHER}/partners")


def test_a_group_domain_is_never_researched_as_the_company(session):
    """A shared host is recorded by M2 precisely because it is not a company.

    Two contractors on one franchise portal would otherwise both be researched
    as whoever the portal's root happens to describe.
    """
    seed_all(session)
    session.flush()
    company = _company(session, name="Small Mechanical",
                       domains=(("bigfranchise.com", "GROUP"),))
    with pytest.raises(NoResearchableDomainError) as caught:
        domain_scope(session, company.id)
    assert "bigfranchise.com" in str(caught.value)


def test_a_blocklisted_platform_host_cannot_be_crawled_even_as_identity(session):
    """M2's own blocklist is re-applied here.

    A row claiming `IDENTITY` for `wixsite.com` would be an M2 defect; research
    refuses it anyway rather than trusting the role alone.
    """
    seed_all(session)
    session.flush()
    company = _company(session, name="Tiny HVAC",
                       domains=(("tinyhvac.wixsite.com", "IDENTITY"),))
    with pytest.raises(NoResearchableDomainError):
        domain_scope(session, company.id)


def test_a_defunct_domain_is_out_of_scope(session):
    seed_all(session)
    session.flush()
    company = _company(session, name="Gone Mechanical",
                       domains=(("gonemech.com", "DEFUNCT"),))
    with pytest.raises(NoResearchableDomainError):
        domain_scope(session, company.id)


def test_a_company_with_no_domain_is_reported_not_guessed(session):
    """The company's *name* is never turned into a website."""
    seed_all(session)
    session.flush()
    company = _company(session, name="Allied Mechanical", domains=())
    report = research_company_live(session, company_id=company.id)
    assert report.status == "NO_RESEARCHABLE_DOMAIN"
    assert report.pages_attempted == 0
    assert report.claims_created == 0
    assert "Allied" not in (report.primary_url or "")


# --- §7 / §8: discovery stays inside scope and skips low-value pages ---------


def test_discovery_stays_on_the_companys_own_domains(researchable, session):
    scope = domain_scope(session, researchable.id)
    with _transport() as transport:
        provider = BoRoFirstPartyDiscoveryProvider(transport, scope=scope)
        plan = provider.plan()

    urls = {candidate.url for candidate, _ in plan}
    assert urls, "the plan found pages"
    for url in urls:
        assert scope.allows(url), f"{url} is not this company's domain"
    assert not any(OTHER in url for url in urls)
    assert f"{OTHER}/partners" in set(provider.rejected_out_of_scope), (
        "the other company's page was seen in the sitemap and refused"
    )


def test_discovery_skips_the_blog_and_the_privacy_policy(researchable, session):
    scope = domain_scope(session, researchable.id)
    with _transport() as transport:
        provider = BoRoFirstPartyDiscoveryProvider(transport, scope=scope)
        urls = {c.url for c, _ in provider.plan()}
    assert not any("/blog/" in u for u in urls)
    assert not any("/privacy" in u for u in urls)
    assert not any("facebook.com" in u for u in urls)
    assert f"{BASE}/services/commercial" in urls
    assert f"{BASE}/about" in urls


def test_the_sitemap_robots_declares_is_read(researchable, session):
    scope = domain_scope(session, researchable.id)
    with _transport() as transport:
        assert transport.sitemaps_for(f"{BASE}/") == (f"{BASE}/sitemap.xml",)
        provider = BoRoFirstPartyDiscoveryProvider(transport, scope=scope)
        methods = {c.method for c, _ in provider.plan()}
    assert "SITEMAP" in methods
    assert "HUMAN_SEED" in methods, "the M2 identity address"
    assert "CRAWL_LINK" in methods


def test_search_job_board_registry_and_api_are_deliberately_empty(researchable, session):
    """v1 is first-party only. Returning nothing is the decision, not a stub."""
    scope = domain_scope(session, researchable.id)
    with _transport() as transport:
        provider = BoRoFirstPartyDiscoveryProvider(transport, scope=scope)
        assert provider.search("commercial hvac columbus") == []
        assert provider.job_board() == []
        assert provider.registry() == []
        assert provider.api() == []


def test_an_operator_seed_outside_scope_is_refused(researchable, session):
    """Even a human's explicit URL is checked against M2's scope."""
    scope = domain_scope(session, researchable.id)
    with _transport() as transport:
        provider = BoRoFirstPartyDiscoveryProvider(
            transport, scope=scope,
            human_seed_urls=(f"{OTHER}/about", f"{BASE}/services/commercial"),
        )
        seeds = {c.url for c in provider.human_seeds()}
    assert f"{BASE}/services/commercial" in seeds
    assert not any(OTHER in u for u in seeds)


# --- §9: bounded crawl ------------------------------------------------------


def test_a_retrieval_budget_stops_the_run_and_says_so(researchable, session):
    """"We stopped looking" must not read as "there was nothing there"."""
    budget = CrawlBudget(delay_seconds=0.0, max_retrievals=3, max_depth=2)
    with _transport(budget=budget) as transport:
        report = research_company_live(
            session, company_id=researchable.id, budget=budget, transport=transport,
        )
    assert report.budget_stopped_at == "MAX_RETRIEVALS"
    assert report.unretrieved_sources > 0
    assert report.pages_fetched <= 3


def test_depth_is_bounded(researchable, session):
    """Two hops reaches /services/commercial/chillers and stops."""
    scope = domain_scope(session, researchable.id)
    budget = CrawlBudget(delay_seconds=0.0, max_depth=1)
    with _transport(budget=budget) as transport:
        shallow = {c.url for c, _ in BoRoFirstPartyDiscoveryProvider(
            transport, scope=scope, budget=budget).plan()}
    deep_budget = CrawlBudget(delay_seconds=0.0, max_depth=2)
    with _transport(budget=deep_budget) as transport:
        deep = {c.url for c, _ in BoRoFirstPartyDiscoveryProvider(
            transport, scope=scope, budget=deep_budget).plan()}

    nested = f"{BASE}/services/commercial/chillers"
    assert nested not in shallow
    assert nested in deep


# --- §1 / §4: the full live path, with real provenance ----------------------


def test_a_live_run_produces_evidence_claims_and_a_reviewable_queue(
    researchable, session
):
    """The definition of done, driven once.

    Company → first-party fetches → bodies → derivations → extractions →
    evidence → claims → gaps → profiles, with every row the fixture path
    produces and none of the fixture corpus.
    """
    record: list[str] = []
    with _transport(record=record) as transport:
        report = research_company_live(
            session, company_id=researchable.id, transport=transport,
        )

    assert report.status in ("COMPLETED", "PARTIAL"), report.error
    assert report.pages_fetched >= 4
    assert report.fetch_outcomes.get("OK", 0) >= 4
    assert report.bytes_downloaded > 0

    # Every request went to this company's host.
    assert record, "requests were made"
    for url in record:
        assert HOST in url, f"a request left the company's domain: {url}"

    # The ordinary M3 rows exist, and none of them came from a fixture.
    counts = {
        model.__name__: session.scalar(
            select(func.count()).select_from(model)) or 0
        for model in (m.ResearchSource, m.ResearchFetchEvent,
                      m.ResearchArtifactBody, m.ResearchTextDerivation,
                      m.ResearchExtraction, m.ResearchEvidenceItem)
    }
    for name, count in counts.items():
        assert count > 0, f"no {name} rows from a live run"

    assert report.claims_created > 0
    assert report.observed_attributes, "the site said something operational"
    assert report.coverage is not None

    # Provenance walks from every claim back to a page on this company's site.
    for claim_id in session.scalars(select(CompanyClaim.id).where(
        CompanyClaim.subject_company_id == researchable.id
    )).all():
        evidence_ids = session.scalars(select(m.ClaimEvidenceLink.evidence_item_id)
            .where(m.ClaimEvidenceLink.claim_id == claim_id)).all()
        assert evidence_ids, f"claim {claim_id} has no evidence"
        for evidence_id in evidence_ids:
            provenance = review.provenance_of_evidence(session, evidence_id)
            assert provenance.company_id == researchable.id
            item = session.get(m.ResearchEvidenceItem, evidence_id)
            source = session.get(m.ResearchSource, item.source_id)
            assert HOST in source.normalized_locator, (
                f"evidence for this company came from {source.normalized_locator}"
            )
            assert item.quote, "a claim whose evidence has no quote is unauditable"


def test_a_second_live_run_is_conditional_and_cheap(researchable, session):
    """A re-run sends `If-None-Match` and the server can answer 304."""
    with _transport() as transport:
        research_company_live(session, company_id=researchable.id, transport=transport)
    session.flush()

    seen: list[str | None] = []

    def handler(request):
        if request.url.host == HOST and request.url.path not in ("/robots.txt", "/sitemap.xml"):
            seen.append(request.headers.get("if-none-match"))
            if request.headers.get("if-none-match"):
                return httpx.Response(304, headers={
                    "etag": request.headers["if-none-match"]})
        return _site_handler()(request)

    with _transport(handler) as transport:
        second = research_company_live(
            session, company_id=researchable.id, transport=transport,
        )

    assert any(v is not None for v in seen), "the re-run revalidated"
    assert second.fetch_outcomes.get("NOT_MODIFIED", 0) > 0


def test_the_attempt_records_the_scope_it_ran_under(researchable, session):
    with _transport() as transport:
        report = research_company_live(
            session, company_id=researchable.id, transport=transport,
        )
    attempt = session.get(m.OperationalResearchAttempt, report.attempt_id)
    seeds = attempt.attempt_seed_inputs
    assert seeds["provider"] == "boro_first_party"
    assert seeds["primary"] == f"{BASE}/"
    assert HOST in seeds["in_scope_domains"]
    assert seeds["policy_version"]
    assert attempt.attempt_seed_inputs_hash


def test_an_unreachable_site_is_a_failed_retrieval_not_an_empty_success(
    researchable, session
):
    """The account should look unresearched, not researched-and-empty."""
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("refused", request=request)

    with _transport(handler) as transport:
        report = research_company_live(
            session, company_id=researchable.id, transport=transport,
        )

    assert report.pages_fetched == 0
    assert report.claims_created == 0
    assert report.fetch_outcomes.get("TRANSPORT_ERROR", 0) > 0
    assert report.failed_sources
    assert report.open_gaps, "unreachable is a gap, not a fact"


def test_the_fixture_path_still_works_and_touches_no_network(session):
    """The QA path is unchanged, and remains the default when nothing is passed."""
    seed_all(session)
    session.flush()
    company = _company(session, name="Fixture Co", domains=(("fixture.test", "IDENTITY"),))
    result = run_pipeline(session, company_id=company.id)
    assert result.status in ("COMPLETED", "PARTIAL")
    attempt = session.get(m.OperationalResearchAttempt, result.attempt.id)
    assert attempt.attempt_seed_inputs["provider"] == "fixture"


def test_a_live_transport_without_a_provider_is_refused(researchable, session):
    """The fixture provider names addresses that do not exist.

    Handing them to a live transport would send real requests to invented
    hostnames, so the combination cannot be assembled by omission.
    """
    with _transport() as transport:
        with pytest.raises(ValueError, match="explicit provider"):
            run_pipeline(session, company_id=researchable.id, transport=transport)


def test_a_review_candidate_from_a_live_run_is_answerable(researchable, session):
    """The queue an operator opens after a real run behaves like any other.

    The prose reader is made unconfident so the deferral path runs: on a real
    site whether anything defers depends on that site, and this test is about
    the queue, not about the corpus.
    """
    import dataclasses

    import boro_gtm.research.services.extraction as extraction_module
    from boro_gtm.research.services.extraction import PROSE_EXTRACTOR

    weak = dataclasses.replace(PROSE_EXTRACTOR, extractor_confidence=0.10)
    original = extraction_module.DEFAULT_EXTRACTORS
    extraction_module.DEFAULT_EXTRACTORS = tuple(
        weak if x.extractor_id == PROSE_EXTRACTOR.extractor_id else x
        for x in original
    )
    try:
        with _transport() as transport:
            report = research_company_live(
                session, company_id=researchable.id, transport=transport,
            )
    finally:
        extraction_module.DEFAULT_EXTRACTORS = original

    pending = review.pending_candidates(
        session, company_id=researchable.id, limit=500)
    assert pending, "a weak reading of a real page waits for a human"

    candidate = pending[0]
    stored = review.stored_observation(session, candidate)
    assert stored["quote"]
    outcome = review.review_candidate(
        session, candidate_id=candidate.id, decision="CONFIRM",
        actor="operator", now=None,
    )
    session.flush()
    assert outcome.is_operative
    assert outcome.company_id == researchable.id
    assert report.company_id == researchable.id


def test_two_companies_researched_in_turn_do_not_share_evidence(session):
    """The cross-contamination check, with two real-shaped accounts."""
    seed_all(session)
    session.flush()
    first = _company(session, name="Big Mechanical Inc",
                     domains=((HOST, "IDENTITY"),))
    second = _company(session, name="Allied Mechanical Inc",
                      domains=(("alliedmechanical.com", "IDENTITY"),))

    for company in (first, second):
        with _transport() as transport:
            research_company_live(session, company_id=company.id,
                                  transport=transport)
        session.flush()

    for company, host in ((first, HOST), (second, "alliedmechanical.com")):
        claims = session.scalars(select(CompanyClaim.id).where(
            CompanyClaim.subject_company_id == company.id)).all()
        for claim_id in claims:
            for evidence_id in session.scalars(
                select(m.ClaimEvidenceLink.evidence_item_id)
                .where(m.ClaimEvidenceLink.claim_id == claim_id)
            ).all():
                item = session.get(m.ResearchEvidenceItem, evidence_id)
                source = session.get(m.ResearchSource, item.source_id)
                assert host in source.normalized_locator, (
                    f"{company.id} cites {source.normalized_locator}"
                )


def test_the_report_does_not_invent_contradictions(researchable, session):
    """`contradictions` is a status map, and most of its entries are `false`.

    Listing its keys reported every observed attribute as contradicted. An
    operator deciding whether an account's evidence is trustworthy reads exactly
    this field, and a false alarm there is worse than no field at all.
    """
    with _transport() as transport:
        report = research_company_live(
            session, company_id=researchable.id, transport=transport,
        )

    profile = session.get(m.OperationalResearchProfile, researchable.id)
    states = profile.contradictions or {}
    assert states, "the site produced claims, so there are contradiction states"
    assert any(s.get("contradiction") is False for s in states.values())

    truly = {k for k, s in states.items() if s.get("contradiction")}
    assert report.contradicted_attributes == sorted(truly)
    assert set(report.contradicted_attributes) <= set(report.observed_attributes)
