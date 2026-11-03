"""The official Odoo partner directory: companies that already own Odoo client demand.

Listing pages `/partners/country/<slug>-<id>[/page/N]` and partner pages
`/partners/<slug>-<id>` are allowed by odoo.com's robots.txt. The `?country_id=`
links the listing renders are disallowed, so they are stripped before use.
"""

from __future__ import annotations

import re
from typing import Any

from boro_gtm.engine.sources.base import Fetcher, SignalDraft, SourcePage

BASE = "https://www.odoo.com"
COUNTRIES = {"CL": "chile-44", "PE": "peru-168", "CO": "colombia-47", "MX": "mexico-150",
             "AR": "argentina-11", "ES": "spain-67"}
_PARTNER = re.compile(r'href="(/partners/(?!country/)[a-z0-9-]+-\d+)(?:\?[^"]*)?"')
_PAGES = re.compile(r'href="/partners/country/[a-z0-9-]+/page/(\d+)"')
_ITEMPROP_WEBSITE = re.compile(
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>\s*<span itemprop="website"', re.I)
_ITEMPROP_EMAIL = re.compile(r'itemprop="email"[^>]*>\s*([^<\s]+@[^<\s]+)\s*<', re.I)
_TITLE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_GRADE = re.compile(r"(Gold|Silver|Ready|Learning) Partner", re.I)
_EXCLUDED_HOSTS = ("odoo.com", "odoo.sh", "facebook.com", "linkedin.com", "twitter.com",
                   "x.com", "instagram.com", "youtube.com", "google.com", "apple.com",
                   "github.com", "wa.me", "whatsapp.com")


class OdooPartnerSource:
    lane = "PARTNER_DELIVERY"
    family = "partner_directory"

    def __init__(self, country: str) -> None:
        self.country = country
        self.slug = COUNTRIES[country]
        self.key = f"odoo_partners_{country.lower()}"

    def next_page(self, fetcher: Fetcher, cursor: dict[str, Any]) -> SourcePage:
        page = int(cursor.get("page", 1))
        url = f"{BASE}/partners/country/{self.slug}" + (f"/page/{page}" if page > 1 else "")
        listing = fetcher.get(url)
        info = {"country": self.country, "page": page, "url": url}
        if listing.outcome != "OK" or not listing.body:
            return SourcePage([], info, cursor, exhausted=False,
                              error=f"{listing.outcome} {listing.http_status}")
        html = listing.body.decode("utf-8", "replace")
        partners = sorted(set(_PARTNER.findall(html)))
        last = max([int(p) for p in _PAGES.findall(html)] + [page])
        items = []
        fetched = 1
        for path in partners:
            detail = fetcher.get(BASE + path)
            fetched += 1
            items.append(self._draft(path, detail.body.decode("utf-8", "replace")
                                     if detail.outcome == "OK" and detail.body else ""))
        exhausted = page >= last or not partners
        return SourcePage(items, info, {"page": page + 1}, exhausted=exhausted,
                          pages_fetched=fetched)

    def _draft(self, path: str, html: str) -> SignalDraft:
        name_match = _TITLE.search(html)
        name = re.sub(r"<[^>]+>|\s+", " ", name_match.group(1)).strip() if name_match else None
        # The partner's own fields, as the directory marks them up. The first
        # external link on the page is a font CDN, which is not anyone's site.
        site = _ITEMPROP_WEBSITE.search(html)
        website = site.group(1) if site else None
        if website:
            host = re.sub(r"^https?://(www\.)?", "", website).split("/")[0].lower()
            if any(host == h or host.endswith("." + h) for h in _EXCLUDED_HOSTS):
                website = None
        mail = _ITEMPROP_EMAIL.search(html)
        published_email = mail.group(1).strip().lower() if mail else None
        grade = _GRADE.search(html)
        level = grade.group(0) if grade else "Odoo Partner"
        country_es = {"CL": "Chile", "PE": "Perú", "CO": "Colombia", "MX": "México",
                      "AR": "Argentina", "ES": "España"}[self.country]
        excerpt = (f"{name}: {level}, Odoo implementation partner listed for {country_es}. "
                   "Odoo ERP.")
        evidence_es = (f"aparece como {level} en el directorio oficial de Odoo para "
                       f"{country_es}")
        evidence_en = f"is listed as an {level} in Odoo's official partner directory"
        return SignalDraft(
            external_id=path, kind="PARTNER_LISTING", lane=self.lane, url=BASE + path,
            title=f"{level} — Odoo partner directory ({self.country})", excerpt=excerpt,
            company_name=name, company_domain=website, country=self.country,
            attributes={"segment": "odoo_partner", "grade": level,
                        "published_email": published_email,
                        "evidence_es": evidence_es, "evidence_en": evidence_en},
            reject_reason=None if website else "no_company_website_on_listing",
        )
