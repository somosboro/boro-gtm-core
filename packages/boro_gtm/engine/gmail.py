"""Gmail for the corporate sender, over the REST API (CE-ADR-007).

No Google SDK: httpx is already a dependency, and the four calls the engine
makes (send, find by Message-ID, list, get) do not justify one.

Credentials live in a JSON file outside the repository, readable only by the
service user: `{"client_id", "client_secret", "refresh_token"}`. Nothing here
logs or returns a token.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from boro_gtm.core.errors import GtmError

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = ("https://www.googleapis.com/auth/gmail.send "
          "https://www.googleapis.com/auth/gmail.readonly")
CREDENTIALS_ENV = "GTM_ENGINE_GMAIL_CREDENTIALS"
#: Headers kept on ingested messages. Enough to classify bounces and
#: auto-replies; never the body.
KEPT_HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To",
                "References", "Auto-Submitted", "X-Autoreply", "X-Autorespond",
                "Precedence", "Content-Type", "X-Failed-Recipients", "List-Unsubscribe")


class GmailUnavailableError(GtmError):
    code = "GMAIL_UNAVAILABLE"
    http_status = 503


@dataclass(frozen=True)
class SentRef:
    message_id: str
    thread_id: str


@dataclass
class RemoteMessage:
    id: str
    thread_id: str
    label_ids: list[str]
    headers: dict[str, str]
    snippet: str
    internal_date: float
    body_text: str = ""

    @property
    def from_email(self) -> str | None:
        found = getaddresses([self.headers.get("From", "")])
        return found[0][1].lower() if found and found[0][1] else None

    @property
    def to_emails(self) -> list[str]:
        return [a.lower() for _, a in getaddresses(
            [self.headers.get("To", ""), self.headers.get("Cc", "")]) if a]


class MailGateway(Protocol):
    sender: str

    def send(self, raw: bytes, thread_id: str | None) -> SentRef: ...
    def find_by_rfc822(self, rfc822_id: str) -> SentRef | None: ...
    def list_ids(self, query: str, page_token: str | None) -> tuple[list[str], str | None]: ...
    def get(self, message_id: str) -> RemoteMessage: ...


def build_mime(*, sender: str, sender_name: str, to: str, subject: str, body: str,
               rfc822_id: str, in_reply_to: str | None, when) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = rfc822_id
    msg["Date"] = format_datetime(when)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return bytes(msg)


class GmailApiGateway:
    """The real thing. Constructed only when credentials exist."""

    #: Seconds between API calls. The per-user quota (6,000 units/min on the
    #: corporate project, not adjustable) is enforced over short windows, so an
    #: unpaced backfill is refused after ~100 messages.
    MIN_INTERVAL = 0.25
    #: Rate-limit refusals retried (reads only) before giving up.
    MAX_RETRIES = 8
    MAX_BACKOFF = 90.0

    def __init__(self, sender: str, credentials_path: str | None = None,
                 client: httpx.Client | None = None,
                 sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        path = credentials_path or os.environ.get(CREDENTIALS_ENV)
        if not path or not Path(path).exists():
            raise GmailUnavailableError(
                f"no Gmail credentials: set {CREDENTIALS_ENV} to the OAuth JSON file",
                details={"env_var": CREDENTIALS_ENV})
        if os.name == "posix" and Path(path).stat().st_mode & 0o077:
            raise GmailUnavailableError(
                "Gmail credentials file must be readable by its owner only (chmod 600)",
                details={"env_var": CREDENTIALS_ENV})
        self._creds = json.loads(Path(path).read_text())
        missing = {"client_id", "client_secret", "refresh_token"} - set(self._creds)
        if missing:
            raise GmailUnavailableError(f"Gmail credentials file lacks {sorted(missing)}")
        self.sender = sender
        self._client = client or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_expires = 0.0
        self._sleep, self._clock = sleep, clock
        self._last_call = float("-inf")

    def _auth(self) -> dict[str, str]:
        if self._token is None or time.time() > self._token_expires - 60:
            response = self._client.post(TOKEN_URL, data={
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
                "refresh_token": self._creds["refresh_token"],
                "grant_type": "refresh_token",
            })
            if response.status_code != 200:
                raise GmailUnavailableError(
                    f"token refresh failed with HTTP {response.status_code}")
            payload = response.json()
            self._token = payload["access_token"]
            self._token_expires = time.time() + int(payload.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        # Only reads are retried: a refused send is retried by the sender's
        # own reconciliation (find by Message-ID first), never blind here.
        retries = self.MAX_RETRIES if method == "GET" else 0
        for attempt in range(retries + 1):
            wait = self._last_call + self.MIN_INTERVAL - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()
            response = self._client.request(method, f"{API}{path}", headers=self._auth(),
                                            **kwargs)
            if attempt < retries and _rate_limited(response):
                self._sleep(_backoff(response, attempt, self.MAX_BACKOFF))
                continue
            break
        if response.status_code >= 400:
            raise GmailUnavailableError(f"Gmail {method} {path.split('?')[0]} -> "
                                        f"HTTP {response.status_code}"
                                        + (" (rate limited)" if _rate_limited(response) else ""))
        return response.json()

    def mailbox(self) -> str:
        """The address these credentials actually open (gmail.readonly allows it)."""
        return str(self._call("GET", "/profile")["emailAddress"]).lower()

    def send(self, raw: bytes, thread_id: str | None) -> SentRef:
        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(raw).decode()}
        if thread_id:
            body["threadId"] = thread_id
        data = self._call("POST", "/messages/send", json=body)
        return SentRef(data["id"], data["threadId"])

    def find_by_rfc822(self, rfc822_id: str) -> SentRef | None:
        ids, _ = self.list_ids(f"rfc822msgid:{rfc822_id.strip('<>')}", None)
        if not ids:
            return None
        message = self.get(ids[0])
        return SentRef(message.id, message.thread_id)

    def list_ids(self, query: str, page_token: str | None) -> tuple[list[str], str | None]:
        params: dict[str, Any] = {"q": query, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        data = self._call("GET", "/messages", params=params)
        return [m["id"] for m in data.get("messages", [])], data.get("nextPageToken")

    def get(self, message_id: str) -> RemoteMessage:
        data = self._call("GET", f"/messages/{message_id}", params={"format": "full"})
        payload = data.get("payload") or {}
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])
                   if h["name"] in KEPT_HEADERS}
        return RemoteMessage(
            id=data["id"], thread_id=data["threadId"], label_ids=data.get("labelIds", []),
            headers=headers, snippet=data.get("snippet", ""),
            internal_date=int(data.get("internalDate", "0")) / 1000,
            body_text=_plain_text(payload)[:20000],
        )


_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


def _rate_limited(response: httpx.Response) -> bool:
    """429, or Gmail's 403 whose reason is a quota (not a permission) refusal."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        errors = (response.json().get("error") or {}).get("errors") or []
    except ValueError:
        return False
    return any(isinstance(e, dict) and e.get("reason") in _RATE_REASONS for e in errors)


def _backoff(response: httpx.Response, attempt: int, ceiling: float) -> float:
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isdigit():
        return min(float(retry_after), ceiling)
    return min(2.0 ** (attempt + 1), ceiling)


def _plain_text(part: dict[str, Any]) -> str:
    if part.get("mimeType", "").startswith("text/plain") and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace")
    return "\n".join(_plain_text(p) for p in part.get("parts", []) or [])


@dataclass
class FakeGateway:
    """An in-memory Gmail, for tests and for shadow rehearsals."""

    sender: str = "abourgeoisroa@somosboro.com"
    sent: list[dict[str, Any]] = field(default_factory=list)
    inbox: dict[str, RemoteMessage] = field(default_factory=dict)
    fail_after_send: bool = False

    def send(self, raw: bytes, thread_id: str | None) -> SentRef:
        import email

        parsed = email.message_from_bytes(raw)
        n = len(self.sent) + 1
        ref = SentRef(f"m{n}", thread_id or f"t{n}")
        self.sent.append({"raw": raw, "rfc822": parsed["Message-ID"], "to": parsed["To"],
                          "subject": parsed["Subject"], "ref": ref})
        self.inbox[ref.message_id] = RemoteMessage(
            id=ref.message_id, thread_id=ref.thread_id, label_ids=["SENT"],
            headers={k: parsed[k] for k in ("From", "To", "Subject", "Message-ID", "Date")
                     if parsed[k]},
            snippet=parsed["Subject"] or "", internal_date=time.time())
        if self.fail_after_send:
            self.fail_after_send = False
            raise GmailUnavailableError("connection dropped after the send was accepted")
        return ref

    def find_by_rfc822(self, rfc822_id: str) -> SentRef | None:
        for item in self.sent:
            if item["rfc822"] == rfc822_id:
                return item["ref"]
        return None

    def list_ids(self, query: str, page_token: str | None) -> tuple[list[str], str | None]:
        return sorted(self.inbox, key=lambda k: self.inbox[k].internal_date), None

    def get(self, message_id: str) -> RemoteMessage:
        return self.inbox[message_id]

    def deliver(self, message: RemoteMessage) -> None:
        self.inbox[message.id] = message


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
