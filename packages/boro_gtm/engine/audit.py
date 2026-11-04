"""The READY quality audit (cutover requires one; cutover.AUDIT_THRESHOLDS).

`sample` draws READY targets across every lane and source and lays out what a
reviewer must judge, with the mechanical checks already answered. The reviewer
records the verdicts with `record`; the cutover report reads them.
"""

from __future__ import annotations

import random
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from boro_gtm.engine import controls
from boro_gtm.engine import states as S
from boro_gtm.engine.models import CeAccount, CeContact, CeSignal, CeTarget
from boro_gtm.engine.outreach import render
from boro_gtm.engine.pipeline import (
    email_domain,
    is_role_mailbox,
    now_utc,
    prior_contact,
    same_company_domain,
    suppression_for,
)
from boro_gtm.engine.strategy import Strategy

METRICS = ("account_identity", "domain", "signal", "lane", "source", "score_defensible",
           "contact_belongs", "channel_appropriate", "no_support_mailbox", "no_gmail_duplicate",
           "no_suppression", "message_reflects_evidence", "no_invented_pain")


def sample(session: Session, strategy: Strategy, n: int = 30, seed: int = 11) -> list[dict]:
    rows = list(session.execute(select(CeTarget, CeAccount, CeContact, CeSignal).join(
        CeAccount, CeAccount.id == CeTarget.account_id).join(
        CeContact, CeContact.id == CeTarget.contact_id).outerjoin(
        CeSignal, CeSignal.id == CeTarget.signal_id).where(CeTarget.state == S.READY)))
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        groups.setdefault((row[0].lane, row[3].source_key if row[3] else "-"), []).append(row)
    rng = random.Random(seed)
    for members in groups.values():
        rng.shuffle(members)
    picked: list = []
    while len(picked) < n and any(groups.values()):
        for key in sorted(groups):
            if groups[key] and len(picked) < n:
                picked.append(groups[key].pop())
    out = []
    for target, account, contact, signal in picked:
        message = render(strategy, target=target, account=account, contact=contact,
                         signal=signal, step="INITIAL")
        out.append({
            "target_id": str(target.id), "lane": target.lane,
            "source": signal.source_key if signal else None,
            "company": account.name, "domain": account.domain, "country": target.country,
            "signal": {"kind": signal.kind if signal else None,
                       "url": signal.url if signal else None,
                       "title": signal.title if signal else None,
                       "posted_at": signal.posted_at if signal else None,
                       "expires_at": target.expires_at},
            "score": target.score, "score_components": target.score_components,
            "contact": {"email": contact.email, "source_url": contact.source_url,
                        "is_generic": contact.is_generic, "role": contact.role},
            "mechanical": {
                "contact_on_company_domain": same_company_domain(contact.email, account.domain),
                "role_mailbox": is_role_mailbox(contact.email, contact_role=contact.role),
                "gmail_prior_contact": prior_contact(
                    session, contact.email, email_domain(contact.email) or "", account.domain),
                "suppressed": suppression_for(session, contact.email) is not None},
            "message": {"subject": message["subject"], "body": message["body"]},
        })
    return out


def record(session: Session, verdicts: dict[str, Any]) -> dict[str, Any]:
    """`verdicts`: {"sample": N, "metrics": {metric: passes}, "defects": [...], "by": "..."}."""
    sample_size = int(verdicts["sample"])
    metrics = {k: int(verdicts["metrics"][k]) for k in METRICS}
    if any(v > sample_size or v < 0 for v in metrics.values()):
        raise ValueError("a metric counts more passes than the sample has targets")
    value = {"at": now_utc().isoformat(), "sample": sample_size, "metrics": metrics,
             "defects": verdicts.get("defects", []), "by": verdicts.get("by")}
    controls.put(session, controls.READY_AUDIT_KEY, value)
    return value
