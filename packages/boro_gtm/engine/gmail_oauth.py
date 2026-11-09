"""Connect the corporate mailbox once: OAuth 2.0 for an installed app (CE-ADR-007).

The flow follows RFC 8252 (OAuth for native apps) and RFC 7636 (PKCE):

* a loopback listener on ``127.0.0.1`` and an ephemeral port receives the
  redirect, so nobody copies an authorization code out of an address bar;
* ``state`` is a fresh random value, compared in constant time before the code
  is even looked at;
* the code is bound to a PKCE ``code_verifier`` (S256) that never leaves this
  process;
* the grant must carry exactly ``gmail.send`` and ``gmail.readonly``, and the
  token must open the corporate sender's mailbox, or nothing is written.

No authorization code, verifier, client secret, access token or refresh token
is ever printed, logged, put in an exception message or passed on a command
line. The only output is the credentials file, created with mode 600 where the
platform has modes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from boro_gtm.core.errors import GtmError
from boro_gtm.engine.gmail import API, SCOPES, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REQUIRED_SCOPES = frozenset(SCOPES.split())
LOOPBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/oauth2/callback"
_SAFE_TOKEN = re.compile(r"[^a-z0-9_.-]")


class OAuthError(GtmError):
    code = "GMAIL_OAUTH"
    http_status = 400


def _safe(value: Any) -> str:
    """An OAuth error code from a redirect or a JSON body, stripped to a harmless token."""
    return _SAFE_TOKEN.sub("", str(value or "").lower())[:64] or "unknown"


@dataclass(frozen=True)
class Pkce:
    verifier: str = field(repr=False)
    challenge: str
    method: str = "S256"


def new_pkce() -> Pkce:
    """RFC 7636 §4.1–4.2: a 86-character verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return Pkce(verifier=verifier, challenge=challenge)


def new_state() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str = field(repr=False)


def load_client(path: Path) -> ClientConfig:
    """The ``client_secret_….json`` Google Cloud gives for a *Desktop app* client."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OAuthError(f"cannot read the OAuth client file {Path(path).name}") from exc
    installed = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(installed, dict):
        raise OAuthError("the OAuth client must be of type 'Desktop app' "
                         "(the JSON has no 'installed' section)")
    if not installed.get("client_id") or not installed.get("client_secret"):
        raise OAuthError("the OAuth client file lacks client_id or client_secret")
    return ClientConfig(installed["client_id"], installed["client_secret"])


def authorization_url(*, client_id: str, redirect_uri: str, state: str, pkce: Pkce,
                      login_hint: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
        "login_hint": login_hint,
    })


def parse_callback(request_target: str, expected_state: str) -> str:
    """The authorization code from the redirect, once ``state`` has been checked.

    ``state`` is checked first and in constant time: a redirect that does not
    answer this request is rejected whatever else it carries.
    """
    query = parse_qs(urlsplit(request_target).query, keep_blank_values=True)
    states = query.get("state", [])
    if len(states) != 1 or not hmac.compare_digest(states[0].encode(), expected_state.encode()):
        raise OAuthError("the redirect does not answer this authorization request "
                         "(state mismatch); nothing was exchanged")
    if "error" in query:
        raise OAuthError(f"authorization refused by Google: {_safe(query['error'][0])}")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise OAuthError("the redirect carried no authorization code")
    return codes[0]


@dataclass(frozen=True)
class Grant:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    scopes: frozenset[str] = frozenset()


def exchange_code(http: httpx.Client, client: ClientConfig, *, code: str, pkce: Pkce,
                  redirect_uri: str) -> Grant:
    try:
        response = http.post(TOKEN_URL, data={
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": pkce.verifier,
        }, timeout=30)
    except httpx.HTTPError as exc:
        raise OAuthError(f"token exchange failed: {type(exc).__name__}") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code != 200:
        raise OAuthError(f"token exchange failed (HTTP {response.status_code}, "
                         f"{_safe(payload.get('error'))})")
    if not payload.get("refresh_token") or not payload.get("access_token"):
        raise OAuthError("Google returned no refresh token; revoke the app's access at "
                         "myaccount.google.com/permissions and connect again")
    granted = frozenset(str(payload.get("scope", "")).split())
    if granted != REQUIRED_SCOPES:
        raise OAuthError("the grant must carry exactly gmail.send and gmail.readonly; "
                         f"missing {sorted(REQUIRED_SCOPES - granted)}, "
                         f"extra {sorted(granted - REQUIRED_SCOPES)}")
    return Grant(payload["access_token"], payload["refresh_token"], granted)


def mailbox_of(http: httpx.Client, access_token: str) -> str:
    try:
        response = http.get(f"{API}/profile",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    except httpx.HTTPError as exc:
        raise OAuthError(f"could not read the mailbox profile: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise OAuthError(f"could not read the mailbox profile (HTTP {response.status_code})")
    return str(response.json().get("emailAddress", "")).lower()


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def write_credentials(out: Path, client: ClientConfig, refresh_token: str) -> None:
    """Write atomically, never readable by others for an instant, never inside a repository."""
    out = Path(out)
    if _inside_git_worktree(out):
        raise OAuthError("refusing to write Gmail credentials inside a git working tree")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = out.with_name(f".{out.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"client_id": client.client_id, "client_secret": client.client_secret,
                       "refresh_token": refresh_token}, handle)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_PAGE = ("<!doctype html><meta charset='utf-8'><title>BoRo GTM</title>"
         "<p style='font-family:sans-serif'>{}</p>")


class _Listener:
    """A one-shot loopback HTTP server that keeps the first callback it receives."""

    def __init__(self, host: str = LOOPBACK_HOST, port: int = 0) -> None:
        listener = self
        self.target: str | None = None
        self._got = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                if urlsplit(self.path).path != CALLBACK_PATH or listener._got.is_set():
                    self.send_response(404)
                    self.end_headers()
                    return
                listener.target = self.path
                listener._got.set()
                body = _PAGE.format("Autorización recibida. Vuelve a la terminal; "
                                    "ya puedes cerrar esta pestaña.").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                # The default writes the request line, code included, to stderr.
                return

        self._server = HTTPServer((host, port), Handler)
        self._server.timeout = 0.5
        self.redirect_uri = f"http://{host}:{self._server.server_port}{CALLBACK_PATH}"

    def wait(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        try:
            while not self._got.is_set():
                if time.monotonic() > deadline:
                    raise OAuthError(f"no authorization arrived within {int(timeout)} s")
                self._server.handle_request()
        finally:
            self._server.server_close()
        assert self.target is not None
        return self.target


def connect(client_secret_file: Path, out: Path, expected_mailbox: str, *,
            http: httpx.Client | None = None,
            open_browser: Callable[[str], Any] | None = None,
            echo: Callable[[str], Any] = print,
            timeout: float = 300.0,
            port: int = 0) -> dict[str, Any]:
    """Run the whole flow. Returns what may be shown: path, mailbox, scopes."""
    import webbrowser

    client = load_client(client_secret_file)
    pkce, state = new_pkce(), new_state()
    listener = _Listener(port=port)
    url = authorization_url(client_id=client.client_id, redirect_uri=listener.redirect_uri,
                            state=state, pkce=pkce, login_hint=expected_mailbox)
    echo(f"Sign in as {expected_mailbox} in the browser window that opens. "
         "If none opens, open this address yourself:")
    echo(url)
    (open_browser or webbrowser.open)(url)
    target = listener.wait(timeout)
    code = parse_callback(target, state)
    own_client = http is None
    http = http or httpx.Client(timeout=30)
    try:
        grant = exchange_code(http, client, code=code, pkce=pkce,
                              redirect_uri=listener.redirect_uri)
        opened = mailbox_of(http, grant.access_token)
    finally:
        if own_client:
            http.close()
    if opened != expected_mailbox.lower():
        raise OAuthError(f"the token opens {opened or 'an unknown mailbox'}, not "
                         f"{expected_mailbox}; nothing was written")
    write_credentials(out, client, grant.refresh_token)
    return {"written": str(out), "mailbox": opened, "scopes_granted": sorted(grant.scopes)}
