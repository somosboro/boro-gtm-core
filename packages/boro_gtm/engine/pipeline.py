"""Accounts, targets, transitions, suppression and eligibility.

The deterministic heart of the engine. Nothing in this module calls a model or
the network.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from boro_gtm.core.errors import GtmError
from boro_gtm.discovery.domain.models import CompanyDomain
from boro_gtm.discovery.services.resolution import domain_role, normalize_domain
from boro_gtm.engine import states as S
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeMailEvent,
    CeMessage,
    CeSignal,
    CeSuppression,
    CeTarget,
    CeTargetEvent,
)
from boro_gtm.engine.strategy import Strategy

_EMAIL = re.compile(r"^[a-z0-9._%+\-']+@[a-z0-9.\-]+\.[a-z]{2,}$")
#: Mailboxes that are a channel for the company rather than a person.
GENERIC_LOCAL_PARTS = frozenset({
    "info", "contacto", "contact", "hola", "hello", "ventas", "sales", "comercial",
    "admin", "office", "oficina", "negocios", "business", "partners", "alianzas",
})
#: Mailboxes nobody should ever receive a sales email at.
NEVER_LOCAL_PARTS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "mailer-daemon",
    "abuse", "privacy", "legal", "soporte", "support", "jobs", "careers", "empleos",
    "rrhh", "hr", "billing", "facturacion", "webmaster",
})
#: Words that make a mailbox a service, finance, legal or hiring desk, wherever
#: they appear in the local part: `soporte.tecnico@`, `atencionalcliente@`.
_ROLE_WORDS = re.compile(
    r"(soporte|support|help|ayuda|mesadeayuda|servicedesk|helpdesk|ticket|incidencia"
    r"|atencion|servicioalcliente|servicio\.?cliente|customer|clientes|postventa|reclamo"
    r"|billing|factura|invoice|cobranza|pagos|payment|contabilidad|accounting|tesoreria"
    r"|^servicios?$|^servicio[._-]|finanzas|^cfo$|^admon$|administracion|^adm$"
    r"|privacy|privacidad|legal|dpo|datospersonales|compliance|abuse|security|seguridad"
    r"|noreply|no-reply|donotreply|mailer|bounce|unsubscribe|newsletter|notificacion"
    r"|notification|alerts?$|webmaster|hostmaster|postmaster"
    r"|rrhh|recursoshumanos|talento|seleccion|reclutamiento|recruit|curriculum|^cv$"
    r"|postula|jobs?$|careers?$|empleo|trabaja|hiring|^hr$|^rh$)")
#: Hiring desks are acceptable only where the posting itself asks contractors
#: or freelancers to write there (a contact with this role).
CONTRACTOR_RECRUITING = "contractor_recruiting"
_HIRING_WORDS = re.compile(r"(jobs?$|careers?$|hiring|recruit|reclutamiento|talento|seleccion"
                           r"|postula|empleo|trabaja|^hr$|^rh$|rrhh|curriculum|^cv$)")


def is_role_mailbox(email_or_local: str, *, contact_role: str | None = None) -> bool:
    """A support, finance, legal, system or hiring desk: never a sales inbox."""
    local = email_or_local.split("@")[0].split("+")[0].lower()
    if local in NEVER_LOCAL_PARTS or _ROLE_WORDS.search(local.replace("_", ".")):
        if contact_role == CONTRACTOR_RECRUITING and _HIRING_WORDS.search(local) and not (
                _ROLE_WORDS.search(_HIRING_WORDS.sub("", local))):
            return False
        return True
    return False


#: Personal mailbox providers: an address there never identifies a company.
FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.es", "outlook.com", "outlook.es",
    "live.com", "msn.com", "yahoo.com", "yahoo.es", "yahoo.com.mx", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "yandex.com", "mail.com",
})

#: How long a "no" holds before a new signal may reopen the account.
REJECTION_COOLDOWN = timedelta(days=180)


class EngineError(GtmError):
    code = "ENGINE_ERROR"
    http_status = 409


def now_utc() -> datetime:
    return datetime.now(UTC)


# --- normalization ----------------------------------------------------------


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip().lower().removeprefix("mailto:").split("?")[0]
    return email if _EMAIL.match(email) else None


def email_domain(email: str) -> str | None:
    return normalize_domain(email.rsplit("@", 1)[-1])


def account_key(domain: str | None) -> str | None:
    """The registrable domain an account is keyed by, or None if it cannot be.

    Hosting tenants and platforms are refused: a company that only exists on
    `someone.lovable.app` cannot be deduplicated by domain (M2-ADR-054).
    """
    normalized = normalize_domain(domain)
    if not normalized or domain_role(normalized) != "IDENTITY" or normalized in FREEMAIL:
        return None
    return normalized


def same_company_domain(email_or_domain: str, account_domain: str) -> bool:
    """Is this address (or domain) the company's own: its domain, a subdomain,
    or a sister domain under the same name (`opens.lat` for `opens.cl`)?"""
    value = email_or_domain.lower()
    found = email_domain(value) if "@" in value else normalize_domain(value)
    if not found or not account_domain or found in FREEMAIL:
        return False
    if found == account_domain or found.endswith("." + account_domain):
        return True
    label = found.split(".")[0]
    return len(label) >= 4 and label == account_domain.split(".")[0]


def get_or_create_account(session: Session, domain: str, *, name: str | None = None,
                          country: str | None = None) -> CeAccount:
    key = account_key(domain)
    if key is None:
        raise EngineError(f"{domain!r} cannot identify an account", details={"domain": domain})
    account = session.scalar(select(CeAccount).where(CeAccount.domain == key))
    if account is None:
        m2 = session.scalar(select(CompanyDomain.company_id).where(
            CompanyDomain.domain_normalized == key, CompanyDomain.domain_role == "IDENTITY"))
        account = CeAccount(domain=key, name=name, country=country, m2_company_id=m2,
                            created_at=now_utc())
        session.add(account)
        session.flush()
    else:
        account.name = account.name or name
        account.country = account.country or country
    return account


# --- suppression ------------------------------------------------------------


def suppress(session: Session, *, kind: str, value: str, reason: str,
             evidence_ref: str | None = None) -> bool:
    """Idempotent. Returns True when this call added the suppression."""
    value = value.strip().lower()
    exists = session.scalar(select(CeSuppression.id).where(
        CeSuppression.kind == kind, CeSuppression.value == value))
    if exists:
        return False
    session.add(CeSuppression(kind=kind, value=value, reason=reason,
                              evidence_ref=evidence_ref, created_at=now_utc()))
    session.flush()
    return True


def suppression_for(session: Session, email: str) -> CeSuppression | None:
    email = email.lower()
    domain = email_domain(email)
    return session.scalar(select(CeSuppression).where(
        ((CeSuppression.kind == "EMAIL") & (CeSuppression.value == email))
        | ((CeSuppression.kind == "DOMAIN") & (CeSuppression.value == (domain or "")))
    ).limit(1))


# --- transitions ------------------------------------------------------------


def transition(session: Session, target: CeTarget, to_state: str, *, reason: str,
               actor: str, details: dict[str, Any] | None = None,
               at: datetime | None = None) -> None:
    S.check_transition(target.state, to_state)
    moment = at or now_utc()
    session.add(CeTargetEvent(target_id=target.id, from_state=target.state, to_state=to_state,
                              reason=reason, actor=actor, details=details or {}, at=moment))
    target.state = to_state
    target.state_reason = reason
    target.state_changed_at = moment
    session.flush()


# --- targets from signals ---------------------------------------------------


def open_target_for(session: Session, account_id: uuid.UUID) -> CeTarget | None:
    return session.scalar(select(CeTarget).where(
        CeTarget.account_id == account_id, CeTarget.state.in_(S.OPEN_STATES)))


def target_from_signal(session: Session, strategy: Strategy, signal: CeSignal,
                       *, actor: str = "system:sourcing") -> tuple[CeTarget | None, str]:
    """Attach a signal to an account's pursuit. Returns (target, outcome).

    `outcome` is one of CREATED, REOPENED, ATTACHED (an open target exists —
    the signal is recorded, nothing new starts), NO_ACCOUNT, EXPIRED.
    """
    domain = signal.company_domain
    if not domain or account_key(domain) is None:
        return None, "NO_ACCOUNT"
    if signal.expires_at is not None and signal.expires_at <= now_utc():
        return None, "EXPIRED"
    account = get_or_create_account(session, domain, name=signal.company_name,
                                    country=signal.country)
    signal.account_id = account.id
    existing = open_target_for(session, account.id)
    if existing is not None:
        return existing, "ATTACHED"
    previous = session.scalar(select(CeTarget).where(CeTarget.account_id == account.id)
                              .order_by(CeTarget.created_at.desc()).limit(1))
    if previous is not None and previous.state not in S.REOPENABLE:
        # WON, BLOCKED or SUPPRESSED: a new job post does not undo that.
        return previous, "ATTACHED"
    moment = now_utc()
    target = CeTarget(
        account_id=account.id, signal_id=signal.id, lane=signal.lane,
        segment=(signal.attributes or {}).get("segment"), country=signal.country,
        state=S.DISCOVERED, strategy_version=strategy.version, expires_at=signal.expires_at,
        reopened_from_id=previous.id if previous is not None else None,
        created_at=moment, state_changed_at=moment,
    )
    session.add(target)
    session.flush()
    session.add(CeTargetEvent(target_id=target.id, from_state=None, to_state=S.DISCOVERED,
                              reason=f"signal {signal.source_key}:{signal.external_id}",
                              actor=actor, details={"signal_id": str(signal.id)}, at=moment))
    session.flush()
    return target, "REOPENED" if previous is not None else "CREATED"


# --- eligibility ------------------------------------------------------------


def prior_contact(session: Session, email: str, domain: str,
                  *other_domains: str | None) -> str | None:
    """Has BoRo already written to this person or company? Gmail is the authority.

    Only messages Gmail labelled SENT count (drafts never do), at the address
    or anywhere at the company's domain, its subdomains included.
    """
    if session.scalar(select(CeMessage.id).where(
            CeMessage.to_email == email, CeMessage.step == "INITIAL",
            CeMessage.status.in_(("SENT", "SENDING"))).limit(1)):
        return "engine_initial_already_sent"
    out = session.scalar(select(CeMailEvent.id).where(
        CeMailEvent.direction == "OUT", CeMailEvent.to_emails.any(email)).limit(1))
    if out:
        return "gmail_history_same_address"
    for each in dict.fromkeys(d for d in (domain, *other_domains) if d):
        pattern = r"@([a-z0-9-]+\.)*" + re.escape(each.lower()) + r"(,|$)"
        domain_hit = session.scalar(select(CeMailEvent.id).where(
            CeMailEvent.direction == "OUT",
            func.lower(func.array_to_string(CeMailEvent.to_emails, ",")).op("~")(pattern))
            .limit(1))
        if domain_hit:
            return "gmail_history_same_domain"
    # A sister domain under the same name may be the same company (`info@opens.lat`
    # was written to, so `opens.cl` may be contacted too): see same_company_domain.
    # It cannot prove it, so the planner holds such a target for a person to decide.
    if sister_domain_sends(session, domain, *other_domains):
        return SISTER_DOMAIN
    return None


#: Gmail shows a send only to a same-name domain under another TLD. Not proof of the
#: same company: the target is held in READY, unplanned, until `gtm engine sister-review`.
SISTER_DOMAIN = "gmail_history_sister_domain"
SISTER_HELD = "held:" + SISTER_DOMAIN
SISTER_CLEARED = "cleared:" + SISTER_DOMAIN
SISTER_CONFIRMED = SISTER_DOMAIN + ":confirmed_same_company"


def sister_domain_sends(session: Session, domain: str, *other_domains: str | None) -> list[str]:
    """Addresses BoRo wrote to at a sister domain of any of these domains."""
    found: list[str] = []
    for each in dict.fromkeys(d.lower() for d in (domain, *other_domains) if d):
        label = each.split(".")[0]
        if each in FREEMAIL or len(label) < 4:
            continue
        pattern = r"@" + re.escape(label) + r"\.[a-z0-9.-]+(,|$)"
        for recipients in session.scalars(select(CeMailEvent.to_emails).where(
                CeMailEvent.direction == "OUT",
                func.lower(func.array_to_string(CeMailEvent.to_emails, ",")).op("~")(pattern))):
            found += [a.lower() for a in recipients or [] if same_company_domain(a, each)
                      and a.lower() not in found]
    return found


def only_sister_history(reasons: list[str]) -> bool:
    """The only sign of prior contact is a sister domain."""
    history = [r for r in reasons
               if r.startswith("gmail_history") or r == "engine_initial_already_sent"]
    return history == [SISTER_DOMAIN]


def _sister_reviews(session: Session, target_ids: list[uuid.UUID] | None = None
                    ) -> dict[uuid.UUID, CeTargetEvent]:
    query = select(CeTargetEvent).where(CeTargetEvent.reason.in_((SISTER_HELD, SISTER_CLEARED)))
    if target_ids is not None:
        query = query.where(CeTargetEvent.target_id.in_(target_ids))
    latest: dict[uuid.UUID, CeTargetEvent] = {}
    for event in session.scalars(query.order_by(CeTargetEvent.at, CeTargetEvent.id)):
        latest[event.target_id] = event
    return latest


def sister_review_state(session: Session, target_id: uuid.UUID) -> str | None:
    """`held`, `cleared` (a person said: another company) or None."""
    event = _sister_reviews(session, [target_id]).get(target_id)
    return None if event is None else ("held" if event.reason == SISTER_HELD else "cleared")


def held_for_sister_review(session: Session) -> list[CeTargetEvent]:
    """The open holds: targets still READY whose latest sister review is a hold."""
    held = [e for e in _sister_reviews(session).values() if e.reason == SISTER_HELD]
    return [e for e in held if (t := session.get(CeTarget, e.target_id)) and t.state == S.READY]


def hold_for_sister_review(session: Session, target: CeTarget, *, actor: str,
                           details: dict[str, Any] | None = None) -> bool:
    """Record a hold (append-only, state unchanged). False if already held."""
    if sister_review_state(session, target.id) == "held":
        return False
    account = session.get(CeAccount, target.account_id)
    contact = session.get(CeContact, target.contact_id) if target.contact_id else None
    domains = [account.domain if account else None,
               email_domain(contact.email) if contact else None]
    session.add(CeTargetEvent(
        target_id=target.id, from_state=target.state, to_state=target.state,
        reason=SISTER_HELD, actor=actor, at=now_utc(),
        details={"sent_to": sister_domain_sends(session, *[d for d in domains if d]),
                 **(details or {})}))
    session.flush()
    return True


def jurisdiction_status(strategy: Strategy, country: str | None) -> str:
    """ALLOWED, REVIEW or UNKNOWN. UNKNOWN is treated as REVIEW by callers."""
    j = strategy.jurisdictions
    code = (country or "").upper()
    if code in j["allowed"]:
        return "ALLOWED"
    if code in j.get("requires_postal_address", []):
        return "ALLOWED" if strategy.sender.get("postal_address") else "REVIEW"
    if code in j["review"]:
        return "REVIEW"
    return "UNKNOWN"


def send_blockers(session: Session, strategy: Strategy, target: CeTarget,
                  contact: CeContact | None, *, step: str) -> list[str]:
    """Every reason this message may not go out. Empty means it may."""
    reasons: list[str] = []
    expected = {"INITIAL": (S.READY,), "F1": (S.CONTACTED,), "F2": (S.F1,)}[step]
    if target.state not in expected:
        reasons.append(f"state_{target.state}_not_{'_'.join(expected)}")
    if contact is None:
        return reasons + ["no_contact"]
    if not contact.source_url:
        reasons.append("contact_unverified")
    if normalize_email(contact.email) is None:
        reasons.append("contact_email_invalid")
    elif is_role_mailbox(contact.email, contact_role=contact.role):
        # Support, jobs, legal, billing: a published address, but not one a
        # sales email belongs in, wherever it came from.
        reasons.append("role_mailbox_not_for_sales")
    hit = suppression_for(session, contact.email)
    if hit is not None:
        reasons.append(f"suppressed_{hit.reason.lower()}")
    if target.expires_at is not None and target.expires_at <= now_utc():
        reasons.append("signal_expired")
    if step == "INITIAL":
        domain = email_domain(contact.email) or ""
        account = session.get(CeAccount, target.account_id)
        prior = prior_contact(session, contact.email, domain, account.domain if account else None)
        if prior == SISTER_DOMAIN and sister_review_state(session, target.id) == "cleared":
            prior = None  # a person decided the sister domain is another company
        if prior:
            reasons.append(prior)
        if jurisdiction_status(strategy, target.country) != "ALLOWED":
            reasons.append("jurisdiction_review")
        # A rejection closes the pursuit; a new signal may reopen the account,
        # but not within REJECTION_COOLDOWN of being told no.
        rejected = session.scalar(select(CeTargetEvent.id).join(
            CeTarget, CeTarget.id == CeTargetEvent.target_id).where(
            CeTarget.account_id == target.account_id, CeTargetEvent.to_state == S.LOST,
            CeTargetEvent.reason.like("rejection%"),
            CeTargetEvent.at > now_utc() - REJECTION_COOLDOWN).limit(1))
        if rejected:
            reasons.append("account_previously_rejected")
    return reasons
