"""The personalization gate (CE-ADR-022): deterministic, no network, no model.

Replies came to emails that named a person, cited a checkable recent fact and
asked a specific question; generic sends to info@/contacto@ bounced and got no
answer, and some promised localizations BoRo has not delivered. Nothing is
drafted from a brief that fails `check_brief`, and nothing is sent from a draft
that fails `check_draft`. Both return reasons a person can act on.

`config/capabilities.yaml` says what may be claimed; `config/proof_cases.yaml`
says which cases may be cited (written permission only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import yaml

from boro_gtm.core.config import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "config"
MAX_WORDS = 120
FACT_MAX_AGE_DAYS = 90

#: Desks, not people: a first email to one of these is not personal.
GENERIC_LOCAL_PARTS = frozenset({
    "info", "contacto", "contact", "hola", "hello", "ventas", "soporte", "admin", "office",
    "sales", "support", "comercial", "administracion", "recepcion", "noreply", "no-reply",
})
_HYPOTHESIS = re.compile(r"\b(no s[eé] si|puede que|podr[ií]a(n)?|quiz[aá]s?|tal vez"
                         r"|i'?m not sure|not sure (if|whether)|might|may be)\b", re.I)
#: Interest questions invite a polite no; the question must be about what they did.
_INTEREST = re.compile(
    r"(interesar[ií]a|tiene[ns]? inter[eé]s|tienes inter[eé]s|le(s)? gustar[ií]a|te gustar[ií]a"
    r"|le(s)? sirve|te sirve|would you be interested|are you interested|would it be worth)", re.I)
#: Past behaviour: a preterite / perfect verb or an explicit "last time".
_PAST = re.compile(
    r"(\b\w{2,}(aron|ieron|aste|iste)\b|\b\w{2,}[^aeiou\s]?(ó|ió)\b|\bha(n|s)? \w+(ado|ido)\b"
    r"|\bla [uú]ltima vez\b|\bcuando (tuvieron|necesitaron|les falt)|\bdid you\b"
    r"|\bhave you\b|\blast time\b|\bwhen you (had|needed)\b)", re.I)
_OPT_OUT = re.compile(r"(no te vuelvo a escribir|no le vuelvo a escribir|no vuelvo a escribir"
                      r"|dar(te|le)? de baja|responde \"?no\"?|unsubscribe|reply \"?no\"?)", re.I)
_EMAIL = re.compile(r"^[a-z0-9._%+\-']+@([a-z0-9-]+\.)+[a-z]{2,}$")


@dataclass(frozen=True)
class Verdict:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


@lru_cache(maxsize=4)
def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def capabilities() -> dict[str, Any]:
    return _load("capabilities.yaml")


def proof_cases() -> dict[str, Any]:
    return _load("proof_cases.yaml")


def proof_case(case_id: str | None) -> dict[str, Any] | None:
    return next((c for c in proof_cases()["cases"] if c["id"] == case_id), None)


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _on_domain(host: str, domains: set[str]) -> bool:
    host = host.removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in domains)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def _term_hits(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", text, re.I)]


def is_generic_mailbox(email: str) -> bool:
    return email.split("@")[0].lower() in GENERIC_LOCAL_PARTS


def check_brief(brief: Any, *, company_domains: set[str], today: date,
                mx_ok: bool | None) -> Verdict:
    """Every reason this brief may not be drafted from. `mx_ok` None = not checked yet."""
    errors: list[str] = []
    domains = {d.lower() for d in company_domains if d}
    if not (brief.recipient_name or "").strip():
        errors.append("recipient_name missing: name the person")
    if not (brief.recipient_role or "").strip():
        errors.append("recipient_role missing")
    email = (brief.recipient_email or "").strip().lower()
    if not _EMAIL.match(email):
        errors.append("recipient_email missing or invalid")
    else:
        if is_generic_mailbox(email):
            errors.append(f"recipient_email {email} is a generic mailbox")
        if not _on_domain(email.split("@")[1], domains):
            errors.append(f"recipient_email {email} is not on the company's domain")
        if mx_ok is None:
            errors.append("recipient domain MX not checked")
        elif not mx_ok:
            errors.append(f"recipient domain {email.split('@')[1]} has no MX record")
    if not _on_domain(_host(brief.email_source_url), domains):
        errors.append("email_source_url missing or not on the company's own domain")
    if not (brief.fact_text or "").strip():
        errors.append("fact_text missing")
    if not _host(brief.fact_source_url):
        errors.append("fact_source_url missing")
    if brief.fact_date is None:
        errors.append("fact_date missing")
    elif brief.fact_date > today:
        errors.append("fact_date is in the future")
    elif today - brief.fact_date > timedelta(days=FACT_MAX_AGE_DAYS):
        errors.append(f"fact_date {brief.fact_date} is older than {FACT_MAX_AGE_DAYS} days")
    if not _HYPOTHESIS.search(brief.hypothesis or ""):
        errors.append("hypothesis must be phrased as a doubt (\"no sé si\", \"puede que\", "
                      "\"podría\")")
    question = (brief.question or "").strip()
    if question.count("?") != 1:
        errors.append("question must be exactly one question")
    if _INTEREST.search(question):
        errors.append("question asks for interest; ask about what they did")
    elif question and not _PAST.search(question):
        errors.append("question must be about past behaviour")
    if not (brief.why_now or "").strip():
        errors.append("why_now missing")
    if proof_case(brief.proof_case_id) is None:
        errors.append(f"proof_case_id {brief.proof_case_id!r} is not a citable case "
                      "(config/proof_cases.yaml)")
    return Verdict(errors=errors)


def claim_problems(text: str) -> list[str]:
    """Claims BoRo may not make, and cases it may not cite, found in this text."""
    problems = []
    for item in capabilities().get("blocked", []):
        for hit in _term_hits(text, item["terms"]):
            problems.append(f"claims {hit}: {item['reason']}")
    for item in proof_cases().get("not_citable", []):
        for hit in _term_hits(text, item["terms"]):
            problems.append(f"cites {hit}: no written permission ({item['id']})")
    return problems


def check_draft(subject: str, body: str, *, recipient_name: str | None = None,
                attachments: int = 0) -> Verdict:
    errors: list[str] = []
    warnings: list[str] = []
    words = _words(body)
    if words > MAX_WORDS:
        errors.append(f"body has {words} words; at most {MAX_WORDS}")
    if not _OPT_OUT.search(body):
        errors.append("body has no way to opt out")
    if attachments:
        errors.append("a first email carries no attachments")
    if _INTEREST.search(body):
        errors.append("body asks for interest instead of asking about what they did")
    errors += claim_problems(f"{subject}\n{body}")
    first = (recipient_name or "").split()[0] if (recipient_name or "").strip() else ""
    if first and first.lower() not in body.lower():
        warnings.append(f"body does not name {first}")
    if re.search(r"https?://", body):
        warnings.append("body contains a link")
    if body.count("?") > 2:
        warnings.append("body asks more than two questions")
    return Verdict(errors=errors, warnings=warnings)
