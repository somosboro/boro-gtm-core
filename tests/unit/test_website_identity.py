"""What a provider-listed website URL may say about identity (M2-ADR-055).

Pure: no database, no network. The URLs are the shapes the first live Google
Places run actually returned.
"""

from __future__ import annotations

import pytest

from boro_gtm.discovery.providers.google_places import GooglePlacesAdapter
from boro_gtm.discovery.services.resolution import (
    domain_role,
    is_identity_domain,
    normalize_domain,
)
from boro_gtm.discovery.website_identity import (
    CONTENT_QUERY,
    DEEP_PATH,
    ROOT,
    SUBDOMAIN,
    website_identity,
)


@pytest.mark.parametrize("url", [
    "https://mckenneys.com",
    "https://mckenneys.com/",
    "http://www.mckenneys.com/",
    "HTTPS://WWW.McKenneys.com/",
    "mckenneys.com",
    "https://mckenneys.com/index.html",
    "https://mckenneys.com/home",
    "https://mckenneys.com/#contact",
    "https://www.acme.co.uk/",
])
def test_a_root_url_supplies_its_domain(url):
    site = website_identity(url)
    assert (site.shared, site.shape) == (False, ROOT)
    assert is_identity_domain(site.host)


@pytest.mark.parametrize("url", [
    # Verbatim parameter names from the Phase A corpus.
    "https://hjac.com/?utm_campaign=gmb",
    "https://www.airco.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp",
    "https://example.com/?utm_source=g&utm_medium=m&utm_campaign=c&utm_content=x&utm_term=t&utm_id=1",
    "https://example.com/?cid=LSTL_ASV-US000187&utm_source=gmb",
    "https://example.com/?se_campaign=local",
    # Ad-platform click identifiers.
    "https://example.com/?gclid=EAIaIQobChMI",
    "https://example.com/?fbclid=IwAR0&msclkid=abc",
    "https://example.com?UTM_SOURCE=Google",
])
def test_tracking_parameters_do_not_demote_a_root(url):
    site = website_identity(url)
    assert (site.shared, site.shape) == (False, ROOT)


@pytest.mark.parametrize("url", [
    # The two Phase A findings, verbatim in shape.
    "https://www.aireserv.com/dallas/?cid=LSTL_ASV-US000187&utm_source=gmb",
    "https://smartcaresolutions.com/providers/almcoe-refrigeration/",
    "https://aireserv.com/atlanta/",
    "https://group.com/brands/a/",
    "https://www.bigmechanical.com/contact",
    "https://example.com/locations/dallas.html",
])
def test_a_deep_path_is_a_page_on_a_domain_not_the_domain(url):
    site = website_identity(url)
    assert (site.shared, site.shape) == (True, DEEP_PATH)
    assert site.host, "the host is kept as evidence"


@pytest.mark.parametrize("url", [
    "https://franchisor.com/?location=dallas",
    "https://franchisor.com/?utm_source=google&store=114",
    "https://example.com/index.php?page_id=7",
])
def test_a_parameter_that_selects_content_is_not_tracking_noise(url):
    assert website_identity(url).shape == CONTENT_QUERY
    assert website_identity(url).shared is True


@pytest.mark.parametrize("url", [
    "https://dallas.franchisor.com/",
    "https://foo.brand-new-host.app/",
    "https://shop.acme.co.uk/",
])
def test_a_subdomain_of_someone_elses_domain_is_shared(url):
    """What keeps the rule independent of any list of hosting platforms."""
    assert website_identity(url).shape == SUBDOMAIN
    assert website_identity(url).shared is True


@pytest.mark.parametrize("url", [None, "", "   ", "https://", "http://[bad"])
def test_no_website_says_nothing(url):
    site = website_identity(url)
    assert (site.host, site.shared, site.shape) == (None, False, None)


# --- the roles M2 gives a domain (M2-ADR-054) -------------------------------


@pytest.mark.parametrize(("domain", "role"), [
    ("mckenneys.com", "IDENTITY"),
    ("www.mckenneys.com", "IDENTITY"),
    ("acme.co.uk", "IDENTITY"),
    ("expressheatingandair.lovable.app", "ALTERNATE"),
    ("foo.web.app", "ALTERNATE"),
    ("foo.pages.dev", "ALTERNATE"),
    ("foo.vercel.app", "ALTERNATE"),
    ("foo.github.io", "ALTERNATE"),
    ("krause.wixsite.com", "ALTERNATE"),
    ("lovable.app", "GROUP"),
    ("github.io", "GROUP"),
    ("facebook.com", "GROUP"),
    ("someone.housecallpro.com", "GROUP"),
    ("localhost", None),
    (None, None),
])
def test_the_role_a_domain_may_hold(domain, role):
    assert domain_role(domain) == role
    assert is_identity_domain(domain) is (role == "IDENTITY")


@pytest.mark.parametrize("platform", [
    "lovable.app", "web.app", "pages.dev", "vercel.app", "github.io",
])
def test_a_tenant_never_normalizes_to_its_platform(platform):
    foo, bar = normalize_domain(f"foo.{platform}"), normalize_domain(f"bar.{platform}")
    assert foo == f"foo.{platform}" and bar == f"bar.{platform}"
    assert platform not in {foo, bar}
    assert normalize_domain(f"https://www.foo.{platform}/x?y=1") == f"foo.{platform}"


# --- the adapter ------------------------------------------------------------


def _candidate(website):
    return GooglePlacesAdapter().normalize({
        "id": "ChIJa", "displayName": {"text": "A Contractor"}, "websiteUri": website,
    })


def test_the_adapter_marks_a_root_as_the_listings_own_domain():
    candidate = _candidate("https://mckenneys.com/?utm_source=google")
    assert candidate.registrable_domain == "mckenneys.com"
    assert candidate.registrable_domain_shared is False
    assert candidate.raw_extras["website_shape"] == ROOT


def test_the_adapter_marks_a_deep_path_as_shared_and_keeps_the_host():
    candidate = _candidate("https://www.aireserv.com/dallas/?cid=LSTL")
    assert candidate.registrable_domain == "www.aireserv.com"
    assert candidate.registrable_domain_shared is True
    assert candidate.raw_extras["website_shape"] == DEEP_PATH


def test_the_adapter_keeps_a_tenant_hostname_whole():
    candidate = _candidate("http://expressheatingandair.lovable.app/")
    assert candidate.registrable_domain == "expressheatingandair.lovable.app"
    assert normalize_domain(candidate.registrable_domain) != "lovable.app"
    assert not is_identity_domain(candidate.registrable_domain)


def test_the_normalizer_version_moved_so_old_normalizations_are_not_reused():
    assert GooglePlacesAdapter().capabilities().normalizer_version == "2"


# --- what may enter the same-run identity index (M2-ADR-056) -----------------


def _identity_key(website):
    from boro_gtm.discovery.services.resolution import identity_key

    return identity_key(_candidate(website))


@pytest.mark.parametrize(("website", "key"), [
    ("https://mckenneys.com/", "mckenneys.com"),
    ("https://www.mckenneys.com/?utm_source=google", "mckenneys.com"),
    ("https://www.acme.co.uk/", "acme.co.uk"),
    # Everything below is recorded as evidence and is never an identity key.
    ("https://aireserv.com/dallas/", None),                # deep path
    ("https://group.com/brands/a/", None),                 # parent brand page
    ("https://franchisor.com/?location=dallas", None),     # content query
    ("https://dallas.franchisor.com/", None),              # foreign subdomain
    ("https://foo.lovable.app/", None),                    # hosting tenant: ALTERNATE
    ("https://lovable.app/", None),                        # the platform: GROUP
    ("https://www.facebook.com/", None),                   # blocklisted: GROUP
    (None, None),
])
def test_only_an_identity_capable_root_is_an_identity_key(website, key):
    assert _identity_key(website) == key


def test_the_index_refuses_one_domain_for_two_companies():
    import uuid

    from boro_gtm.discovery.services.resolution import (
        IdentityIndexInvariantError,
        RunIdentityIndex,
    )

    index = RunIdentityIndex()
    first, second = uuid.uuid4(), uuid.uuid4()
    index.record("mckenneys.com", first)
    index.record("mckenneys.com", first)          # idempotent
    assert index.get("mckenneys.com") == first and len(index) == 1
    with pytest.raises(IdentityIndexInvariantError):
        index.record("mckenneys.com", second)
    assert index.get("mckenneys.com") == first, "the first acceptance is not overwritten"


def test_each_index_is_its_own_state():
    from boro_gtm.discovery.services.resolution import RunIdentityIndex

    one, two = RunIdentityIndex(), RunIdentityIndex()
    import uuid
    one.record("mckenneys.com", uuid.uuid4())
    assert two.get("mckenneys.com") is None
