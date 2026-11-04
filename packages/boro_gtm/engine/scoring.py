"""The 100-point commercial qualification score (CE-ADR-005).

Practical, not an ICP study: it asks whether there is something observable to
respond to, how soon, whether BoRo can do it, whether the buyer hires vendors,
whether BoRo can reach them, and whether money is visible. Every component
keeps the evidence it was awarded for, so a score can be argued with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from boro_gtm.engine.strategy import Strategy

SIGNAL_BASE = {
    "EXPLICIT_DEMAND": 22,
    "PARTNER_LISTING": 17,
    "TRIGGER": 15,
    "HISTORIC_TRIGGER": 8,
    "IMPORT": 12,
}


@dataclass
class Score:
    total: int
    components: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {"total": self.total, "components": self.components}


def _hits(text: str, terms: list[str]) -> list[str]:
    """Whole-word matches only: "app" must not match "whatsapp" or "apply"."""
    lowered = text.lower()
    return sorted({t for t in terms
                   if re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", lowered)})


#: What a job-board title must contain to be software delivery BoRo can do.
DELIVERY_TITLE = re.compile(
    r"(desarroll|developer|\bdev\b|programador|ingenier[oa] de software|software engineer"
    r"|backend|back-end|frontend|front-end|full.?stack|odoo|\berp\b|integraci|\bapi\b"
    r"|arquitect[oa] de software|devops|mobile|react|node|python|nestjs|next\.?js"
    r"|implementaci[oó]n|consultor[a]? (t[eé]cnic|funcional|erp|odoo|sap))", re.I)


def score_target(strategy: Strategy, *, kind: str, title: str | None, excerpt: str | None,
                 posted_at: datetime | None, expires_at: datetime | None,
                 has_verified_channel: bool, channel_is_person: bool,
                 vendor_evidence: str | None = None,
                 now: datetime | None = None) -> Score:
    w = strategy.raw["qualification"]["weights"]
    text = f"{title or ''} {excerpt or ''}"
    moment = now or datetime.now(UTC)
    c: dict[str, dict[str, Any]] = {}

    base = SIGNAL_BASE.get(kind, 10)
    signal_terms = _hits(text, strategy.terms("vendor_terms"))
    c["signal"] = {"points": min(w["signal"], base + min(len(signal_terms), 3)),
                   "kind": kind, "evidence": signal_terms[:5]}

    urgency = 6
    reasons: list[str] = []
    if posted_at is not None:
        age = moment - posted_at
        if age <= timedelta(days=7):
            urgency, reasons = 16, ["posted_within_7_days"]
        elif age <= timedelta(days=21):
            urgency, reasons = 11, ["posted_within_21_days"]
        else:
            urgency, reasons = 4, ["older_than_21_days"]
    if expires_at is not None and expires_at - moment <= timedelta(days=14):
        urgency += 3
        reasons.append("deadline_within_14_days")
    urgent_terms = _hits(text, strategy.terms("urgency_terms"))
    if urgent_terms:
        urgency += 3
        reasons.extend(urgent_terms[:3])
    if kind == "PARTNER_LISTING":
        # A directory listing is standing capacity demand, not a dated request.
        urgency, reasons = max(urgency, 10), reasons + ["standing_partner_demand"]
    c["urgency"] = {"points": min(w["urgency"], urgency), "evidence": reasons}

    fit_terms = _hits(text, strategy.terms("technical_fit_terms"))
    fit = min(w["technical_fit"], 6 + 3 * len(fit_terms)) if fit_terms else 4
    if kind == "EXPLICIT_DEMAND" and not DELIVERY_TITLE.search(title or ""):
        # A posting for a growth marketer that mentions "app" is not a
        # software request, however many keywords the description carries.
        fit = min(fit, 4)
        fit_terms = ["title_not_software_delivery", *fit_terms]
    if kind == "PARTNER_LISTING" and "odoo" in text.lower():
        fit = w["technical_fit"]
    c["technical_fit"] = {"points": fit, "evidence": fit_terms[:6]}

    vendor = 6
    if kind in ("EXPLICIT_DEMAND", "PARTNER_LISTING"):
        vendor = 12
    if vendor_evidence:
        # The company says it delivers software to clients: it buys capacity.
        vendor = max(vendor, 12)
    if signal_terms:
        vendor += 3
    c["vendor_suitability"] = {"points": min(w["vendor_suitability"], vendor),
                               "evidence": signal_terms[:5] + (
                                   [f"delivery_firm:{vendor_evidence}"] if vendor_evidence
                                   else [])}

    access = 0
    if has_verified_channel:
        access = w["channel_access"] if channel_is_person else 7
    c["channel_access"] = {"points": access,
                           "evidence": ["named_role_channel" if channel_is_person else
                                        "company_channel" if has_verified_channel else
                                        "no_verified_channel"]}

    money = _hits(text, strategy.terms("economic_terms"))
    economic = 7 if money else (5 if kind == "PARTNER_LISTING" else 3)
    c["economic"] = {"points": min(w["economic"], economic), "evidence": money[:4]}

    total = sum(part["points"] for part in c.values())
    return Score(total=max(0, min(100, total)), components=c)
