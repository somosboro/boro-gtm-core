"""Planning, sending and following up, exactly once (CE-ADR-004, CE-ADR-006).

Three guarantees, each held by the database rather than by care:

* one message per target per step — `UNIQUE (target_id, step)`;
* a message is marked SENDING and committed *before* the network call, with a
  Message-ID fixed in advance, so a crashed send is reconciled by asking Gmail
  whether that Message-ID exists, never by sending again blind;
* the daily cap counts every initial already scheduled, sending, sent or
  shadowed today, under an advisory lock.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.business_days import BusinessCalendar
from boro_gtm.engine.gmail import MailGateway, build_mime
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeControl,
    CeMailEvent,
    CeMessage,
    CeSignal,
    CeTarget,
)
from boro_gtm.engine.pipeline import (
    EngineError,
    is_role_mailbox,
    now_utc,
    send_blockers,
    transition,
)
from boro_gtm.engine.strategy import Strategy

SPANISH = frozenset({"CL", "PE", "CO", "MX", "AR", "ES", "UY", "EC", "BO", "PY", "CR", "PA",
                     "GT", "DO", "VE", "SV", "HN", "NI"})
PAUSE_KEY = "sending_paused"
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
#: Advisory-lock key for the daily cap. Any constant; it only has to be shared.
_CAP_LOCK = 774_211_001


def language_for(country: str | None) -> str:
    return "es" if (country or "").upper() in SPANISH else "en"


def choose_proof(strategy: Strategy, text: str, language: str) -> str:
    lowered = text.lower()
    proofs = strategy.raw["proof_points"]
    if any(t in lowered for t in ("clínic", "clinic", "salud", "health", "pacient", "patient")):
        key = "clinical_erp"
    elif "odoo" in lowered or "erp" in lowered:
        key = "patient_ops"
    elif any(t in lowered for t in ("mobile", "móvil", "movil", "react native", "expo", "app")):
        key = "quecompro"
    elif any(t in lowered for t in ("web", "sitio", "website", "portal")):
        key = "viba"
    else:
        key = "quecompro"
    return proofs[key][language]


def first_name(contact: CeContact, language: str) -> str:
    if contact.full_name and not contact.is_generic:
        return contact.full_name.split()[0]
    return "equipo" if language == "es" else "there"


# --- copy helpers (strategy "copy" section; absent in older strategies) ----------

#: A personal mailbox written as name.surname or name_surname.
_PERSON_LOCAL = re.compile(r"^([a-záéíóúñü]{3,})[._]([a-záéíóúñü]{2,})$")
_GREETING = {"es": {"person": "Hola {name}", "team": "Hola, equipo de {company}"},
             "en": {"person": "Hi {name}", "team": "Hi {company} team"}}


def _copy(strategy: Strategy) -> dict[str, Any]:
    return strategy.raw.get("copy") or {}


def short_company(strategy: Strategy, name: str) -> str:
    """The company name without a trailing legal form ("Blue Minds SpA" -> "Blue Minds")."""
    cleaned = (name or "").strip()
    for suffix in sorted(_copy(strategy).get("legal_suffixes", []), key=len, reverse=True):
        if cleaned.lower().endswith(" " + suffix.lower()):
            return cleaned[: -len(suffix)].rstrip(" ,.") or cleaned
    return cleaned


def person_name(contact: CeContact) -> str | None:
    """A first name only when the contact is clearly one person; never a guess at a desk."""
    if contact.is_generic or is_role_mailbox(contact.email, contact_role=contact.role):
        return None
    if contact.full_name:
        return contact.full_name.split()[0]
    match = _PERSON_LOCAL.match(contact.email.split("@")[0].lower())
    return match.group(1).capitalize() if match else None


def greeting(strategy: Strategy, contact: CeContact, company: str, language: str) -> str:
    forms = _copy(strategy).get("greeting", {}).get(language) or _GREETING[language]
    name = person_name(contact)
    return forms["person"].format(name=name) if name else forms["team"].format(company=company)


def loc_short(strategy: Strategy, country: str | None, language: str) -> str:
    """The country's e-invoicing system in one word (DTE, SUNAT, CFDI, DIAN...)."""
    table = _copy(strategy).get("localization_short", {}).get(language, {})
    return table.get((country or "").upper()) or table.get("_default", "")


def segment_text(strategy: Strategy, key: str, *, segment: str, evidence: str,
                 country: str | None, language: str) -> str:
    """Copy that depends on the kind of company: Odoo partners (from the directory, or an
    import whose evidence names Odoo) or any other software firm."""
    table = _copy(strategy).get(key, {})
    odoo = segment == "odoo_partner" or "odoo" in evidence.lower()
    forms = (table.get("odoo_partner") if odoo else None) or table.get("_default") or {}
    return forms.get(language, "").format(loc_short=loc_short(strategy, country, language))


def cta(strategy: Strategy, lane: str, contact: CeContact, language: str) -> str:
    """The closing question, addressed to the person (usted) or to the team (ustedes)."""
    forms = _copy(strategy).get("cta", {}).get(lane, {}).get(language, {})
    return forms.get("person" if person_name(contact) else "team", "")


def render(strategy: Strategy, *, target: CeTarget, account: CeAccount, contact: CeContact,
           signal: CeSignal | None, step: str) -> dict[str, str]:
    language = language_for(target.country)
    template = strategy.template(target.lane)
    parts = template[language][step]
    attrs = (signal.attributes if signal is not None else {}) or {}
    evidence = attrs.get(f"evidence_{language}") or attrs.get("evidence") or ""
    signal_title = (signal.title if signal is not None else "") or ""
    company = account.name or account.domain
    company_short = short_company(strategy, company)
    by_segment = {"segment": str(attrs.get("segment") or ""), "evidence": evidence,
                  "country": target.country, "language": language}
    values = {
        "first_name": first_name(contact, language),
        "greeting": greeting(strategy, contact, company_short, language),
        "company": company,
        "company_short": company_short,
        "hook": segment_text(strategy, "hook", **by_segment),
        "topic": segment_text(strategy, "topic", **by_segment),
        "example_work": segment_text(strategy, "example_work", **by_segment),
        # Optional: a second proof only some segments have (empty for the others).
        "follow_up_proof": segment_text(strategy, "follow_up_proof", **by_segment),
        "cta": cta(strategy, target.lane, contact, language),
        "loc_short": loc_short(strategy, target.country, language),
        "signature_short": _copy(strategy).get("signature_short") or strategy.sender["signature"],
        "evidence": evidence,
        "signal_title": signal_title[:90],
        "proof": choose_proof(strategy, f"{signal_title} {signal.excerpt if signal else ''}",
                              language),
        "signature": strategy.sender["signature"],
    }
    if "{evidence}" in parts["body"] and not evidence:
        raise EngineError("template needs observable evidence and the signal has none",
                          details={"target_id": str(target.id)})
    if "{signal_title}" in parts["body"] + parts["subject"] and not signal_title:
        raise EngineError("template needs the signal title", details={"target_id": str(target.id)})
    for key in ("hook", "topic", "example_work", "cta"):
        if "{" + key + "}" in parts["body"] + parts["subject"] and not values[key]:
            raise EngineError(f"template uses {{{key}}} and the strategy's copy defines none",
                              details={"target_id": str(target.id)})
    subject = parts["subject"].format(**values)
    body = parts["body"].format(**values)
    if _PLACEHOLDER.search(subject + body):
        raise EngineError("unfilled placeholder", details={"target_id": str(target.id)})
    return {"subject": subject, "body": body, "language": language,
            "variant": template["variant"], "offer": template["offer"]}


def rfc822_id(target_id: uuid.UUID, step: str, sender: str) -> str:
    digest = hashlib.sha256(f"{target_id}:{step}".encode()).hexdigest()[:32]
    return f"<ce.{digest}@{sender.split('@')[1]}>"


# --- controls ---------------------------------------------------------------


def paused(session: Session) -> dict[str, Any] | None:
    row = session.get(CeControl, PAUSE_KEY)
    return row.value if row is not None and row.value.get("paused") else None


def set_pause(session: Session, *, paused_: bool, reason: str) -> None:
    row = session.get(CeControl, PAUSE_KEY)
    value = {"paused": paused_, "reason": reason, "at": now_utc().isoformat()}
    if row is None:
        session.add(CeControl(key=PAUSE_KEY, value=value, updated_at=now_utc()))
    else:
        row.value, row.updated_at = value, now_utc()
    session.flush()


# --- planning initials ------------------------------------------------------


def initials_counted_today(session: Session, cal: BusinessCalendar, day: date) -> int:
    start = cal.opening(day).replace(hour=0, minute=0)
    end = start + timedelta(days=1)
    return session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.step == "INITIAL",
        CeMessage.status.in_(("SCHEDULED", "SENDING", "SENT", "SHADOW")),
        CeMessage.due_at >= start, CeMessage.due_at < end)) or 0


#: No lane may take more than this share of a day's first emails, even when
#: another lane is short: the mix is an experiment, and an easy lane (the Odoo
#: directory) must not quietly become the whole programme. Shortage is reported.
MAX_LANE_SHARE = 0.6


def _lane_quotas(strategy: Strategy, slots: int) -> dict[str, int]:
    mix = strategy.lane_mix
    quotas = {lane: int(slots * share) for lane, share in mix.items()}
    leftover = slots - sum(quotas.values())
    for lane in sorted(mix, key=lambda k: -mix[k]):
        if leftover <= 0:
            break
        quotas[lane] += 1
        leftover -= 1
    return quotas


def plan_initials(session: Session, strategy: Strategy, *, now: datetime | None = None,
                  actor: str = "system:planner", shadow: bool | None = None) -> dict[str, Any]:
    """Schedule today's first contacts, spread over the business day.

    In shadow mode a target is rehearsed once. Live, a target whose first
    email was only ever rehearsed (SHADOW) is plannable for real.
    """
    if shadow is None:
        from boro_gtm.engine.settings import engine_settings

        shadow = engine_settings().shadow_mode
    cal = BusinessCalendar(strategy)
    moment = now or now_utc()
    day = cal.business_day_of(moment)
    if not cal.is_business_day(day):
        return {"planned": 0, "reason": "not_a_business_day"}
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CAP_LOCK})
    already = initials_counted_today(session, cal, day)
    slots = max(0, strategy.daily_cap - already)
    if slots == 0:
        return {"planned": 0, "reason": "daily_cap_reached", "cap": strategy.daily_cap}

    has_initial = select(CeMessage.target_id).where(CeMessage.step == "INITIAL")
    if not shadow:
        has_initial = has_initial.where(CeMessage.status.not_in(("SHADOW", "CANCELLED")))
    ready = list(session.scalars(
        select(CeTarget).where(CeTarget.state == S.READY, CeTarget.id.not_in(has_initial))
        .order_by(CeTarget.score.desc().nulls_last(), CeTarget.created_at)
    ).all())
    quotas = _lane_quotas(strategy, slots)
    chosen: list[CeTarget] = []
    for lane, quota in quotas.items():
        chosen.extend([t for t in ready if t.lane == lane][:quota])
    shortage = {lane: quota - len([t for t in chosen if t.lane == lane])
                for lane, quota in quotas.items()}
    shortage = {lane: n for lane, n in shortage.items() if n > 0}
    # A short lane's share goes to the best remaining targets of other lanes,
    # but never past MAX_LANE_SHARE of the day: quality is not lowered and one
    # lane does not become the programme. What stays unfilled is the shortage.
    cap_per_lane = max(1, math.ceil(strategy.daily_cap * MAX_LANE_SHARE))
    for target in [t for t in ready if t not in chosen]:
        if len(chosen) >= slots:
            break
        if sum(1 for t in chosen if t.lane == target.lane) < cap_per_lane:
            chosen.append(target)

    start = max(cal.next_open_moment(moment), moment)
    gap = timedelta(minutes=strategy.min_minutes_between_sends)
    closing = cal.closing(cal.business_day_of(start))
    planned, blocked = 0, []
    for target in chosen:
        contact = session.get(CeContact, target.contact_id) if target.contact_id else None
        reasons = send_blockers(session, strategy, target, contact, step="INITIAL")
        if any(r.startswith("gmail_history") for r in reasons):
            # Gmail shows BoRo already wrote here: take that sequence over if
            # it is still running, otherwise close the pursuit.
            from boro_gtm.engine.history import adopt_target

            adopt_target(session, strategy, target, actor=actor, now=moment)
            blocked.append({"target_id": str(target.id), "reasons": reasons})
            continue
        if reasons:
            _route_blocked(session, target, reasons, actor)
            blocked.append({"target_id": str(target.id), "reasons": reasons})
            continue
        jitter = int(hashlib.sha256(str(target.id).encode()).hexdigest(), 16) % 5
        due = start + gap * planned + timedelta(minutes=jitter)
        if due > closing:
            break
        _create_message(session, strategy, target, contact, "INITIAL", due)
        planned += 1
    return {"planned": planned, "blocked": blocked, "cap": strategy.daily_cap,
            "already_today": already, "quotas": quotas, "lane_shortage": shortage,
            "unfilled_slots": max(0, slots - len(chosen))}


def _route_blocked(session: Session, target: CeTarget, reasons: list[str], actor: str) -> None:
    if any(r.startswith("suppressed") for r in reasons):
        transition(session, target, S.SUPPRESSED, reason=";".join(reasons), actor=actor)
    elif "signal_expired" in reasons:
        transition(session, target, S.EXPIRED, reason="signal_expired", actor=actor)
    elif reasons == ["jurisdiction_review"]:
        # READY cannot go back to COMPLIANCE_REVIEW; it stays READY, unplanned,
        # and the report lists it. Enrichment routes new ones before READY.
        return
    else:
        transition(session, target, S.BLOCKED, reason=";".join(reasons), actor=actor)


def _create_message(session: Session, strategy: Strategy, target: CeTarget,
                    contact: CeContact, step: str, due: datetime,
                    in_reply_to: str | None = None) -> CeMessage:
    account = session.get(CeAccount, target.account_id)
    signal = session.get(CeSignal, target.signal_id) if target.signal_id else None
    rendered = render(strategy, target=target, account=account, contact=contact,
                      signal=signal, step=step)
    message = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id,
                                                     CeMessage.step == step))
    if message is not None and message.status not in ("SHADOW", "CANCELLED"):
        raise EngineError(f"{step} already exists for this target",
                          details={"target_id": str(target.id), "status": message.status})
    if message is None:
        # A rehearsed (SHADOW) or cancelled row is re-armed below; its Message-ID
        # never left the box, so reusing it is safe.
        message = CeMessage(target_id=target.id, step=step, created_at=now_utc())
        session.add(message)
    message.contact_id = contact.id
    message.lane, message.segment, message.country = target.lane, target.segment, target.country
    message.language, message.variant = rendered["language"], rendered["variant"]
    message.offer, message.strategy_version = rendered["offer"], strategy.version
    message.to_email = contact.email.lower()
    message.subject, message.body = rendered["subject"], rendered["body"]
    message.rfc822_message_id = rfc822_id(target.id, step, strategy.sender["email"])
    message.in_reply_to, message.status, message.due_at = in_reply_to, "SCHEDULED", due
    message.sent_at = message.claimed_at = message.last_error = None
    message.attempts = 0
    session.flush()
    return message


def rearm_shadow_followups(session: Session) -> int:
    """Live mode: follow-ups that were only rehearsed are owed for real."""
    expected = {"F1": S.CONTACTED, "F2": S.F1}
    count = 0
    for message in session.scalars(select(CeMessage).where(
            CeMessage.status == "SHADOW", CeMessage.step.in_(tuple(expected)))):
        target = session.get(CeTarget, message.target_id)
        if target.state == expected[message.step]:
            message.status, message.sent_at = "SCHEDULED", None
            count += 1
    session.flush()
    return count


# --- follow-up dates --------------------------------------------------------


def followup_due(strategy: Strategy, *, lane: str, step: str, initial_sent: datetime,
                 f1_sent: datetime | None = None,
                 expires_at: datetime | None = None) -> datetime | None:
    """When `step` is due, or None when it must not be sent at all."""
    cal = BusinessCalendar(strategy)
    rule = strategy.cadence(lane)
    sent_local = cal.local(initial_sent)
    if step == "F1":
        day = cal.add_business_days(sent_local.date(), rule["f1_business_days"])
        due = datetime.combine(day, sent_local.time(), cal.tz)
    elif step == "F2":
        if f1_sent is None:
            raise ValueError("F2 is computed from F1")
        earliest = max(initial_sent + timedelta(days=rule["f2_min_calendar_days"]),
                       f1_sent + timedelta(days=rule["f2_min_days_after_f1"]))
        latest = (initial_sent + timedelta(days=rule["f2_max_calendar_days"])
                  if "f2_max_calendar_days" in rule else None)
        due = earliest
        if latest is not None and due > latest:
            due = latest if latest > f1_sent + timedelta(days=1) else earliest
    else:
        raise ValueError(step)
    due = cal.next_open_moment(due)
    if step == "F2" and latest is not None and due > cal.closing(cal.local(latest).date()):
        # A weekend or holiday pushed F2 past the window's last day: use the
        # last business day inside the window instead, never one after it.
        floor = cal.local(f1_sent).date() + timedelta(days=rule["f2_min_days_after_f1"])
        day = cal.local(latest).date()
        while day >= floor and not cal.is_business_day(day):
            day -= timedelta(days=1)
        if day >= floor:
            due = cal.next_open_moment(datetime.combine(day, sent_local.time(), cal.tz))
    if expires_at is not None:
        if expires_at <= due:
            # Explicit demand: speed matters, so a follow-up that would land
            # after the deadline moves to the last business moment before it.
            before = cal.next_open_moment(expires_at - timedelta(days=1))
            if before >= expires_at or (step == "F2" and f1_sent and before <= f1_sent):
                return None
            due = before
    return due


def postpone_followups(session: Session, strategy: Strategy, target_id: uuid.UUID,
                       until: date) -> int:
    """An out-of-office reply moves pending follow-ups past the return date."""
    cal = BusinessCalendar(strategy)
    resume = cal.opening(cal.add_business_days(until, 1))
    moved = 0
    for message in session.scalars(select(CeMessage).where(
            CeMessage.target_id == target_id, CeMessage.status == "SCHEDULED",
            CeMessage.step != "INITIAL")):
        if message.due_at < resume:
            message.due_at = resume
            moved += 1
    session.flush()
    return moved


def cancel_pending(session: Session, target_id: uuid.UUID, reason: str) -> int:
    rows = list(session.scalars(select(CeMessage).where(
        CeMessage.target_id == target_id, CeMessage.status == "SCHEDULED")))
    for message in rows:
        message.status = "CANCELLED"
        message.last_error = reason
    session.flush()
    return len(rows)


# --- sending ----------------------------------------------------------------

_NEXT_STATE = {"INITIAL": S.CONTACTED, "F1": S.F1, "F2": S.F2}


def _last_real_send(session: Session) -> datetime | None:
    return session.scalar(select(func.max(CeMessage.sent_at)).where(CeMessage.status == "SENT"))


def send_due(session: Session, strategy: Strategy, gateway: MailGateway | None, *,
             shadow: bool, now: datetime | None = None, limit: int = 1,
             actor: str = "system:sender") -> list[dict[str, Any]]:
    """Send (or shadow) due messages. Commits per message, by design."""
    cal = BusinessCalendar(strategy)
    moment = now or now_utc()
    results: list[dict[str, Any]] = []
    if not shadow and gateway is None:
        return [{"skipped": "no_gateway"}]
    if not cal.within_hours(moment):
        return [{"skipped": "outside_business_hours"}]
    if not shadow and (pause := paused(session)):
        return [{"skipped": "paused", "reason": pause.get("reason")}]
    for _ in range(limit):
        if not shadow:
            last = _last_real_send(session)
            if last and moment - last < timedelta(minutes=strategy.min_minutes_between_sends):
                results.append({"skipped": "spacing"})
                break
        message = session.scalar(
            select(CeMessage).where(CeMessage.status == "SCHEDULED", CeMessage.due_at <= moment)
            .order_by(CeMessage.due_at).limit(1).with_for_update(skip_locked=True))
        if message is None:
            break
        results.append(_send_one(session, strategy, gateway, message, shadow=shadow,
                                 moment=moment, actor=actor))
        session.commit()
    return results


def _send_one(session: Session, strategy: Strategy, gateway: MailGateway | None,
              message: CeMessage, *, shadow: bool, moment: datetime,
              actor: str) -> dict[str, Any]:
    target = session.get(CeTarget, message.target_id)
    contact = session.get(CeContact, message.contact_id)
    reasons = send_blockers(session, strategy, target, contact, step=message.step)
    # In shadow mode the target never advances, so a follow-up would always
    # look "out of state". That is the only check shadow mode relaxes.
    if shadow and message.step != "INITIAL":
        reasons = [r for r in reasons if not r.startswith("state_")]
    if reasons:
        message.status = "BLOCKED"
        message.last_error = ";".join(reasons)
        session.flush()
        return {"message_id": str(message.id), "status": "BLOCKED", "reasons": reasons}
    if shadow:
        message.status = "SHADOW"
        message.sent_at = moment
        session.flush()
        return {"message_id": str(message.id), "status": "SHADOW", "to": message.to_email}

    message.status = "SENDING"
    message.attempts += 1
    message.claimed_at = now_utc()
    session.commit()  # durable intent before the network call
    try:
        existing = gateway.find_by_rfc822(message.rfc822_message_id)
        ref = existing or gateway.send(_mime(strategy, message), _thread_of(session, message))
    except Exception as exc:  # noqa: BLE001 - recorded, reconciled later
        message.last_error = str(exc)[:2000]
        session.flush()
        return {"message_id": str(message.id), "status": "SENDING_UNCONFIRMED",
                "error": message.last_error}
    _mark_sent(session, strategy, message, target, ref.message_id, ref.thread_id, moment, actor)
    return {"message_id": str(message.id), "status": "SENT", "to": message.to_email,
            "step": message.step}


def _thread_of(session: Session, message: CeMessage) -> str | None:
    if message.step == "INITIAL":
        return None
    return session.scalar(select(CeMessage.gmail_thread_id).where(
        CeMessage.target_id == message.target_id, CeMessage.step == "INITIAL"))


def _mime(strategy: Strategy, message: CeMessage) -> bytes:
    return build_mime(sender=strategy.sender["email"], sender_name=strategy.sender["name"],
                      to=message.to_email, subject=message.subject, body=message.body,
                      rfc822_id=message.rfc822_message_id, in_reply_to=message.in_reply_to,
                      when=now_utc())


def _mark_sent(session: Session, strategy: Strategy, message: CeMessage, target: CeTarget,
               gmail_id: str, thread_id: str, moment: datetime, actor: str) -> None:
    message.status = "SENT"
    message.sent_at = message.sent_at or moment
    message.gmail_message_id = gmail_id
    message.gmail_thread_id = thread_id
    message.last_error = None
    session.flush()
    transition(session, target, _NEXT_STATE[message.step],
               reason=f"{message.step.lower()}_sent", actor=actor,
               details={"message_id": str(message.id)}, at=message.sent_at)
    _schedule_next(session, strategy, target, message)


def _schedule_next(session: Session, strategy: Strategy, target: CeTarget,
                   sent: CeMessage) -> None:
    contact = session.get(CeContact, sent.contact_id)
    initial = session.scalar(select(CeMessage).where(
        CeMessage.target_id == target.id, CeMessage.step == "INITIAL"))
    if sent.step == "INITIAL":
        due = followup_due(strategy, lane=target.lane, step="F1", initial_sent=sent.sent_at,
                           expires_at=target.expires_at)
        nxt = "F1"
    elif sent.step == "F1":
        due = followup_due(strategy, lane=target.lane, step="F2",
                           initial_sent=initial.sent_at, f1_sent=sent.sent_at,
                           expires_at=target.expires_at)
        nxt = "F2"
    else:
        return
    if due is None:
        return
    _create_message(session, strategy, target, contact, nxt, due,
                    in_reply_to=initial.rfc822_message_id)


def reconcile_sending(session: Session, strategy: Strategy, gateway: MailGateway,
                      *, older_than: timedelta = timedelta(minutes=10),
                      actor: str = "system:reconciler") -> list[dict[str, Any]]:
    """A message left SENDING by a dead worker is resolved against Gmail, never resent blind."""
    cutoff = now_utc() - older_than
    out = []
    for message in list(session.scalars(select(CeMessage).where(
            CeMessage.status == "SENDING", CeMessage.claimed_at < cutoff)
            .with_for_update(skip_locked=True))):
        ref = gateway.find_by_rfc822(message.rfc822_message_id)
        target = session.get(CeTarget, message.target_id)
        if ref is not None:
            _mark_sent(session, strategy, message, target, ref.message_id, ref.thread_id,
                       now_utc(), actor)
            out.append({"message_id": str(message.id), "resolved": "SENT"})
        elif message.attempts >= 3:
            message.status = "FAILED"
            out.append({"message_id": str(message.id), "resolved": "FAILED"})
        else:
            message.status = "SCHEDULED"
            message.due_at = now_utc() + timedelta(minutes=15)
            out.append({"message_id": str(message.id), "resolved": "RESCHEDULED"})
        session.commit()
    return out


# --- closing sequences ------------------------------------------------------


def close_sequences(session: Session, strategy: Strategy, *, now: datetime | None = None,
                    grace_days: int = 7, actor: str = "system:closer") -> dict[str, int]:
    """F2 with no reply → CLOSED_NO_REPLY; passed deadlines → EXPIRED."""
    moment = now or now_utc()
    closed = expired = 0
    for target in list(session.scalars(select(CeTarget).where(
            CeTarget.expires_at.is_not(None), CeTarget.expires_at <= moment,
            CeTarget.state.in_((S.DISCOVERED, S.ENRICH, S.COMPLIANCE_REVIEW, S.READY,
                                S.CONTACTED, S.F1, S.F2))))):
        cancel_pending(session, target.id, "expired")
        transition(session, target, S.EXPIRED, reason="opportunity_deadline_passed",
                   actor=actor, at=moment)
        expired += 1
    for target in list(session.scalars(select(CeTarget).where(CeTarget.state == S.F2))):
        f2 = session.scalar(select(CeMessage).where(
            CeMessage.target_id == target.id, CeMessage.step == "F2",
            CeMessage.status == "SENT"))
        if f2 and f2.sent_at and moment - f2.sent_at >= timedelta(days=grace_days):
            transition(session, target, S.CLOSED_NO_REPLY, reason="no_reply_after_f2",
                       actor=actor, at=moment)
            closed += 1
    return {"closed_no_reply": closed, "expired": expired}


def deliverability_guard(session: Session, strategy: Strategy) -> dict[str, Any]:
    """Pause sending when recent bounces exceed the configured rate."""
    rules = strategy.deliverability
    recent = list(session.scalars(select(CeMessage.to_email).where(CeMessage.status == "SENT")
                                  .order_by(CeMessage.sent_at.desc())
                                  .limit(rules["bounce_window_sends"])))
    if len(recent) < rules["min_sends_before_bounce_check"]:
        return {"checked": False, "sends": len(recent)}
    bounced = session.scalar(select(func.count()).select_from(CeMailEvent).where(
        CeMailEvent.classification == "bounce",
        CeMailEvent.headers["failed_recipient"].astext.in_(recent))) or 0
    rate = bounced / len(recent)
    breached = rate > rules["max_bounce_rate"]
    if breached and rules.get("pause_on_breach") and not paused(session):
        set_pause(session, paused_=True,
                  reason=f"bounce rate {rate:.1%} over the last {len(recent)} sends")
    return {"checked": True, "sends": len(recent), "bounces": bounced, "rate": round(rate, 4),
            "breached": breached}
