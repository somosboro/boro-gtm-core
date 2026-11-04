"""ENRICH → READY: verify, find a published channel, score, route (CE-ADR-005).

An address is only ever one the company published on its own site. Nothing is
derived from a name, no pattern is guessed, and an address on another domain
than the company's is ignored.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.models import CeAccount, CeContact, CeSignal, CeTarget
from boro_gtm.engine.pipeline import (
    GENERIC_LOCAL_PARTS,
    email_domain,
    is_role_mailbox,
    jurisdiction_status,
    normalize_email,
    now_utc,
    prior_contact,
    same_company_domain,
    suppression_for,
    transition,
)
from boro_gtm.engine.scoring import score_target
from boro_gtm.engine.sources.base import Fetcher
from boro_gtm.engine.strategy import Strategy

CONTACT_PATHS = ("", "contacto", "contact", "contactanos", "contact-us", "nosotros", "about",
                 "about-us", "equipo", "team", "empresa")
_EMAIL = re.compile(r"(?:mailto:)?([A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_CF_EMAIL = re.compile(r'data-cfemail="([0-9a-f]+)"')


def _decode_cf(hex_string: str) -> str:
    key = int(hex_string[:2], 16)
    return "".join(chr(int(hex_string[i:i + 2], 16) ^ key) for i in range(2, len(hex_string), 2))


def published_emails(html: str, domain: str) -> list[str]:
    found = {m for m in _EMAIL.findall(html)} | {_decode_cf(h) for h in _CF_EMAIL.findall(html)}
    keep = []
    for raw in found:
        email = normalize_email(raw)
        if not email or email_domain(email) != domain:
            continue
        if is_role_mailbox(email):
            continue
        if re.search(r"\.(png|jpg|jpeg|gif|webp|svg)$", email):
            continue
        keep.append(email)
    return sorted(keep)


COMMERCIAL_LOCAL_PARTS = ("partners", "alianzas", "negocios", "business", "ventas", "sales",
                          "comercial", "proyectos", "projects", "nuevosnegocios")


def _rank(email: str) -> tuple[int, str]:
    """A named person first, then a commercial desk, then a general inbox."""
    local = email.split("@")[0]
    if local in COMMERCIAL_LOCAL_PARTS:
        return (1, email)
    if local in GENERIC_LOCAL_PARTS:
        return (2, email)
    return (0, email)


def find_channel(fetcher: Fetcher, domain: str) -> tuple[str, str] | None:
    """(email, page it was published on), or None.

    Keeps reading the contact pages until it finds a named person, so a
    general inbox on the home page does not hide the team page.
    """
    best: tuple[str, str] | None = None
    for path in CONTACT_PATHS:
        url = urljoin(f"https://{domain}/", path)
        result = fetcher.get(url)
        if result.outcome != "OK" or not result.body:
            if path == "" and result.outcome in ("DENIED", "ROBOTS_DENIED", "LOGIN_WALL"):
                return None
            continue
        emails = published_emails(result.body.decode("utf-8", "replace"), domain)
        if emails:
            found = sorted(emails, key=_rank)[0]
            if best is None or _rank(found) < _rank(best[0]):
                best = (found, result.final_url or url)
            if _rank(best[0])[0] == 0:
                break
    return best


def enrich_target(session: Session, strategy: Strategy, target: CeTarget, fetcher: Fetcher,
                  *, actor: str = "system:enrichment") -> dict[str, Any]:
    if target.state == S.DISCOVERED:
        transition(session, target, S.ENRICH, reason="enrichment_started", actor=actor)
    if target.state != S.ENRICH:
        return {"target_id": str(target.id), "skipped": target.state}
    account = session.get(CeAccount, target.account_id)
    signal = session.get(CeSignal, target.signal_id) if target.signal_id else None
    if target.contact_id is None:
        channel = None
        attrs = (signal.attributes or {}) if signal else {}
        listed = normalize_email(attrs.get("published_email"))
        if (listed and same_company_domain(listed, account.domain) and signal.url
                and not is_role_mailbox(listed, contact_role=attrs.get("contact_role"))):
            # Published by the company itself in a directory it maintains, or
            # in its own posting, on its own domain: a channel with a source.
            channel = (listed, signal.url)
        if channel is None or _rank(channel[0])[0] != 0:
            # A general inbox is a fallback; a named person on the site is better.
            site = find_channel(fetcher, account.domain)
            if site is not None and (channel is None or _rank(site[0]) < _rank(channel[0])):
                channel = site
        if channel is not None:
            email, source_url = channel
            contact = session.scalar(select(CeContact).where(CeContact.email == email))
            if contact is None:
                local = email.split("@")[0]
                listed_role = (attrs.get("contact_role")
                               if email == listed and source_url == signal.url else None)
                contact = CeContact(account_id=account.id, email=email, role=listed_role,
                                    is_generic=_rank(email)[0] != 0 or not re.search(
                                        r"[a-z]{2,}[._-]?[a-z]{2,}", local),
                                    source_url=source_url, verified_at=now_utc())
                session.add(contact)
                session.flush()
            target.contact_id = contact.id
    contact = session.get(CeContact, target.contact_id) if target.contact_id else None
    score = score_target(
        strategy, kind=signal.kind if signal else "IMPORT",
        title=signal.title if signal else None, excerpt=signal.excerpt if signal else None,
        posted_at=signal.posted_at if signal else None, expires_at=target.expires_at,
        has_verified_channel=contact is not None,
        channel_is_person=contact is not None and not contact.is_generic,
        vendor_evidence=((signal.attributes or {}).get("delivery_evidence") if signal else None))
    target.score = score.total
    target.score_components = score.as_json()
    target.priority = ("HIGH" if score.total >= strategy.high_priority else
                       "NORMAL" if score.total >= strategy.threshold else "LOW")
    outcome = _route(session, strategy, target, account, contact, actor)
    return {"target_id": str(target.id), "domain": account.domain, "score": score.total,
            "contact": contact.email if contact else None, "state": target.state,
            "outcome": outcome}


def _route(session: Session, strategy: Strategy, target: CeTarget, account: CeAccount,
           contact: CeContact | None, actor: str) -> str:
    if contact is None:
        transition(session, target, S.BLOCKED, reason="no_published_channel", actor=actor)
        return "no_channel"
    hit = suppression_for(session, contact.email)
    if hit is not None:
        transition(session, target, S.SUPPRESSED, reason=f"suppressed_{hit.reason.lower()}",
                   actor=actor)
        return "suppressed"
    prior = prior_contact(session, contact.email, email_domain(contact.email) or account.domain,
                          account.domain)
    if prior:
        # Gmail says BoRo already wrote here: take over that sequence if it is
        # still running, otherwise close the pursuit. Never a second first email.
        from boro_gtm.engine.history import adopt_target

        return adopt_target(session, strategy, target, actor=actor)
    if target.score < strategy.threshold:
        transition(session, target, S.BLOCKED, reason=f"score_{target.score}_below_threshold",
                   actor=actor, details={"score": target.score_components})
        return "below_threshold"
    if jurisdiction_status(strategy, target.country) != "ALLOWED":
        transition(session, target, S.COMPLIANCE_REVIEW,
                   reason=f"jurisdiction_{target.country or 'unknown'}", actor=actor)
        return "compliance_review"
    transition(session, target, S.READY, reason="qualified", actor=actor,
               details={"score": target.score, "contact": contact.email})
    return "ready"


def requalify_unusable_channels(session: Session, *, actor: str = "system:enrichment") -> int:
    """READY targets whose contact a newer rule rejects go back to ENRICH."""
    count = 0
    for target, contact in session.execute(select(CeTarget, CeContact).join(
            CeContact, CeContact.id == CeTarget.contact_id).where(CeTarget.state == S.READY)):
        if is_role_mailbox(contact.email, contact_role=contact.role):
            from boro_gtm.engine.outreach import cancel_pending

            cancel_pending(session, target.id, "requalify_role_mailbox")
            target.contact_id = None
            transition(session, target, S.ENRICH, reason="requalify:role_mailbox", actor=actor,
                       details={"dropped_contact": contact.email})
            count += 1
    return count


def enrich_batch(session: Session, strategy: Strategy, fetcher: Fetcher, *,
                 limit: int = 30) -> list[dict[str, Any]]:
    requalify_unusable_channels(session)
    targets = list(session.scalars(select(CeTarget).where(
        CeTarget.state.in_((S.DISCOVERED, S.ENRICH))).order_by(CeTarget.created_at).limit(limit)))
    out = []
    for target in targets:
        try:
            out.append(enrich_target(session, strategy, target, fetcher))
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the batch
            out.append({"target_id": str(target.id), "error": f"{type(exc).__name__}: {exc}"})
        session.commit()
    return out
