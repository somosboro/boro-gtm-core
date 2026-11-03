"""Import an externally produced pool (CSV or XLSX), idempotently.

Rows become IMPORT signals keyed by (file name, row identity), so re-importing
the same file creates nothing. Nothing is trusted: every row goes through the
same account, contact, suppression and Gmail-history checks as a sourced one.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine.models import CeContact, CeSignal
from boro_gtm.engine.pipeline import (
    account_key,
    get_or_create_account,
    normalize_email,
    now_utc,
    target_from_signal,
)
from boro_gtm.engine.states import LANES
from boro_gtm.engine.strategy import Strategy

ALIASES = {
    "company": ("company", "company_name", "empresa", "account"),
    "domain": ("domain", "website", "web", "sitio", "url", "canonical_domain"),
    "lane": ("lane", "carril", "canal"),
    "country": ("country", "pais", "país"),
    "email": ("email", "contact_email", "correo"),
    "contact_name": ("contact_name", "name", "nombre", "buyer"),
    "role": ("role", "cargo", "title"),
    "signal_url": ("signal_url", "source_url", "source", "evidence_url", "fuente"),
    "signal": ("signal", "evidence", "evidencia", "trigger", "reason"),
    "email_source_url": ("email_source_url", "contact_source", "email_source"),
}
LANE_ALIASES = {"partner": "PARTNER_DELIVERY", "partner delivery": "PARTNER_DELIVERY",
                "explicit": "EXPLICIT_DEMAND", "explicit demand": "EXPLICIT_DEMAND",
                "trigger": "TRIGGER_OVERFLOW", "overflow": "TRIGGER_OVERFLOW",
                "trigger / overflow": "TRIGGER_OVERFLOW"}


def read_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        sheet = load_workbook(path, read_only=True, data_only=True).active
        rows = list(sheet.iter_rows(values_only=True))
        header = [str(h or "").strip() for h in rows[0]]
        return [{header[i]: "" if v is None else str(v) for i, v in enumerate(r)}
                for r in rows[1:] if any(v is not None for v in r)]
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _pick(row: dict[str, str], field: str) -> str:
    lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    for alias in ALIASES[field]:
        if lowered.get(alias):
            return lowered[alias]
    return ""


def _lane(value: str) -> str | None:
    v = value.strip()
    if v.upper() in LANES:
        return v.upper()
    return LANE_ALIASES.get(v.lower())


def import_pool(session: Session, strategy: Strategy, path: Path) -> dict[str, Any]:
    rows = read_rows(path)
    report: dict[str, Any] = {"file": path.name, "rows": len(rows), "created": 0,
                              "reopened": 0, "duplicates": 0, "rejected": []}
    for number, row in enumerate(rows, start=2):
        domain = account_key(_pick(row, "domain"))
        lane = _lane(_pick(row, "lane"))
        if domain is None:
            report["rejected"].append({"row": number, "reason": "no_identity_domain"})
            continue
        if lane is None:
            report["rejected"].append({"row": number, "reason": "unknown_lane"})
            continue
        external = hashlib.sha256(f"{domain}|{lane}".encode()).hexdigest()[:32]
        key = f"import:{path.stem}"[:64]
        if session.scalar(select(CeSignal.id).where(CeSignal.source_key == key,
                                                    CeSignal.external_id == external)):
            report["duplicates"] += 1
            continue
        signal_text = _pick(row, "signal")
        signal = CeSignal(
            source_key=key, external_id=external, kind="IMPORT", lane=lane,
            url=_pick(row, "signal_url") or None, title=signal_text[:200] or None,
            excerpt=signal_text or None, company_name=_pick(row, "company") or None,
            company_domain=domain, country=(_pick(row, "country") or "").upper()[:2] or None,
            observed_at=now_utc(),
            attributes={"segment": "imported_pool", "import_row": number,
                        "evidence_es": signal_text, "evidence_en": signal_text},
        )
        session.add(signal)
        session.flush()
        target, outcome = target_from_signal(session, strategy, signal, actor="import")
        if outcome == "CREATED":
            report["created"] += 1
        elif outcome == "REOPENED":
            report["reopened"] += 1
        else:
            report["duplicates"] += 1
            continue
        email = normalize_email(_pick(row, "email"))
        source = _pick(row, "email_source_url")
        # An address without the page it was published on is a guess: it is
        # not attached, and enrichment looks for a published one instead.
        if email and source:
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
        elif email:
            report["rejected"].append({"row": number, "reason": "email_without_public_source",
                                       "kept_target": True})
    session.flush()
    return report
