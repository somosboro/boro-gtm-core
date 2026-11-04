"""Hacker News "Who is hiring?" — contract and freelance roles (TRIGGER/OVERFLOW).

Read through the public Algolia HN API (hn.algolia.com/api), which exists for
exactly this. Each monthly thread's top-level comments are posts by the hiring
company itself (the thread's rules forbid recruiters). Only posts that offer
contract, contractor or freelance work are signals; a full-time hire is
rejected and counted.

Identity: the company's own site linked in the post, accepted only when its
domain carries the company's name, or the domain of an address the post
publishes. An address the post publishes for contractors is a legitimate
channel (role `contractor_recruiting`), because the poster asked to be written
to there.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import quote

from boro_gtm.engine.pipeline import CONTRACTOR_RECRUITING, account_key, normalize_email
from boro_gtm.engine.scoring import DELIVERY_TITLE
from boro_gtm.engine.sources.base import Fetcher, SignalDraft, SourcePage

API = "https://hn.algolia.com/api/v1"
THREAD_TITLE = re.compile(r"^Ask HN: Who is hiring\? \(", re.I)
CONTRACT = re.compile(r"\b(contract(or|ors)?|freelanc\w*|1099|fractional|part[- ]time contract"
                      r"|project[- ]based|consultants? wanted)\b", re.I)
_URL = re.compile(r'href="(https?://[^"]+)"')
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_NOT_COMPANY = ("ycombinator.com", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
                "linkedin.com", "github.com", "notion.site", "notion.so", "google.com",
                "forms.gle", "typeform.com", "wellfound.com", "angel.co", "breezy.hr",
                "bamboohr.com", "recruitee.com", "jobvite.com", "smartrecruiters.com",
                "calendly.com", "docs.google.com", "airtable.com", "rippling.com")
COUNTRIES = [(re.compile(p, re.I), c) for p, c in (
    (r"\b(US|USA|U\.S\.|United States|NYC|New York|San Francisco|SF Bay|Bay Area|Seattle"
     r"|Boston|Austin|Chicago|Los Angeles|Denver|REMOTE \(US)", "US"),
    (r"\b(UK|United Kingdom|London)\b", "GB"), (r"\b(Canada|Toronto|Vancouver)\b", "CA"),
    (r"\b(Germany|Berlin|Munich)\b", "DE"), (r"\b(Spain|Madrid|Barcelona)\b", "ES"),
    (r"\b(Chile|Santiago)\b", "CL"), (r"\b(Mexico|México|CDMX)\b", "MX"),
    (r"\b(Colombia|Bogot[aá]|Medell[ií]n)\b", "CO"), (r"\b(Argentina|Buenos Aires)\b", "AR"),
    (r"\b(Peru|Perú|Lima)\b", "PE"), (r"\b(Uruguay|Montevideo)\b", "UY"))]
THREAD_LIFETIME = timedelta(days=35)


def _plain(html: str) -> str:
    text = re.sub(r"<p>", "\n", html or "")
    return unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def _tokens(name: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(w) >= 4}


def _names_domain(company: str, domain: str) -> bool:
    """Does the domain carry the company's name (`Acme Robotics` → acmerobotics.com)?"""
    label = domain.split(".")[0]
    joined = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    return bool(joined) and (label == joined or any(t in label for t in _tokens(company)))


class HnHiringSource:
    key = "hn_who_is_hiring"
    lane = "TRIGGER_OVERFLOW"
    family = "public_hiring_thread"

    def next_page(self, fetcher: Fetcher, cursor: dict[str, Any]) -> SourcePage:
        story = cursor.get("story_id")
        if story is None:
            found = fetcher.get(f"{API}/search_by_date?tags=story,author_whoishiring"
                                "&hitsPerPage=10")
            if found.outcome != "OK" or not found.body:
                return SourcePage([], {"step": "find_thread"}, cursor, exhausted=False,
                                  error=f"{found.outcome} {found.http_status}")
            hits = [h for h in json.loads(found.body).get("hits", [])
                    if THREAD_TITLE.search(h.get("title") or "")]
            if not hits:
                return SourcePage([], {"step": "find_thread"}, cursor, exhausted=True,
                                  pages_fetched=1)
            story = hits[0]["objectID"]
            cursor = {"story_id": story, "story_created_at": hits[0].get("created_at"),
                      "page": 0}
        page = int(cursor.get("page", 0))
        url = (f"{API}/search?tags=comment,story_{quote(str(story))}"
               f"&hitsPerPage=100&page={page}")
        result = fetcher.get(url)
        info = {"story_id": story, "page": page, "url": url}
        if result.outcome != "OK" or not result.body:
            return SourcePage([], info, cursor, exhausted=False,
                              error=f"{result.outcome} {result.http_status}")
        payload = json.loads(result.body)
        top = [h for h in payload.get("hits", []) if str(h.get("parent_id")) == str(story)]
        thread_at = cursor.get("story_created_at")
        items = [self._draft(h, thread_at) for h in top]
        last = page + 1 >= int(payload.get("nbPages", 1))
        return SourcePage(items, info, {**cursor, "page": page + 1}, exhausted=last)

    def _draft(self, hit: dict[str, Any], thread_at: str | None) -> SignalDraft:
        html = hit.get("comment_text") or ""
        text = _plain(html)
        head = text.splitlines()[0] if text else ""
        company = re.sub(r"\(\s*https?://[^)]*\)|https?://\S+", "",
                         head.split("|")[0]).strip(" -–(")[:120] or None
        domain = None
        for link in _URL.findall(html):
            host = re.sub(r"^https?://", "", unescape(link)).split("/")[0].lower()
            key = account_key(host)
            if key and not any(key == n or key.endswith("." + n) for n in _NOT_COMPANY) and (
                    _names_domain(company or "", key)):
                domain = key
                break
        emails = [e for e in (normalize_email(m) for m in _EMAIL.findall(text)) if e]
        email = next((e for e in emails if domain and e.endswith("@" + domain)), None)
        if domain is None and emails:
            own = account_key(emails[0].split("@")[1])
            if own and _names_domain(company or "", own):
                domain, email = own, emails[0]
        country = next((c for p, c in COUNTRIES if p.search(head)), None)
        created = hit.get("created_at")
        posted = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
        start = (datetime.fromisoformat(thread_at.replace("Z", "+00:00"))
                 if thread_at else posted)
        contract = CONTRACT.search(text)
        reject = ("full_time_employment_not_vendor_demand" if not contract else
                  "not_software_delivery" if not DELIVERY_TITLE.search(
                      "|".join(head.split("|")[1:])) else
                  "no_company_site_in_post" if domain is None else None)
        return SignalDraft(
            external_id=str(hit.get("objectID")), kind="TRIGGER", lane=self.lane,
            url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            title=head[:200] or None, excerpt=text[:1500], company_name=company,
            company_domain=domain, country=country, posted_at=posted,
            expires_at=(start + THREAD_LIFETIME) if start else None,
            attributes={"segment": "contract_hiring_post", "signal_timing": "CURRENT",
                        "published_email": email,
                        "contact_role": CONTRACTOR_RECRUITING if email else None,
                        "evidence_es": "publicó en el hilo \"Who is hiring\" de Hacker News "
                                       "que busca desarrolladores por contrato o freelance",
                        "evidence_en": "posted in Hacker News' \"Who is hiring\" thread that it "
                                       "is looking for contract or freelance engineers"},
            reject_reason=reject,
        )
