"""The commercial engine, end to end, with no network and no real Gmail.

Every test drives the real services against PostgreSQL. Gmail is the in-memory
`FakeGateway`; websites are a dictionary of canned responses.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from boro_gtm.engine import states as S
from boro_gtm.engine.enrichment import enrich_target, published_emails
from boro_gtm.engine.gmail import FakeGateway, RemoteMessage
from boro_gtm.engine.importer import import_pool
from boro_gtm.engine.models import (
    CeAccount,
    CeContact,
    CeMailEvent,
    CeMessage,
    CeSignal,
    CeSourceRun,
    CeSuppression,
    CeTarget,
    CeTargetEvent,
)
from boro_gtm.engine.outreach import (
    close_sequences,
    followup_due,
    plan_initials,
    reconcile_sending,
    send_due,
)
from boro_gtm.engine.pipeline import (
    EngineError,
    get_or_create_account,
    suppress,
    target_from_signal,
    transition,
)
from boro_gtm.engine.replies import classify, ingest
from boro_gtm.engine.sources.base import SignalDraft, SourcePage
from boro_gtm.engine.sourcing import run_ladder, run_source
from boro_gtm.engine.strategy import load_strategy
from boro_gtm.research.fixtures.transport import FetchResult

pytestmark = pytest.mark.integration

SCL = ZoneInfo("America/Santiago")
#: A Wednesday, inside business hours, with no holiday nearby.
WED_10 = datetime(2026, 11, 4, 10, 0, tzinfo=SCL)


@pytest.fixture
def strategy():
    return load_strategy()


class SiteFetcher:
    """Canned websites. Anything not listed is a 404."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        body = self.pages.get(url.rstrip("/")) or self.pages.get(url)
        if body is None:
            return FetchResult(outcome="NOT_FOUND", http_status=404)
        return FetchResult(outcome="OK", http_status=200, body=body.encode(), final_url=url)


def _signal(session, *, domain="acme-odoo.cl", lane="PARTNER_DELIVERY", country="CL",
            kind="PARTNER_LISTING", external_id=None, title="Gold Partner — Odoo directory (CL)",
            excerpt="Odoo implementation partner. Odoo ERP integración API.",
            posted_at=None, expires_at=None, source_key="test_source") -> CeSignal:
    signal = CeSignal(
        source_key=source_key, external_id=external_id or str(uuid.uuid4()), kind=kind,
        lane=lane, url=f"https://example.org/{domain}", title=title, excerpt=excerpt,
        company_name=domain.split(".")[0].title(), company_domain=domain, country=country,
        posted_at=posted_at, expires_at=expires_at, observed_at=datetime.now(UTC),
        attributes={"evidence_es": "aparece como Gold Partner en el directorio oficial de Odoo",
                    "evidence_en": "is listed as a Gold Partner in Odoo's directory"},
    )
    session.add(signal)
    session.flush()
    return signal


def _ready_target(session, strategy, *, domain="acme-odoo.cl", email=None, lane="PARTNER_DELIVERY",
                  country="CL", score=80, **signal_kw) -> CeTarget:
    signal = _signal(session, domain=domain, lane=lane, country=country, **signal_kw)
    target, outcome = target_from_signal(session, strategy, signal)
    assert outcome == "CREATED"
    account = session.get(CeAccount, target.account_id)
    contact = CeContact(account_id=account.id, email=email or f"gerencia@{domain}",
                        full_name="Ana Pérez", role="CEO", source_url=f"https://{domain}/contacto",
                        verified_at=datetime.now(UTC))
    session.add(contact)
    session.flush()
    target.contact_id = contact.id
    target.score = score
    transition(session, target, S.ENRICH, reason="t", actor="test")
    transition(session, target, S.READY, reason="t", actor="test")
    return target


# --- identity and duplicates -------------------------------------------------


def test_the_same_domain_twice_is_one_account(session):
    a = get_or_create_account(session, "https://www.Acme-Odoo.cl/contacto")
    b = get_or_create_account(session, "acme-odoo.cl")
    assert a.id == b.id
    assert session.scalar(select(func.count()).select_from(CeAccount)) == 1


def test_a_hosting_tenant_cannot_be_an_account(session):
    with pytest.raises(EngineError):
        get_or_create_account(session, "https://acme.lovable.app/")


def test_a_second_signal_for_an_account_attaches_instead_of_starting_a_second_pursuit(
        session, strategy):
    first, outcome1 = target_from_signal(session, strategy, _signal(session))
    again, outcome2 = target_from_signal(session, strategy, _signal(session, lane="TRIGGER_OVERFLOW",
                                                                     kind="TRIGGER"))
    assert (outcome1, outcome2) == ("CREATED", "ATTACHED")
    assert again.id == first.id
    assert session.scalar(select(func.count()).select_from(CeTarget)) == 1


def test_a_closed_pursuit_is_reopened_only_by_a_new_signal(session, strategy):
    target = _ready_target(session, strategy)
    transition(session, target, S.EXPIRED, reason="t", actor="test")
    reopened, outcome = target_from_signal(session, strategy, _signal(session))
    assert outcome == "REOPENED"
    assert reopened.reopened_from_id == target.id


def test_the_same_contact_twice_is_one_contact(session, strategy):
    fetcher = SiteFetcher({"https://acme-odoo.cl": '<a href="mailto:ventas@acme-odoo.cl">x</a>',
                           "https://beta.cl": '<a href="mailto:ventas@acme-odoo.cl">x</a>'})
    t1, _ = target_from_signal(session, strategy, _signal(session))
    enrich_target(session, strategy, t1, fetcher)
    assert session.scalar(select(func.count()).select_from(CeContact)) == 1


def test_a_duplicate_source_result_is_counted_not_stored_twice(session, strategy):
    class Twice:
        key, lane, family = "dup_source", "PARTNER_DELIVERY", "test"

        def next_page(self, fetcher, cursor):
            item = SignalDraft(external_id="same", kind="PARTNER_LISTING", lane=self.lane,
                               url=None, title="t", excerpt="odoo", company_name="Acme",
                               company_domain="acme-odoo.cl", country="CL")
            page = int(cursor.get("page", 1))
            return SourcePage([item], {"page": page}, {"page": page + 1}, exhausted=page >= 2)

    run = run_source(session, strategy, Twice(), SiteFetcher(), wanted=10)
    assert (run.accepted, run.duplicates, run.results_inspected) == (1, 1, 2)
    assert session.scalar(select(func.count()).select_from(CeSignal)) == 1


def test_gmail_history_blocks_a_first_contact(session, strategy):
    target = _ready_target(session, strategy, email="ana@acme-odoo.cl")
    session.add(CeMailEvent(gmail_message_id="old1", gmail_thread_id="th", direction="OUT",
                            from_email="abourgeoisroa@somosboro.com",
                            to_emails=["otra.persona@acme-odoo.cl"], headers={},
                            ingested_at=datetime.now(UTC)))
    session.flush()
    result = plan_initials(session, strategy, now=WED_10)
    assert result["planned"] == 0
    assert target.state == S.BLOCKED
    assert "gmail_history_same_domain" in target.state_reason


# --- outreach ---------------------------------------------------------------


def test_a_ready_target_cannot_be_sent_twice(session, strategy):
    _ready_target(session, strategy)
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    plan_initials(session, strategy, now=WED_10 + timedelta(minutes=30))
    assert session.scalar(select(func.count()).select_from(CeMessage)) == 1
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(hours=1))
    assert len(gateway.sent) == 1


def test_a_worker_crash_after_send_is_reconciled_not_resent(session, strategy):
    target = _ready_target(session, strategy)
    gateway = FakeGateway(fail_after_send=True)
    plan_initials(session, strategy, now=WED_10)
    result = send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    assert result[0]["status"] == "SENDING_UNCONFIRMED"
    message = session.scalar(select(CeMessage))
    assert message.status == "SENDING"
    message.claimed_at = datetime.now(UTC) - timedelta(minutes=30)
    reconciled = reconcile_sending(session, strategy, gateway)
    assert reconciled[0]["resolved"] == "SENT"
    assert len(gateway.sent) == 1, "the restart asked Gmail; it did not send again"
    assert target.state == S.CONTACTED


def test_a_send_retry_checks_gmail_before_sending(session, strategy):
    _ready_target(session, strategy)
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    message = session.scalar(select(CeMessage))
    gateway.send(b"Message-ID: " + message.rfc822_message_id.encode() + b"\n\nx", None)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    assert len(gateway.sent) == 1
    assert message.status == "SENT"


def test_the_daily_cap_holds(session, strategy):
    for i in range(25):
        _ready_target(session, strategy, domain=f"partner{i}.cl")
    first = plan_initials(session, strategy, now=WED_10)
    second = plan_initials(session, strategy, now=WED_10 + timedelta(hours=1))
    assert first["planned"] == 20
    assert second == {"planned": 0, "reason": "daily_cap_reached", "cap": 20}


def test_sends_are_spread_over_the_day(session, strategy):
    for i in range(5):
        _ready_target(session, strategy, domain=f"partner{i}.cl")
    plan_initials(session, strategy, now=WED_10)
    dues = sorted(session.scalars(select(CeMessage.due_at)))
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(dues, dues[1:], strict=False)]
    assert all(g >= 8 for g in gaps), gaps


def test_a_suppressed_contact_is_never_planned(session, strategy):
    target = _ready_target(session, strategy, email="no@acme-odoo.cl")
    suppress(session, kind="EMAIL", value="no@acme-odoo.cl", reason="OPT_OUT")
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 0
    assert target.state == S.SUPPRESSED


def test_a_bounced_address_is_suppressed_and_the_sequence_stops(session, strategy):
    target = _ready_target(session, strategy, email="ana@acme-odoo.cl")
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    sent = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    gateway.deliver(RemoteMessage(
        id="b1", thread_id=sent.gmail_thread_id, label_ids=["INBOX"],
        headers={"From": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                 "Subject": "Delivery Status Notification (Failure)",
                 "X-Failed-Recipients": "ana@acme-odoo.cl"},
        snippet="Address not found", internal_date=WED_10.timestamp() + 900))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert session.scalar(select(CeSuppression.reason).where(
        CeSuppression.value == "ana@acme-odoo.cl")) == "BOUNCE"
    assert target.state == S.SUPPRESSED
    assert session.scalar(select(CeMessage.status).where(CeMessage.step == "F1")) == "CANCELLED"


def test_an_invalid_transition_is_refused(session, strategy):
    target = _ready_target(session, strategy)
    with pytest.raises(S.IllegalTransitionError):
        transition(session, target, S.WON, reason="skip ahead", actor="test")


def test_target_history_is_append_only(session, strategy):
    _ready_target(session, strategy)
    with pytest.raises(Exception, match="append-only|not allowed|immutable|UPDATE"):
        with session.begin_nested():
            session.execute(text("UPDATE ce_target_events SET reason = 'edited'"))


def test_shadow_mode_renders_but_never_sends_or_advances(session, strategy):
    target = _ready_target(session, strategy)
    plan_initials(session, strategy, now=WED_10)
    result = send_due(session, strategy, None, shadow=True, now=WED_10 + timedelta(minutes=10))
    assert result[0]["status"] == "SHADOW"
    assert target.state == S.READY
    message = session.scalar(select(CeMessage))
    assert message.status == "SHADOW" and "Odoo" in message.body


def test_no_message_goes_out_outside_business_hours(session, strategy):
    _ready_target(session, strategy)
    plan_initials(session, strategy, now=WED_10)
    night = datetime(2026, 11, 4, 22, 0, tzinfo=SCL)
    assert send_due(session, strategy, FakeGateway(), shadow=False, now=night) == [
        {"skipped": "outside_business_hours"}]


def test_a_spanish_speaking_target_gets_a_spanish_message_built_on_real_evidence(
        session, strategy):
    _ready_target(session, strategy)
    plan_initials(session, strategy, now=WED_10)
    message = session.scalar(select(CeMessage))
    assert message.language == "es"
    assert "directorio oficial de Odoo" in message.body
    assert "no te vuelvo a escribir" in message.body


def test_jurisdictions_needing_review_are_not_planned(session, strategy):
    target = _ready_target(session, strategy, domain="socio.es", country="ES")
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 0
    assert target.state == S.READY


# --- follow-up cadences ----------------------------------------------------


def test_partner_f1_is_four_business_days_later(strategy):
    due = followup_due(strategy, lane="PARTNER_DELIVERY", step="F1", initial_sent=WED_10)
    assert due.astimezone(SCL).date() == date(2026, 11, 10)  # Wed → Thu, Fri, Mon, Tue


def test_partner_f1_skips_a_holiday(strategy):
    # 2026-11-20 is a Chilean holiday (Friday).
    due = followup_due(strategy, lane="PARTNER_DELIVERY", step="F1",
                       initial_sent=datetime(2026, 11, 18, 10, 0, tzinfo=SCL))
    assert due.astimezone(SCL).date() == date(2026, 11, 25)


def test_partner_f2_waits_fourteen_days_and_seven_after_f1(strategy):
    f1 = datetime(2026, 11, 10, 10, 0, tzinfo=SCL)
    due = followup_due(strategy, lane="PARTNER_DELIVERY", step="F2", initial_sent=WED_10,
                       f1_sent=f1)
    assert due - WED_10 >= timedelta(days=14)
    assert due - f1 >= timedelta(days=7)
    late_f1 = datetime(2026, 11, 13, 10, 0, tzinfo=SCL)
    due2 = followup_due(strategy, lane="PARTNER_DELIVERY", step="F2", initial_sent=WED_10,
                        f1_sent=late_f1)
    assert due2 - late_f1 >= timedelta(days=7)


def test_explicit_demand_f1_is_two_business_days(strategy):
    due = followup_due(strategy, lane="EXPLICIT_DEMAND", step="F1", initial_sent=WED_10)
    assert due.astimezone(SCL).date() == date(2026, 11, 6)


def test_explicit_demand_f2_lands_five_to_seven_days_after_initial(strategy):
    f1 = datetime(2026, 11, 6, 10, 0, tzinfo=SCL)
    due = followup_due(strategy, lane="EXPLICIT_DEMAND", step="F2", initial_sent=WED_10,
                       f1_sent=f1)
    assert timedelta(days=5) <= due - WED_10 <= timedelta(days=7)


def test_explicit_demand_follow_up_moves_ahead_of_the_deadline(strategy):
    deadline = datetime(2026, 11, 9, 18, 0, tzinfo=SCL)
    due = followup_due(strategy, lane="EXPLICIT_DEMAND", step="F2", initial_sent=WED_10,
                       f1_sent=datetime(2026, 11, 6, 10, 0, tzinfo=SCL), expires_at=deadline)
    assert due is None or due < deadline


def test_an_out_of_office_reply_postpones_follow_ups(session, strategy):
    target = _ready_target(session, strategy, email="ana@acme-odoo.cl")
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    f1 = session.scalar(select(CeMessage).where(CeMessage.step == "F1"))
    original = f1.due_at
    sent = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    gateway.deliver(RemoteMessage(
        id="ooo", thread_id=sent.gmail_thread_id, label_ids=["INBOX"],
        headers={"From": "Ana <ana@acme-odoo.cl>", "Subject": "Respuesta automática: fuera de la oficina",
                 "Auto-Submitted": "auto-replied"},
        snippet="", internal_date=WED_10.timestamp() + 600,
        body_text="Estoy de vacaciones hasta el 20 de noviembre."))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert f1.due_at > original
    assert f1.due_at.astimezone(SCL).date() > date(2026, 11, 20)
    assert target.state == S.CONTACTED


def test_an_expired_opportunity_is_closed_and_its_follow_ups_cancelled(session, strategy):
    target = _ready_target(session, strategy, lane="EXPLICIT_DEMAND", kind="EXPLICIT_DEMAND",
                           expires_at=datetime(2026, 11, 7, 12, 0, tzinfo=SCL))
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    close_sequences(session, strategy, now=datetime(2026, 11, 8, 9, 0, tzinfo=SCL))
    assert target.state == S.EXPIRED
    statuses = set(session.scalars(select(CeMessage.status).where(CeMessage.step != "INITIAL")))
    assert statuses <= {"CANCELLED"}


def test_a_reply_cancels_scheduled_follow_ups(session, strategy):
    target = _ready_target(session, strategy, email="ana@acme-odoo.cl")
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    sent = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    gateway.deliver(_reply(sent.gmail_thread_id, "Hola Amaru, me interesa. ¿Hablamos el jueves?"))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert target.state == S.REPLIED
    assert session.scalar(select(CeMessage.status).where(CeMessage.step == "F1")) == "CANCELLED"


def test_no_reply_after_f2_closes_the_pursuit(session, strategy):
    target = _ready_target(session, strategy)
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    for step in ("F1", "F2"):
        message = session.scalar(select(CeMessage).where(CeMessage.step == step))
        moment = message.due_at + timedelta(minutes=1)
        send_due(session, strategy, gateway, shadow=False, now=moment)
    assert target.state == S.F2
    f2 = session.scalar(select(CeMessage).where(CeMessage.step == "F2"))
    close_sequences(session, strategy, now=f2.sent_at + timedelta(days=8))
    assert target.state == S.CLOSED_NO_REPLY
    assert len(gateway.sent) == 3


# --- replies ----------------------------------------------------------------


def _reply(thread: str, body: str, sender="Ana <ana@acme-odoo.cl>", subject="Re: hola",
           **headers) -> RemoteMessage:
    return RemoteMessage(id=f"r{uuid.uuid4().hex[:8]}", thread_id=thread, label_ids=["INBOX"],
                         headers={"From": sender, "Subject": subject, **headers}, snippet="",
                         internal_date=WED_10.timestamp() + 3600, body_text=body)


@pytest.mark.parametrize(("body", "headers", "label", "escalate"), [
    ("Gracias, ¿cuánto cobran por hora y cuál sería el presupuesto?", {}, "pricing_request", True),
    ("No estamos interesados, gracias.", {}, "rejection", False),
    ("Te sugiero escribirle a Pedro, él ve proveedores: pedro@acme.cl", {}, "referral", True),
    ("Por favor dame de baja de esta lista.", {}, "opt_out", False),
    ("Me interesa, cuéntame más.", {}, "positive_interest", False),
    ("¿Tienen portafolio o casos de éxito con Odoo?", {}, "capability_request", False),
    ("I'm out of the office until 2026-11-20.", {"Auto-Submitted": "auto-replied"}, "auto_reply",
     False),
    ("Can we agree exclusivity and an SLA before we start?", {}, "human_other", True),
])
def test_reply_classification(body, headers, label, escalate):
    result = classify(_reply("t", body, **headers), today=date(2026, 11, 4))
    assert result.label == label
    assert result.escalate is escalate


def test_a_bare_no_is_an_opt_out():
    assert classify(_reply("t", "No")).label == "opt_out"


def test_a_rejection_closes_the_pursuit_and_blocks_a_quick_reopen(session, strategy):
    target = _ready_target(session, strategy, email="ana@acme-odoo.cl")
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    sent = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    gateway.deliver(_reply(sent.gmail_thread_id, "No nos interesa, gracias."))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert target.state == S.LOST
    reopened, outcome = target_from_signal(session, strategy, _signal(session))
    assert outcome == "REOPENED"
    contact = session.get(CeContact, target.contact_id)
    reopened.contact_id = contact.id
    from boro_gtm.engine.pipeline import send_blockers
    transition(session, reopened, S.ENRICH, reason="t", actor="test")
    transition(session, reopened, S.READY, reason="t", actor="test")
    assert "account_previously_rejected" in send_blockers(session, strategy, reopened, contact,
                                                          step="INITIAL")


def test_an_opt_out_suppresses_the_sender(session, strategy):
    target = _ready_target(session, strategy, email="ana@acme-odoo.cl")
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    sent = session.scalar(select(CeMessage).where(CeMessage.step == "INITIAL"))
    gateway.deliver(_reply(sent.gmail_thread_id, "Por favor no me escriban más correos."))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert target.state == S.SUPPRESSED
    assert session.scalar(select(CeSuppression.reason)) == "OPT_OUT"


def test_ingestion_is_idempotent(session, strategy):
    gateway = FakeGateway()
    gateway.deliver(_reply("x", "hola"))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert session.scalar(select(func.count()).select_from(CeMailEvent)) == 1


# --- workers ----------------------------------------------------------------


def test_a_task_held_by_another_worker_is_skipped(migrated_engine, monkeypatch, strategy):
    from boro_gtm.engine import worker
    from boro_gtm.engine.settings import engine_settings

    monkeypatch.setattr(worker, "get_engine", lambda: migrated_engine)
    holder = migrated_engine.connect()
    holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": worker._lock_key("plan")})
    try:
        engine = worker.Engine(strategy, engine_settings())
        assert worker.run_task(engine, "plan", worker="w2")["status"] == "LOCKED_ELSEWHERE"
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": worker._lock_key("plan")})
        holder.close()


def test_concurrent_senders_never_claim_the_same_message(committed_sessions, strategy):
    a, b = committed_sessions(), committed_sessions()
    _ready_target(a, strategy)
    plan_initials(a, strategy, now=WED_10)
    a.commit()
    held = a.scalar(select(CeMessage).with_for_update())
    assert held is not None
    result = send_due(b, strategy, FakeGateway(), shadow=False, now=WED_10 + timedelta(minutes=10))
    assert result == [], "the second worker skipped the locked row instead of waiting or sending"
    a.rollback()


def test_a_failed_send_is_retried_then_parked(session, strategy):
    _ready_target(session, strategy)

    class Down(FakeGateway):
        def find_by_rfc822(self, rfc822_id):
            return None

        def send(self, raw, thread_id):
            from boro_gtm.engine.gmail import GmailUnavailableError
            raise GmailUnavailableError("down")

    gateway = Down()
    plan_initials(session, strategy, now=WED_10)
    message = session.scalar(select(CeMessage))
    outcomes = []
    for _ in range(3):
        send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
        message.claimed_at = datetime.now(UTC) - timedelta(hours=1)
        outcomes += [r["resolved"] for r in reconcile_sending(session, strategy, gateway)]
        message.due_at = WED_10
    assert outcomes == ["RESCHEDULED", "RESCHEDULED", "FAILED"]


# --- sourcing ---------------------------------------------------------------


def _gob_page(ids: list[str], total_pages: int, modality: str = "3",
              title: str = "Desarrollador Odoo freelance") -> str:
    return json.dumps({"meta": {"page": 1, "total_pages": total_pages}, "data": [
        {"id": i, "attributes": {
            "title": f"{title} {i}", "description": "Integración API",
            "modality": {"data": {"id": modality}}, "countries": ["Chile"],
            "published_at": int(WED_10.timestamp()),
            "company": {"data": {"attributes": {"name": f"Empresa {i}",
                                                "web": f"https://empresa-{i}.cl"}}}}}
        for i in ids]})


class GetOnBoardFetcher(SiteFetcher):
    def __init__(self, pages: dict[tuple[str, int], str]) -> None:
        super().__init__()
        self.api = pages

    def get(self, url):
        self.calls.append(url)
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(url).query)
        body = self.api.get((q["query"][0], int(q["page"][0])))
        if body is None:
            return FetchResult(outcome="OK", http_status=200, body=json.dumps(
                {"meta": {"total_pages": 1}, "data": []}).encode())
        return FetchResult(outcome="OK", http_status=200, body=body.encode())


def test_sourcing_follows_pagination_and_keeps_its_cursor(session, strategy):
    from boro_gtm.engine.sources.getonboard import GetOnBoardSource

    fetcher = GetOnBoardFetcher({("odoo", 1): _gob_page(["a", "b"], 2),
                                 ("odoo", 2): _gob_page(["c"], 2)})
    run = run_source(session, strategy, GetOnBoardSource(), fetcher, wanted=2, max_pages=5)
    assert run.accepted == 2
    from boro_gtm.engine.models import CeSourceCursor
    assert session.get(CeSourceCursor, "getonboard").cursor == {"query_index": 0, "page": 2}
    run2 = run_source(session, strategy, GetOnBoardSource(), fetcher, wanted=5, max_pages=1)
    assert run2.accepted == 1, "the second run continued where the first stopped"


def test_a_source_that_runs_dry_says_so(session, strategy):
    from boro_gtm.engine.sources.getonboard import QUERIES, GetOnBoardSource

    fetcher = GetOnBoardFetcher({("odoo", 1): _gob_page(["a"], 1, modality="1",
                                                         title="Ingeniero backend")})
    run = run_source(session, strategy, GetOnBoardSource(), fetcher, wanted=20,
                     max_pages=len(QUERIES) + 2)
    assert run.status == "EXHAUSTED"
    assert run.cursor_exhausted
    assert run.rejection_reasons == {"full_time_employment_not_vendor_demand": 1}
    assert len(run.queries) == len(QUERIES)


def test_the_ladder_reports_why_it_fell_short(session, strategy):
    from boro_gtm.engine.sources.getonboard import GetOnBoardSource

    result = run_ladder(session, strategy, GetOnBoardFetcher({}), target_new=5,
                        ladder=[GetOnBoardSource()], max_pages_per_source=50)
    assert result["accepted"] == 0
    assert result["below_target_reason"] == "ladder_exhausted"
    assert session.scalar(select(CeSourceRun.status)) == "EXHAUSTED"


def test_an_expired_posting_is_not_a_target(session, strategy):
    signal = _signal(session, expires_at=datetime.now(UTC) - timedelta(days=1))
    assert target_from_signal(session, strategy, signal) == (None, "EXPIRED")


# --- enrichment -------------------------------------------------------------


def test_enrichment_uses_only_published_addresses_on_the_company_domain():
    html = ('<a href="mailto:Ana.Perez@acme-odoo.cl">Ana</a> info@gmail.com '
            'noreply@acme-odoo.cl logo@2x.png <span data-cfemail="x"></span>')
    assert published_emails(html, "acme-odoo.cl") == ["ana.perez@acme-odoo.cl"]


def test_a_qualified_target_with_a_published_channel_becomes_ready(session, strategy):
    target, _ = target_from_signal(session, strategy, _signal(session))
    fetcher = SiteFetcher({"https://acme-odoo.cl/contacto":
                           '<p>Escríbenos a <a href="mailto:alianzas@acme-odoo.cl">alianzas</a></p>'})
    result = enrich_target(session, strategy, target, fetcher)
    assert result["outcome"] == "ready", result
    assert target.state == S.READY
    assert target.score >= strategy.threshold
    contact = session.get(CeContact, target.contact_id)
    assert contact.source_url.endswith("/contacto")


def test_no_published_channel_means_no_contact_and_no_guess(session, strategy):
    target, _ = target_from_signal(session, strategy, _signal(session))
    enrich_target(session, strategy, target, SiteFetcher({"https://acme-odoo.cl": "<p>Hola</p>"}))
    assert target.state == S.BLOCKED
    assert target.state_reason == "no_published_channel"
    assert session.scalar(select(func.count()).select_from(CeContact)) == 0


def test_a_target_in_a_review_jurisdiction_waits_for_review(session, strategy):
    target, _ = target_from_signal(session, strategy, _signal(session, domain="socio.es",
                                                              country="ES"))
    enrich_target(session, strategy, target,
                  SiteFetcher({"https://socio.es": '<a href="mailto:hola@socio.es">x</a>'}))
    assert target.state == S.COMPLIANCE_REVIEW


def test_score_components_keep_their_evidence(session, strategy):
    target, _ = target_from_signal(session, strategy, _signal(session))
    enrich_target(session, strategy, target,
                  SiteFetcher({"https://acme-odoo.cl": '<a href="mailto:info@acme-odoo.cl">x</a>'}))
    components = target.score_components["components"]
    assert set(components) == {"signal", "urgency", "technical_fit", "vendor_suitability",
                               "channel_access", "economic"}
    assert "odoo" in components["technical_fit"]["evidence"]


# --- import -----------------------------------------------------------------


def test_a_pool_import_is_idempotent_and_never_trusts_an_unsourced_address(
        session, strategy, tmp_path):
    pool = tmp_path / "BoRo_Week1_Commercial_Pool_100.csv"
    pool.write_text("company,domain,lane,country,email,email_source_url,signal\n"
                    "Acme,https://acme-odoo.cl,partner,CL,ana@acme-odoo.cl,"
                    "https://acme-odoo.cl/equipo,Odoo partner\n"
                    "Beta,beta.cl,explicit,CL,juan@beta.cl,,Busca integrador\n"
                    "Nope,,partner,CL,,,\n")
    first = import_pool(session, strategy, pool)
    second = import_pool(session, strategy, pool)
    assert (first["created"], second["created"], second["duplicates"]) == (2, 0, 2)
    assert session.scalar(select(func.count()).select_from(CeContact)) == 1
    reasons = {r["reason"] for r in first["rejected"]}
    assert reasons == {"email_without_public_source", "no_identity_domain"}


# --- reporting ----------------------------------------------------------------


def test_the_funnel_is_reported_per_lane_never_pooled(session, strategy):
    from boro_gtm.engine.reports import funnel

    _ready_target(session, strategy, domain="p1.cl")
    _ready_target(session, strategy, domain="e1.cl", lane="EXPLICIT_DEMAND",
                  kind="EXPLICIT_DEMAND", title="Busco integrador Odoo")
    gateway = FakeGateway()
    plan_initials(session, strategy, now=WED_10)
    for minute in (10, 40):
        send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=minute))
    lanes = {row["key"]: row for row in funnel(session)}
    assert set(lanes) == {"PARTNER_DELIVERY", "EXPLICIT_DEMAND"}
    assert all(row["contacted"] == 1 for row in lanes.values())


def test_history_records_every_transition(session, strategy):
    target = _ready_target(session, strategy)
    states = list(session.scalars(select(CeTargetEvent.to_state).where(
        CeTargetEvent.target_id == target.id).order_by(CeTargetEvent.id)))
    assert states == [S.DISCOVERED, S.ENRICH, S.READY]


def test_an_xlsx_pool_imports_like_a_csv(session, strategy, tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Empresa", "Website", "Carril", "País", "Evidencia"])
    sheet.append(["Acme", "acme-odoo.cl", "Partner Delivery", "CL", "Partner Odoo Gold"])
    path = tmp_path / "pool.xlsx"
    book.save(path)
    assert import_pool(session, strategy, path)["created"] == 1


def test_a_failed_task_is_retried_soon_not_a_whole_interval_later(session):
    from boro_gtm.engine import worker
    from boro_gtm.engine.models import CeTaskRun

    now = datetime.now(UTC)
    session.add(CeTaskRun(task="source", worker="w", status="OK", started_at=now - timedelta(hours=8)))
    session.add(CeTaskRun(task="source", worker="w", status="FAILED",
                          started_at=now - timedelta(minutes=20), error="interrupted"))
    session.flush()
    assert worker._due(session, "source", timedelta(hours=6)) is True
    session.add(CeTaskRun(task="source", worker="w", status="FAILED",
                          started_at=now - timedelta(minutes=5), error="interrupted"))
    session.flush()
    assert worker._due(session, "source", timedelta(hours=6)) is False
