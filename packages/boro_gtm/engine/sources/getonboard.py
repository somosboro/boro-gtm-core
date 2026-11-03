"""Get on Board public jobs API: contractor and freelance demand in LatAm tech.

`robots.txt` allows `*` with `Content-Signal: search=yes, ai-train=no`. The
engine indexes postings to respond to them; it trains nothing.

Only postings that ask for project-based or freelance work become EXPLICIT
DEMAND. A full-time hire is an employment ad, not a vendor opportunity, and is
counted as rejected rather than stretched into a "trigger".
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import quote

from boro_gtm.engine.scoring import DELIVERY_TITLE
from boro_gtm.engine.sources.base import Fetcher, SignalDraft, SourcePage

API = "https://www.getonbrd.com/api/v0/search/jobs"
QUERIES = ("odoo", "integraciones", "backend freelance", "nestjs", "proyecto desarrollo",
           "desarrollador freelance", "react native", "erp", "api rest", "consultor odoo")
FREELANCE_MODALITY_IDS = {"3"}
_PROJECT_TERMS = re.compile(r"(freelance|por proyecto|proyecto (puntual|acotado)|contrato a plazo"
                            r"|contractor|project.based|honorarios|consultor[ií]a externa)", re.I)
COUNTRY_CODES = {"Chile": "CL", "Perú": "PE", "Peru": "PE", "Colombia": "CO", "México": "MX",
                 "Mexico": "MX", "Argentina": "AR", "Uruguay": "UY", "Ecuador": "EC",
                 "España": "ES", "Spain": "ES"}
#: Postings live about a month; a deadline makes follow-ups land before it closes.
DEFAULT_LIFETIME = timedelta(days=30)


def _text(html: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


class GetOnBoardSource:
    key = "getonboard"
    lane = "EXPLICIT_DEMAND"
    family = "explicit_demand"

    def next_page(self, fetcher: Fetcher, cursor: dict[str, Any]) -> SourcePage:
        index = int(cursor.get("query_index", 0))
        page = int(cursor.get("page", 1))
        if index >= len(QUERIES):
            return SourcePage([], {"query": None}, cursor, exhausted=True, pages_fetched=0)
        query = QUERIES[index]
        url = (f"{API}?query={quote(query)}&per_page=50&page={page}"
               "&expand=%5B%22company%22%5D")
        result = fetcher.get(url)
        info = {"query": query, "page": page, "url": url}
        if result.outcome != "OK" or not result.body:
            return SourcePage([], info, {"query_index": index + 1, "page": 1}, exhausted=False,
                              error=f"{result.outcome} {result.http_status}")
        payload = json.loads(result.body)
        total = int((payload.get("meta") or {}).get("total_pages", 1))
        items = [self._draft(job) for job in payload.get("data", [])]
        after = ({"query_index": index, "page": page + 1} if page < total
                 else {"query_index": index + 1, "page": 1})
        return SourcePage(items, info, after,
                          exhausted=(after["query_index"] >= len(QUERIES)))

    def _draft(self, job: dict[str, Any]) -> SignalDraft:
        a = job.get("attributes", {})
        company = ((a.get("company") or {}).get("data") or {}).get("attributes", {})
        modality = str(((a.get("modality") or {}).get("data") or {}).get("id", ""))
        description = _text(" ".join(str(a.get(k) or "") for k in
                                     ("description", "projects", "functions", "desirable")))
        countries = a.get("countries") or []
        country = COUNTRY_CODES.get(countries[0]) if countries else company.get("country")
        published = a.get("published_at")
        posted = datetime.fromtimestamp(published, UTC) if published else None
        title = a.get("title")
        project_based = modality in FREELANCE_MODALITY_IDS or bool(
            _PROJECT_TERMS.search(f"{title} {description}"))
        return SignalDraft(
            external_id=str(job.get("id")), kind="EXPLICIT_DEMAND", lane=self.lane,
            url=f"https://www.getonbrd.com/jobs/{job.get('id')}", title=title,
            excerpt=description[:1500], company_name=company.get("name"),
            company_domain=company.get("web"), country=(country or "").upper() or None,
            posted_at=posted, expires_at=(posted + DEFAULT_LIFETIME) if posted else None,
            attributes={"modality_id": modality, "segment": "job_board_project",
                        "evidence_es": f"publicó \"{title}\" en Get on Board",
                        "evidence_en": f"posted \"{title}\" on Get on Board"},
            reject_reason=("full_time_employment_not_vendor_demand" if not project_based else
                           None if DELIVERY_TITLE.search(title or "") else
                           "not_software_delivery"),
        )
