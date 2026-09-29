"""The crawl policy for BoRo's own cohort. Versioned, because it is a judgement.

These numbers are not universal truths; they are what worked against real U.S.
commercial HVAC and mechanical contractor sites. They are versioned and recorded
on the discovery context so that a run can be read later and the policy it ran
under can be known, rather than inferred from the date (M3-ADR-072).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

#: Bump when any constant below changes meaning.
FIRST_PARTY_POLICY_VERSION = "1"

#: Honest, attributable, and reachable. A crawler that hides who it is has
#: already decided the site owner's preference does not matter.
USER_AGENT = (
    "BoRoGTMResearchBot/0.3 (+https://somosboro.com/bot; "
    "operational research; contact borotech.contact@gmail.com)"
)

#: Path fragments that suggest a page describes how the company *operates*.
#: Ordered loosely by how often they carried real evidence in the pilot.
RELEVANT_PATH_TERMS: tuple[str, ...] = (
    "commercial", "service", "services", "maintenance", "preventive",
    "preventative", "emergency", "24-7", "24-hour", "247", "industrial",
    "mechanical", "hvac", "refrigeration", "chiller", "boiler", "plumbing",
    "controls", "building-automation", "retrofit", "installation",
    "locations", "location", "branches", "branch", "service-area",
    "areas-served", "coverage",
    "about", "about-us", "company", "who-we-are", "our-team", "team",
    "history", "capabilities", "why",
    "industries", "markets", "sectors", "verticals", "clients", "customers",
    "careers", "career", "jobs", "employment", "join", "apply",
    "technician", "technicians", "apprentice", "fleet",
    "projects", "portfolio", "case-studies", "case-study",
    "contact", "contact-us",
)

#: Path fragments that reliably carried nothing operational. Checked *before*
#: the relevant terms, because "/blog/commercial-hvac-tips" matches both and is
#: a blog post.
IRRELEVANT_PATH_TERMS: tuple[str, ...] = (
    "privacy", "terms", "tos", "legal", "cookie", "disclaimer", "accessibility",
    "sitemap.xml", "wp-json", "wp-admin", "wp-login", "xmlrpc",
    "/blog/", "/news/", "/press/", "/article/", "/articles/", "/post/",
    "/posts/", "/tag/", "/tags/", "/category/", "/categories/", "/author/",
    "/archive/", "/archives/", "/feed", "/rss", "/comments/",
    "/page/", "/paged/",
    "cart", "checkout", "basket", "shop", "product", "store", "pricing",
    "login", "signin", "sign-in", "register", "account", "my-account",
    "password", "logout", "admin", "portal", "payment", "pay-bill", "paybill",
    "search", "?s=", "?q=",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "yelp.com", "pinterest", "tiktok", "maps.google",
)

#: Extensions that are not documents this pipeline can read.
IRRELEVANT_SUFFIXES: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif",
    ".tiff", ".avif", ".mp4", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a",
    ".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".gz", ".tar", ".rar", ".7z", ".dmg", ".exe", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".rss", ".atom",
)

#: What the extraction pipeline can actually read today. A document outside this
#: set is a `MIME_MISMATCH`, which is a recorded retrieval outcome and therefore
#: an honest gap, not a silent skip.
ACCEPTED_MEDIA_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "text/plain",
    "application/pdf", "application/json",
})


#: Query parameters that change how a page is *presented*, not which document
#: it is. Stripped at discovery so one page is not fetched twice.
#:
#: Found in the first pilot: `tdindustries.com/service-maintenance` and
#: `…?hsLang=en` returned byte-identical bodies, so M3's corroboration counted
#: them as one document and confidence was unaffected — the defence worked. But
#: they were still two retrievals of one page, which is two requests we owed the
#: site no reason for. Stripped here rather than in `normalize_locator`, because
#: this is a judgement about the sites BoRo researches and not a fact about URLs.
PRESENTATION_PARAMS = frozenset({
    "hslang", "lang", "language", "locale", "hl",
    "print", "printable", "amp", "output", "format",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "_ga", "_gl", "yclid", "igshid",
})


def strip_presentation_params(url: str) -> str:
    """Drop parameters that do not select a different document."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in PRESENTATION_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), parts.fragment))


@dataclass(frozen=True, slots=True)
class CrawlBudget:
    """Conservative by design: a pilot, not a scrape.

    Defaults chosen after fetching real contractor sites. Twenty-five pages
    covers a typical contractor's operational pages with room for a locations
    index; the ones that need more are usually the ones with paginated blogs,
    which the relevance policy excludes anyway.
    """

    #: Documents actually retrieved per research attempt.
    max_pages: int = 25
    #: Link hops from the homepage. Two reaches "/services/commercial/" — the
    #: depth at which contractors describe what they actually do.
    max_depth: int = 2
    #: Per document. Larger than any contractor page seen; a 5 MB "page" is a
    #: video embed or a mistake.
    max_bytes: int = 5_000_000
    #: Total retrievals attempted, including failures. Bounds the worst case
    #: where a site answers 500 to everything.
    max_retrievals: int = 40
    #: Seconds between requests to one host. A contractor's site is usually on
    #: shared hosting; there is no reason to be fast.
    delay_seconds: float = 1.0
    #: Per request, connect + read.
    timeout_seconds: float = 15.0
    #: Bounded, and every hop re-checked for safety.
    max_redirects: int = 5


def is_relevant(url: str) -> bool:
    """Whether this address plausibly describes how the company operates.

    Deterministic and cheap: a substring policy over the path, not a model. It
    is allowed to be wrong in both directions — a missed page is a truthful gap,
    and an irrelevant page that gets fetched simply yields no observations.
    """
    parts = urlsplit(url)
    path = (parts.path or "/").lower()
    full = f"{path}?{parts.query.lower()}" if parts.query else path

    if path.endswith(IRRELEVANT_SUFFIXES):
        return False
    if any(term in full for term in IRRELEVANT_PATH_TERMS):
        return False
    if path in {"/", ""}:
        return True                  # the homepage always earns one fetch
    return any(term in path for term in RELEVANT_PATH_TERMS)


def relevance_hint(url: str) -> float:
    """A coarse ordering, so the budget is spent on the best pages first.

    Not a confidence and not evidence of anything — it only decides what gets
    fetched before the budget runs out.
    """
    path = (urlsplit(url).path or "/").lower()
    if path in {"/", ""}:
        return 1.0
    for score, terms in (
        (0.95, ("commercial", "preventive", "preventative", "maintenance")),
        (0.90, ("service", "services", "capabilities", "industrial")),
        (0.85, ("about", "company", "who-we-are", "history")),
        (0.80, ("careers", "jobs", "employment", "technician")),
        (0.75, ("locations", "branches", "service-area", "areas-served")),
        (0.70, ("industries", "markets", "sectors", "clients")),
        (0.60, ("projects", "portfolio", "case-studies")),
        (0.50, ("contact", "emergency", "24-7")),
    ):
        if any(term in path for term in terms):
            return score
    return 0.40
