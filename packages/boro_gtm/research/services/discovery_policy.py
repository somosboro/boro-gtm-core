"""Which addresses may seed research for one company.

A shared platform domain is not a company's website. Crawling `linkedin.com`
because a company has a page there would mix every company's evidence into one
source, and the source row is what independence and corroboration are computed
over — so one bad seed does not produce one bad claim, it corrupts the weighting
for everyone on that domain.

Group and platform domains are therefore refused as seeds. A *specific page* on
one of them can still be a legitimate third-party source later, discovered by
name; what this refuses is treating the domain as the company's own site.
"""

from __future__ import annotations

from boro_gtm.research.policies import host_of, normalize_locator, registrable_domain

#: Domains shared by many companies. Not exhaustive, and deliberately small:
#: a guessy list is worse than a short honest one, because it would silently
#: drop real sources.
GROUP_AND_PLATFORM_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "youtube.com", "google.com", "sites.google.com", "wixsite.com",
    "wordpress.com", "blogspot.com", "weebly.com", "squarespace.com",
    "godaddysites.com", "business.site", "yelp.com", "angi.com",
    "houzz.com", "bbb.org", "indeed.com", "glassdoor.com",
})


def is_crawlable_seed(url: str) -> bool:
    """Whether this address may stand in for the company's own site."""
    host = host_of(normalize_locator(url))
    if not host:
        return False
    domain = registrable_domain(host)
    return domain not in GROUP_AND_PLATFORM_DOMAINS and host not in (
        GROUP_AND_PLATFORM_DOMAINS
    )
