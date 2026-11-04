"""Import an externally produced pool (CSV or XLSX), idempotently.

Rows become IMPORT signals keyed by (file name, row identity), so re-importing
the same file creates nothing. Nothing in the file is trusted: its READY flag
and score are kept for comparison only, and every row goes through the same
identity, contact, suppression and Gmail-history checks as a sourced one.

Identity comes from, in order: a domain column, the contact address's domain,
the source URL when it is the company's own site, or an exact company-name
match with an account the engine already knows (for instance from the Odoo
directory). A row with none of these is not given a guessed domain: an
application through a platform (Upwork, LinkedIn, an ATS) is kept as a MANUAL
application, anything else waits for enrichment.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine.models import CeContact, CeSignal, CeTarget
from boro_gtm.engine.pipeline import (
    account_key,
    email_domain,
    get_or_create_account,
    is_role_mailbox,
    normalize_email,
    now_utc,
    same_company_domain,
    target_from_signal,
)
from boro_gtm.engine.states import LANES
from boro_gtm.engine.strategy import Strategy

ALIASES = {
    "company": ("company", "company_name", "empresa", "account", "company / opportunity"),
    "domain": ("domain", "website", "web", "sitio", "canonical_domain"),
    "lane": ("lane", "carril", "canal", "type"),
    "country": ("country", "pais", "país"),
    "email": ("email", "contact_email", "correo", "contact / apply"),
    "contact_name": ("contact_name", "name", "nombre", "buyer"),
    "role": ("role", "cargo", "title"),
    "signal_url": ("signal_url", "source_url", "source", "evidence_url", "fuente", "url"),
    "signal": ("signal", "evidence", "evidencia", "trigger", "reason"),
    "email_source_url": ("email_source_url", "contact_source", "email_source"),
    "readiness": ("readiness", "ready"),
    "score": ("score", "puntaje"),
    "row_id": ("id", "row_id"),
}
LANE_ALIASES = {"partner": "PARTNER_DELIVERY", "partner delivery": "PARTNER_DELIVERY",
                "explicit": "EXPLICIT_DEMAND", "explicit demand": "EXPLICIT_DEMAND",
                "trigger": "TRIGGER_OVERFLOW", "overflow": "TRIGGER_OVERFLOW",
                "trigger / overflow": "TRIGGER_OVERFLOW", "trigger/overflow": "TRIGGER_OVERFLOW"}
#: Where a row was *found*, never whose company it is.
SOURCE_PLATFORMS = ("odoo.com", "upwork.com", "linkedin.com", "getonbrd.com", "getonbrd.cl",
                    "indeed.com", "ashbyhq.com", "recruitee.com", "lever.co", "greenhouse.io",
                    "workable.com", "freelancer.com", "workana.com", "odoo.sh")
#: A channel that is an application through someone else's platform.
_PLATFORM_CHANNEL = re.compile(r"(upwork|linkedin|get on board|getonbrd|ashby|recruitee|indeed"
                               r"|application|proposal|apply|postula|formulario|form)", re.I)
_HISTORIC = re.compile(r"(historic|hist[oó]ric|past|previous|anterior|used to|solía)", re.I)
_LEGAL = {"spa", "sac", "sas", "ltda", "limitada", "eirl", "sa", "s", "a", "c", "llc", "inc",
          "srl", "ltd", "cia", "y", "de", "cv", "sl", "slu", "corp"}
COUNTRY_CODES = {"chile": "CL", "peru": "PE", "perú": "PE", "colombia": "CO", "mexico": "MX",
                 "méxico": "MX", "argentina": "AR", "uruguay": "UY", "ecuador": "EC",
                 "bolivia": "BO", "paraguay": "PY", "venezuela": "VE", "spain": "ES",
                 "españa": "ES", "australia": "AU", "united states": "US", "usa": "US"}


def read_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=True)
        # The first sheet is the pool; later sheets (feeds, dashboards) are not rows.
        rows = list(book.worksheets[0].iter_rows(values_only=True))
        header = [str(h or "").strip() for h in rows[0]]
        return [{header[i]: "" if v is None else str(v) for i, v in enumerate(r)
                 if i < len(header)} for r in rows[1:] if any(v is not None for v in r)]
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _pick(row: dict[str, str], field: str) -> str:
    lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    for alias in ALIASES[field]:
        value = lowered.get(alias)
        if value and value.lower() != "none":
            return value
    return ""


def _lane(value: str) -> str | None:
    v = value.strip()
    if v.upper() in LANES:
        return v.upper()
    return LANE_ALIASES.get(v.lower())


def normalize_name(name: str | None) -> str:
    """'Blanco Martín & Asociados SpA' → 'blanco martin asociados'."""
    if not name:
        return ""
    plain = unicodedata.normalize("NFKD", name.split("|")[0].split("—")[0])
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", plain) if w and w not in _LEGAL]
    return " ".join(words)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_platform(domain: str) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in SOURCE_PLATFORMS)


def _country(value: str) -> str | None:
    head = value.split("/")[0].strip().lower()
    if len(head) == 2 and head.isalpha():
        return head.upper()
    return COUNTRY_CODES.get(head)


def _known_domain_by_name(session: Session, name: str) -> str | None:
    """The domain of the one account the engine already knows under this name."""
    wanted = normalize_name(name)
    if not wanted:
        return None
    found: set[str] = set()
    for company, domain in session.execute(
            select(CeSignal.company_name, CeSignal.company_domain).where(
                CeSignal.company_domain.is_not(None), CeSignal.company_name.is_not(None))):
        have = normalize_name(company)
        if have and (have == wanted or have.startswith(wanted + " ")
                     or wanted.startswith(have + " ")):
            found.add(domain)
    return found.pop() if len(found) == 1 else None


def _resolve_domain(session: Session, row: dict[str, str], email: str | None
                    ) -> tuple[str | None, str]:
    explicit = account_key(_pick(row, "domain"))
    if explicit:
        return explicit, "domain_column"
    # A company the engine already knows keeps that identity: an address in
    # the pool on another domain (contacto@odoosistema.com for Consulnet,
    # consulnet.io) must not start a second account for the same company.
    by_name = _known_domain_by_name(session, _pick(row, "company"))
    if by_name:
        return by_name, "name_matches_known_account"
    if email:
        from_email = account_key(email_domain(email))
        if from_email and not _is_platform(from_email):
            return from_email, "contact_email_domain"
    url = _pick(row, "signal_url")
    if url.startswith("http"):
        host = _host(url)
        from_url = account_key(host)
        if from_url and not _is_platform(from_url):
            return from_url, "source_url_is_company_site"
    return None, "unresolved"


def _published_source(session: Session, email: str, domain: str) -> str | None:
    """Where the engine itself has seen this address published, if anywhere."""
    contact = session.scalar(select(CeContact).where(CeContact.email == email))
    if contact is not None and contact.source_url:
        return contact.source_url
    for url, attrs in session.execute(select(CeSignal.url, CeSignal.attributes).where(
            CeSignal.company_domain == domain)):
        if url and normalize_email((attrs or {}).get("published_email")) == email:
            return url
    return None


def _evidence(url: str, text: str) -> tuple[str, str]:
    """A sentence a message can carry, from what the row's source verifiably shows.

    The pool's own notes ("Gold; 78 refs; …") are analyst shorthand, not
    something to quote to the company.
    """
    if "odoo.com/partners" in url:
        grade = re.search(r"\b(Gold|Silver|Ready)\b", text)
        level = f"{grade.group(1)} Partner" if grade else "partner"
        return (f"aparece como {level} en el directorio oficial de Odoo",
                f"is listed as an Odoo {level} in Odoo's official directory")
    if "getonbrd." in url:
        return ("publicó búsquedas de desarrolladores en Get on Board",
                "posted developer openings on Get on Board")
    return ("publicó una búsqueda de perfiles técnicos", "posted an opening for technical profiles")


def _kind(lane: str, url: str, text: str) -> tuple[str, str]:
    if lane == "PARTNER_DELIVERY":
        return ("PARTNER_LISTING" if "odoo.com/partners" in url else "IMPORT"), "STANDING"
    if lane == "EXPLICIT_DEMAND":
        return "EXPLICIT_DEMAND", "CURRENT_UNVERIFIED_DATE"
    if _HISTORIC.search(text):
        return "HISTORIC_TRIGGER", "HISTORIC_SIGNAL"
    return "TRIGGER", "CURRENT_UNVERIFIED_DATE"


def import_pool(session: Session, strategy: Strategy, path: Path) -> dict[str, Any]:
    rows = read_rows(path)
    key = f"import:{path.stem}"[:64]
    report: dict[str, Any] = {"file": path.name, "source_key": key, "rows_read": len(rows),
                              "created": 0, "attached_to_existing": 0, "reopened": 0,
                              "duplicates": 0, "manual_application": 0,
                              "needs_enrichment_no_domain": 0, "expired": 0,
                              "contacts_attached": 0, "rejected": [], "rows": []}
    reasons: Counter[str] = Counter()
    for number, row in enumerate(rows, start=2):
        company = _pick(row, "company")
        lane = _lane(_pick(row, "lane"))
        line = {"row": number, "company": company, "lane": lane}
        report["rows"].append(line)
        if not company:
            line["outcome"] = "rejected:no_company"
            report["rejected"].append({"row": number, "reason": "no_company"})
            continue
        if lane is None:
            line["outcome"] = "rejected:unknown_lane"
            report["rejected"].append({"row": number, "reason": "unknown_lane"})
            continue
        url = _pick(row, "signal_url")
        signal_text = _pick(row, "signal")
        external = hashlib.sha256(
            f"{lane}|{normalize_name(company)}|{url}".encode()).hexdigest()[:32]
        channel = _pick(row, "email")
        email = normalize_email(channel)
        earlier = session.scalar(select(CeSignal).where(CeSignal.source_key == key,
                                                        CeSignal.external_id == external))
        if earlier is not None:
            domain, how = (None, "unresolved")
            if earlier.company_domain is None and (earlier.attributes or {}).get(
                    "channel") != "PLATFORM_APPLICATION":
                # Identity may be known now (e.g. the directory was crawled since).
                domain, how = _resolve_domain(session, row, email)
            if domain is None:
                report["duplicates"] += 1
                line["outcome"] = "duplicate_of_earlier_import"
                continue
            earlier.company_domain = domain
            earlier.attributes = {**earlier.attributes, "identity_from": how}
            report["resolved_on_reimport"] = report.get("resolved_on_reimport", 0) + 1
            signal = earlier
        else:
            signal = None
            domain, how = _resolve_domain(session, row, email)
        kind, timing = _kind(lane, url, signal_text)
        manual = domain is None and bool(channel) and email is None and bool(
            _PLATFORM_CHANNEL.search(channel))
        signal = signal or CeSignal(
            source_key=key, external_id=external, kind=kind, lane=lane,
            url=url or None, title=signal_text[:200] or None, excerpt=signal_text or None,
            company_name=company, company_domain=domain,
            country=_country(_pick(row, "country")), observed_at=now_utc(),
            attributes={"segment": "imported_pool", "import_row": number,
                        "pool_id": _pick(row, "row_id"),
                        "pool_readiness": _pick(row, "readiness") or None,
                        "pool_score": _pick(row, "score") or None,
                        "pool_channel": channel or None, "identity_from": how,
                        "signal_timing": timing,
                        "channel": "PLATFORM_APPLICATION" if manual else "EMAIL",
                        "pool_signal": signal_text,
                        "evidence_es": _evidence(url, signal_text)[0],
                        "evidence_en": _evidence(url, signal_text)[1]},
        )
        session.add(signal)
        session.flush()
        line["domain"], line["identity_from"] = domain, how
        if domain is None:
            if manual:
                report["manual_application"] += 1
                line["outcome"] = "manual_application"
            else:
                report["needs_enrichment_no_domain"] += 1
                line["outcome"] = "needs_enrichment_no_domain"
            continue
        target, outcome = target_from_signal(session, strategy, signal, actor="import")
        line["outcome"] = outcome.lower()
        if outcome == "CREATED":
            report["created"] += 1
        elif outcome == "REOPENED":
            report["reopened"] += 1
        elif outcome == "ATTACHED":
            report["attached_to_existing"] += 1
        elif outcome == "EXPIRED":
            report["expired"] += 1
        if outcome not in ("CREATED", "REOPENED") or not email:
            continue
        if not same_company_domain(email, domain):
            reasons["contact_not_on_company_domain"] += 1
            line["contact"] = "not_on_company_domain"
            continue
        if is_role_mailbox(email):
            # e.g. soporte@: kept out; enrichment looks for a commercial channel.
            reasons["contact_is_role_mailbox"] += 1
            line["contact"] = "role_mailbox_rejected"
            continue
        source = _pick(row, "email_source_url") or _published_source(session, email, domain)
        if not source:
            # An address with no page it was published on is not attached;
            # enrichment looks for a published one instead.
            reasons["contact_without_public_source"] += 1
            line["contact"] = "no_public_source"
            continue
        account = get_or_create_account(session, domain)
        contact = session.scalar(select(CeContact).where(CeContact.email == email))
        if contact is None:
            contact = CeContact(account_id=account.id, email=email,
                                full_name=_pick(row, "contact_name") or None,
                                role=_pick(row, "role") or None, source_url=source,
                                verified_at=now_utc())
            session.add(contact)
            session.flush()
        target.contact_id = contact.id
        report["contacts_attached"] += 1
        line["contact"] = "attached"
    report["contact_notes"] = dict(reasons)
    session.flush()
    return report


def import_status(session: Session, source_key: str) -> dict[str, Any]:
    """Where every row of an import stands now, after enrichment and Gmail."""
    signals = list(session.scalars(select(CeSignal).where(CeSignal.source_key == source_key)))
    states: Counter[str] = Counter()
    by_lane: dict[str, Counter[str]] = {}
    reasons: Counter[str] = Counter()
    for signal in signals:
        attrs = signal.attributes or {}
        if signal.account_id is None:
            state = ("MANUAL_APPLICATION" if attrs.get("channel") == "PLATFORM_APPLICATION"
                     else "ENRICH_NO_DOMAIN")
        else:
            target = session.scalar(select(CeTarget).where(
                CeTarget.account_id == signal.account_id).order_by(
                CeTarget.created_at.desc()).limit(1))
            state = target.state if target else "NO_TARGET"
            if target is not None and target.state in ("BLOCKED", "SUPPRESSED", "EXPIRED"):
                reasons[f"{target.state}:{(target.state_reason or '').split(';')[0]}"] += 1
        states[state] += 1
        by_lane.setdefault(signal.lane, Counter())[state] += 1
    return {"source_key": source_key, "rows": len(signals), "by_state": dict(states),
            "by_lane": {k: dict(v) for k, v in by_lane.items()},
            "terminal_reasons": dict(reasons)}
