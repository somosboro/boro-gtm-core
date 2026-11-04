"""The official Odoo partner directory: companies that already own Odoo client demand.

Listing pages `/partners/country/<slug>-<id>[/page/N]` and partner pages
`/partners/<slug>-<id>` are allowed by odoo.com's robots.txt. The `?country_id=`
links the listing renders are disallowed, so they are stripped before use.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from boro_gtm.engine.pipeline import account_key
from boro_gtm.engine.sources.base import Fetcher, SignalDraft, SourcePage

BASE = "https://www.odoo.com"
COUNTRIES = {"CL": "chile-44", "PE": "peru-168", "CO": "colombia-47", "MX": "mexico-150",
             "AR": "argentina-11", "ES": "spain-67", "UY": "uruguay-225", "EC": "ecuador-62",
             "BO": "bolivia-28", "PY": "paraguay-179", "CR": "costa-rica-48",
             "PA": "panama-167", "GT": "guatemala-88", "DO": "dominican-republic-60"}
COUNTRY_NAMES_ES = {"CL": "Chile", "PE": "Perú", "CO": "Colombia", "MX": "México",
                    "AR": "Argentina", "ES": "España", "UY": "Uruguay", "EC": "Ecuador",
                    "BO": "Bolivia", "PY": "Paraguay", "CR": "Costa Rica", "PA": "Panamá",
                    "GT": "Guatemala", "DO": "República Dominicana"}
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


_BADGE = re.compile(r"\s+(Gold|Silver|Ready|Learning)\s*$", re.I)


def clean_partner_name(raw: str) -> tuple[str | None, str | None]:
    """'C&amp;M Consultoría SpA   Ready' → ('C&M Consultoría SpA', 'Ready').

    The directory's title carries the partner level as a badge after the name.
    """
    text = re.sub(r"\s+", " ", unescape(raw or "")).strip()
    badge = _BADGE.search(text)
    if badge:
        text = text[:badge.start()].strip()
    return (text or None), (badge.group(1).title() if badge else None)


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
        raw = re.sub(r"<[^>]+>|\s+", " ", name_match.group(1)).strip() if name_match else ""
        name, badge = clean_partner_name(raw)
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
        identity_from = "directory_website" if website else None
        if website is None and published_email and "@" in published_email:
            # No website on the listing, but the partner published an address on
            # its own (non-freemail) domain: that domain is the partner's.
            own = account_key(published_email.rsplit("@", 1)[1])
            if own:
                website, identity_from = own, "directory_email_domain"
        grade = _GRADE.search(html)
        level = (f"{badge} Partner" if badge else grade.group(0) if grade else "Odoo Partner")
        country_es = COUNTRY_NAMES_ES[self.country]
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
                        "identity_from": identity_from,
                        "published_email": published_email,
                        "evidence_es": evidence_es, "evidence_en": evidence_en},
            reject_reason=None if website else "no_company_website_on_listing",
        )
