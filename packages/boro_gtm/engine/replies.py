"""Gmail ingestion and reply handling (CE-ADR-007, CE-ADR-008).

Ingestion is idempotent on the Gmail message id. Classification is rules first:
bounces, auto-replies and opt-outs are recognized deterministically, because
each one suppresses an address or moves a date and must never depend on a
model's mood. A model may *suggest* a label for an ordinary human reply; any
human reply moves the target to REPLIED regardless, and anything touching
price, scope, dates, SLA, exclusivity, IP, legal terms, capacity or
subcontracting is escalated to Amaru.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import states as S
from boro_gtm.engine.gmail import MailGateway, RemoteMessage, parse_date
from boro_gtm.engine.llm import LlmProvider, NullProvider
from boro_gtm.engine.models import CeContact, CeControl, CeMailEvent, CeMessage, CeTarget
from boro_gtm.engine.outreach import cancel_pending, postpone_followups
from boro_gtm.engine.pipeline import (
    FREEMAIL,
    email_domain,
    normalize_email,
    now_utc,
    suppress,
    transition,
)
from boro_gtm.engine.strategy import Strategy

LABELS = ("bounce", "auto_reply", "bulk", "opt_out", "rejection", "referral", "meeting_request",
          "pricing_request", "scope_request", "capability_request", "request_info",
          "positive_interest", "not_relevant", "human_other")

_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
_BOUNCE_SUBJECT = re.compile(
    r"(delivery status notification|undeliverable|undelivered|mail delivery (failed|subsystem)"
    r"|returned mail|failure notice|no se (pudo|ha podido) entregar|mensaje no entregado)", re.I)
_AUTO_SUBJECT = re.compile(
    r"(out of (the )?office|automatic reply|auto.?reply|autoreply|respuesta autom[aá]tica"
    r"|fuera de (la )?oficina|vacaciones|on vacation|away from)", re.I)
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("opt_out", re.compile(
        r"(unsubscribe|remove me|take me off|stop (emailing|writing)|do not (email|contact)"
        r"|no me (escrib|contact)|da(r|me|rme)? de baja|d[eé]nme de baja"
        r"|no (m[aá]s|vuelvan a) (correo|escrib)"
        r"|elimin(a|en)(me)? de (su|tu|la) lista"
        # CE-ADR-022: "no contactar", "no contacten" and a bare "baja" are opt-outs too.
        r"|no (me |nos )?contact(ar|en|es)|\bbaja\b)", re.I)),
    ("rejection", re.compile(
        r"(not interested|no (estamos|estoy) interesad|no nos interesa|no me interesa|no,? gracias"
        r"|no thanks|we('re| are) (all )?set|ya (tenemos|contamos)|we already (have|work)"
        r"|no necesitamos|not (a )?(good )?fit|no es (el|un) buen momento)", re.I)),
    ("referral", re.compile(
        r"(the right person|best person|reach out to|contact(ar)? a |escrib(e|ir)le a|habla con"
        r"|copying|cc'?ing|te (presento|copio)|copio a|forwarded to|reenvi[eé])", re.I)),
    ("meeting_request", re.compile(
        r"(meeting|reuni[oó]n|llamada|a call|una call|calendly|agenda(r|mos)|schedule a|"
        r"hablemos|let'?s talk|zoom|google meet|teams)", re.I)),
    ("pricing_request", re.compile(
        r"(pric(e|ing)|cost|rate card|hourly rate|tarifa|precio|costo|cotiza|presupuesto|"
        r"cu[aá]nto (cuesta|cobran|sale))", re.I)),
    ("scope_request", re.compile(r"(scope|alcance|requirements|requerimientos|brief)", re.I)),
    ("capability_request", re.compile(
        r"(portfolio|portafolio|case stud|casos de [ée]xito|experience (with|in)"
        r"|experiencia (con|en)"
        r"|referencias|references)", re.I)),
    ("not_relevant", re.compile(
        r"(not relevant|no aplica|wrong person|persona equivocada|no corresponde|no somos)", re.I)),
    ("positive_interest", re.compile(
        r"(interested|interesa|sounds (good|great)|me parece (bien|bueno)|suena bien|perfecto"
        r"|of course|claro|con gusto|happy to)", re.I)),
]
ESCALATION_TERMS: dict[str, re.Pattern[str]] = {
    "pricing": re.compile(r"(pric|cost|tarifa|precio|costo|cotiza|presupuesto|budget|fee)", re.I),
    "discount": re.compile(r"(discount|descuento|rebaja)", re.I),
    "scope": re.compile(r"(scope|alcance|requirements|requerimientos|deliverable|entregable)",
                        re.I),
    "dates": re.compile(r"(deadline|plazo|fecha|start date|timeline|cronograma|by (monday|"
                        r"tuesday|wednesday|thursday|friday|next week))", re.I),
    "sla": re.compile(r"\b(sla|uptime|support hours|tiempo de respuesta)\b", re.I),
    "exclusivity": re.compile(r"(exclusiv)", re.I),
    "ip": re.compile(r"(intellectual property|propiedad intelectual|\bip\b|source code ownership"
                     r"|c[oó]digo fuente)", re.I),
    "legal": re.compile(r"(contract|contrato|nda|legal|terms and conditions|t[eé]rminos|"
                        r"liabilit|responsabilidad|factura|invoice)", re.I),
    "capacity": re.compile(r"(how many (devs|developers|people)|team size|tama[ñn]o del equipo|"
                           r"dedicated|full.?time|capacidad|capacity)", re.I),
    "subcontracting": re.compile(r"(subcontrat|white.?label|marca blanca|outsourc)", re.I),
    "negotiation": re.compile(r"(negotiat|negocia|counter.?offer|contraoferta)", re.I),
}
_DATE_PATTERNS = [
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})"),
]
_MONTHS = {m: i for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
     "octubre", "noviembre", "diciembre"], start=1)}
_MONTHS.update({m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)})
_MONTH_DAY = re.compile(r"(\d{1,2}) de (" + "|".join(_MONTHS) + r")|(" + "|".join(_MONTHS)
                        + r") (\d{1,2})", re.I)


@dataclass
class Classification:
    label: str
    source: str = "RULES"
    escalate: bool = False
    escalation_reasons: list[str] = field(default_factory=list)
    failed_recipient: str | None = None
    return_date: date | None = None


def classify(message: RemoteMessage, *, llm: LlmProvider | None = None,
             today: date | None = None) -> Classification:
    headers = {k.lower(): v for k, v in message.headers.items()}
    sender = (message.from_email or "").lower()
    subject = headers.get("subject", "")
    text = f"{subject}\n{message.body_text or message.snippet}"

    if (sender.startswith(_BOUNCE_SENDERS) or "delivery-status" in headers.get("content-type", "")
            or _BOUNCE_SUBJECT.search(subject)):
        failed = normalize_email(headers.get("x-failed-recipients", "").split(",")[0])
        if failed is None:
            found = re.search(r"(?:final-recipient:\s*rfc822;\s*|<|\s)([\w.+'-]+@[\w.-]+\.\w+)",
                              text, re.I)
            failed = normalize_email(found.group(1)) if found else None
        return Classification("bounce", failed_recipient=failed)

    if "list-unsubscribe" in headers or headers.get("precedence", "").lower() in ("bulk", "list"):
        # A newsletter's "unsubscribe" link is not anyone asking BoRo to stop.
        return Classification("bulk")

    auto = headers.get("auto-submitted", "no").lower()
    if (auto not in ("", "no") or "x-autoreply" in headers or "x-autorespond" in headers
            or headers.get("precedence", "").lower() in ("auto_reply", "bulk", "junk")
            or _AUTO_SUBJECT.search(subject)):
        return Classification("auto_reply", return_date=_return_date(text, today or date.today()))

    stripped = _strip_quoted(text)
    if re.fullmatch(r"\s*(re:[^\n]*\n)?\s*no\.?\s*", stripped, re.I | re.S):
        return Classification("opt_out")
    label = None
    for name, pattern in _RULES:
        if pattern.search(stripped):
            label = name
            break
    source = "RULES"
    if label is None:
        suggestion = (llm or NullProvider()).classify_reply(stripped)
        if suggestion in LABELS and suggestion not in ("bounce", "auto_reply", "bulk", "opt_out"):
            label, source = suggestion, "LLM"
        else:
            label = "human_other"
    reasons = [k for k, p in ESCALATION_TERMS.items() if p.search(stripped)]
    return Classification(label, source=source, escalate=bool(reasons) or label in (
        "pricing_request", "scope_request", "meeting_request", "referral"),
        escalation_reasons=reasons)


def _strip_quoted(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^(on .* wrote:|el .* escribi[oó]:)", line, re.I):
            break
        lines.append(line)
    return "\n".join(lines)


def _return_date(text: str, today: date) -> date | None:
    candidates: list[date] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                if len(m.group(1)) == 4:
                    candidates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                else:
                    candidates.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
            except ValueError:
                continue
    for m in _MONTH_DAY.finditer(text.lower()):
        day = int(m.group(1) or m.group(4))
        month = _MONTHS[(m.group(2) or m.group(3)).lower()]
        year = today.year + (1 if month < today.month else 0)
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    future = [d for d in candidates if today <= d <= today + timedelta(days=60)]
    return max(future) if future else None


# --- ingestion --------------------------------------------------------------

CURSOR_KEY = "gmail_ingest_cursor"
BACKFILL_KEY = "gmail_history_backfill"
#: Everything except chats and unsent drafts, trash included: a sent message
#: someone later deleted was still sent. Spam is listed and then skipped.
_QUERY = "after:{after} in:anywhere -in:chats -in:drafts"


def ingest(session: Session, strategy: Strategy, gateway: MailGateway, *,
           since: date | None = None, max_messages: int = 500,
           llm: LlmProvider | None = None, apply_effects: bool = True,
           backfill: bool = False) -> dict[str, Any]:
    """Pull new Gmail messages, store each once, and apply its consequence.

    `max_messages` bounds new messages fetched per call. The cursor advances
    only when the listing was read to the end, so a busy mailbox is caught up
    over several calls instead of having its oldest messages skipped.
    """
    from boro_gtm.engine import history

    control = session.get(CeControl, CURSOR_KEY)
    after = since or (date.fromisoformat(control.value["after"]) if control else
                      date.today() - timedelta(days=120))
    query = _QUERY.format(after=after.strftime("%Y/%m/%d"))
    stats: dict[str, Any] = {"seen": 0, "new": 0, "skipped_drafts_spam": 0, "effects": 0,
                             "query": query, "complete": False}
    token: str | None = None
    newest = after
    while stats["new"] < max_messages:
        ids, token = gateway.list_ids(query, token)
        for gid in ids:
            stats["seen"] += 1
            if session.scalar(select(CeMailEvent.id).where(CeMailEvent.gmail_message_id == gid)):
                continue
            if stats["new"] >= max_messages:
                token = token or "budget"
                break
            remote = gateway.get(gid)
            if {"DRAFT", "SPAM"} & set(remote.label_ids):
                stats["skipped_drafts_spam"] += 1
                continue
            event = _store(session, strategy, remote, llm)
            stats["new"] += 1
            sent = event.sent_at.date() if event.sent_at else newest
            newest = max(newest, sent - timedelta(days=1))
            if apply_effects:
                stats["effects"] += (apply_reply(session, strategy, event)
                                     if event.direction == "IN" else
                                     history.apply_outbound(session, strategy, event))
        if not token:
            stats["complete"] = True
            break
    if stats["complete"] and not since:
        value = {"after": newest.isoformat()}
        if control is None:
            session.add(CeControl(key=CURSOR_KEY, value=value, updated_at=now_utc()))
        else:
            control.value, control.updated_at = value, now_utc()
    if backfill:
        record = {"since": after.isoformat(), "complete": stats["complete"],
                  "seen": stats["seen"], "new": stats["new"], "at": now_utc().isoformat()}
        row = session.get(CeControl, BACKFILL_KEY)
        if row is None:
            session.add(CeControl(key=BACKFILL_KEY, value=record, updated_at=now_utc()))
        elif stats["complete"] or not row.value.get("complete"):
            row.value, row.updated_at = record, now_utc()
    if apply_effects:
        stats["history"] = {k: (len(v) if isinstance(v, list) else v)
                            for k, v in history.reconcile(session, strategy).items()}
    session.flush()
    return stats


def _store(session: Session, strategy: Strategy, remote: RemoteMessage,
           llm: LlmProvider | None) -> CeMailEvent:
    # Gmail's SENT label is the proof; a message merely *from* the sender
    # (a draft, a copy) is not a send.
    outgoing = "SENT" in remote.label_ids and "DRAFT" not in remote.label_ids
    when = parse_date(remote.headers.get("Date")) or datetime.fromtimestamp(
        remote.internal_date, UTC)
    event = CeMailEvent(
        gmail_message_id=remote.id, gmail_thread_id=remote.thread_id,
        rfc822_message_id=remote.headers.get("Message-ID"),
        direction="OUT" if outgoing else "IN", from_email=remote.from_email,
        to_emails=remote.to_emails, subject=remote.headers.get("Subject"),
        snippet=(remote.snippet or "")[:500],
        headers={k: v for k, v in remote.headers.items() if k != "Subject"},
        sent_at=when, ingested_at=now_utc(),
    )
    if not outgoing:
        result = classify(remote, llm=llm, today=when.date())
        event.classification = result.label
        event.classification_source = result.source
        event.escalate = result.escalate
        event.escalation_reasons = result.escalation_reasons
        if result.failed_recipient:
            event.headers = {**event.headers, "failed_recipient": result.failed_recipient}
        if result.return_date:
            event.headers = {**event.headers, "return_date": result.return_date.isoformat()}
    event.message_id, event.target_id = _match(session, remote, event)
    session.add(event)
    session.flush()
    return event


def _match(session: Session, remote: RemoteMessage, event: CeMailEvent):
    message = session.scalar(select(CeMessage).where(
        CeMessage.gmail_thread_id == remote.thread_id).order_by(CeMessage.created_at).limit(1))
    if message is None:
        refs = " ".join(remote.headers.get(h, "") for h in ("In-Reply-To", "References"))
        for ref in re.findall(r"<[^>]+>", refs):
            message = session.scalar(select(CeMessage).where(CeMessage.rfc822_message_id == ref))
            if message:
                break
    if message is None and event.headers.get("failed_recipient"):
        message = session.scalar(select(CeMessage).where(
            CeMessage.to_email == event.headers["failed_recipient"],
            CeMessage.status == "SENT").order_by(CeMessage.sent_at.desc()).limit(1))
    if message is None and event.direction == "IN" and event.from_email:
        contact = session.scalar(select(CeContact).where(CeContact.email == event.from_email))
        if contact is not None:
            target = session.scalar(select(CeTarget).where(
                CeTarget.contact_id == contact.id, CeTarget.state.in_(S.IN_SEQUENCE)))
            return None, target.id if target else None
    if message is None:
        return None, None
    return message.id, message.target_id


def apply_reply(session: Session, strategy: Strategy, event: CeMailEvent,
                actor: str = "system:replies") -> int:
    """The deterministic consequence of one inbound message. Returns 1 if anything changed."""
    if event.processed_at is not None:
        return 0
    event.processed_at = now_utc()
    label = event.classification
    target = session.get(CeTarget, event.target_id) if event.target_id else None
    from boro_gtm.engine import research

    research.record_reply(session, event.message_id, label or "human_other", actor=actor)
    if label == "bounce":
        failed = event.headers.get("failed_recipient")
        if failed:
            suppress(session, kind="EMAIL", value=failed, reason="BOUNCE",
                     evidence_ref=f"gmail:{event.gmail_message_id}")
        if target is not None and target.state in S.IN_SEQUENCE:
            cancel_pending(session, target.id, "bounce")
            transition(session, target, S.SUPPRESSED, reason="bounce", actor=actor,
                       details={"gmail_message_id": event.gmail_message_id})
        return 1
    if label == "auto_reply":
        returning = event.headers.get("return_date")
        if target is not None and returning:
            postpone_followups(session, strategy, target.id, date.fromisoformat(returning))
            return 1
        return 0
    if label == "opt_out":
        if not _answers_boro(session, event):
            return 0
        if event.from_email:
            suppress(session, kind="EMAIL", value=event.from_email, reason="OPT_OUT",
                     evidence_ref=f"gmail:{event.gmail_message_id}")
            # The whole company asked not to be written to (CE-ADR-022), never a freemail host.
            domain = email_domain(event.from_email)
            if domain and domain not in FREEMAIL:
                suppress(session, kind="DOMAIN", value=domain, reason="OPT_OUT",
                         evidence_ref=f"gmail:{event.gmail_message_id}")
        if target is not None and target.state in S.IN_SEQUENCE + (S.REPLIED,):
            cancel_pending(session, target.id, "opt_out")
            transition(session, target, S.SUPPRESSED, reason="opt_out", actor=actor)
        return 1
    if target is None or target.state not in S.IN_SEQUENCE:
        return 0
    # Any human reply stops the sequence. What it means is for Amaru, unless it
    # is a plain rejection, which closes this pursuit (a new signal may reopen).
    cancel_pending(session, target.id, f"reply:{label}")
    transition(session, target, S.REPLIED, reason=f"reply:{label}", actor=actor,
               details={"gmail_message_id": event.gmail_message_id,
                        "escalate": event.escalate,
                        "escalation_reasons": event.escalation_reasons})
    if label in ("rejection", "not_relevant"):
        transition(session, target, S.LOST, reason=f"rejection:{label}", actor=actor)
    return 1


def _answers_boro(session: Session, event: CeMailEvent) -> bool:
    """Is this inbound message a response to something BoRo sent?"""
    if event.message_id is not None or event.target_id is not None:
        return True
    if session.scalar(select(CeMailEvent.id).where(
            CeMailEvent.gmail_thread_id == event.gmail_thread_id,
            CeMailEvent.direction == "OUT").limit(1)):
        return True
    return bool(event.from_email and session.scalar(select(CeMailEvent.id).where(
        CeMailEvent.direction == "OUT", CeMailEvent.to_emails.any(event.from_email)).limit(1)))


def domain_of(email: str) -> str | None:
    return email_domain(email)
