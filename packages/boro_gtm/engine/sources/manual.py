"""Sources the engine deliberately does not crawl (MANUAL_SOURCE).

Each was audited (docs/audits/SOURCE_COVERAGE_2026-11.md). Some forbid
automated access or sit behind login walls and anti-bot checks; some answer
only through a platform application that a person must write. They are listed
in every ladder report so a short day is never mistaken for an empty market.
"""

from __future__ import annotations

MANUAL_SOURCES: tuple[dict[str, str], ...] = (
    {"key": "upwork", "lane": "EXPLICIT_DEMAND", "family": "freelance_marketplace",
     "reason": "Terms forbid scraping; job search paths are disallowed in robots.txt; the "
               "only channel is a proposal written on the platform.",
     "workflow": "Daily: Amaru reviews Upwork saved searches and applies by hand; imported "
                 "pools keep these rows as MANUAL_APPLICATION (`gtm engine manual`)."},
    {"key": "freelancer_com", "lane": "EXPLICIT_DEMAND", "family": "freelance_marketplace",
     "reason": "A public projects API exists, but the buyer is anonymous and the only channel "
               "is a bid on the platform: nothing the engine may email.",
     "workflow": "Optional manual bidding; not an email lane."},
    {"key": "workana", "lane": "EXPLICIT_DEMAND", "family": "freelance_marketplace",
     "reason": "Anonymous buyers; proposals only through the platform.",
     "workflow": "Manual proposals."},
    {"key": "linkedin", "lane": "TRIGGER_OVERFLOW", "family": "professional_network",
     "reason": "Login wall and anti-bot measures; the user agreement forbids automated access.",
     "workflow": "Manual review; a company found there enters through `import-pool` with its "
                 "own website and a published address."},
    {"key": "indeed", "lane": "TRIGGER_OVERFLOW", "family": "job_board",
     "reason": "Anti-bot protection and terms forbid scraping.",
     "workflow": "Manual review; import with the company's own site."},
    {"key": "clutch", "lane": "PARTNER_DELIVERY", "family": "agency_directory",
     "reason": "Behind a Cloudflare challenge (HTTP 403 even for robots.txt).",
     "workflow": "Manual research; import."},
    {"key": "hubspot_shopify_partner_directories", "lane": "PARTNER_DELIVERY",
     "family": "partner_directory",
     "reason": "Client-rendered applications with no public listing API.",
     "workflow": "Manual research; import."},
    {"key": "mercado_publico", "lane": "EXPLICIT_DEMAND", "family": "public_procurement",
     "reason": "Official API needs a free ticket (not yet issued), and tenders are answered "
               "through the portal, not by email to the buyer.",
     "workflow": "Once a ticket exists, an adapter can list tenders for Amaru to bid on."},
)
