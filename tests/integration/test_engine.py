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
    CeControl,
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
    """The template-path strategy 2026-11.2, read directly. These tests cover the planner,
    sender and follow-ups underneath the personalization gate; the gate itself (2026-11.3,
    CE-ADR-022) is covered in test_personalization.py."""
    import hashlib
    from pathlib import Path

    from boro_gtm.engine.strategy import Strategy

    path = (Path(__file__).resolve().parents[2] / "commercial" / "strategies"
            / "superseded-2026-11.2.json")
    data = path.read_bytes()
    raw = json.loads(data)
    # Since CE-ADR-022 every message is checked at send time against the citable cases
    # and claims; the template path is exercised with 2026-11.3's allowed proof points.
    current = json.loads((path.parent / "active-2026-11.3.json").read_bytes())
    raw["proof_points"] = current["proof_points"]
    raw["copy"]["follow_up_proof"] = current["copy"]["follow_up_proof"]
    raw["copy"]["example_work"] = current["copy"]["example_work"]
    return Strategy(raw=raw, sha256=hashlib.sha256(data).hexdigest(), path=path)


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
    assert target.state_reason.startswith("gmail_history")


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


class _GmailApi:
    """Gmail's REST surface for GmailApiGateway, over httpx.MockTransport.

    `on_send` decides what a POST /messages/send does: "timeout_after_accept"
    stores the message and then drops the connection (the ambiguous case), and
    "quota" refuses it with Gmail's 403 rateLimitExceeded (nothing stored).
    """

    def __init__(self, on_send: str) -> None:
        self.on_send = on_send
        self.posts = 0
        self.stored: dict[str, str] = {}  # rfc822 Message-ID -> gmail id

    def handler(self, request):
        import base64
        import email as email_lib

        import httpx

        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        path = request.url.path
        if request.method == "POST" and path.endswith("/messages/send"):
            self.posts += 1
            raw = base64.urlsafe_b64decode(json.loads(request.content)["raw"])
            rfc822 = email_lib.message_from_bytes(raw)["Message-ID"]
            if self.on_send == "quota":
                return httpx.Response(403, json={"error": {"code": 403, "errors": [
                    {"reason": "rateLimitExceeded"}]}})
            self.stored[rfc822] = f"g{self.posts}"
            raise httpx.ReadTimeout("connection dropped after Gmail accepted", request=request)
        if request.method == "GET" and path.endswith("/messages"):
            wanted = request.url.params.get("q", "").removeprefix("rfc822msgid:")
            ids = [g for r, g in self.stored.items() if r.strip("<>") == wanted]
            return httpx.Response(200, json={"messages": [{"id": i} for i in ids]})
        if request.method == "GET" and "/messages/" in path:
            gid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": gid, "threadId": f"t-{gid}",
                                             "labelIds": ["SENT"], "payload": {"headers": []}})
        return httpx.Response(404, json={})

    def gateway(self, tmp_path):
        import httpx

        from boro_gtm.engine.gmail import GmailApiGateway

        creds = tmp_path / "gmail.json"
        creds.write_text(json.dumps({"client_id": "c", "client_secret": "s",
                                     "refresh_token": "r"}))
        creds.chmod(0o600)
        return GmailApiGateway("abourgeoisroa@somosboro.com", str(creds),
                               httpx.Client(transport=httpx.MockTransport(self.handler)),
                               sleep=lambda _s: None)


def test_a_send_timeout_with_the_real_gateway_is_ambiguous_and_never_posted_twice(
        session, strategy, tmp_path):
    target = _ready_target(session, strategy)
    api = _GmailApi("timeout_after_accept")
    gateway = api.gateway(tmp_path)
    plan_initials(session, strategy, now=WED_10)
    result = send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    message = session.scalar(select(CeMessage))
    assert api.posts == 1
    assert result[0]["status"] == "SENDING_UNCONFIRMED", "ambiguous: neither SENT nor retried"
    assert message.status == "SENDING" and target.state == S.READY
    message.claimed_at = datetime.now(UTC) - timedelta(minutes=30)
    assert reconcile_sending(session, strategy, gateway)[0]["resolved"] == "SENT"
    assert api.posts == 1, "the reconciler found it by Message-ID; it never posted again"
    assert message.status == "SENT" and message.gmail_message_id == "g1"
    assert target.state == S.CONTACTED


def test_a_quota_refused_send_is_not_retried_by_the_gateway_and_is_rescheduled(
        session, strategy, tmp_path):
    _ready_target(session, strategy)
    api = _GmailApi("quota")
    gateway = api.gateway(tmp_path)
    plan_initials(session, strategy, now=WED_10)
    result = send_due(session, strategy, gateway, shadow=False, now=WED_10 + timedelta(minutes=10))
    message = session.scalar(select(CeMessage))
    assert api.posts == 1 and result[0]["status"] == "SENDING_UNCONFIRMED"
    assert "rate limited" in message.last_error
    message.claimed_at = datetime.now(UTC) - timedelta(minutes=30)
    assert reconcile_sending(session, strategy, gateway)[0]["resolved"] == "RESCHEDULED"
    assert api.posts == 1, "Gmail is asked first; nothing is posted blind"
    assert message.status == "SCHEDULED"


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
    lanes = ("PARTNER_DELIVERY", "EXPLICIT_DEMAND", "TRIGGER_OVERFLOW")
    for i in range(25):
        _ready_target(session, strategy, domain=f"partner{i}.cl", lane=lanes[i % 3])
    first = plan_initials(session, strategy, now=WED_10)
    second = plan_initials(session, strategy, now=WED_10 + timedelta(hours=1))
    assert first["planned"] == 20
    assert second == {"planned": 0, "reason": "daily_cap_reached", "cap": 20}


def test_one_easy_lane_cannot_fill_the_whole_day(session, strategy):
    for i in range(25):
        _ready_target(session, strategy, domain=f"partner{i}.cl")
    result = plan_initials(session, strategy, now=WED_10)
    # 60% of the cap at most; the rest is recorded as shortage, not filled.
    assert result["planned"] == 12
    assert result["lane_shortage"] == {"EXPLICIT_DEMAND": 8, "TRIGGER_OVERFLOW": 4}
    assert result["unfilled_slots"] == 8


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
    # v3 (2026-11.2): the Odoo listing selects the Odoo-partner hook and the country's
    # localization (CL -> DTE), never a claim the evidence does not support.
    assert "partner Odoo" in message.body and "DTE" in message.body
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
    assert result["lanes"]["EXPLICIT_DEMAND"]["reason"] == "every_source_on_lane_exhausted"
    assert result["lanes"]["PARTNER_DELIVERY"]["reason"] == "no_automated_source_for_lane"
    assert "EXPLICIT_DEMAND: every_source_on_lane_exhausted" in result["below_target_reason"]
    assert {m["source"] for m in result["manual_sources"]} >= {"upwork", "linkedin"}
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
    assert (first["created"], second["created"], second["duplicates"]) == (2, 0, 3)
    assert session.scalar(select(func.count()).select_from(CeContact)) == 1
    assert first["contact_notes"] == {"contact_without_public_source": 1}
    assert first["needs_enrichment_no_domain"] == 1


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


_PARTNER_PAGE = """<html><head><link href="https://fonts.gstatic.com/" rel="preconnect"/></head>
<body><h1>BCN CONSULTORES LIMITADA <span>Silver</span></h1><p>Silver Partner</p>
<div><i aria-label="Website"></i>
<a class="text-break w-100" href="http://www.bcncons.com"><span itemprop="website">http://www.bcncons.com</span></a></div>
<div><i aria-label="Email"></i> <span class="text-break w-100" itemprop="email">jescalona@bcncons.com</span></div>
<a href="https://github.com/odoo">github</a></body></html>"""


def test_the_odoo_adapter_reads_the_partners_own_website_not_the_first_link():
    """Found on the first VPS pass: every partner resolved to fonts.gstatic.com."""
    from boro_gtm.engine.sources.odoo_partners import OdooPartnerSource

    draft = OdooPartnerSource("CL")._draft("/partners/bcn-consultores-limitada-9184625",
                                           _PARTNER_PAGE)
    assert draft.company_domain == "http://www.bcncons.com"
    assert draft.attributes["published_email"] == "jescalona@bcncons.com"
    assert draft.reject_reason is None


def test_an_email_the_partner_published_in_the_directory_is_a_usable_channel(session, strategy):
    signal = _signal(session, domain="bcncons.com")
    signal.attributes = {**signal.attributes, "published_email": "jescalona@bcncons.com"}
    target, _ = target_from_signal(session, strategy, signal)
    enrich_target(session, strategy, target, SiteFetcher())
    contact = session.get(CeContact, target.contact_id)
    assert contact.email == "jescalona@bcncons.com"
    assert contact.source_url == signal.url
    assert target.state == S.READY


def test_a_directory_email_on_another_domain_is_not_used(session, strategy):
    signal = _signal(session, domain="bcncons.com")
    signal.attributes = {**signal.attributes, "published_email": "someone@gmail.com"}
    target, _ = target_from_signal(session, strategy, signal)
    enrich_target(session, strategy, target, SiteFetcher())
    assert target.contact_id is None
    assert target.state == S.BLOCKED


def test_a_support_mailbox_is_never_written_to(session, strategy):
    target = _ready_target(session, strategy, email="soporte@acme-odoo.cl")
    assert plan_initials(session, strategy, now=WED_10)["planned"] == 0
    assert target.state == S.BLOCKED
    assert "role_mailbox_not_for_sales" in target.state_reason


def test_a_support_mailbox_in_the_directory_is_not_taken_as_the_channel(session, strategy):
    signal = _signal(session, domain="bcncons.com")
    signal.attributes = {**signal.attributes, "published_email": "soporte@bcncons.com"}
    target, _ = target_from_signal(session, strategy, signal)
    enrich_target(session, strategy, target, SiteFetcher())
    assert target.contact_id is None


# --- production-readiness hardening (2026-11 sprint) -------------------------


def _sent(gid, thread, to, when, subject="Capacidad de entrega", labels=("SENT",)):
    return RemoteMessage(id=gid, thread_id=thread, label_ids=list(labels),
                         headers={"From": "Amaru <abourgeoisroa@somosboro.com>", "To": to,
                                  "Subject": subject, "Message-ID": f"<{gid}@mail.gmail.com>"},
                         snippet="", internal_date=when.timestamp())


@pytest.mark.parametrize(("address", "role", "blocked"), [
    ("soporte@x.cl", None, True), ("soporte.tecnico@x.cl", None, True),
    ("atencionalcliente@x.cl", None, True), ("help@x.com", None, True),
    ("helpdesk@x.com", None, True), ("billing@x.com", None, True),
    ("facturacion@x.cl", None, True), ("privacidad@x.cl", None, True),
    ("customer.service@x.com", None, True), ("noreply@x.com", None, True),
    ("jobs@x.com", None, True), ("jobs@x.com", "contractor_recruiting", False),
    ("hiring@x.com", "contractor_recruiting", False),
    ("soporte@x.cl", "contractor_recruiting", True),
    ("ventas@x.cl", None, False), ("contacto@x.cl", None, False),
    ("ana.perez@x.cl", None, False), ("gerencia@x.cl", None, False),
])
def test_role_mailboxes_are_never_a_sales_channel(address, role, blocked):
    from boro_gtm.engine.pipeline import is_role_mailbox

    assert is_role_mailbox(address, contact_role=role) is blocked


def test_enrichment_keeps_looking_for_a_named_person_past_a_general_inbox(session, strategy):
    target, _ = target_from_signal(session, strategy, _signal(session, domain="acme-odoo.cl"))
    fetcher = SiteFetcher({"https://acme-odoo.cl": "<a href='mailto:info@acme-odoo.cl'>",
                           "https://acme-odoo.cl/equipo": "Ana: ana.perez@acme-odoo.cl"})
    enrich_target(session, strategy, target, fetcher)
    assert session.get(CeContact, target.contact_id).email == "ana.perez@acme-odoo.cl"


def test_a_draft_is_never_history_and_a_trashed_send_still_is(session, strategy):
    gateway = FakeGateway()
    when = WED_10 - timedelta(days=2)
    gateway.deliver(_sent("d1", "t1", "info@afgo.com", when, labels=("DRAFT", "TRASH")))
    gateway.deliver(_sent("s1", "t2", "ventas@cmcorp.cl", when, labels=("SENT", "TRASH")))
    stats = ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    assert stats["skipped_drafts_spam"] == 1
    out = session.scalars(select(CeMailEvent.to_emails).where(CeMailEvent.direction == "OUT"))
    assert [e for e in out] == [["ventas@cmcorp.cl"]]
    from boro_gtm.engine.pipeline import prior_contact

    assert prior_contact(session, "info@afgo.com", "afgo.com") is None
    assert prior_contact(session, "otra@cmcorp.cl", "cmcorp.cl") == "gmail_history_same_domain"
    assert prior_contact(session, "info@cmcorp.com", "cmcorp.com") == (
        "gmail_history_sister_domain"), "same name under another TLD is the same company"
    backfill = session.get(CeControl, "gmail_history_backfill").value
    assert backfill["complete"] is True and backfill["since"] == "2026-06-01"


def test_a_newsletter_unsubscribe_link_suppresses_nobody(session, strategy):
    gateway = FakeGateway()
    gateway.deliver(_reply("news", "Click here to unsubscribe", sender="news@partner.cl",
                           headers={"List-Unsubscribe": "<mailto:u@partner.cl>"}))
    gateway.deliver(_reply("cold", "Unsubscribe me", sender="random@stranger.cl"))
    ingest(session, strategy, gateway, since=date(2026, 11, 1))
    assert session.scalar(select(func.count()).select_from(CeSuppression)) == 0


def test_ingestion_never_skips_history_when_a_run_is_cut_short(session, strategy):
    gateway = FakeGateway()
    for i in range(5):
        gateway.deliver(_reply(f"t{i}", "hola"))
    first = ingest(session, strategy, gateway, max_messages=2)
    assert first["complete"] is False
    assert session.get(CeControl, "gmail_ingest_cursor") is None, "cursor waits for the end"
    ingest(session, strategy, gateway, max_messages=2)
    last = ingest(session, strategy, gateway, max_messages=10)
    assert last["complete"] is True
    assert session.scalar(select(func.count()).select_from(CeMailEvent)) == 5


def test_a_chatgpt_send_today_is_adopted_not_sent_again(session, strategy):
    target = _ready_target(session, strategy, domain="cmcorp.cl", email="gerencia@cmcorp.cl")
    plan_initials(session, strategy, now=WED_10 - timedelta(hours=1))
    send_due(session, strategy, None, shadow=True, now=WED_10)
    shadow = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id))
    assert shadow.status == "SHADOW"
    gateway = FakeGateway()
    sent_at = datetime.now(UTC) - timedelta(hours=2)
    gateway.deliver(_sent("cg1", "thr1", "ventas@cmcorp.cl", sent_at))
    stats = ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    assert stats["history"]["adopted"] == 1
    assert target.state == S.CONTACTED
    initial = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id,
                                                     CeMessage.step == "INITIAL"))
    assert (initial.status, initial.variant, initial.to_email) == (
        "SENT", "external_gmail", "ventas@cmcorp.cl")
    f1 = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id,
                                                CeMessage.step == "F1"))
    from boro_gtm.engine.outreach import followup_due

    # Parked until a person confirms the thread is BoRo outreach (adopted-review).
    assert (f1.status, f1.last_error) == ("CANCELLED", "awaiting_adoption_confirmation")
    assert f1.due_at == followup_due(strategy, lane=target.lane, step="F1",
                                     initial_sent=initial.sent_at)
    assert f1.in_reply_to == "<cg1@mail.gmail.com>"
    assert f1.subject == "Re: Capacidad de entrega", "the follow-up stays in the Gmail thread"
    event = session.scalar(select(CeTargetEvent).where(
        CeTargetEvent.reason == "gmail_history_initial"))
    assert event.details["replaced_engine_message"]["status"] == "SHADOW"


def test_going_live_rerenders_a_rehearsed_follow_up_and_never_sends_it_late(session, strategy):
    """2026-11-12: a rehearsed F1 (copy v1, due 6 Nov) was re-armed as-is at go-live and
    sent within a minute. Re-armed follow-ups are rendered again and due from now on."""
    from boro_gtm.engine.business_days import BusinessCalendar
    from boro_gtm.engine.outreach import rearm_shadow_followups

    target = _ready_target(session, strategy, domain="cmcorp.cl", email="gerencia@cmcorp.cl")
    gateway = FakeGateway()
    gateway.deliver(_sent("cg1", "thr1", "ventas@cmcorp.cl", datetime.now(UTC) - timedelta(hours=2)))
    ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    from boro_gtm.engine.actions import adopted_review

    adopted_review(session, target.id, "continue", actor="test", strategy=strategy)
    f1 = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id,
                                                CeMessage.step == "F1"))
    send_due(session, strategy, None, shadow=True, now=f1.due_at)
    assert f1.status == "SHADOW"
    f1.body, f1.subject = "copy of an older strategy", "Re: an older subject"
    session.flush()
    live_at = f1.due_at + timedelta(days=6)
    assert rearm_shadow_followups(session, strategy, now=live_at) == 1
    assert f1.status == "SCHEDULED" and f1.sent_at is None
    assert f1.body != "copy of an older strategy" and f1.strategy_version == strategy.version
    assert f1.subject == "Re: Capacidad de entrega", "stays in the real Gmail thread"
    assert f1.due_at >= BusinessCalendar(strategy).next_open_moment(live_at)


def test_an_adopted_thread_sends_nothing_until_a_person_confirms_it(session, strategy):
    """2026-11-12: a Qué Compro proposal to Cencosud was adopted and got an engine F1."""
    from boro_gtm.engine.actions import adopted_review, adopted_review_list

    keep = _ready_target(session, strategy, domain="cmcorp.cl", email="gerencia@cmcorp.cl")
    drop = _ready_target(session, strategy, domain="cencosud.com", email="info@cencosud.com")
    gateway = FakeGateway()
    sent_at = datetime.now(UTC) - timedelta(hours=2)
    gateway.deliver(_sent("k1", "tk", "ventas@cmcorp.cl", sent_at))
    gateway.deliver(_sent("c1", "tc", "comunicaciones@cencosud.com", sent_at,
                          subject="Propuesta para Jumbo | lanzamiento de Qué Compro"))
    ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    listed = {r["company"]: r for r in adopted_review_list(session)}
    assert len(listed) == 2
    assert any("Qué Compro" in (r["thread_subject"] or "") for r in listed.values())
    far = datetime.now(UTC) + timedelta(days=30)
    assert session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.status == "SCHEDULED", CeMessage.due_at <= far)) == 0, "nothing is owed yet"

    adopted_review(session, drop.id, "stop", actor="test", strategy=strategy)
    assert drop.state == S.LOST
    adopted_review(session, keep.id, "continue", actor="test", strategy=strategy)
    f1 = session.scalar(select(CeMessage).where(CeMessage.target_id == keep.id,
                                                CeMessage.step == "F1"))
    assert f1.status == "SCHEDULED" and f1.subject == "Re: Capacidad de entrega"
    assert adopted_review_list(session) == []
    with pytest.raises(EngineError):
        adopted_review(session, keep.id, "stop", actor="test", strategy=strategy)


def test_a_failed_chatgpt_send_leaves_only_a_draft_and_no_contact(session, strategy):
    target = _ready_target(session, strategy, domain="konos.cl", email="ventas@konos.cl")
    gateway = FakeGateway()
    gateway.deliver(_sent("k1", "tk", "ventas@konos.cl", datetime.now(UTC), labels=("DRAFT",)))
    ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    assert target.state == S.READY


def test_an_old_gmail_sequence_blocks_and_is_not_adopted(session, strategy):
    target = _ready_target(session, strategy, domain="old.cl", email="ana@old.cl")
    gateway = FakeGateway()
    gateway.deliver(_sent("o1", "to", "ana@old.cl", datetime.now(UTC) - timedelta(days=60)))
    ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    assert (target.state, target.state_reason) == (S.BLOCKED, "gmail_history_sequence_finished")


def test_a_reply_already_in_the_adopted_thread_takes_effect(session, strategy):
    target = _ready_target(session, strategy, domain="giraffos.com", email="ana@giraffos.com")
    gateway = FakeGateway()
    gateway.deliver(_sent("g1", "tg", "ana@giraffos.com", datetime.now(UTC) - timedelta(days=2)))
    reply = _reply("tg", "Me interesa, ¿cuándo podemos hablar?", sender="Ana <ana@giraffos.com>")
    reply.internal_date = (datetime.now(UTC) - timedelta(days=1)).timestamp()
    gateway.deliver(reply)
    ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    assert target.state == S.REPLIED
    assert session.scalar(select(func.count()).select_from(CeMessage).where(
        CeMessage.target_id == target.id, CeMessage.status == "SCHEDULED")) == 0


def test_a_follow_up_chatgpt_sends_after_adoption_is_recorded(session, strategy):
    target = _ready_target(session, strategy, domain="adhoc.inc", email="tev@adhoc.inc")
    gateway = FakeGateway()
    first = datetime.now(UTC) - timedelta(days=6)
    gateway.deliver(_sent("a1", "ta", "tev@adhoc.inc", first))
    ingest(session, strategy, gateway, since=date(2026, 6, 1), backfill=True)
    assert target.state == S.CONTACTED
    gateway.deliver(_sent("a2", "ta", "tev@adhoc.inc", datetime.now(UTC), subject="Re: hola"))
    ingest(session, strategy, gateway, since=date(2026, 6, 1))
    assert target.state == S.F1
    steps = dict(session.execute(select(CeMessage.step, CeMessage.status).where(
        CeMessage.target_id == target.id)).all())
    # F2 is owed, but parked while nobody has confirmed the adopted thread.
    assert steps == {"INITIAL": "SENT", "F1": "SENT", "F2": "CANCELLED"}


def test_going_live_plans_a_target_that_was_only_rehearsed(session, strategy):
    target = _ready_target(session, strategy, domain="dimabe.cl", email="ana@dimabe.cl")
    plan_initials(session, strategy, now=WED_10, shadow=True)
    send_due(session, strategy, None, shadow=True, now=WED_10 + timedelta(minutes=30))
    assert plan_initials(session, strategy, now=WED_10 + timedelta(days=1),
                         shadow=True)["planned"] == 0, "rehearsed once"
    live = plan_initials(session, strategy, now=WED_10 + timedelta(days=1), shadow=False)
    assert live["planned"] == 1
    row = session.scalar(select(CeMessage).where(CeMessage.target_id == target.id))
    assert (row.status, row.attempts) == ("SCHEDULED", 0)
    assert session.scalar(select(func.count()).select_from(CeMessage)) == 1


def _pool_xlsx(tmp_path, rows):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Week 1 Leads"
    sheet.append(["ID", "Day", "Type", "Company / Opportunity", "Country", "Signal",
                  "Contact / Apply", "Source", "Readiness", "Score", "Status"])
    for row in rows:
        sheet.append(row)
    book.create_sheet("Feeds").append(["Feed", "Purpose"])
    path = tmp_path / "BoRo_Week1_Commercial_Pool_100.xlsx"
    book.save(path)
    return path


def test_the_week1_pool_format_imports_without_trusting_it(session, strategy, tmp_path):
    odoo = "https://www.odoo.com/partners/country/chile-44"
    known = _signal(session, domain="bmya.cl", source_key="odoo_partners_cl")
    known.company_name = "Blanco Martín & Asociados"
    known.url = "https://www.odoo.com/partners/blanco-martin-123"
    known.attributes = {**known.attributes, "published_email": "odoocl@bmya.cl"}
    target_from_signal(session, strategy, known)
    path = _pool_xlsx(tmp_path, [
        [1, 1, "Partner Delivery", "Konos", "Chile", "Gold; 78 refs", "ventas@konos.cl", odoo,
         "READY", 95, "Queued"],
        [5, 1, "Partner Delivery", "Elite Solutions", "Chile", "Silver",
         "soporte@elitesolutions.cl", odoo, "READY", 93, "Queued"],
        [12, 1, "Partner Delivery", "Blanco Martín & Asociados SpA", "Chile", "Silver",
         "odoocl@bmya.cl", odoo, "READY", 90, "Queued"],
        [2, 1, "Explicit Demand", "B2B SaaS Platform", "Worldwide", "USD 6k",
         "Upwork proposal", "https://www.upwork.com/freelance-jobs/apply/x_~01/", "READY", 95,
         "Queued"],
        [19, 1, "Trigger / Overflow", "Moveapps", "Chile / Remote", "Freelance monthly",
         "LinkedIn application", "https://cl.linkedin.com/jobs/view/x", "READY", 90, "Queued"],
        [47, 3, "Trigger / Overflow", "HV Talent", "LATAM / Remote", "Contractor role",
         "Company role/application", "https://hvtalent.com/publication/x/", "READY", 83,
         "Queued"],
        [34, 2, "Partner Delivery", "IT GRUPO", "Peru", "Gold; 139 refs", None,
         "https://www.odoo.com/partners/country/peru-168", "ENRICH", 82, "Queued"],
    ])
    report = import_pool(session, strategy, path)
    assert report["rows_read"] == 7
    rows = {r["company"]: r for r in report["rows"]}
    assert rows["Konos"]["outcome"] == "created"
    assert rows["Konos"]["identity_from"] == "contact_email_domain"
    assert rows["Konos"]["contact"] == "no_public_source", "the pool's word is not a source"
    assert rows["Elite Solutions"]["contact"] == "role_mailbox_rejected"
    assert rows["Blanco Martín & Asociados SpA"]["outcome"] == "attached"
    assert rows["B2B SaaS Platform"]["outcome"] == "manual_application"
    assert rows["Moveapps"]["outcome"] == "manual_application"
    assert rows["HV Talent"]["identity_from"] == "source_url_is_company_site"
    assert rows["IT GRUPO"]["outcome"] == "needs_enrichment_no_domain"
    signal = session.scalar(select(CeSignal).where(CeSignal.company_name == "Konos"))
    assert signal.attributes["pool_readiness"] == "READY"
    assert signal.kind == "PARTNER_LISTING"
    again = import_pool(session, strategy, path)
    assert again["duplicates"] == 7 and again["created"] == 0
    from boro_gtm.engine.importer import import_status

    status = import_status(session, report["source_key"])
    assert status["by_state"]["MANUAL_APPLICATION"] == 2
    assert status["by_state"]["ENRICH_NO_DOMAIN"] == 1


def test_a_pool_address_the_directory_published_is_attached_with_that_source(
        session, strategy, tmp_path):
    odoo_signal = _signal(session, domain="konos.cl", source_key="odoo_partners_cl")
    odoo_signal.url = "https://www.odoo.com/partners/konos-1"
    odoo_signal.attributes = {**odoo_signal.attributes, "published_email": "ventas@konos.cl"}
    session.flush()
    path = _pool_xlsx(tmp_path, [[1, 1, "Partner Delivery", "Konos", "Chile", "Gold",
                                  "ventas@konos.cl",
                                  "https://www.odoo.com/partners/country/chile-44", "READY",
                                  95, "Queued"]])
    report = import_pool(session, strategy, path)
    assert report["contacts_attached"] == 1
    contact = session.scalar(select(CeContact).where(CeContact.email == "ventas@konos.cl"))
    assert contact.source_url == "https://www.odoo.com/partners/konos-1"


def _algolia(hits, pages=1):
    return json.dumps({"hits": hits, "nbPages": pages})


def test_hn_contract_posts_become_trigger_signals_with_their_own_channel():
    from boro_gtm.engine.sources.hn_hiring import HnHiringSource

    thread = {"hits": [{"objectID": "500", "title": "Ask HN: Who is hiring? (November 2026)",
                        "created_at": "2026-11-02T15:00:00Z"}]}
    comments = _algolia([
        {"objectID": "501", "parent_id": 500, "created_at": "2026-11-02T16:00:00Z",
         "comment_text": "Acme Robotics | Backend engineer | REMOTE (US) | CONTRACT<p>"
                         "We need Node and Postgres help. <a href=\"https://acmerobotics.com/jobs\">"
                         "site</a> Email hiring@acmerobotics.com"},
        {"objectID": "502", "parent_id": 500, "created_at": "2026-11-02T16:00:00Z",
         "comment_text": "Beta | Staff engineer | ONSITE | Full-time <a href=\"https://beta.io\">x</a>"},
        {"objectID": "503", "parent_id": 501, "created_at": "2026-11-02T17:00:00Z",
         "comment_text": "a reply, not a post"},
    ])

    class Fetch:
        def get(self, url):
            body = json.dumps(thread) if "search_by_date" in url else comments
            return FetchResult(outcome="OK", http_status=200, body=body.encode())

    source = HnHiringSource()
    page = source.next_page(Fetch(), {})
    assert page.exhausted
    assert [d.external_id for d in page.items] == ["501", "502"]
    acme, beta = page.items
    assert (acme.company_domain, acme.reject_reason, acme.country) == (
        "acmerobotics.com", None, "US")
    assert acme.attributes["published_email"] == "hiring@acmerobotics.com"
    assert acme.attributes["contact_role"] == "contractor_recruiting"
    assert beta.reject_reason == "full_time_employment_not_vendor_demand"


def test_get_on_board_hiring_counts_only_delivery_firms():
    from boro_gtm.engine.sources.getonboard_more import GetOnBoardHiringSource

    def job(jid, title, about):
        return {"id": jid, "attributes": {
            "title": title, "description": "", "countries": ["Chile"],
            "published_at": int(datetime.now(UTC).timestamp()),
            "company": {"data": {"attributes": {"name": jid, "web": f"https://{jid}.cl",
                                                "description": about}}}}}

    payload = json.dumps({"data": [
        job("factory", "Desarrollador Full Stack", "Somos una software factory chilena"),
        job("product", "Desarrollador Full Stack", "Nuestra app de delivery"),
        job("factory2", "Diseñador gráfico", "Consultora de software con clientes en LatAm"),
    ], "meta": {"total_pages": 1}})

    class Fetch:
        def get(self, url):
            return FetchResult(outcome="OK", http_status=200, body=payload.encode())

    page = GetOnBoardHiringSource().next_page(Fetch(), {})
    reasons = {d.external_id: d.reject_reason for d in page.items}
    assert reasons == {"factory": None, "product": "not_a_delivery_firm",
                       "factory2": "not_software_delivery"}
    assert page.items[0].lane == "TRIGGER_OVERFLOW"


def test_get_on_board_company_directory_yields_delivery_partners():
    from boro_gtm.engine.sources.getonboard_more import GetOnBoardCompaniesSource

    payload = json.dumps({"data": [
        {"id": "fabrica", "attributes": {"name": "Fábrica", "web": "https://fabrica.cl",
                                         "country": "CL",
                                         "description": "Desarrollo de software a medida"}},
        {"id": "retail", "attributes": {"name": "Retail", "web": "https://retail.cl",
                                        "country": "CL", "description": "Tiendas de ropa"}},
        {"id": "sinweb", "attributes": {"name": "Sin web", "web": None, "country": "CL",
                                        "description": "Somos una software factory"}},
    ], "meta": {"total_pages": 3}})

    class Fetch:
        def get(self, url):
            return FetchResult(outcome="OK", http_status=200, body=payload.encode())

    page = GetOnBoardCompaniesSource().next_page(Fetch(), {})
    assert not page.exhausted and page.cursor_after == {"page": 2}
    assert [d.reject_reason for d in page.items] == [None, "not_a_delivery_firm",
                                                     "no_company_website"]


def test_explicit_f2_stays_inside_its_window_across_a_holiday(strategy):
    from boro_gtm.engine.outreach import followup_due

    # Initial Wed 2026-12-02; window ends Wed 12-09, the day after the 12-08 holiday.
    initial = datetime(2026, 12, 2, 10, 0, tzinfo=SCL)
    f1 = followup_due(strategy, lane="EXPLICIT_DEMAND", step="F1", initial_sent=initial)
    f2 = followup_due(strategy, lane="EXPLICIT_DEMAND", step="F2", initial_sent=initial,
                      f1_sent=f1)
    assert 5 <= (f2.astimezone(SCL).date() - initial.date()).days <= 7


def test_verification_runs_on_the_real_database_and_leaves_nothing(session, strategy):
    from boro_gtm.engine import verify

    before = session.scalar(select(func.count()).select_from(CeAccount))
    result = verify.run(session, strategy)
    assert result["ok"], result
    assert session.scalar(select(func.count()).select_from(CeAccount)) == before
    assert session.scalar(select(func.count()).select_from(CeSuppression)) == 0


_GMAIL_HISTORY = {"gmail_history_same_address", "gmail_history_same_domain",
                  "gmail_history_sister_domain", "engine_initial_already_sent"}


def _history_out(session, gid, to):
    session.add(CeMailEvent(gmail_message_id=gid, gmail_thread_id=gid, direction="OUT",
                            from_email="abourgeoisroa@somosboro.com", to_emails=[to],
                            headers={}, sent_at=WED_10 - timedelta(days=3),
                            ingested_at=datetime.now(UTC)))
    session.flush()


def _initial_blockers(session, strategy, domain, email):
    """The production gate a first email passes before it is sent (send_blockers)."""
    from boro_gtm.engine.pipeline import send_blockers

    target = _ready_target(session, strategy, domain=domain, email=email)
    contact = session.get(CeContact, target.contact_id)
    return set(send_blockers(session, strategy, target, contact, step="INITIAL"))


def test_a_send_to_a_sister_domain_blocks_the_same_companys_first_email(session, strategy):
    _history_out(session, "o1", "info@opens.lat")  # the real 3 Nov send to Open Solutions
    assert "gmail_history_sister_domain" in _initial_blockers(
        session, strategy, "opens.cl", "ventas@opens.cl")
    # A person at a subdomain of the contacted domain: caught via the account domain.
    assert "gmail_history_same_domain" in _initial_blockers(
        session, strategy, "opens.lat", "ana@mail.opens.lat")


@pytest.mark.parametrize(("sent_to", "domain", "email", "why"), [
    ("info@opens.lat", "opensource.cl", "ana@opensource.cl",
     "a longer name that merely starts the same is another company"),
    ("info@opens.lat", "opensolutions.cl", "ana@opensolutions.cl",
     "a name containing the contacted name is another company"),
    ("info@opens.lat", "myopens.cl", "ana@myopens.cl", "suffix match is not a match"),
    ("contacto.hadriva@gmail.com", "hadriva.cl", "ana@hadriva.cl",
     "a freemail address never turns its local part into a company domain"),
    ("someone@gmail.com", "gmailtools.cl", "ana@gmailtools.cl",
     "freemail providers are never a company's sister domain"),
    ("hola@abc.io", "abc.cl", "ana@abc.cl", "names under 4 characters never match"),
])
def test_superficial_name_matches_do_not_block_a_first_email(session, strategy, sent_to, domain,
                                                             email, why):
    _history_out(session, "h1", sent_to)
    assert not (_initial_blockers(session, strategy, domain, email) & _GMAIL_HISTORY), why


def test_same_name_under_another_tld_is_treated_as_the_same_company(session, strategy):
    """Known, deliberate over-match, identical to same_company_domain (used by parity and
    the READY audit): it can only withhold a first email, never cause one. Accounts whose
    sole link to Gmail history is a sister domain are listed for manual review."""
    _history_out(session, "o1", "info@opens.lat")
    assert "gmail_history_sister_domain" in _initial_blockers(
        session, strategy, "opens.mx", "info@opens.mx")


def _initials(session, target):
    return list(session.scalars(select(CeMessage).where(CeMessage.target_id == target.id,
                                                         CeMessage.step == "INITIAL")))


def test_a_sister_only_match_is_held_for_review_not_blocked(session, strategy):
    from boro_gtm.engine.actions import sister_review_list

    _history_out(session, "o1", "info@opens.lat")
    target = _ready_target(session, strategy, domain="opens.cl", email="ventas@opens.cl")
    result = plan_initials(session, strategy, now=WED_10)
    assert result["planned"] == 0 and result["blocked"][0]["held_for_review"] is True
    assert target.state == S.READY and _initials(session, target) == []
    held = sister_review_list(session)
    assert [h["domain"] for h in held] == ["opens.cl"]
    assert held[0]["gmail_sent_to"] == ["info@opens.lat"]
    # Held targets are not picked again, so they never eat a day's slots.
    again = plan_initials(session, strategy, now=WED_10 + timedelta(days=1))
    assert again["blocked"] == [] and _initials(session, target) == []


def test_a_person_decides_a_held_sister_match(session, strategy):
    from boro_gtm.engine.actions import sister_review, sister_review_list
    from boro_gtm.engine.pipeline import SISTER_CONFIRMED

    _history_out(session, "o1", "info@opens.lat")
    other = _ready_target(session, strategy, domain="opens.mx", email="ana@opens.mx")
    same = _ready_target(session, strategy, domain="opens.cl", email="ventas@opens.cl")
    plan_initials(session, strategy, now=WED_10)
    sister_review(session, same.id, "same", actor="test")
    assert same.state == S.BLOCKED
    assert session.scalar(select(CeTargetEvent.reason).where(
        CeTargetEvent.target_id == same.id, CeTargetEvent.to_state == S.BLOCKED)
    ) == SISTER_CONFIRMED
    sister_review(session, other.id, "different", actor="test")
    assert sister_review_list(session) == []
    plan_initials(session, strategy, now=WED_10 + timedelta(days=1), shadow=False)
    assert [m.status for m in _initials(session, other)] == ["SCHEDULED"]
    with pytest.raises(EngineError):
        sister_review(session, other.id, "same", actor="test")  # no longer held


def test_a_sister_send_seen_after_planning_holds_at_send_time(session, strategy):
    from boro_gtm.engine.actions import sister_review

    target = _ready_target(session, strategy, domain="opens.cl", email="ventas@opens.cl")
    plan_initials(session, strategy, now=WED_10 - timedelta(hours=1))
    [message] = _initials(session, target)
    _history_out(session, "o1", "info@opens.lat")  # Gmail ingest finds it before the send
    send_due(session, strategy, FakeGateway(), shadow=False, now=message.due_at)
    assert (message.status, target.state) == ("BLOCKED", S.READY)
    sister_review(session, target.id, "different", actor="test")
    assert message.status == "CANCELLED"
    plan_initials(session, strategy, now=WED_10 + timedelta(days=1), shadow=False)
    assert message.status == "SCHEDULED", "the cleared target gets its first email after all"


@pytest.mark.parametrize(("stored", "reads_gmail"), [
    ({"value": {"seen": 801, "new": 0, "complete": True}}, True),  # worker._jsonable
    ({"seen": 801, "new": 0, "complete": True}, True),             # bare (CLI, older rows)
    ({"value": {"skipped": "no_gmail_credentials"}}, False),       # ran, read nothing
    ({"skipped": "no_gmail_credentials"}, False),
    ({"value": None}, False),
])
def test_the_cutover_report_reads_the_last_ingest_result_wrapped_or_bare(
        session, strategy, monkeypatch, tmp_path, stored, reads_gmail):
    from boro_gtm.engine.cutover import report
    from boro_gtm.engine.models import CeTaskRun
    from boro_gtm.engine.pipeline import now_utc

    creds = tmp_path / "gmail.json"
    creds.write_text("{}")
    creds.chmod(0o600)
    monkeypatch.setenv("GTM_ENGINE_GMAIL_CREDENTIALS", str(creds))
    session.add(CeTaskRun(task="ingest", worker="outreach", status="OK", started_at=now_utc(),
                          finished_at=now_utc(), result=stored))
    session.flush()
    check = report(session)["checks"]["gmail_oauth_connected"]
    assert check["ok"] is reads_gmail, check
    assert (check["last_successful_read"] is not None) is reads_gmail


def test_the_cutover_report_judges_only_the_latest_successful_ingest(session, strategy,
                                                                     monkeypatch, tmp_path):
    from boro_gtm.engine.cutover import report
    from boro_gtm.engine.models import CeTaskRun
    from boro_gtm.engine.pipeline import now_utc

    creds = tmp_path / "gmail.json"
    creds.write_text("{}")
    creds.chmod(0o600)
    monkeypatch.setenv("GTM_ENGINE_GMAIL_CREDENTIALS", str(creds))
    moment = now_utc()
    session.add_all([
        CeTaskRun(task="ingest", worker="outreach", status="OK",
                  started_at=moment - timedelta(minutes=30), result={"value": {"seen": 801}}),
        # A later failure does not hide the last good read...
        CeTaskRun(task="ingest", worker="outreach", status="FAILED",
                  started_at=moment - timedelta(minutes=5), error="GmailUnavailableError"),
    ])
    session.flush()
    assert report(session)["checks"]["gmail_oauth_connected"]["ok"] is True
    # ...but a good read older than an hour is not "connected".
    session.query(CeTaskRun).filter(CeTaskRun.status == "OK").update(
        {"started_at": moment - timedelta(hours=2)})
    session.flush()
    assert report(session)["checks"]["gmail_oauth_connected"]["ok"] is False


def test_the_cutover_report_cannot_pass_on_configuration_alone(session, strategy,
                                                               monkeypatch, tmp_path):
    from boro_gtm.engine.cutover import report

    creds = tmp_path / "gmail.json"
    creds.write_text("{}")
    creds.chmod(0o600)
    monkeypatch.setenv("GTM_ENGINE_GMAIL_CREDENTIALS", str(creds))
    result = report(session)
    assert result["ready_for_live"] is False
    for check in ("gmail_oauth_connected", "gmail_history_backfill_complete",
                  "shadow_ran_two_business_days_after_backfill", "ready_audit_precision",
                  "shadow_parity_no_critical_mismatch", "worker_restart_tested",
                  "database_backup_exists", "restore_test_passed", "off_host_backup"):
        assert check in result["failing"], check


def test_a_verified_pc_copy_counts_as_off_host_only_while_fresh(session, strategy):
    from boro_gtm.engine import controls
    from boro_gtm.engine.cutover import report

    def off_host(**copy):
        controls.put(session, controls.OFF_HOST_COPY_KEY, copy)
        session.flush()
        return report(session)["checks"]["off_host_backup"]["ok"]

    now = datetime.now(UTC)
    assert off_host(verified=True, backup_at=(now - timedelta(hours=30)).isoformat()) is True
    assert off_host(verified=False, backup_at=(now - timedelta(hours=1)).isoformat()) is False
    assert off_host(verified=True, backup_at=(now - timedelta(hours=60)).isoformat()) is False
    assert off_host(verified=True) is False


def test_parity_flags_a_shadow_email_to_a_company_already_contacted(session, strategy):
    from boro_gtm.engine import parity

    target = _ready_target(session, strategy, domain="tierranube.cl", email="ana@tierranube.cl")
    session.add(CeMailEvent(gmail_message_id="x1", gmail_thread_id="tx", direction="OUT",
                            from_email="abourgeoisroa@somosboro.com",
                            to_emails=["ventas@tierranube.cl"], headers={},
                            sent_at=WED_10 - timedelta(days=3), ingested_at=datetime.now(UTC)))
    session.flush()
    # Planned as if Gmail had not been read yet (the risk parity exists to catch).
    from boro_gtm.engine.outreach import _create_message

    message = _create_message(session, strategy, target, session.get(CeContact, target.contact_id),
                              "INITIAL", WED_10)
    message.status = "SHADOW"
    session.flush()
    day = parity.day_report(session, strategy, WED_10.date())
    assert day["critical_mismatches"] == 1
    assert day["critical"][0]["critical"] == ["would_contact_already_contacted"]


def _gmail_transport(mailbox="abourgeoisroa@somosboro.com"):
    import httpx

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == "oauth2.googleapis.com":
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer at"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": mailbox})
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _creds(tmp_path, mode=0o600):
    path = tmp_path / "gmail.json"
    path.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    path.chmod(mode)
    return path


def test_the_gmail_gateway_refreshes_its_token_once_and_reads_the_mailbox(tmp_path):
    from boro_gtm.engine.gmail import GmailApiGateway

    client, calls = _gmail_transport()
    gateway = GmailApiGateway("abourgeoisroa@somosboro.com", str(_creds(tmp_path)), client)
    assert gateway.mailbox() == "abourgeoisroa@somosboro.com"
    assert gateway.list_ids("in:sent", None) == (["m1"], None)
    assert calls.count("/token") == 1, "one refresh serves both calls"


def test_gmail_credentials_readable_by_others_are_refused(tmp_path):
    from boro_gtm.engine.gmail import GmailApiGateway, GmailUnavailableError

    with pytest.raises(GmailUnavailableError, match="chmod 600"):
        GmailApiGateway("abourgeoisroa@somosboro.com", str(_creds(tmp_path, 0o644)))


def test_a_token_for_another_mailbox_is_never_used(tmp_path, monkeypatch):
    from boro_gtm.engine import worker as w
    from boro_gtm.engine.gmail import GmailApiGateway, GmailUnavailableError
    from boro_gtm.engine.settings import engine_settings

    client, _ = _gmail_transport(mailbox="amaru.personal@gmail.com")
    creds = _creds(tmp_path)
    monkeypatch.setenv("GTM_ENGINE_GMAIL_CREDENTIALS", str(creds))
    monkeypatch.setattr(w, "GmailApiGateway",
                        lambda sender, path: GmailApiGateway(sender, path, client))
    engine = w.Engine(load_strategy(), engine_settings())
    with pytest.raises(GmailUnavailableError, match="not the corporate sender"):
        engine.gateway()


def test_a_pool_row_is_resolved_on_reimport_once_the_directory_knows_it(
        session, strategy, tmp_path):
    rows = [[34, 2, "Partner Delivery", "IT GRUPO", "Peru", "Gold; 139 refs", None,
             "https://www.odoo.com/partners/country/peru-168", "ENRICH", 82, "Queued"]]
    path = _pool_xlsx(tmp_path, rows)
    assert import_pool(session, strategy, path)["needs_enrichment_no_domain"] == 1
    crawled = _signal(session, domain="itgrupo.net", source_key="odoo_partners_pe")
    crawled.company_name = "IT Grupo"
    session.flush()
    target_from_signal(session, strategy, crawled)
    again = import_pool(session, strategy, path)
    assert again["resolved_on_reimport"] == 1
    assert again["rows"][0]["outcome"] == "attached", "the crawled partner already has a target"
    signal = session.scalar(select(CeSignal).where(CeSignal.source_key.like("import:%")))
    assert signal.company_domain == "itgrupo.net"
    assert signal.attributes["evidence_es"] == ("aparece como Gold Partner en el directorio "
                                                "oficial de Odoo")


def test_a_personal_mailbox_provider_is_never_a_company():
    from boro_gtm.engine.pipeline import account_key, same_company_domain

    assert account_key("gmail.com") is None
    assert account_key("hotmail.com") is None
    assert same_company_domain("info@opens.lat", "opens.cl")
    assert not same_company_domain("contacto@odoosistema.com", "consulnet.io")
    assert not same_company_domain("someone@gmail.com", "gmail.com")


def test_a_partner_without_a_website_is_identified_by_its_published_address():
    from boro_gtm.engine.sources.odoo_partners import OdooPartnerSource

    page = ('<h1>Blue Minds SpA</h1> Ready Partner '
            '<span itemprop="email">grobles@blueminds.cl</span>')
    draft = OdooPartnerSource("CL")._draft("/partners/blue-minds-1", page)
    assert (draft.company_domain, draft.reject_reason) == ("blueminds.cl", None)
    assert draft.attributes["identity_from"] == "directory_email_domain"
    freemail = OdooPartnerSource("CL")._draft(
        "/partners/x-2", '<h1>X</h1><span itemprop="email">x@gmail.com</span>')
    assert freemail.reject_reason == "no_company_website_on_listing"


def test_a_directory_address_on_a_sister_domain_is_the_partners_own(session, strategy):
    signal = _signal(session, domain="opens.cl")
    signal.attributes = {**signal.attributes, "published_email": "info@opens.lat"}
    target, _ = target_from_signal(session, strategy, signal)
    enrich_target(session, strategy, target, SiteFetcher())
    assert session.get(CeContact, target.contact_id).email == "info@opens.lat"


def test_a_ready_target_on_a_support_mailbox_is_requalified(session, strategy):
    from boro_gtm.engine.enrichment import enrich_batch

    target = _ready_target(session, strategy, domain="elitesolutions.cl",
                           email="soporte@elitesolutions.cl")
    fetcher = SiteFetcher({"https://elitesolutions.cl/contacto":
                           "Escríbenos: comercial@elitesolutions.cl"})
    enrich_batch(session, strategy, fetcher)
    assert target.state == S.READY
    assert session.get(CeContact, target.contact_id).email == "comercial@elitesolutions.cl"
    reasons = session.scalars(select(CeTargetEvent.reason).where(
        CeTargetEvent.target_id == target.id))
    assert "requalify:role_mailbox" in list(reasons)


@pytest.mark.parametrize(("text", "delivery"), [
    ("Somos una software factory chilena", True),
    ("Desarrollamos aplicaciones web para nuestros clientes", True),
    ("Agencia de marketing para nuestros clientes", False),
    ("Reclutamiento de talento para nuestros clientes", False),
    ("Nuestra app de delivery de comida", False),
])
def test_a_delivery_firm_is_one_that_builds_software_for_clients(text, delivery):
    from boro_gtm.engine.sources.delivery import delivery_evidence

    assert (delivery_evidence(text) is not None) is delivery


def test_hn_rejects_non_software_contract_roles_and_cleans_company_names():
    from boro_gtm.engine.sources.hn_hiring import HnHiringSource

    source = HnHiringSource()
    sales = source._draft({"objectID": "1", "created_at": "2026-11-02T16:00:00Z",
                           "comment_text": "REVNODE | Founding Sales | Remote | Part-time "
                                           "contract <a href=\"https://revnode.com\">x</a>"},
                          "2026-11-02T15:00:00Z")
    assert sales.reject_reason == "not_software_delivery"
    named = source._draft({"objectID": "2", "created_at": "2026-11-02T16:00:00Z",
                           "comment_text": "Sendery ( https://sendery.co ) | iOS Engineer | "
                                           "Contract <a href=\"https://sendery.co\">x</a>"},
                          "2026-11-02T15:00:00Z")
    assert named.company_name == "Sendery"
    assert named.company_domain == "sendery.co"


def test_a_directory_title_badge_and_html_entities_never_reach_a_message():
    from boro_gtm.engine.sources.odoo_partners import OdooPartnerSource, clean_partner_name

    assert clean_partner_name("C&amp;M Consultoría Tecnológica SpA   Ready") == (
        "C&M Consultoría Tecnológica SpA", "Ready")
    assert clean_partner_name("Lumaki Silver") == ("Lumaki", "Silver")
    draft = OdooPartnerSource("CL")._draft(
        "/partners/iosbox-1", '<h1>IOSBOX Spa <span>Ready</span></h1>'
        '<a href="https://iosbox.cl"><span itemprop="website">iosbox.cl</span></a>')
    assert draft.company_name == "IOSBOX Spa"
    assert draft.attributes["grade"] == "Ready Partner"


@pytest.mark.parametrize("address", ["servicio@vite24.com", "cfo@andirent.co", "admon@argil.mx",
                                     "finanzas@x.cl", "administracion@x.cl"])
def test_service_and_finance_desks_are_not_sales_channels(address):
    from boro_gtm.engine.pipeline import is_role_mailbox

    assert is_role_mailbox(address)


def test_a_pool_address_on_another_domain_does_not_split_a_known_company(
        session, strategy, tmp_path):
    known = _signal(session, domain="consulnet.io", source_key="odoo_partners_cl")
    known.company_name = "Consulnet"
    target_from_signal(session, strategy, known)
    path = _pool_xlsx(tmp_path, [[4, 1, "Partner Delivery", "Consulnet", "Chile", "Silver",
                                  "contacto@odoosistema.com",
                                  "https://www.odoo.com/partners/country/chile-44", "READY",
                                  93, "Queued"]])
    row = import_pool(session, strategy, path)["rows"][0]
    assert (row["domain"], row["outcome"]) == ("consulnet.io", "attached")
    assert session.scalar(select(func.count()).select_from(CeAccount)) == 1
