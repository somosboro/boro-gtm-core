"""A live discovery run, driven end to end against captured Places responses.

Real provider code, real M2 ingestion, resolution, claims and projections — only
the socket is a function. CI never reaches Google, and the parts that decide
company identity are exactly the ones that run against BoRo's real cohort.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from boro_gtm.discovery.domain.models import (
    Company,
    CompanyDomain,
    CompanyName,
    CompanyProfile,
    DiscoveryProvider,
    DiscoveryQuery,
    EntityResolutionDecision,
    ProviderEntity,
    ProviderRecordBody,
    ProviderRecordSighting,
    ProviderRecordVersion,
)
from boro_gtm.discovery.live.plan import (
    QUERY_PLAN_VERSION,
    QueryBudget,
    plan_run,
)
from boro_gtm.discovery.live.runner import run_live_discovery
from boro_gtm.discovery.providers.google_places import (
    PROVIDER_KEY,
    GooglePlacesAdapter,
)
from boro_gtm.discovery.seeds import seed_all

pytestmark = pytest.mark.integration

KEY = "test-key-not-a-real-credential"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _place(place_id: str, name: str, website: str | None, city="Dallas",
           postal="75201", phone="(214) 555-0100") -> dict:
    place: dict = {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": f"100 Trade St, {city}, TX {postal}, USA",
        "addressComponents": [
            {"longText": city, "shortText": city, "types": ["locality"]},
            {"longText": postal, "shortText": postal, "types": ["postal_code"]},
            {"longText": "United States", "shortText": "US", "types": ["country"]},
        ],
        "nationalPhoneNumber": phone,
        "types": ["hvac_contractor", "general_contractor"],
        "primaryType": "hvac_contractor",
        "businessStatus": "OPERATIONAL",
        "location": {"latitude": 32.7767, "longitude": -96.797},
    }
    if website is not None:
        place["websiteUri"] = website
    return place


def _handler(pages: list[dict], record=None):
    """Serve captured pages in order; extra requests get an empty page."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if record is not None:
            record.append(body)
        index = len(calls) - 1
        if index < len(pages):
            return httpx.Response(200, json=pages[index])
        return httpx.Response(200, json={"places": []})

    handler.calls = calls
    return handler


def _adapter(handler) -> GooglePlacesAdapter:
    return GooglePlacesAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)), key=KEY
    )


@pytest.fixture
def m2(session):
    seed_all(session)
    session.flush()
    return session


@pytest.fixture
def provider(m2) -> DiscoveryProvider:
    row = m2.scalar(select(DiscoveryProvider).where(
        DiscoveryProvider.provider_key == PROVIDER_KEY))
    assert row is not None, "the production provider must be seeded"
    return row


def _plan(metros=("dallas_tx",), intents=("commercial HVAC contractor",), **budget):
    return plan_run(metros=metros, intents=intents,
                    budget=QueryBudget(**{"max_queries": 10, "max_results": 200,
                                          **budget}))


# --- seeding and declaration ------------------------------------------------


def test_the_production_provider_is_seeded_as_non_fixture(provider):
    assert provider.is_fixture is False
    assert provider.identity_capability == "NATIVE_EXTERNAL_ID"
    assert provider.media_type == "application/json"
    assert provider.is_active is True


# --- a complete run ---------------------------------------------------------


def test_a_run_turns_places_results_into_canonical_companies(m2, provider):
    handler = _handler([{"places": [
        _place("ChIJa", "Big Mechanical Inc", "https://www.bigmechanical.com/"),
        _place("ChIJb", "Allied Mechanical LLC", "https://alliedmech.com/"),
    ]}])
    report = run_live_discovery(
        m2, provider=provider, adapter=_adapter(handler), planned=_plan(),
    )
    m2.flush()

    assert report.fetch_complete is True
    assert report.status == "COMPLETED"
    assert report.records_fetched == 2
    assert report.provider_entities == 2
    assert report.record_errors == 0
    assert report.normalization_errors == 0

    # Raw evidence exists, append-only, with a body per record.
    assert m2.scalar(select(func.count()).select_from(ProviderEntity)) == 2
    assert m2.scalar(select(func.count()).select_from(ProviderRecordVersion)) == 2
    assert m2.scalar(select(func.count()).select_from(ProviderRecordBody)) == 2

    # Canonical companies came from M2's own resolution, not from the provider.
    assert report.new_companies == 2
    names = set(m2.scalars(select(CompanyName.name_raw)).all())
    assert {"Big Mechanical Inc", "Allied Mechanical LLC"} <= names


def test_the_display_name_is_stored_as_a_trading_name_not_a_legal_name(m2, provider):
    """A Places display name is not a legal registration (M2-ADR-042)."""
    handler = _handler([{"places": [
        _place("ChIJa", "Big Mechanical Inc", "https://bigmechanical.com/")]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    rows = m2.execute(select(CompanyName.name_raw, CompanyName.name_type)).all()
    assert rows == [("Big Mechanical Inc", "TRADING")]
    assert m2.scalar(select(func.count()).select_from(CompanyName).where(
        CompanyName.name_type == "LEGAL")) == 0


def test_a_first_party_website_becomes_the_identity_domain(m2, provider):
    handler = _handler([{"places": [
        _place("ChIJa", "Big Mechanical Inc", "https://www.bigmechanical.com/contact")]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    rows = m2.execute(select(CompanyDomain.domain_normalized,
                             CompanyDomain.domain_role)).all()
    assert rows == [("bigmechanical.com", "IDENTITY")]
    assert report_domains(m2) == {"bigmechanical.com"}


def report_domains(session) -> set[str]:
    return set(session.scalars(select(CompanyDomain.domain_normalized).where(
        CompanyDomain.domain_role == "IDENTITY")).all())


@pytest.mark.parametrize("website", [
    "https://www.facebook.com/bigmechanical",
    "https://www.yelp.com/biz/big-mechanical-dallas",
    "https://bigmechanical.wixsite.com/home",
    "https://www.angi.com/companylist/us/tx/dallas/big-mechanical.htm",
    "https://bigmechanical.housecallpro.com/book",
])
def test_a_directory_or_platform_website_never_becomes_identity(m2, provider, website):
    """The P0 case. Treating a directory as identity merges every contractor on it."""
    handler = _handler([{"places": [_place("ChIJa", "Big Mechanical Inc", website)]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    identity = report_domains(m2)
    assert identity == set(), f"{website} must not carry identity"
    roles = set(m2.scalars(select(CompanyDomain.domain_role)).all())
    assert roles <= {"GROUP"}, "a shared host is recorded, and it is not identity"


# --- §12 resolution convergence --------------------------------------------


def test_the_same_place_id_twice_is_one_provider_entity(m2, provider):
    """Two intents finding one listing converge, and provenance keeps both."""
    place = _place("ChIJsame", "Big Mechanical Inc", "https://bigmechanical.com/")
    handler = _handler([{"places": [place]}, {"places": [place]}])
    report = run_live_discovery(
        m2, provider=provider, adapter=_adapter(handler),
        planned=_plan(intents=("commercial HVAC contractor",
                               "commercial mechanical contractor")),
    )
    m2.flush()

    assert report.records_fetched == 2
    assert m2.scalar(select(func.count()).select_from(ProviderEntity)) == 1
    assert m2.scalar(select(func.count()).select_from(ProviderRecordVersion)) == 1
    # Two queries saw it, and both sightings survive.
    assert m2.scalar(select(func.count()).select_from(ProviderRecordSighting)) == 2
    assert m2.scalar(select(func.count()).select_from(Company)) == 1


def test_two_place_ids_sharing_a_domain_do_not_merge_wrongly(m2, provider):
    """Two branch listings of one contractor, discovered in the same run.

    Measured behaviour, not aspiration. `_deterministic_match` reads
    `CompanyDomain`, which is a **projection** rebuilt after resolution — so
    within a single run the second listing cannot yet see the first company's
    domain, and two canonical companies are created. The projection's partial
    unique index then gives the domain `IDENTITY` on one and demotes it to
    `GROUP` on the other (M2-ADR-047).

    That is a duplicate, and it is the safe direction: M2 under-merges rather
    than merging two organisations that might not be one. What this test
    protects is the part that must never slip — no wrong merge, and never two
    companies holding one domain as identity.
    """
    handler = _handler([{"places": [
        _place("ChIJnorth", "Big Mechanical Inc", "https://bigmechanical.com/",
               city="Dallas", postal="75201"),
        _place("ChIJsouth", "Big Mechanical Inc", "https://www.bigmechanical.com/",
               city="Fort Worth", postal="76102", phone="(817) 555-0200"),
    ]}])
    report = run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                                planned=_plan())
    m2.flush()

    assert m2.scalar(select(func.count()).select_from(ProviderEntity)) == 2, (
        "two listings are two provider entities"
    )

    # The invariant: exactly one company may hold a domain as IDENTITY.
    identity_owners = m2.execute(
        select(CompanyDomain.company_id).where(
            CompanyDomain.domain_normalized == "bigmechanical.com",
            CompanyDomain.domain_role == "IDENTITY",
        )
    ).all()
    assert len(identity_owners) == 1, (
        "two companies holding one identity domain would be a merge waiting to "
        "happen"
    )
    demoted = m2.execute(
        select(CompanyDomain.company_id).where(
            CompanyDomain.domain_normalized == "bigmechanical.com",
            CompanyDomain.domain_role == "GROUP",
        )
    ).all()
    assert len(demoted) == 1, "the duplicate's domain is demoted, not duplicated"

    # Recorded as a duplicate for the operator, never silently averaged away.
    assert report.canonical_companies == 2
    assert report.companies_with_identity_domain == 1
    assert report.companies_without_domain == 1
    assert report.new_companies == 2


def test_a_domain_seen_in_an_earlier_run_matches_instead_of_duplicating(m2, provider):
    """Across runs the projection exists, so the deterministic match works.

    This is the other half of M2-ADR-047: the limitation is intra-run ordering,
    not the matching rule.
    """
    first = _handler([{"places": [
        _place("ChIJnorth", "Big Mechanical Inc", "https://bigmechanical.com/")]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(first),
                       planned=_plan())
    m2.flush()
    assert m2.scalar(select(func.count()).select_from(Company)) == 1

    second = _handler([{"places": [
        _place("ChIJsouth", "Big Mechanical Inc", "https://www.bigmechanical.com/",
               city="Fort Worth", postal="76102", phone="(817) 555-0200")]}])
    report = run_live_discovery(m2, provider=provider, adapter=_adapter(second),
                               planned=_plan(intents=("commercial HVAC service",)))
    m2.flush()

    assert m2.scalar(select(func.count()).select_from(Company)) == 1, (
        "an exact identity-domain match is the strongest deterministic signal"
    )
    assert report.matched_companies >= 1
    assert m2.scalar(select(func.count()).select_from(ProviderEntity)) == 2


def test_the_same_name_alone_never_merges_two_companies(m2, provider):
    """The rule M2 exists to protect. Two "Allied Mechanical" are two companies."""
    handler = _handler([{"places": [
        _place("ChIJone", "Allied Mechanical", "https://allied-mechanical-tx.com/",
               city="Dallas"),
        _place("ChIJtwo", "Allied Mechanical", "https://alliedmechanicalohio.com/",
               city="Columbus", postal="43215", phone="(614) 555-0300"),
    ]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    assert m2.scalar(select(func.count()).select_from(Company)) == 2
    assert report_domains(m2) == {
        "allied-mechanical-tx.com", "alliedmechanicalohio.com",
    }


def test_a_listing_with_no_website_creates_no_company(m2, provider):
    """A name alone is not identity, so the record is parked as AMBIGUOUS.

    The raw evidence stays on file for a later normalizer or a second provider;
    nothing canonical is minted from a name.
    """
    handler = _handler([{"places": [_place("ChIJnoweb", "No Website HVAC", None)]}])
    report = run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                                planned=_plan())
    m2.flush()

    assert report.records_fetched == 1
    assert m2.scalar(select(func.count()).select_from(ProviderEntity)) == 1
    assert m2.scalar(select(func.count()).select_from(Company)) == 0
    assert report.ambiguous_entities == 1
    decisions = set(m2.scalars(select(EntityResolutionDecision.decision)).all())
    assert decisions == {"AMBIGUOUS"}


def test_provider_categories_are_never_a_fact_vertical(m2, provider):
    """Google's taxonomy is what it filed the business under, not an ICP fact."""
    from boro_gtm.discovery.domain.models import CompanyClaim

    handler = _handler([{"places": [
        _place("ChIJa", "Big Mechanical Inc", "https://bigmechanical.com/")]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    vertical_claims = m2.execute(select(
        CompanyClaim.attribute_key, CompanyClaim.fact_type
    ).where(CompanyClaim.attribute_key.like("%vertical%"))).all()
    for _key, fact_type in vertical_claims:
        assert fact_type != "FACT", "provider taxonomy cannot become a FACT"


# --- §7 / §9 query provenance ----------------------------------------------


def test_every_record_names_the_query_metro_intent_and_page(m2, provider):
    handler = _handler([
        {"places": [_place("ChIJa", "A Mechanical", "https://amech.com/")],
         "nextPageToken": "TOKEN1"},
        {"places": [_place("ChIJb", "B Mechanical", "https://bmech.com/")]},
    ])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    queries = m2.scalars(select(DiscoveryQuery).order_by(
        DiscoveryQuery.page_number)).all()
    assert len(queries) == 2, "one query row per provider page"
    assert [q.parameters["page"] for q in queries] == [0, 1]
    for query in queries:
        assert query.parameters["query_plan_version"] == QUERY_PLAN_VERSION
        assert query.parameters["metro"] == "dallas_tx"
        assert query.parameters["intent"] == "commercial HVAC contractor"
        assert query.parameters["text_query"].startswith("commercial HVAC contractor in")
    assert queries[1].parameters["page_token"] == "TOKEN1"

    # Each stored record is reachable from the query that saw it.
    assert m2.scalar(select(func.count()).select_from(ProviderRecordSighting)) == 2


def test_the_stored_body_is_one_place_so_identity_is_the_place(m2, provider):
    handler = _handler([{"places": [
        _place("ChIJa", "A Mechanical", "https://amech.com/")],
        "nextPageToken": "T"}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    for (payload,) in m2.execute(select(ProviderRecordBody.raw_body)).all():
        parsed = json.loads(bytes(payload))
        assert "places" not in parsed and "nextPageToken" not in parsed
        assert parsed["id"].startswith("ChIJ")


# --- §8 budget --------------------------------------------------------------


def test_the_query_budget_caps_provider_requests(m2, provider):
    handler = _handler([
        {"places": [_place(f"ChIJ{i}", f"Co {i}", f"https://co{i}.com/")],
         "nextPageToken": f"T{i}"} for i in range(9)
    ])
    report = run_live_discovery(
        m2, provider=provider, adapter=_adapter(handler),
        planned=_plan(metros=("dallas_tx", "atlanta_ga"),
                      intents=("commercial HVAC contractor",
                               "commercial mechanical contractor"),
                      max_queries=3),
    )
    m2.flush()
    assert report.queries_issued == 3
    assert report.budget_stopped_at == "MAX_QUERIES"
    assert len(handler.calls) == 3, "no request is made past the cap"


def test_the_result_budget_caps_records_stored(m2, provider):
    handler = _handler([
        {"places": [_place(f"ChIJ{i}", f"Co {i}", f"https://co{i}.com/")
                    for i in range(5)], "nextPageToken": f"T{i}"}
        for i in range(4)
    ])
    report = run_live_discovery(
        m2, provider=provider, adapter=_adapter(handler),
        planned=_plan(max_results=5),
    )
    assert report.budget_stopped_at == "MAX_RESULTS"
    assert report.records_fetched <= 10


def test_planning_is_deterministic_and_touches_nothing():
    first = plan_run(metros=("dallas_tx", "atlanta_ga"))
    second = plan_run(metros=("dallas_tx", "atlanta_ga"))
    assert [s.text_query for s in first.steps] == [s.text_query for s in second.steps]
    assert first.as_dict() == second.as_dict()
    assert first.planned_first_page_queries == 10


# --- §9 partial fetch -------------------------------------------------------


def test_a_provider_failure_part_way_is_partial_and_blocks_canonical_writes(
    m2, provider
):
    """Evidence already paid for is kept; `fetch_completed_at` is never set."""
    from boro_gtm.discovery.domain.models import DiscoveryRun

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"places": [
                _place("ChIJa", "A Mechanical", "https://amech.com/")]})
        return httpx.Response(503)

    report = run_live_discovery(
        m2, provider=provider, adapter=_adapter(handler),
        planned=_plan(intents=("commercial HVAC contractor",
                               "commercial mechanical contractor")),
    )
    m2.flush()

    assert report.status == "PARTIAL_FETCH"
    assert report.fetch_complete is False
    assert report.provider_error
    run = m2.get(DiscoveryRun, report.run_id)
    assert run.fetch_completed_at is None, (
        "an incomplete fetch must never look complete"
    )
    # The evidence from the page that succeeded is retained.
    assert m2.scalar(select(func.count()).select_from(ProviderRecordVersion)) == 1
    # And nothing canonical was written from an unfinished run.
    assert m2.scalar(select(func.count()).select_from(Company)) == 0


def test_a_rate_limit_is_recorded_as_a_provider_failure(m2, provider):
    report = run_live_discovery(
        m2, provider=provider,
        adapter=_adapter(lambda r: httpx.Response(429)), planned=_plan(),
    )
    assert report.status == "PARTIAL_FETCH"
    assert "quota" in (report.provider_error or "").lower()


def test_a_credential_error_is_recorded_and_nothing_is_invented(m2, provider):
    report = run_live_discovery(
        m2, provider=provider,
        adapter=_adapter(lambda r: httpx.Response(403)), planned=_plan(),
    )
    assert report.status == "PARTIAL_FETCH"
    assert m2.scalar(select(func.count()).select_from(Company)) == 0


# --- §15 the holdout never reaches the provider -----------------------------


def test_no_holdout_company_reaches_the_query_plan():
    """Queries are built from market and metro, never from known companies."""
    plan = plan_run()
    text = " ".join(step.text_query for step in plan.steps).lower()
    for known in ("tdindustries", "comfort systems", "coolsys", "southland",
                  "campbell", "bigmechanical", "emcor"):
        assert known not in text
    # Nothing in the plan module reads a company table or a cohort file.
    import inspect

    from boro_gtm.discovery.live import plan as plan_module

    source = inspect.getsource(plan_module)
    for forbidden in ("Company", "CompanyDomain", "holdout", "csv", "session"):
        assert forbidden not in source, f"the query plan must not reference {forbidden}"


# --- projection -------------------------------------------------------------


def test_a_discovered_company_gets_an_inspectable_profile(m2, provider):
    handler = _handler([{"places": [
        _place("ChIJa", "Big Mechanical Inc", "https://bigmechanical.com/")]}])
    run_live_discovery(m2, provider=provider, adapter=_adapter(handler),
                       planned=_plan())
    m2.flush()

    # The runner already rebuilt the projections; this is the operator's view.
    profile = m2.scalars(select(CompanyProfile)).one()
    assert profile.primary_domain == "bigmechanical.com"
    assert profile.employee_count_min is None, "Places says nothing about headcount"
    assert profile.derived_from_claim_ids


# --- §20 the autonomous chain: M2 discovery → M3 first-party research --------


def test_a_discovered_company_is_researchable_by_m3_without_being_loaded(
    m2, provider
):
    """The whole point. Nobody supplied this company.

    A Places listing became a canonical M2 company with an identity domain, and
    M3 researched that company's own website from it — no `load-cohort`, no
    operator-supplied list, no manual domain. Two mocked sockets, one real chain
    (M2-ADR-048).
    """
    import boro_gtm.research.services.extraction as extraction_module
    from boro_gtm.research.domain import models as rm
    from boro_gtm.research.live.policy import USER_AGENT, CrawlBudget
    from boro_gtm.research.live.runner import research_company_live
    from boro_gtm.research.live.transport import ProductionWebTransport
    from boro_gtm.research.seeds import seed_all as seed_research
    from boro_gtm.research.services import review

    seed_research(m2)
    m2.flush()

    # 1. M2 discovers the company from a provider listing.
    handler = _handler([{"places": [
        _place("ChIJdiscovered", "Discovered Mechanical Inc",
               "https://discoveredmech.com/")]}])
    discovery = run_live_discovery(m2, provider=provider,
                                  adapter=_adapter(handler), planned=_plan())
    m2.flush()
    assert discovery.new_companies == 1

    company_id = m2.scalar(select(CompanyDomain.company_id).where(
        CompanyDomain.domain_role == "IDENTITY",
        CompanyDomain.domain_normalized == "discoveredmech.com",
    ))
    assert company_id is not None, "M2 produced a researchable identity domain"

    # Nothing was loaded by hand: the company's only name came from the provider.
    assert m2.scalars(select(CompanyName.name_type)).all() == ["TRADING"]

    # 2. M3 researches that company's own website, from M2's domain alone.
    pages = {
        "/": ('<html><body><h1>Discovered Mechanical</h1>'
              '<a href="/services/commercial">Commercial Services</a>'
              '<a href="/about">About</a></body></html>'),
        "/services/commercial": ('<html><body><p>We provide preventive '
                                 'maintenance and 24/7 emergency service for '
                                 'commercial clients.</p></body></html>'),
        "/about": '<html><body><p>Commercial mechanical contracting.</p></body></html>',
    }
    visited: list[str] = []

    def site(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if request.url.host != "discoveredmech.com":
            raise AssertionError(f"M3 left the company's domain: {request.url}")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nAllow: /\n",
                                  headers={"content-type": "text/plain"})
        if request.url.path == "/sitemap.xml":
            return httpx.Response(404)
        body = pages.get(request.url.path.rstrip("/") or "/")
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode(),
                              headers={"content-type": "text/html"})

    transport = ProductionWebTransport(
        budget=CrawlBudget(delay_seconds=0.0),
        client=httpx.Client(transport=httpx.MockTransport(site),
                            follow_redirects=False,
                            headers={"User-Agent": USER_AGENT}),
        resolver=lambda host, port: ["93.184.216.34"],
        respect_robots=True, sleeper=lambda _s: None,
    )
    try:
        research = research_company_live(m2, company_id=company_id,
                                        transport=transport)
    finally:
        transport.close()
    m2.flush()

    # 3. The evidence exists and belongs to the company M2 discovered.
    assert research.status in ("COMPLETED", "PARTIAL"), research.error
    assert research.primary_url == "https://discoveredmech.com/"
    assert research.pages_fetched >= 2
    assert research.claims_created >= 1, "the site said something operational"
    assert visited, "M3 actually fetched"

    for claim_id in m2.scalars(select(rm.ClaimEvidenceLink.claim_id).distinct()).all():
        for evidence_id in m2.scalars(select(rm.ClaimEvidenceLink.evidence_item_id)
                                      .where(rm.ClaimEvidenceLink.claim_id == claim_id)).all():
            provenance = review.provenance_of_evidence(m2, evidence_id)
            assert provenance.company_id == company_id, (
                "M3 evidence must belong to the company M2 discovered"
            )
            item = m2.get(rm.ResearchEvidenceItem, evidence_id)
            source = m2.get(rm.ResearchSource, item.source_id)
            assert "discoveredmech.com" in source.normalized_locator

    # And the operational profile is the company's, not the provider's.
    operational = m2.get(rm.OperationalResearchProfile, company_id)
    assert operational is not None
    assert operational.facts, "M3 produced operational facts for a discovered account"
    assert extraction_module.DEFAULT_EXTRACTORS, "the real extractors ran"
