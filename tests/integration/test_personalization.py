"""The personalization gate end to end (CE-ADR-022): brief → gate → draft → approval → send."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from boro_gtm.engine import research
from boro_gtm.engine import states as S
from boro_gtm.engine.gmail import FakeGateway
from boro_gtm.engine.models import (
    CeDraftGeneration,
    CeMessage,
    CeResearchBrief,
    CeScoreEvent,
    CeSuppression,
)
from boro_gtm.engine.outreach import plan_initials, send_due
from boro_gtm.engine.replies import classify, ingest
from boro_gtm.engine.strategy import load_strategy
from tests.integration.test_engine import WED_10, _ready_target, _reply

TODAY = WED_10.date()  # 2026-11-04, a Chilean business day
GOOD_BODY = ("Hola Ana,\n\nVi que publicaron dos implementaciones de Odoo 17 en octubre. No sé si "
             "diciembre les queda corto para módulos a medida.\n\n¿Cómo resolvieron la última "
             "migración de versión que tomaron?\n\nAmaru\nBoRo Studio · somosboro.com\n\n"
             "Si no es relevante, responda \"no\" y no le vuelvo a escribir.")


@pytest.fixture
def strategy():
    return load_strategy()


class Draws:
    """A model stand-in: returns the draft it is given, records the prompts it saw."""

    def __init__(self, subject="Acme-Odoo: migraciones de Odoo", body=GOOD_BODY):
        self.answer = json.dumps({"subject": subject, "body": body}, ensure_ascii=False)
        self.prompts: list[str] = []

    def draft(self, prompt):
        self.prompts.append(prompt)
        return "test-model", self.answer


class Resolver:
    def __init__(self, ok=True):
        self.ok = ok

    def resolve(self, domain, kind, lifetime=None):
        import dns.resolver

        if not self.ok:
            raise dns.resolver.NoAnswer()
        return ["mx.example"]


def _brief_fields(**overrides):
    fields = {"recipient_name": "Ana Pérez", "recipient_role": "Gerente de proyectos",
              "recipient_email": "ana.perez@acme-odoo.cl",
              "email_source_url": "https://www.acme-odoo.cl/equipo",
              "fact_text": "Publicaron dos implementaciones de Odoo 17 en octubre",
              "fact_source_url": "https://www.acme-odoo.cl/blog", "fact_date": "2026-10-20",
              "hypothesis": "No sé si diciembre les queda corto para módulos a medida",
              "question": "¿Cómo resolvieron la última migración de versión que tomaron?",
              "why_now": "Dos implementaciones nuevas antes de fin de año",
              "proof_case_id": "nutrition_practice", "batch": "2026-11-a"}
    fields.update(overrides)
    return fields


def _partner(session, strategy, domain="acme-odoo.cl", country="CL",
             source_key="odoo_partners_cl"):
    return _ready_target(session, strategy, domain=domain, email=f"info@{domain}",
                         country=country, source_key=source_key)


def _researched(session, strategy, target, **overrides):
    brief = research.upsert_brief(session, target.id, _brief_fields(**overrides), actor="test")
    research.evaluate(session, strategy, brief, today=TODAY, resolver=Resolver())
    return brief


def test_no_first_email_without_an_approved_brief(session, strategy):
    _partner(session, strategy)
    result = plan_initials(session, strategy, now=WED_10)
    assert result["planned"] == 0
    assert session.scalar(select(func.count()).select_from(CeMessage)) == 0


def test_a_brief_goes_from_research_to_a_sent_personal_email(session, strategy):
    target = _partner(session, strategy)
    brief = _researched(session, strategy, target)
    assert (brief.status, brief.reasons) == ("READY_TO_DRAFT", [])
    model = Draws()
    outcome = research.draft(session, strategy, brief, model)
    assert outcome["needs_approval"] is True and brief.status == "DRAFTED"
    prompt = model.prompts[0]
    assert "Ana Pérez" in prompt and "nutrición" in prompt and "inventes" in prompt
    generation = session.scalar(select(CeDraftGeneration))
    assert generation.ok and generation.model == "test-model" and generation.prompt == prompt
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 0, "not approved yet"

    research.approve(session, brief.id, actor="amaru")
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 1
    message = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    assert (message.brief_id, message.to_email, message.body) == (
        brief.id, "ana.perez@acme-odoo.cl", GOOD_BODY)
    assert message.variant == "personalized-v1"

    send_due(session, strategy, FakeGateway(), shadow=False, now=message.due_at)
    assert message.status == "SENT" and target.state == S.CONTACTED
    assert brief.status == "SENT"
    totals = research.scoreboard(session, strategy, now=WED_10)["totals"]
    assert (totals["researched"], totals["drafted"], totals["approved"], totals["sent"]) == (
        1, 1, 1, 1)
    f1 = session.scalar(select(CeMessage).where(CeMessage.step == "F1"))
    assert f1.subject == "Re: Acme-Odoo: migraciones de Odoo", "follow-up stays in the thread"
    assert "CFDI" not in f1.body and "BoRo OS" not in f1.body


def test_a_brief_that_fails_the_gate_says_why_and_is_never_drafted(session, strategy):
    target = _partner(session, strategy)
    brief = _researched(session, strategy, target, recipient_email="info@acme-odoo.cl",
                        question="¿Le interesaría conversar?", fact_date="2026-06-01")
    assert brief.status == "NEEDS_RESEARCH"
    assert any("generic mailbox" in r for r in brief.reasons)
    assert any("asks for interest" in r for r in brief.reasons)
    assert any("older than 90 days" in r for r in brief.reasons)
    with pytest.raises(Exception, match="READY_TO_DRAFT"):
        research.draft(session, strategy, brief, Draws())


def test_a_domain_without_mx_fails_the_gate(session, strategy):
    target = _partner(session, strategy)
    brief = research.upsert_brief(session, target.id, _brief_fields(), actor="test")
    research.evaluate(session, strategy, brief, today=TODAY, resolver=Resolver(ok=False))
    assert brief.status == "NEEDS_RESEARCH" and any("MX" in r for r in brief.reasons)


def test_a_draft_promising_cfdi_is_rejected_and_kept_for_audit(session, strategy):
    target = _partner(session, strategy)
    brief = _researched(session, strategy, target)
    bad = GOOD_BODY.replace("módulos a medida", "ajustes de CFDI")
    outcome = research.draft(session, strategy, brief, Draws(body=bad))
    assert outcome["drafted"] is False and brief.status == "READY_TO_DRAFT"
    assert any("CFDI" in r for r in brief.reasons)
    generation = session.scalar(select(CeDraftGeneration))
    assert generation.ok is False and "CFDI" in json.dumps(generation.validator)


def test_after_the_first_20_of_a_batch_a_clean_draft_needs_no_approval(session, strategy):
    for _ in range(20):
        session.add(CeScoreEvent(kind="DRAFTED", batch="2026-11-a", actor="test", details={},
                                 at=WED_10))
    session.flush()
    target = _partner(session, strategy)
    brief = _researched(session, strategy, target)
    outcome = research.draft(session, strategy, brief, Draws())
    assert outcome["needs_approval"] is False and brief.status == "APPROVED"
    # A warning brings the person back even after the first 20.
    other = _partner(session, strategy, domain="beta-odoo.cl")
    warned = _researched(session, strategy, other, recipient_email="ana.perez@beta-odoo.cl",
                         email_source_url="https://beta-odoo.cl/equipo")
    research.draft(session, strategy, warned, Draws(body=GOOD_BODY + "\nhttps://x.cl"))
    assert warned.status == "DRAFTED"


def test_only_the_active_lane_is_planned(session, strategy):
    outside = _partner(session, strategy, source_key="getonboard_companies")
    brief = _researched(session, strategy, outside)
    research.draft(session, strategy, brief, Draws())
    research.approve(session, brief.id, actor="amaru")
    result = plan_initials(session, strategy, now=WED_10)
    assert result["planned"] == 0
    assert result["skipped"][0]["reason"] == "not_ready_or_out_of_focus"


def test_the_daily_cap_is_five(session, strategy):
    for i in range(7):
        target = _partner(session, strategy, domain=f"p{i}-odoo.cl")
        brief = _researched(session, strategy, target,
                            recipient_email=f"ana.perez@p{i}-odoo.cl",
                            email_source_url=f"https://p{i}-odoo.cl/equipo")
        research.draft(session, strategy, brief, Draws())
        if brief.status == "DRAFTED":
            research.approve(session, brief.id, actor="amaru")
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 5


def test_send_time_refuses_an_initial_without_a_brief_and_any_blocked_claim(session, strategy):
    from boro_gtm.engine.outreach import _create_message

    target = _partner(session, strategy)
    from boro_gtm.engine.models import CeContact

    contact = session.get(CeContact, target.contact_id)
    message = _create_message(session, strategy, target, contact, "INITIAL", WED_10)
    message.body = message.body + " Hacemos CFDI."
    session.flush()
    send_due(session, strategy, FakeGateway(), shadow=False, now=WED_10)
    assert message.status == "BLOCKED"
    assert "no_approved_research_brief" in message.last_error
    assert "CFDI" in message.last_error


def test_spain_gets_a_manual_outreach_task_never_an_email(session, strategy):
    target = _partner(session, strategy, domain="ibero-odoo.es", country="ES",
                      source_key="odoo_partners_es")
    brief = _researched(session, strategy, target, recipient_email="ana.perez@ibero-odoo.es",
                        email_source_url="https://ibero-odoo.es/equipo")
    research.draft(session, strategy, brief, Draws())
    assert brief.status == "MANUAL_OUTREACH"
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 0
    rows = research.manual_tasks(session)
    assert rows[0]["company"] and rows[0]["draft"] == GOOD_BODY
    assert "brief_id,company" in research.manual_csv(rows)
    research.mark_manual(session, brief.id, "sent", actor="amaru")
    research.mark_manual(session, brief.id, "replied", actor="amaru")
    manual = research.scoreboard(session, strategy, now=WED_10)["totals"]["manual"]
    assert (manual["sent"], manual["replied"]) == (1, 1)


@pytest.mark.parametrize("text", ["Por favor no contactar.", "No contacten más a nadie aquí.",
                                  "baja", "Unsubscribe"])
def test_new_opt_out_phrases(text):
    assert classify(_reply("t", text)).label == "opt_out"


def test_an_opt_out_suppresses_the_sender_and_the_company_domain(session, strategy):
    target = _partner(session, strategy)
    brief = _researched(session, strategy, target)
    research.draft(session, strategy, brief, Draws())
    research.approve(session, brief.id, actor="amaru")
    plan_initials(session, strategy, now=WED_10)
    gateway = FakeGateway()
    message = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    send_due(session, strategy, gateway, shadow=False, now=message.due_at)
    gateway.deliver(_reply(message.gmail_thread_id, "No contactar, gracias.",
                           sender="Ana <ana.perez@acme-odoo.cl>"))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    kinds = set(session.execute(select(CeSuppression.kind, CeSuppression.value)).all())
    assert ("EMAIL", "ana.perez@acme-odoo.cl") in kinds and ("DOMAIN", "acme-odoo.cl") in kinds
    assert target.state == S.SUPPRESSED
    assert research.scoreboard(session, strategy, now=WED_10)["totals"]["replies"] == 1


def test_scoreboard_gates(session, strategy):
    before = research.scoreboard(session, strategy, now=WED_10)["gates"]
    assert before["a_conversations_by_deadline"]["status"] == "PENDING"
    after_deadline = WED_10 + timedelta(days=40)
    failed = research.scoreboard(session, strategy, now=after_deadline)["gates"]
    assert failed["a_conversations_by_deadline"]["status"] == "FAILED"
    assert "clínicas sobre Odoo" in failed["a_conversations_by_deadline"]["if_failed"]
    for _ in range(150):
        session.add(CeScoreEvent(kind="SENT", batch="b", actor="t", details={}, at=WED_10))
    session.flush()
    alert = research.scoreboard(session, strategy, now=WED_10)["gates"]["b_abandon_channel"]
    assert alert["alert"] is True and alert["message"] == "abandonar canal"
    for kind, count in (("CONVERSATION", 10), ("NAMED_NEED", 3)):
        for _ in range(count):
            session.add(CeScoreEvent(kind=kind, actor="t", details={}, at=WED_10))
    session.flush()
    passed = research.scoreboard(session, strategy, now=after_deadline)["gates"]
    assert passed["a_conversations_by_deadline"]["status"] == "PASSED"


def test_research_never_edits_an_approved_brief(session, strategy):
    target = _partner(session, strategy)
    brief = _researched(session, strategy, target)
    research.draft(session, strategy, brief, Draws())
    research.approve(session, brief.id, actor="amaru")
    with pytest.raises(Exception, match="approved or sent"):
        research.upsert_brief(session, target.id, _brief_fields(why_now="otro"), actor="t")
    assert session.scalar(select(func.count()).select_from(CeResearchBrief)) == 1
