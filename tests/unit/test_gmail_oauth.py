"""The one-time Gmail OAuth connection: PKCE, state, loopback redirect, no leaked secrets."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from boro_gtm.engine import gmail_oauth as oauth
from boro_gtm.engine.gmail_oauth import OAuthError

MAILBOX = "abourgeoisroa@somosboro.com"
SEND = "https://www.googleapis.com/auth/gmail.send"
READ = "https://www.googleapis.com/auth/gmail.readonly"

CLIENT_SECRET = "GOCSPX-client-secret-sentinel"
CODE = "4/0auth-code-sentinel"
ACCESS = "ya29.access-token-sentinel"
REFRESH = "1//refresh-token-sentinel"
SECRETS = (CLIENT_SECRET, CODE, ACCESS, REFRESH)


@pytest.fixture
def client_file(tmp_path):
    path = tmp_path / "client_secret_test.json"
    path.write_text(json.dumps({"installed": {
        "client_id": "123.apps.googleusercontent.com", "client_secret": CLIENT_SECRET}}))
    return path


@pytest.fixture
def out(tmp_path_factory):
    # tmp_path lives outside any git working tree, as the real target must.
    return tmp_path_factory.mktemp("home") / ".boro-gtm" / "gmail.json"


def _google(scope=f"{SEND} {READ}", mailbox=MAILBOX, token_status=200, token_body=None):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            seen["token_form"] = parse_qs(request.content.decode())
            body = token_body if token_body is not None else {
                "access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599,
                "scope": scope, "token_type": "Bearer"}
            return httpx.Response(token_status, json=body)
        assert request.url.path.endswith("/profile")
        assert request.headers["Authorization"] == f"Bearer {ACCESS}"
        return httpx.Response(200, json={"emailAddress": mailbox})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _browser(*, state_override=None, params=None):
    """Plays the user: follows the consent URL and lets Google redirect to the listener."""
    seen: dict = {}

    def open_browser(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        seen["auth_query"] = query
        redirect = query["redirect_uri"][0]
        answer = params if params is not None else {"code": CODE}
        answer = {"state": state_override or query["state"][0], **answer}

        def follow() -> None:
            seen["favicon"] = httpx.get(redirect.replace(oauth.CALLBACK_PATH, "/favicon.ico"))
            seen["response"] = httpx.get(f"{redirect}?{urlencode(answer)}")

        threading.Thread(target=follow, daemon=True).start()

    return open_browser, seen


# -- PKCE and state ----------------------------------------------------------------

def test_pkce_uses_s256_with_a_verifier_rfc_7636_accepts():
    pkce = oauth.new_pkce()
    assert pkce.method == "S256"
    assert 43 <= len(pkce.verifier) <= 128
    assert set(pkce.verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                                     "0123456789-._~")
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pkce.verifier.encode()).digest()).rstrip(b"=").decode()
    assert pkce.challenge == expected
    assert pkce.verifier not in repr(pkce)
    assert oauth.new_pkce().verifier != pkce.verifier
    assert oauth.new_state() != oauth.new_state()


def test_the_consent_url_carries_challenge_state_and_only_the_two_scopes():
    pkce = oauth.new_pkce()
    url = oauth.authorization_url(client_id="cid", redirect_uri="http://127.0.0.1:5/cb",
                                  state="st", pkce=pkce, login_hint=MAILBOX)
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge"] == [pkce.challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["st"]
    assert set(query["scope"][0].split()) == {SEND, READ}
    assert pkce.verifier not in url


@pytest.mark.parametrize("target", [
    f"/oauth2/callback?code={CODE}&state=wrong",
    f"/oauth2/callback?code={CODE}",
    f"/oauth2/callback?code={CODE}&state=expected&state=expected",
    "/oauth2/callback?error=access_denied&state=wrong",
])
def test_a_redirect_with_the_wrong_state_is_refused_before_the_code_is_read(target):
    with pytest.raises(OAuthError, match="state mismatch") as exc:
        oauth.parse_callback(target, "expected")
    assert CODE not in str(exc.value)


def test_an_authorization_error_is_reported_without_echoing_attacker_text():
    with pytest.raises(OAuthError, match="access_denied"):
        oauth.parse_callback("/oauth2/callback?error=access_denied&state=s", "s")
    with pytest.raises(OAuthError) as exc:
        oauth.parse_callback("/oauth2/callback?error=%3Cscript%3Ex&state=s", "s")
    assert "<" not in str(exc.value)
    with pytest.raises(OAuthError, match="no authorization code"):
        oauth.parse_callback("/oauth2/callback?state=s", "s")


# -- exchange ----------------------------------------------------------------------

def test_the_exchange_sends_the_verifier_and_the_same_redirect_uri():
    http, seen = _google()
    pkce = oauth.new_pkce()
    grant = oauth.exchange_code(http, oauth.ClientConfig("cid", CLIENT_SECRET), code=CODE,
                                pkce=pkce, redirect_uri="http://127.0.0.1:9/oauth2/callback")
    form = seen["token_form"]
    assert form["code_verifier"] == [pkce.verifier]
    assert form["grant_type"] == ["authorization_code"]
    assert form["redirect_uri"] == ["http://127.0.0.1:9/oauth2/callback"]
    assert grant.scopes == {SEND, READ}
    assert REFRESH not in repr(grant) and ACCESS not in repr(grant)


@pytest.mark.parametrize("scope, fragment", [
    (SEND, "missing"),
    (f"{SEND} {READ} https://mail.google.com/", "extra"),
])
def test_a_grant_with_other_scopes_is_refused(scope, fragment):
    http, _ = _google(scope=scope)
    with pytest.raises(OAuthError, match=fragment):
        oauth.exchange_code(http, oauth.ClientConfig("cid", CLIENT_SECRET), code=CODE,
                            pkce=oauth.new_pkce(), redirect_uri="http://127.0.0.1:9/")


def test_a_failed_exchange_reports_the_error_code_and_no_secret():
    http, _ = _google(token_status=400, token_body={
        "error": "invalid_grant", "error_description": f"Bad code {CODE}"})
    with pytest.raises(OAuthError, match=r"HTTP 400, invalid_grant") as exc:
        oauth.exchange_code(http, oauth.ClientConfig("cid", CLIENT_SECRET), code=CODE,
                            pkce=oauth.new_pkce(), redirect_uri="http://127.0.0.1:9/")
    assert all(s not in str(exc.value) for s in SECRETS)


def test_a_grant_without_a_refresh_token_is_refused():
    http, _ = _google(token_body={"access_token": ACCESS, "scope": f"{SEND} {READ}"})
    with pytest.raises(OAuthError, match="no refresh token"):
        oauth.exchange_code(http, oauth.ClientConfig("cid", CLIENT_SECRET), code=CODE,
                            pkce=oauth.new_pkce(), redirect_uri="http://127.0.0.1:9/")


def test_only_a_desktop_app_client_is_accepted(tmp_path):
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "c", "client_secret": CLIENT_SECRET}}))
    with pytest.raises(OAuthError, match="Desktop app") as exc:
        oauth.load_client(web)
    assert CLIENT_SECRET not in str(exc.value)
    assert CLIENT_SECRET not in repr(oauth.ClientConfig("c", CLIENT_SECRET))


# -- the whole flow ----------------------------------------------------------------

def test_the_full_flow_writes_a_private_file_and_prints_no_secret(
        client_file, out, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    http, seen_google = _google()
    open_browser, seen = _browser()
    lines: list[str] = []
    result = oauth.connect(client_file, out, MAILBOX, http=http, open_browser=open_browser,
                           echo=lines.append, timeout=10)

    assert result == {"written": str(out), "mailbox": MAILBOX, "scopes_granted": [READ, SEND]}
    assert json.loads(out.read_text()) == {
        "client_id": "123.apps.googleusercontent.com", "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH}
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
    assert not list(out.parent.glob(".*.tmp"))

    # PKCE: the verifier sent at exchange time hashes to the challenge sent at consent time.
    verifier = seen_google["token_form"]["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert seen["auth_query"]["code_challenge"] == [challenge]
    redirect = seen["auth_query"]["redirect_uri"][0]
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/oauth2/callback")
    assert seen_google["token_form"]["redirect_uri"] == [redirect]
    assert seen["favicon"].status_code == 404
    assert seen["response"].status_code == 200
    assert CODE not in seen["response"].text

    shown = "\n".join(lines) + "".join(capsys.readouterr()) + caplog.text
    shown += json.dumps(result)
    assert all(s not in shown for s in (*SECRETS, verifier)), "a secret reached the output"


def test_a_forged_redirect_writes_nothing_and_never_reaches_google(client_file, out):
    http, seen_google = _google()
    open_browser, _ = _browser(state_override="forged")
    with pytest.raises(OAuthError, match="state mismatch"):
        oauth.connect(client_file, out, MAILBOX, http=http, open_browser=open_browser,
                      echo=lambda _l: None, timeout=10)
    assert "token_form" not in seen_google
    assert not out.exists()


def test_a_refused_consent_writes_nothing(client_file, out):
    http, seen_google = _google()
    open_browser, _ = _browser(params={"error": "access_denied"})
    with pytest.raises(OAuthError, match="access_denied"):
        oauth.connect(client_file, out, MAILBOX, http=http, open_browser=open_browser,
                      echo=lambda _l: None, timeout=10)
    assert "token_form" not in seen_google
    assert not out.exists()


def test_a_token_for_another_mailbox_writes_nothing(client_file, out):
    http, _ = _google(mailbox="someone@gmail.com")
    open_browser, _ = _browser()
    with pytest.raises(OAuthError, match="someone@gmail.com") as exc:
        oauth.connect(client_file, out, MAILBOX, http=http, open_browser=open_browser,
                      echo=lambda _l: None, timeout=10)
    assert not out.exists()
    assert all(s not in str(exc.value) for s in SECRETS)


def test_no_callback_times_out(client_file, out):
    with pytest.raises(OAuthError, match="within 1 s"):
        oauth.connect(client_file, out, MAILBOX, http=_google()[0],
                      open_browser=lambda _u: None, echo=lambda _l: None, timeout=1)
    assert not out.exists()


def test_credentials_are_never_written_inside_a_git_working_tree(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(OAuthError, match="git working tree"):
        oauth.write_credentials(tmp_path / "secrets" / "gmail.json",
                                oauth.ClientConfig("c", CLIENT_SECRET), REFRESH)
    assert not (tmp_path / "secrets" / "gmail.json").exists()


def test_the_listener_never_logs_the_request_line(capfd):
    listener = oauth._Listener()
    threading.Thread(target=lambda: httpx.get(
        f"{listener.redirect_uri}?code={CODE}&state=s"), daemon=True).start()
    target = listener.wait(5)
    assert CODE in target
    captured = capfd.readouterr()
    assert CODE not in captured.out and CODE not in captured.err


def test_the_cli_offers_only_the_connected_flow():
    from typer.testing import CliRunner

    from boro_gtm.engine.cli import app

    names = {c.name for c in app.registered_commands}
    assert "gmail-connect" in names
    assert not names & {"gmail-auth-url", "gmail-auth-exchange"}
    result = CliRunner().invoke(app, ["gmail-connect", "--help"])
    assert result.exit_code == 0
    assert "--code" not in result.output and "--client-secret " not in result.output


def test_the_documented_paths_are_outside_the_repository():
    repo = Path(__file__).resolve().parents[2]
    runbook = (repo / "docs" / "RUNBOOK_COMMERCIAL_ENGINE.md").read_text(encoding="utf-8")
    assert "gmail-auth-exchange" not in runbook and "gmail-auth-url" not in runbook
    assert "gtm engine gmail-connect" in runbook
