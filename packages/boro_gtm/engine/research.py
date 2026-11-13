"""Research briefs → drafts → approval → first emails, and the weekly scoreboard (CE-ADR-022).

A first email exists only for an APPROVED brief: a named person on the
company's own domain, a recent checkable fact, a doubt-phrased hypothesis and a
question about what they did. The model writes only from the brief; the gate
(`personalization_gate`) checks both brief and draft; Amaru approves the first
drafts of every batch and any draft with warnings. Jurisdictions where the
engine never sends get MANUAL_OUTREACH tasks a person works by hand.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from boro_gtm.engine import personalization_gate as gate
from boro_gtm.engine import states as S
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeDraftGeneration,
    CeMessage,
    CeResearchBrief,
    CeScoreEvent,
    CeSignal,
    CeTarget,
)
from boro_gtm.engine.pipeline import EngineError, normalize_email, now_utc
from boro_gtm.engine.strategy import Strategy

BRIEF_FIELDS = ("recipient_name", "recipient_role", "recipient_email", "email_source_url",
                "fact_text", "fact_source_url", "fact_date", "hypothesis", "question",
                "why_now", "proof_case_id", "batch")
POSITIVE_LABELS = ("meeting_request", "pricing_request", "scope_request", "capability_request",
                   "positive_interest", "referral", "request_info")


# --- strategy switches ------------------------------------------------------


def personalization(strategy: Strategy) -> dict[str, Any]:
    return strategy.raw.get("personalization") or {}


def required(strategy: Strategy) -> bool:
    return bool(personalization(strategy).get("required"))


def focus(strategy: Strategy) -> dict[str, Any]:
    return strategy.raw.get("focus") or {}


def manual_jurisdictions(strategy: Strategy) -> set[str]:
    return {c.upper() for c in strategy.jurisdictions.get("manual_outreach", [])}


def in_focus(session: Session, strategy: Strategy, target: CeTarget,
             today: date | None = None) -> bool:
    """The one active lane (es_odoo_partners until its end date); every other lane is paused."""
    spec = focus(strategy)
    if not spec:
        return True
    until = spec.get("until")
    if until and (today or now_utc().date()) > date.fromisoformat(until):
        return False
    if target.lane != spec.get("lane"):
        return False
    signal = session.get(CeSignal, target.signal_id) if target.signal_id else None
    prefix = spec.get("source_prefix")
    if prefix and not (signal and signal.source_key.startswith(prefix)):
        return False
    countries = {c.upper() for c in spec.get("countries", [])}
    return not countries or (target.country or "").upper() in countries


def lane_paused(strategy: Strategy, lane: str) -> bool:
    return lane in set(focus(strategy).get("paused_lanes", []))


# --- scoreboard events ------------------------------------------------------


def score(session: Session, kind: str, *, actor: str, brief: CeResearchBrief | None = None,
          target_id: uuid.UUID | None = None, details: dict[str, Any] | None = None,
          at: datetime | None = None) -> None:
    if kind not in S.SCORE_EVENTS:
        raise EngineError(f"unknown scoreboard event {kind}", details={"allowed": S.SCORE_EVENTS})
    session.add(CeScoreEvent(kind=kind, brief_id=brief.id if brief else None,
                             target_id=target_id or (brief.target_id if brief else None),
                             batch=brief.batch if brief else None, actor=actor,
                             details=details or {}, at=at or now_utc()))
    session.flush()


# --- briefs ------------------------------------------------------------------


def _domains(session: Session, brief: CeResearchBrief) -> set[str]:
    account = session.get(CeAccount, brief.account_id)
    return {account.domain} if account else set()


def mx_ok(domain: str, resolver=None) -> bool | None:
    """True with an MX record, False with none, None when DNS could not be asked."""
    import dns.exception
    import dns.resolver

    try:
        answers = (resolver or dns.resolver).resolve(domain, "MX", lifetime=8)
        return len(list(answers)) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except dns.exception.DNSException:
        return None


def upsert_brief(session: Session, target_id: uuid.UUID, fields: dict[str, Any], *,
                 actor: str) -> CeResearchBrief:
    """Record what a person researched. Status is recomputed by `evaluate`, never set here."""
    target = session.get(CeTarget, target_id)
    if target is None:
        raise EngineError("unknown target", details={"target_id": str(target_id)})
    unknown = set(fields) - set(BRIEF_FIELDS)
    if unknown:
        raise EngineError("unknown brief fields", details={"fields": sorted(unknown),
                                                           "allowed": BRIEF_FIELDS})
    values = dict(fields)
    if "recipient_email" in values:
        values["recipient_email"] = normalize_email(values["recipient_email"]) or \
            (values["recipient_email"] or "").strip().lower() or None
    if isinstance(values.get("fact_date"), str):
        values["fact_date"] = date.fromisoformat(values["fact_date"])
    email = values.get("recipient_email")
    brief = None
    if email:
        brief = session.scalar(select(CeResearchBrief).where(
            CeResearchBrief.target_id == target.id, CeResearchBrief.recipient_email == email))
    if brief is None:
        # A migrated brief (NEEDS_RESEARCH) for this target is the one research fills in.
        brief = session.scalar(select(CeResearchBrief).where(
            CeResearchBrief.target_id == target.id,
            CeResearchBrief.status == "NEEDS_RESEARCH").order_by(CeResearchBrief.created_at))
    if brief is not None and brief.status in ("APPROVED", "SENT"):
        raise EngineError("an approved or sent brief is not edited; research a new recipient",
                          details={"brief_id": str(brief.id), "status": brief.status})
    if brief is None:
        brief = CeResearchBrief(target_id=target.id, account_id=target.account_id,
                                status="NEEDS_RESEARCH", reasons=[], warnings=[],
                                created_at=now_utc(), updated_at=now_utc())
        session.add(brief)
    for key, value in values.items():
        setattr(brief, key, value)
    brief.status, brief.updated_at = "NEEDS_RESEARCH", now_utc()
    session.flush()
    return brief


def evaluate(session: Session, strategy: Strategy, brief: CeResearchBrief, *,
             today: date | None = None, resolver=None, actor: str = "system:gate") -> gate.Verdict:
    """Run the gate. Fails → NEEDS_RESEARCH with reasons; passes → READY_TO_DRAFT."""
    if brief.status in ("APPROVED", "SENT", "DRAFTED", "MANUAL_OUTREACH"):
        return gate.Verdict()
    email = (brief.recipient_email or "").lower()
    domain = email.split("@")[1] if "@" in email else ""
    verdict = gate.check_brief(brief, company_domains=_domains(session, brief),
                               today=today or now_utc().date(),
                               mx_ok=mx_ok(domain, resolver) if domain else None)
    brief.reasons, brief.updated_at = verdict.errors, now_utc()
    if verdict.ok:
        if brief.status == "NEEDS_RESEARCH":
            score(session, "RESEARCHED", actor=actor, brief=brief)
        brief.status = "READY_TO_DRAFT"
    else:
        brief.status = "NEEDS_RESEARCH"
    session.flush()
    return verdict


# --- drafting -----------------------------------------------------------------


def build_prompt(strategy: Strategy, brief: CeResearchBrief, company: str) -> str:
    case = gate.proof_case(brief.proof_case_id) or {}
    caps = gate.capabilities()
    allowed = "; ".join(c["label"] for c in caps.get("allowed", []))
    blocked = "; ".join(b["reason"] for b in caps.get("blocked", []))
    sender = strategy.sender.get("name", "Amaru")
    return (
        "Escribe el primer correo en frío de BoRo Studio (estudio de software de Santiago de "
        "Chile) a una persona en un partner de Odoo. Usa SOLO los datos de la ficha; no "
        "inventes hechos, cifras, clientes ni capacidades.\n\n"
        f"Ficha:\n- Empresa: {company}\n- Destinatario: {brief.recipient_name} "
        f"({brief.recipient_role})\n- Dato verificado ({brief.fact_date}): {brief.fact_text}\n"
        f"- Hipótesis (formúlala como duda): {brief.hypothesis}\n"
        f"- Por qué ahora: {brief.why_now}\n- Pregunta a hacer, tal cual: {brief.question}\n"
        f"- Caso que puedes citar: {case.get('es', '')}\n\n"
        f"Lo que BoRo sí hace: {allowed}.\nNunca digas que BoRo implementó: {blocked}. No "
        "menciones otros casos.\n\n"
        "Reglas: español, trato de usted, saluda por el nombre, máximo 110 palabras en el "
        "cuerpo, una sola pregunta (la de la ficha), sin preguntar si les interesa, sin "
        "enlaces ni adjuntos. Termina con la firma \"" + sender.split()[0] + "\\nBoRo Studio · "
        "somosboro.com\" y debajo la línea: Si no es relevante, responda \"no\" y no le "
        "vuelvo a escribir.\n\n"
        "Responde solo con JSON: {\"subject\": \"...\", \"body\": \"...\"}. El asunto lleva el "
        "nombre de la empresa y no promete nada."
    )


def _parse_draft(text: str) -> tuple[str, str] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    subject, body = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
    return (subject, body) if subject and body else None


def _snapshot(brief: CeResearchBrief) -> dict[str, Any]:
    return {k: (str(getattr(brief, k)) if getattr(brief, k) is not None else None)
            for k in BRIEF_FIELDS}


def needs_approval(session: Session, strategy: Strategy, brief: CeResearchBrief,
                   warnings: list[str]) -> bool:
    first_n = int(personalization(strategy).get("first_n_per_batch_need_approval", 20))
    drafted_before = session.scalar(select(func.count()).select_from(CeScoreEvent).where(
        CeScoreEvent.kind == "DRAFTED", CeScoreEvent.batch.is_not_distinct_from(brief.batch),
        or_(CeScoreEvent.brief_id.is_(None), CeScoreEvent.brief_id != brief.id))) or 0
    return bool(warnings) or drafted_before < first_n


def draft(session: Session, strategy: Strategy, brief: CeResearchBrief, provider, *,
          actor: str = "system:drafter") -> dict[str, Any]:
    """Ask the model for a draft from the brief alone, then gate it. Records every attempt."""
    if brief.status != "READY_TO_DRAFT":
        raise EngineError("only a READY_TO_DRAFT brief is drafted",
                          details={"brief_id": str(brief.id), "status": brief.status})
    account = session.get(CeAccount, brief.account_id)
    prompt = build_prompt(strategy, brief, account.name or account.domain)
    answer = provider.draft(prompt)
    if answer is None:
        return {"brief_id": str(brief.id), "drafted": False, "reason": "no model available"}
    model, text = answer
    parsed = _parse_draft(text)
    subject, body = parsed if parsed else ("", text)
    verdict = gate.check_draft(subject, body, recipient_name=brief.recipient_name) if parsed \
        else gate.Verdict(errors=["model did not answer with {subject, body} JSON"])
    session.add(CeDraftGeneration(brief_id=brief.id, brief_snapshot=_snapshot(brief),
                                  prompt=prompt, model=model, subject=subject, body=body,
                                  validator=verdict.as_dict(), ok=verdict.ok,
                                  created_at=now_utc()))
    if not verdict.ok:
        brief.reasons, brief.updated_at = verdict.errors, now_utc()
        session.flush()
        return {"brief_id": str(brief.id), "drafted": False, "errors": verdict.errors}
    target = session.get(CeTarget, brief.target_id)
    brief.warnings, brief.reasons, brief.updated_at = verdict.warnings, [], now_utc()
    if (target.country or "").upper() in manual_jurisdictions(strategy):
        brief.status = "MANUAL_OUTREACH"
        score(session, "DRAFTED", actor=actor, brief=brief, details={"manual": True})
        return {"brief_id": str(brief.id), "drafted": True, "status": brief.status}
    must_approve = needs_approval(session, strategy, brief, verdict.warnings)
    brief.status = "DRAFTED"
    score(session, "DRAFTED", actor=actor, brief=brief)
    if not must_approve:
        approve(session, brief.id, actor="system:auto_approval")
    return {"brief_id": str(brief.id), "drafted": True, "status": brief.status,
            "needs_approval": must_approve, "warnings": verdict.warnings}


def latest_ok_draft(session: Session, brief_id: uuid.UUID) -> CeDraftGeneration | None:
    return session.scalar(select(CeDraftGeneration).where(
        CeDraftGeneration.brief_id == brief_id, CeDraftGeneration.ok.is_(True))
        .order_by(CeDraftGeneration.id.desc()).limit(1))


def approve(session: Session, brief_id: uuid.UUID, *, actor: str) -> CeResearchBrief:
    brief = session.get(CeResearchBrief, brief_id)
    if brief is None or brief.status != "DRAFTED":
        raise EngineError("only a DRAFTED brief is approved",
                          details={"brief_id": str(brief_id),
                                   "status": brief.status if brief else None})
    drafted = latest_ok_draft(session, brief.id)
    if drafted is None:
        raise EngineError("this brief has no draft that passed the gate")
    # The draft is checked again: config/*.yaml may have changed since it was written.
    verdict = gate.check_draft(drafted.subject, drafted.body,
                               recipient_name=brief.recipient_name)
    if not verdict.ok:
        brief.status, brief.reasons = "READY_TO_DRAFT", verdict.errors
        session.flush()
        raise EngineError("the draft no longer passes the gate", details=verdict.as_dict())
    brief.status, brief.approved_by, brief.approved_at = "APPROVED", actor, now_utc()
    brief.updated_at = now_utc()
    score(session, "APPROVED", actor=actor, brief=brief)
    return brief


# --- planning personalized first emails ----------------------------------------


def _contact_for(session: Session, brief: CeResearchBrief) -> CeContact:
    contact = session.scalar(select(CeContact).where(CeContact.email == brief.recipient_email))
    if contact is None:
        contact = CeContact(account_id=brief.account_id, full_name=brief.recipient_name,
                            role=brief.recipient_role, email=brief.recipient_email,
                            is_generic=False, source_url=brief.email_source_url,
                            verified_at=now_utc())
        session.add(contact)
        session.flush()
    return contact


def plan_personalized(session: Session, strategy: Strategy, *, now: datetime | None = None,
                      actor: str = "system:planner") -> dict[str, Any]:
    """Schedule today's first emails from APPROVED briefs only, up to the daily cap."""
    from boro_gtm.engine import outreach
    from boro_gtm.engine.business_days import BusinessCalendar
    from boro_gtm.engine.pipeline import send_blockers

    cal = BusinessCalendar(strategy)
    moment = now or now_utc()
    day = cal.business_day_of(moment)
    if not cal.is_business_day(day):
        return {"planned": 0, "reason": "not_a_business_day"}
    from sqlalchemy import text

    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": outreach._CAP_LOCK})
    already = outreach.initials_counted_today(session, cal, day)
    slots = max(0, strategy.daily_cap - already)
    if slots == 0:
        return {"planned": 0, "reason": "daily_cap_reached", "cap": strategy.daily_cap}
    briefs = list(session.scalars(select(CeResearchBrief).where(
        CeResearchBrief.status == "APPROVED").order_by(CeResearchBrief.approved_at)))
    start = max(cal.next_open_moment(moment), moment)
    gap = timedelta(minutes=strategy.min_minutes_between_sends)
    closing = cal.closing(cal.business_day_of(start))
    planned, skipped = 0, []
    for brief in briefs:
        if planned >= slots:
            break
        target = session.get(CeTarget, brief.target_id)
        if target.state != S.READY or not in_focus(session, strategy, target, day):
            skipped.append({"brief_id": str(brief.id), "reason": "not_ready_or_out_of_focus"})
            continue
        if session.scalar(select(CeMessage.id).where(
                CeMessage.target_id == target.id, CeMessage.step == "INITIAL",
                CeMessage.status.not_in(("SHADOW", "CANCELLED")))):
            continue
        drafted = latest_ok_draft(session, brief.id)
        if drafted is None or gate.claim_problems(f"{drafted.subject}\n{drafted.body}"):
            skipped.append({"brief_id": str(brief.id), "reason": "draft_missing_or_blocked"})
            continue
        contact = _contact_for(session, brief)
        target.contact_id = contact.id
        reasons = send_blockers(session, strategy, target, contact, step="INITIAL")
        if reasons:
            skipped.append({"brief_id": str(brief.id), "reasons": reasons})
            continue
        due = start + gap * planned
        if due > closing:
            break
        message = outreach._create_message(session, strategy, target, contact, "INITIAL", due)
        message.subject, message.body = drafted.subject, drafted.body
        message.variant, message.language, message.brief_id = "personalized-v1", "es", brief.id
        session.flush()
        planned += 1
    return {"planned": planned, "cap": strategy.daily_cap, "already_today": already,
            "approved_waiting": len(briefs) - planned, "skipped": skipped}


def mark_sent(session: Session, message: CeMessage, *, actor: str) -> None:
    """A personalized first email went out: the brief is SENT and the scoreboard counts it."""
    if message.brief_id is None or message.step != "INITIAL":
        return
    brief = session.get(CeResearchBrief, message.brief_id)
    if brief is not None and brief.status == "APPROVED":
        brief.status, brief.updated_at = "SENT", now_utc()
        score(session, "SENT", actor=actor, brief=brief, at=message.sent_at)


def record_reply(session: Session, message_id: uuid.UUID | None, label: str, *,
                 actor: str) -> None:
    """Bounces, replies and positive replies to personalized emails, for the scoreboard."""
    if message_id is None:
        return
    message = session.get(CeMessage, message_id)
    if message is None:
        return
    first = message if message.step == "INITIAL" else session.scalar(select(CeMessage).where(
        CeMessage.target_id == message.target_id, CeMessage.step == "INITIAL"))
    if first is None or first.brief_id is None:
        return
    brief = session.get(CeResearchBrief, first.brief_id)
    if label == "bounce":
        score(session, "BOUNCED", actor=actor, brief=brief)
        return
    if label in ("auto_reply", "bulk"):
        return
    score(session, "REPLIED", actor=actor, brief=brief, details={"label": label})
    if label in POSITIVE_LABELS:
        score(session, "POSITIVE_REPLY", actor=actor, brief=brief, details={"label": label})


# --- manual outreach (ES/EU/UK/CA) ---------------------------------------------


MANUAL_MARKS = {"sent": "MANUAL_SENT", "accepted": "MANUAL_ACCEPTED", "replied": "MANUAL_REPLIED"}


def manual_tasks(session: Session) -> list[dict[str, Any]]:
    rows = []
    for brief in session.scalars(select(CeResearchBrief).where(
            CeResearchBrief.status == "MANUAL_OUTREACH").order_by(CeResearchBrief.updated_at)):
        account = session.get(CeAccount, brief.account_id)
        target = session.get(CeTarget, brief.target_id)
        drafted = session.scalar(select(CeDraftGeneration).where(
            CeDraftGeneration.brief_id == brief.id, CeDraftGeneration.ok.is_(True))
            .order_by(CeDraftGeneration.id.desc()).limit(1))
        marks = [e.kind for e in session.scalars(select(CeScoreEvent).where(
            CeScoreEvent.brief_id == brief.id, CeScoreEvent.kind.in_(MANUAL_MARKS.values())))]
        rows.append({"brief_id": str(brief.id), "company": account.name, "domain": account.domain,
                     "country": target.country, "recipient_name": brief.recipient_name,
                     "recipient_role": brief.recipient_role, "fact": brief.fact_text,
                     "fact_source_url": brief.fact_source_url, "question": brief.question,
                     "subject": drafted.subject if drafted else "",
                     "draft": drafted.body if drafted else "", "done": ",".join(marks)})
    return rows


def manual_csv(rows: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    fields = ["brief_id", "company", "domain", "country", "recipient_name", "recipient_role",
              "fact", "fact_source_url", "question", "subject", "draft", "done"]
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def mark_manual(session: Session, brief_id: uuid.UUID, what: str, *, actor: str,
                note: str | None = None) -> dict[str, Any]:
    brief = session.get(CeResearchBrief, brief_id)
    if brief is None or brief.status != "MANUAL_OUTREACH":
        raise EngineError("not a manual outreach task", details={"brief_id": str(brief_id)})
    if what not in MANUAL_MARKS:
        raise EngineError("mark must be sent, accepted or replied",
                          details={"allowed": list(MANUAL_MARKS)})
    score(session, MANUAL_MARKS[what], actor=actor, brief=brief, details={"note": note})
    return {"brief_id": str(brief.id), "marked": what}


# --- scoreboard -------------------------------------------------------------------


HUMAN_MARKS = ("CONVERSATION", "NAMED_NEED", "PROPOSAL")


def mark(session: Session, kind: str, *, actor: str, target_id: uuid.UUID | None = None,
         brief_id: uuid.UUID | None = None, note: str | None = None) -> dict[str, Any]:
    """A person records a conversation, a named project/capacity gap, or a proposal."""
    if kind not in HUMAN_MARKS:
        raise EngineError("mark must be CONVERSATION, NAMED_NEED or PROPOSAL")
    brief = session.get(CeResearchBrief, brief_id) if brief_id else None
    if target_id is None and brief is None:
        raise EngineError("name a target or a brief")
    score(session, kind, actor=actor, brief=brief, target_id=target_id,
          details={"note": note} if note else {})
    return {"marked": kind}


def scoreboard(session: Session, strategy: Strategy, *, now: datetime | None = None,
               weeks: int = 8) -> dict[str, Any]:
    moment = now or now_utc()
    counts = dict(session.execute(select(CeScoreEvent.kind, func.count())
                                  .group_by(CeScoreEvent.kind)).all())

    def n(kind: str) -> int:
        return int(counts.get(kind, 0))

    totals = {"researched": n("RESEARCHED"), "drafted": n("DRAFTED"), "approved": n("APPROVED"),
              "sent": n("SENT"), "bounced": n("BOUNCED"),
              "delivered": max(0, n("SENT") - n("BOUNCED")), "replies": n("REPLIED"),
              "positive_replies": n("POSITIVE_REPLY"), "conversations": n("CONVERSATION"),
              "named_need": n("NAMED_NEED"), "proposals": n("PROPOSAL"),
              "manual": {"sent": n("MANUAL_SENT"), "accepted": n("MANUAL_ACCEPTED"),
                         "replied": n("MANUAL_REPLIED")}}
    since = moment - timedelta(weeks=weeks)
    weekly: dict[str, dict[str, int]] = {}
    for kind, at in session.execute(select(CeScoreEvent.kind, CeScoreEvent.at).where(
            CeScoreEvent.at >= since)):
        year, week, _ = at.isocalendar()
        weekly.setdefault(f"{year}-W{week:02d}", {}).setdefault(kind, 0)
        weekly[f"{year}-W{week:02d}"][kind] += 1
    by_batch: dict[str, dict[str, Any]] = {}
    for batch, kind, count in session.execute(
            select(CeScoreEvent.batch, CeScoreEvent.kind, func.count())
            .where(CeScoreEvent.kind.in_(("SENT", "BOUNCED", "REPLIED", "POSITIVE_REPLY")))
            .group_by(CeScoreEvent.batch, CeScoreEvent.kind)):
        by_batch.setdefault(batch or "(sin lote)", {})[kind.lower()] = count
    for row in by_batch.values():
        delivered = max(0, row.get("sent", 0) - row.get("bounced", 0))
        row["delivered"] = delivered
        row["reply_rate"] = round(row.get("replied", 0) / delivered, 3) if delivered else None
    spec = strategy.raw.get("scoreboard") or {}
    gate_a = spec.get("gate_a", {"date": "2026-12-12", "min_conversations": 10,
                                 "min_named_need": 3})
    deadline = date.fromisoformat(gate_a["date"])
    met = (totals["conversations"] >= gate_a["min_conversations"]
           and totals["named_need"] >= gate_a["min_named_need"])
    a_status = "PASSED" if met else ("FAILED" if moment.date() >= deadline else "PENDING")
    min_delivered = (spec.get("gate_b") or {}).get("min_delivered", 150)
    abandon = totals["delivered"] >= min_delivered and totals["positive_replies"] == 0
    return {
        "as_of": moment.isoformat(), "totals": totals, "weekly": weekly, "by_batch": by_batch,
        "gates": {
            "a_conversations_by_deadline": {
                "deadline": gate_a["date"], "status": a_status,
                "conversations": totals["conversations"], "need": gate_a["min_conversations"],
                "named_need": totals["named_need"], "need_named": gate_a["min_named_need"],
                "if_failed": gate_a.get("fallback", "E: sistema para clínicas sobre Odoo") +
                " (salida predefinida; la decide Amaru, el motor no cambia de foco)"},
            "b_abandon_channel": {"alert": abandon, "delivered": totals["delivered"],
                                  "threshold": min_delivered,
                                  "positive_replies": totals["positive_replies"],
                                  "message": "abandonar canal" if abandon else None},
            "c_reply_rate_by_batch": by_batch,
        },
    }
