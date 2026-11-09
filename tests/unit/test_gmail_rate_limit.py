"""Gmail's per-user quota: reads are paced and retried on a quota refusal, never on a real 403."""

from __future__ import annotations

import json

import httpx
import pytest

from boro_gtm.engine.gmail import GmailApiGateway, GmailUnavailableError

MAILBOX = "abourgeoisroa@somosboro.com"
QUOTA = {"error": {"code": 403, "status": "PERMISSION_DENIED", "errors": [
    {"reason": "rateLimitExceeded", "domain": "usageLimits"}],
    "message": "Quota exceeded for quota metric 'Total Query Cost'"}}
FORBIDDEN = {"error": {"code": 403, "status": "PERMISSION_DENIED", "errors": [
    {"reason": "insufficientPermissions"}], "message": "Insufficient Permission"}}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _gateway(tmp_path, responses):
    """`responses`: per Gmail call, a (status, json, headers) tuple, consumed in order."""
    queue = list(responses)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        calls.append(f"{request.method} {request.url.path}")
        status, body, headers = queue.pop(0)
        return httpx.Response(status, json=body, headers=headers or {})

    creds = tmp_path / "gmail.json"
    creds.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    creds.chmod(0o600)
    clock = FakeClock()
    gateway = GmailApiGateway(MAILBOX, str(creds), httpx.Client(transport=httpx.MockTransport(
        handler)), sleep=clock.sleep, clock=clock)
    return gateway, calls, clock


def test_a_quota_refusal_on_a_read_is_retried_with_backoff(tmp_path):
    gateway, calls, clock = _gateway(tmp_path, [
        (403, QUOTA, None), (429, {}, {"retry-after": "7"}),
        (200, {"messages": [{"id": "m1"}]}, None)])
    assert gateway.list_ids("in:sent", None) == (["m1"], None)
    assert len(calls) == 3
    assert 2.0 in clock.slept, "exponential backoff after the first quota refusal"
    assert 7.0 in clock.slept, "Retry-After is honoured"


def test_a_real_permission_403_is_not_retried(tmp_path):
    gateway, calls, _ = _gateway(tmp_path, [(403, FORBIDDEN, None)])
    with pytest.raises(GmailUnavailableError, match="HTTP 403") as caught:
        gateway.mailbox()
    assert "rate limited" not in str(caught.value)
    assert len(calls) == 1


def test_a_send_is_never_retried_here(tmp_path):
    gateway, calls, _ = _gateway(tmp_path, [(403, QUOTA, None)])
    with pytest.raises(GmailUnavailableError, match="rate limited"):
        gateway.send(b"raw", None)
    assert calls == ["POST /gmail/v1/users/me/messages/send"]


def test_a_send_that_times_out_is_attempted_once_and_the_timeout_surfaces(tmp_path):
    """A timeout may mean Gmail accepted the message: never resend here. The
    sender records SENDING_UNCONFIRMED and the reconciler looks the message up
    by Message-ID before anything is sent again (outreach.reconcile_sending)."""
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        posts.append(request.method)
        raise httpx.ReadTimeout("timed out", request=request)

    creds = tmp_path / "gmail.json"
    creds.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    creds.chmod(0o600)
    clock = FakeClock()
    gateway = GmailApiGateway(MAILBOX, str(creds), httpx.Client(transport=httpx.MockTransport(
        handler)), sleep=clock.sleep, clock=clock)
    with pytest.raises(httpx.ReadTimeout):
        gateway.send(b"raw", None)
    assert posts == ["POST"]


def test_a_read_that_times_out_is_not_retried_as_if_it_were_a_quota_refusal(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        calls.append(request.method)
        raise httpx.ReadTimeout("timed out", request=request)

    creds = tmp_path / "gmail.json"
    creds.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    creds.chmod(0o600)
    clock = FakeClock()
    gateway = GmailApiGateway(MAILBOX, str(creds), httpx.Client(transport=httpx.MockTransport(
        handler)), sleep=clock.sleep, clock=clock)
    with pytest.raises(httpx.ReadTimeout):
        gateway.list_ids("in:sent", None)
    assert calls == ["GET"] and clock.slept == []


def test_reads_give_up_after_the_retry_budget(tmp_path):
    gateway, calls, _ = _gateway(tmp_path, [(403, QUOTA, None)] * (
        GmailApiGateway.MAX_RETRIES + 1))
    with pytest.raises(GmailUnavailableError, match="rate limited"):
        gateway.mailbox()
    assert len(calls) == GmailApiGateway.MAX_RETRIES + 1


def test_calls_are_paced(tmp_path):
    gateway, _, clock = _gateway(tmp_path, [
        (200, {"emailAddress": MAILBOX}, None), (200, {"emailAddress": MAILBOX}, None)])
    gateway.mailbox()
    gateway.mailbox()
    assert clock.slept == [pytest.approx(GmailApiGateway.MIN_INTERVAL)]
