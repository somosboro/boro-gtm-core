"""Two more readings of Get on Board's public API (robots: search=yes, ai-train=no).

* `GetOnBoardHiringSource` — TRIGGER/OVERFLOW. A software delivery firm hiring
  engineers is a firm whose client work is outgrowing its team: capacity
  demand BoRo can absorb. Only firms that describe client delivery count; a
  product company hiring for itself is rejected, and so is a non-software role.
* `GetOnBoardCompaniesSource` — PARTNER DELIVERY. The company directory, read
  for firms that sell software delivery to clients and publish their own site.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from boro_gtm.engine.scoring import DELIVERY_TITLE
from boro_gtm.engine.sources.base import Fetcher, SignalDraft, SourcePage
from boro_gtm.engine.sources.delivery import delivery_evidence
from boro_gtm.engine.sources.getonboard import COUNTRY_CODES, _text

API = "https://www.getonbrd.com/api/v0"
CATEGORIES = ("programming", "mobile-developer", "sysadmin-devops-qa")
#: A hiring post is a current capacity signal for about a month.
HIRING_LIFETIME = timedelta(days=30)


class GetOnBoardHiringSource:
    key = "getonboard_hiring"
    lane = "TRIGGER_OVERFLOW"
    family = "hiring_trigger"

    def next_page(self, fetcher: Fetcher, cursor: dict[str, Any]) -> SourcePage:
        index = int(cursor.get("category_index", 0))
        page = int(cursor.get("page", 1))
        if index >= len(CATEGORIES):
            return SourcePage([], {"category": None}, cursor, exhausted=True, pages_fetched=0)
        category = CATEGORIES[index]
        url = (f"{API}/categories/{category}/jobs?per_page=50&page={page}"
               "&expand=%5B%22company%22%5D")
        result = fetcher.get(url)
        info = {"category": category, "page": page, "url": url}
        if result.outcome != "OK" or not result.body:
            return SourcePage([], info, {"category_index": index + 1, "page": 1},
                              exhausted=False, error=f"{result.outcome} {result.http_status}")
        payload = json.loads(result.body)
        total = int((payload.get("meta") or {}).get("total_pages", 1))
        items = [self._draft(job) for job in payload.get("data", [])]
        after = ({"category_index": index, "page": page + 1} if page < total
                 else {"category_index": index + 1, "page": 1})
        return SourcePage(items, info, after,
                          exhausted=after["category_index"] >= len(CATEGORIES))

    def _draft(self, job: dict[str, Any]) -> SignalDraft:
        a = job.get("attributes", {})
        company = ((a.get("company") or {}).get("data") or {}).get("attributes", {})
        about = _text(" ".join(str(company.get(k) or "") for k in
                               ("description", "long_description", "projects")))
        posting = _text(" ".join(str(a.get(k) or "") for k in ("description", "projects")))
        evidence = delivery_evidence(f"{about} {posting}")
        title = a.get("title") or ""
        countries = a.get("countries") or []
        country = COUNTRY_CODES.get(countries[0]) if countries else company.get("country")
        published = a.get("published_at")
        posted = datetime.fromtimestamp(published, UTC) if published else None
        name = company.get("name")
        reject = (None if evidence and DELIVERY_TITLE.search(title) else
                  "not_software_delivery" if evidence else "not_a_delivery_firm")
        return SignalDraft(
            external_id=str(job.get("id")), kind="TRIGGER", lane=self.lane,
            url=f"https://www.getonbrd.com/jobs/{job.get('id')}", title=title,
            excerpt=f"{about[:700]} || {posting[:700]}", company_name=name,
            company_domain=company.get("web"), country=(country or "").upper() or None,
            posted_at=posted, expires_at=(posted + HIRING_LIFETIME) if posted else None,
            attributes={"segment": "delivery_firm_hiring", "signal_timing": "CURRENT",
                        "delivery_evidence": evidence,
                        "evidence_es": f"está contratando \"{title}\" en Get on Board",
                        "evidence_en": f"is hiring a \"{title}\" on Get on Board"},
            reject_reason=reject,
        )


class GetOnBoardCompaniesSource:
    key = "getonboard_companies"
    lane = "PARTNER_DELIVERY"
    family = "tech_company_directory"

    def next_page(self, fetcher: Fetcher, cursor: dict[str, Any]) -> SourcePage:
        page = int(cursor.get("page", 1))
        url = f"{API}/companies?per_page=100&page={page}"
        result = fetcher.get(url)
        info = {"page": page, "url": url}
        if result.outcome != "OK" or not result.body:
            return SourcePage([], info, cursor, exhausted=False,
                              error=f"{result.outcome} {result.http_status}")
        payload = json.loads(result.body)
        total = int((payload.get("meta") or {}).get("total_pages", 1))
        items = [self._draft(c) for c in payload.get("data", [])]
        return SourcePage(items, info, {"page": page + 1}, exhausted=page >= total)

    def _draft(self, company: dict[str, Any]) -> SignalDraft:
        c = company.get("attributes", {})
        about = _text(" ".join(str(c.get(k) or "") for k in
                               ("description", "long_description", "projects")))
        evidence = delivery_evidence(about)
        web = c.get("web") or None
        slug = company.get("id")
        reject = ("no_company_description" if not about else
                  "not_a_delivery_firm" if not evidence else
                  "no_company_website" if not web else None)
        return SignalDraft(
            external_id=str(slug), kind="PARTNER_LISTING", lane=self.lane,
            url=f"https://www.getonbrd.com/companies/{slug}",
            title="Software delivery firm — Get on Board company profile",
            excerpt=about[:1500], company_name=c.get("name"), company_domain=web,
            country=(c.get("country") or "").upper()[:2] or None,
            attributes={"segment": "delivery_firm_directory", "delivery_evidence": evidence,
                        "evidence_es": "desarrolla software para clientes, según su perfil "
                                       "en Get on Board",
                        "evidence_en": "builds software for clients, according to its Get on "
                                       "Board profile"},
            reject_reason=reject,
        )
