"""A deterministic fictional corpus: Meridian Mechanical Services, Inc.

A commercial HVAC and mechanical contractor in Columbus, Ohio. Everything here
is invented. No fixture resolves to a real company, a real domain or a real
person, and nothing in M3's test suite touches the public internet.

The corpus is written the way real sources read, which is the point: pages
describe *what a company says it does*, and never "this company has a
fragmentation problem". Turning the first into the second is M4's job, and the
fixtures must not do it on M4's behalf, or every downstream test would be
grading a conclusion the fixture author already wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from boro_gtm.research.policies import PDF_PAGE_BREAK, normalize_locator

COMPANY_NAME = "Meridian Mechanical Services, Inc."
PRIMARY_DOMAIN = "www.meridianmechanical.com"
HOME = "https://www.meridianmechanical.com/"


@dataclass(frozen=True, slots=True)
class FixtureResource:
    """One address, and what retrieving it does.

    ``outcome`` is what the fixture transport reports; ``body`` exists only for
    outcomes that yield bytes.
    """

    outcome: str = "OK"
    http_status: int | None = 200
    body: bytes | None = None
    #: The bytes after the corpus is mutated, for the changed-source scenario.
    changed_body: bytes | None = None
    declared_content_type: str | None = "text/html; charset=utf-8"
    etag: str | None = None
    last_modified: str | None = None
    redirect_to: str | None = None
    error_class: str | None = None
    robots_disallowed: bool = False
    byte_limit_exceeded: bool = False
    #: Link targets a crawler would follow out of this page.
    links: tuple[str, ...] = field(default_factory=tuple)


def _html(
    title: str, body: str, *, lang: str = "en", published: str | None = None,
    canonical: str | None = None, head_extra: str = "",
) -> bytes:
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    link = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return (
        f'<!DOCTYPE html><html lang="{lang}"><head><title>{title}</title>'
        f"{meta}{link}{head_extra}"
        "<style>.nav{color:#123}</style>"
        '<script>window.__build="2026-02-11T04:12:00Z";</script>'
        f"</head><body>{body}</body></html>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Company-owned pages
# ---------------------------------------------------------------------------

_ABOUT_BODY = """
<h1>Meridian Mechanical Services</h1>
<p>Meridian Mechanical Services, Inc. has provided commercial HVAC and
mechanical contracting across central Ohio since 1998. We operate from four
service locations: Columbus, Dayton, Toledo and Cincinnati.</p>
<p>Our team includes 58 field technicians, 6 service coordinators and 4
project estimators. We maintain a fleet of 47 service vehicles.</p>
<p>Our in-house sheet metal fabrication shop produces custom ductwork and
transitions for retrofit projects.</p>
<p>We serve Franklin County, Montgomery County, Lucas County and Hamilton
County, Ohio.</p>
<p>Contact dispatch at dispatch@meridianmechanical.com or (614) 555-0142.</p>
"""

_SERVICES_BODY_V1 = """
<h1>Commercial Mechanical Services</h1>
<p>Meridian provides HVAC installation, preventive maintenance, corrective
repair and 24/7 emergency response for commercial and institutional
facilities.</p>
<h2>How a service call runs</h2>
<p>Our service coordinators receive the request, confirm the site and assign a
technician from the dispatch board.</p>
<p>The technician completes the work order on site and submits it to the
service coordinator at the end of the visit.</p>
<p>When a technician identifies work beyond the original order, the finding is
written up and passed to an estimator, who prepares a quote for the customer's
approval before the additional work is scheduled.</p>
<p>Every quote above $5,000 is reviewed and approved by the service manager
before it is sent.</p>
<p>Once the coordinator confirms the work order is complete, the details are
entered into the accounting system for invoicing.</p>
<p>Equipment history is kept in the maintenance binder at each customer site
and in the service coordinator's spreadsheet.</p>
<h2>Systems</h2>
<p>Customers can view upcoming visits through our Meridian Customer Portal.</p>
<p>Categories: chillers, boilers, rooftop units, building automation, sheet
metal fabrication.</p>
"""

# The changed variant is a *semantic* change: the approval threshold moves and a
# new service category appears. Whitespace-only edits belong in the cosmetic
# case below, so the two behaviours cannot be confused.
_SERVICES_BODY_V2 = _SERVICES_BODY_V1.replace(
    "Every quote above $5,000 is reviewed and approved by the service manager",
    "Every quote above $2,500 is reviewed and approved by the service manager",
).replace(
    "metal fabrication.</p>", "metal fabrication, refrigeration.</p>"
)

_PM_BODY = """
<h1>Preventive Maintenance Agreements</h1>
<p>Meridian offers scheduled preventive maintenance agreements with quarterly,
semi-annual and annual visit frequencies. Agreements renew annually.</p>
<p>Technicians record each visit on a paper checklist, photograph the
equipment nameplate, and the coordinator re-enters the checklist results into
the maintenance spreadsheet after the visit.</p>
<p>Planned maintenance covers chillers, boilers, air handling units and
rooftop units.</p>
"""

_EMERGENCY_BODY = """
<h1>24/7 Emergency Service</h1>
<p>Meridian provides 24 hours a day, 7 days a week emergency response for
contract customers across all four service locations.</p>
<p>After-hours calls reach the on-call coordinator, who reaches the on-call
technician by phone.</p>
<p>Call the emergency line at (614) 555-0199.</p>
"""

_ES_BODY = """
<h1>Servicios Mecanicos Comerciales</h1>
<p>Meridian ofrece instalacion, mantenimiento preventivo, reparacion
correctiva y respuesta de emergencia 24/7 para instalaciones comerciales.</p>
"""

_PORTAL_BODY = """
<h1>Customer Portal</h1>
<p>Please sign in to view your service history.</p>
<form><input name="username"><input name="password" type="password"></form>
"""

# ---------------------------------------------------------------------------
# The PDF fixture. A page-delimited text container, not a real PDF.
# ---------------------------------------------------------------------------

_MAINTENANCE_PDF = (
    "%PDF-1.4\n"
    "MERIDIAN MECHANICAL SERVICES, INC.\n"
    "Commercial Maintenance Agreement - Standard Terms\n"
    "Effective 2025-03-01\n"
    + PDF_PAGE_BREAK +
    "SECTION 2. SERVICE DELIVERY\n"
    "2.1 Scheduled visits are performed per the agreed frequency.\n"
    "2.2 The technician records findings on the service ticket and returns the\n"
    "    completed ticket to the service coordinator.\n"
    "2.3 The service coordinator enters the ticket into the accounting system\n"
    "    for invoicing within five business days of completion.\n"
    "2.4 Work outside the scope of this agreement requires a written estimate\n"
    "    approved by the customer before it is performed.\n"
    + PDF_PAGE_BREAK +
    "SECTION 3. RECORDS\n"
    "3.1 The equipment list maintained by Meridian is the record of covered\n"
    "    assets for this agreement.\n"
    "3.2 Meridian retains service tickets for seven years.\n"
).encode("utf-8")

# ---------------------------------------------------------------------------
# Third-party sources
# ---------------------------------------------------------------------------

_JOB_POSTING_JSON = b"""{
  "job_id": "MMS-2026-0114",
  "employer": "Meridian Mechanical Services, Inc.",
  "title": "Service Coordinator",
  "location": "Columbus, OH",
  "posted_at": "2026-01-14",
  "employment_type": "FULL_TIME",
  "description": "The Service Coordinator receives incoming service requests, schedules technicians on the dispatch board, and follows up on open work orders.",
  "responsibilities": [
    "Dispatch technicians and maintain the daily schedule",
    "Re-key completed paper work orders into the accounting system",
    "Chase technicians for missing paperwork before invoicing",
    "Route quotes over threshold to the service manager for approval"
  ],
  "requirements": [
    "Experience with ServiceTitan or a comparable field service platform",
    "Proficiency with QuickBooks",
    "Two years of HVAC service coordination experience"
  ]
}"""

_REGISTRY_JSON = b"""{
  "jurisdiction": "OH",
  "entity_number": "0100123",
  "legal_name": "MERIDIAN MECHANICAL SERVICES INC",
  "status": "ACTIVE",
  "formation_date": "1998-06-22",
  "principal_address": {"city": "Columbus", "state": "OH"},
  "registered_agent": "C. OKONKWO",
  "trade_names": ["MERIDIAN MECHANICAL", "MERIDIAN MECHANICAL SERVICES"],
  "related_entities": [
    {"name": "MERIDIAN MECHANICAL HOLDINGS LLC", "relationship": "PARENT",
     "entity_number": "0200456"}
  ]
}"""

_DIRECTORY_BODY = """
<h1>Meridian Mechanical Services</h1>
<p>HVAC contractor in Columbus, OH.</p>
<p>Employees: approximately 25 technicians.</p>
<p>Locations: 2</p>
<p>Emergency service: not offered.</p>
<p>Listing last verified 2023-08-02.</p>
"""

_NEWS_BODY = """
<h1>Meridian Mechanical opens Cincinnati branch</h1>
<p>Meridian Mechanical Services has opened a fourth service location in
Cincinnati, the company said on Tuesday.</p>
<p>The company also said it is migrating from its legacy dispatch software to
ServiceTitan during 2026.</p>
"""

# ---------------------------------------------------------------------------
# The web
# ---------------------------------------------------------------------------

SERVICES_URL = "https://www.meridianmechanical.com/services"
PM_URL = "https://www.meridianmechanical.com/services/preventive-maintenance"
EMERGENCY_URL = "https://www.meridianmechanical.com/services/emergency"
ABOUT_MIRROR_URL = "https://meridian-mechanical.net/about"
JOB_URL = "https://hvacjobsboard.example/api/jobs/MMS-2026-0114"
REGISTRY_URL = "https://sos.ohio.example/api/entities/0100123"
DIRECTORY_URL = "https://contractordirectory.example/oh/meridian-mechanical"
PDF_URL = "https://www.meridianmechanical.com/docs/maintenance-agreement.pdf"
NEWS_URL = "https://tradepress.example/2026/meridian-cincinnati"
SPANISH_URL = "https://www.meridianmechanical.com/es/servicios"
REDIRECT_URL = "http://meridianmechanical.com/company"


def build_web() -> dict[str, FixtureResource]:
    """Every fixture address, keyed by its **normalized** locator."""
    raw: dict[str, FixtureResource] = {
        HOME: FixtureResource(
            body=_html(
                "About Meridian Mechanical Services", _ABOUT_BODY,
                published="2025-11-04",
            ),
            etag='"home-v1"',
            links=(SERVICES_URL, PM_URL, EMERGENCY_URL, PDF_URL, SPANISH_URL,
                   REDIRECT_URL),
        ),
        SERVICES_URL: FixtureResource(
            body=_html("Commercial Mechanical Services", _SERVICES_BODY_V1,
                       canonical=SERVICES_URL, published="2025-09-18"),
            changed_body=_html("Commercial Mechanical Services", _SERVICES_BODY_V2,
                               canonical=SERVICES_URL, published="2026-03-02"),
            etag='"services-v1"',
            links=(PM_URL, EMERGENCY_URL),
        ),
        PM_URL: FixtureResource(
            body=_html("Preventive Maintenance Agreements", _PM_BODY,
                       published="2025-09-18"),
            etag='"pm-v1"',
            last_modified="Thu, 18 Sep 2025 09:00:00 GMT",
        ),
        EMERGENCY_URL: FixtureResource(
            body=_html("24/7 Emergency Service", _EMERGENCY_BODY, published="2025-06-30"),
            etag='"emergency-v1"',
        ),
        SPANISH_URL: FixtureResource(
            body=_html(
                "Servicios Mecanicos Comerciales", _ES_BODY, lang="es",
                head_extra=(
                    f'<link rel="alternate" hreflang="en" href="{SERVICES_URL}">'
                ),
            ),
            etag='"es-v1"',
        ),
        # Same canonical content as the company's about page, published by the
        # same publisher on a second domain, and undated. It must converge on
        # one semantic artifact and must not read as a second voice.
        ABOUT_MIRROR_URL: FixtureResource(
            body=_html("About Meridian Mechanical Services", _ABOUT_BODY),
            etag='"mirror-v1"',
        ),
        PDF_URL: FixtureResource(
            body=_MAINTENANCE_PDF,
            declared_content_type="application/pdf",
            etag='"pdf-v1"',
        ),
        JOB_URL: FixtureResource(
            body=_JOB_POSTING_JSON,
            declared_content_type="application/json",
            etag='"job-v1"',
        ),
        REGISTRY_URL: FixtureResource(
            body=_REGISTRY_JSON,
            declared_content_type="application/json",
            etag='"registry-v1"',
        ),
        DIRECTORY_URL: FixtureResource(
            body=_html("Meridian Mechanical Services - Directory", _DIRECTORY_BODY),
            etag='"directory-v1"',
        ),
        NEWS_URL: FixtureResource(
            body=_html("Meridian Mechanical opens Cincinnati branch", _NEWS_BODY,
                       published="2026-02-03"),
            etag='"news-v1"',
        ),
        # --- the failure surface ------------------------------------------
        REDIRECT_URL: FixtureResource(
            outcome="OK", http_status=301, redirect_to=HOME, body=None,
        ),
        "https://www.meridianmechanical.com/services/controls-retrofit":
            FixtureResource(outcome="NOT_FOUND", http_status=404, body=None),
        "https://www.meridianmechanical.com/legacy/old-service-page":
            FixtureResource(outcome="GONE", http_status=410, body=None),
        "https://www.meridianmechanical.com/internal/dispatch-board":
            FixtureResource(outcome="ROBOTS_DENIED", http_status=None, body=None,
                            robots_disallowed=True),
        "https://www.meridianmechanical.com/customer-portal":
            FixtureResource(outcome="LOGIN_WALL", http_status=200, body=None),
        "https://contractordirectory.example/premium/meridian":
            FixtureResource(outcome="DENIED", http_status=403, body=None),
        "https://contractordirectory.example/slow/meridian":
            FixtureResource(outcome="TIMEOUT", http_status=None, body=None,
                            error_class="ReadTimeout"),
        "https://broken.example/meridian":
            FixtureResource(outcome="TRANSPORT_ERROR", http_status=None, body=None,
                            error_class="ConnectionResetError"),
        "https://www.meridianmechanical.com/media/facility-tour.mp4":
            FixtureResource(outcome="TOO_LARGE", http_status=200, body=None,
                            declared_content_type="video/mp4",
                            byte_limit_exceeded=True),
        "https://www.meridianmechanical.com/downloads/spec-sheet.pdf":
            FixtureResource(outcome="MIME_MISMATCH", http_status=200, body=None,
                            declared_content_type="application/pdf"),
    }
    return {normalize_locator(url): resource for url, resource in raw.items()}


#: Seeds a human hands the system. Everything else is discovered from these.
HUMAN_SEEDS: tuple[str, ...] = (HOME,)

#: What the fixture sitemap advertises.
SITEMAP: tuple[str, ...] = (
    HOME, SERVICES_URL, PM_URL, EMERGENCY_URL, SPANISH_URL, PDF_URL,
    "https://www.meridianmechanical.com/customer-portal",
    "https://www.meridianmechanical.com/legacy/old-service-page",
    "https://www.meridianmechanical.com/internal/dispatch-board",
    "https://www.meridianmechanical.com/media/facility-tour.mp4",
    "https://www.meridianmechanical.com/downloads/spec-sheet.pdf",
)

#: Two distinct queries returning one shared result, so "the same source found
#: twice" is a real case rather than a contrived duplicate insert.
SEARCH_RESULTS: dict[str, tuple[str, ...]] = {
    "meridian mechanical columbus ohio hvac": (
        DIRECTORY_URL, NEWS_URL, "https://contractordirectory.example/premium/meridian",
    ),
    "meridian mechanical emergency service": (DIRECTORY_URL, EMERGENCY_URL),
    "meridian mechanical controls retrofit": (
        "https://www.meridianmechanical.com/services/controls-retrofit",
        "https://contractordirectory.example/slow/meridian",
    ),
}

JOB_BOARD_RESULTS: tuple[str, ...] = (JOB_URL,)
REGISTRY_RESULTS: tuple[str, ...] = (REGISTRY_URL,)
API_RESULTS: tuple[str, ...] = ("https://broken.example/meridian",)

#: Addresses the crawler is told it may not visit.
ROBOTS_DISALLOWED: tuple[str, ...] = (
    "https://www.meridianmechanical.com/internal/dispatch-board",
)

#: A whitespace-and-comment edit: different bytes, identical canonical content.
def cosmetic_variant(body: bytes) -> bytes:
    return body.replace(b"<body>", b"<body>\n  <!-- rebuilt 2026-04-01 -->\n  ")
