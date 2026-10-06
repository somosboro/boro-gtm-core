"""What a provider-listed website URL may be taken to say about identity.

A listing's "website" is whatever its owner typed. Three shapes look alike and
mean different things (M2-ADR-055):

`https://contractor.com/` — the root of a domain. The registrable domain is a
    candidate identity.
`https://contractor.com/?utm_source=google` — the same root, with a campaign
    tag. Tracking parameters are noise and never demote a real root.
`https://franchisor.com/dallas/` — one page on a domain. Many listings can
    point into the same domain this way: every franchisee, every brand of a
    parent. The domain is evidence of an association, not of being that company.

A subdomain of a domain somebody else registered (`dallas.franchisor.com`,
`shop.unknown-host.app`) is the third case wearing a different URL, and it is
what keeps the rule independent of any list of hosting platforms: a platform
the Public Suffix List has never heard of still cannot merge its tenants.

Pure, and it opens no socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from boro_gtm.core import public_suffix

#: Query parameters that tag a visit and select no content. Every name here was
#: either seen on a real listing in the first live run or is a documented click
#: identifier of an ad platform.
TRACKING_PARAMS = frozenset({
    "gclid", "gclsrc", "gbraid", "wbraid", "gad_source", "gad_campaignid", "dclid",
    "fbclid", "msclkid", "ttclid", "twclid", "li_fat_id", "yclid",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "hsa_acc", "hsa_cam", "hsa_grp",
    "hsa_ad", "hsa_src", "hsa_tgt", "hsa_kw", "hsa_mt", "hsa_net", "hsa_ver",
    "cid", "se_campaign", "y_source", "ref", "ref_src", "source", "campaign",
})
TRACKING_PREFIXES = ("utm_",)

#: Paths that are the front page under another name.
TRIVIAL_PATHS = frozenset({
    "", "/index.html", "/index.htm", "/index.php", "/index.asp", "/index.aspx",
    "/default.asp", "/default.aspx", "/home", "/home.html",
})

ROOT = "ROOT"
DEEP_PATH = "DEEP_PATH"
CONTENT_QUERY = "CONTENT_QUERY"
SUBDOMAIN = "SUBDOMAIN"


@dataclass(frozen=True, slots=True)
class WebsiteIdentity:
    #: The host exactly as listed, lower-cased. Never rewritten: the evidence is
    #: what the provider said.
    host: str | None
    #: Whether the URL addresses a domain the listing does not hold alone.
    shared: bool
    #: `ROOT`, or why it is not one.
    shape: str | None


def is_tracking_param(name: str) -> bool:
    key = name.lower()
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def website_identity(url: str | None) -> WebsiteIdentity:
    """Classify a listed website. A missing or unreadable URL says nothing."""
    text = (url or "").strip()
    if not text:
        return WebsiteIdentity(host=None, shared=False, shape=None)
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return WebsiteIdentity(host=None, shared=False, shape=None)
    if not host:
        return WebsiteIdentity(host=None, shared=False, shape=None)

    path = parts.path.rstrip("/").lower()
    if path not in TRIVIAL_PATHS:
        return WebsiteIdentity(host=host, shared=True, shape=DEEP_PATH)

    if any(not is_tracking_param(name)
           for name, _ in parse_qsl(parts.query, keep_blank_values=True)):
        # `?location=dallas` selects a page as surely as `/dallas/` does. A
        # parameter this module cannot name is treated as content: the cost of
        # being wrong is one company without an identity domain, not a merge.
        return WebsiteIdentity(host=host, shared=True, shape=CONTENT_QUERY)

    bare = host[4:] if host.startswith("www.") else host
    registrable = public_suffix.registrable_domain(bare)
    if registrable is not None and bare != registrable:
        return WebsiteIdentity(host=host, shared=True, shape=SUBDOMAIN)
    return WebsiteIdentity(host=host, shared=False, shape=ROOT)
