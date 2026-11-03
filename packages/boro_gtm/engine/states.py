"""The commercial state machine (CE-ADR-003). Pure: no database, no clock."""

from __future__ import annotations

from boro_gtm.core.errors import GtmError

LANES = ("PARTNER_DELIVERY", "EXPLICIT_DEMAND", "TRIGGER_OVERFLOW")

DISCOVERED = "DISCOVERED"
ENRICH = "ENRICH"
READY = "READY"
COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
CONTACTED = "CONTACTED"
F1 = "F1"
F2 = "F2"
REPLIED = "REPLIED"
CONVERSATION = "CONVERSATION"
OPPORTUNITY = "OPPORTUNITY"
PROPOSAL = "PROPOSAL"
WON = "WON"
LOST = "LOST"
EXPIRED = "EXPIRED"
BLOCKED = "BLOCKED"
SUPPRESSED = "SUPPRESSED"
CLOSED_NO_REPLY = "CLOSED_NO_REPLY"

STATES = (DISCOVERED, ENRICH, READY, COMPLIANCE_REVIEW, CONTACTED, F1, F2, REPLIED,
          CONVERSATION, OPPORTUNITY, PROPOSAL, WON, LOST, EXPIRED, BLOCKED, SUPPRESSED,
          CLOSED_NO_REPLY)
TERMINAL_STATES = (WON, LOST, EXPIRED, BLOCKED, SUPPRESSED, CLOSED_NO_REPLY)
OPEN_STATES = tuple(s for s in STATES if s not in TERMINAL_STATES)
#: States in which a sequence is running and a reply or bounce can arrive.
IN_SEQUENCE = (CONTACTED, F1, F2)
#: A terminal outcome after which a *new material signal* may open a new target.
REOPENABLE = (LOST, EXPIRED, CLOSED_NO_REPLY)

TRANSITIONS: dict[str, frozenset[str]] = {
    DISCOVERED: frozenset({ENRICH, BLOCKED, EXPIRED, SUPPRESSED}),
    ENRICH: frozenset({READY, COMPLIANCE_REVIEW, BLOCKED, EXPIRED, SUPPRESSED}),
    COMPLIANCE_REVIEW: frozenset({READY, BLOCKED, EXPIRED, SUPPRESSED}),
    READY: frozenset({CONTACTED, BLOCKED, EXPIRED, SUPPRESSED}),
    CONTACTED: frozenset({F1, REPLIED, EXPIRED, SUPPRESSED, LOST}),
    F1: frozenset({F2, REPLIED, EXPIRED, SUPPRESSED, LOST}),
    F2: frozenset({REPLIED, CLOSED_NO_REPLY, EXPIRED, SUPPRESSED, LOST}),
    REPLIED: frozenset({CONVERSATION, LOST, SUPPRESSED}),
    CONVERSATION: frozenset({OPPORTUNITY, LOST}),
    OPPORTUNITY: frozenset({PROPOSAL, LOST}),
    PROPOSAL: frozenset({WON, LOST}),
    WON: frozenset(),
    LOST: frozenset(),
    EXPIRED: frozenset(),
    BLOCKED: frozenset(),
    SUPPRESSED: frozenset(),
    CLOSED_NO_REPLY: frozenset(),
}

STEPS = ("INITIAL", "F1", "F2")
#: SCHEDULED → SENDING → SENT is the live path. SHADOW is a message the engine
#: rendered and would have sent with SHADOW_MODE off; it never left the box.
MESSAGE_STATUSES = ("SCHEDULED", "SENDING", "SENT", "SHADOW", "CANCELLED", "FAILED", "BLOCKED")

SUPPRESSION_KINDS = ("EMAIL", "DOMAIN")
SUPPRESSION_REASONS = ("BOUNCE", "OPT_OUT", "REJECTION", "MANUAL", "COMPLIANCE")


class IllegalTransitionError(GtmError):
    code = "ILLEGAL_TRANSITION"
    http_status = 409


def check_transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransitionError(
            f"{current} → {target} is not a commercial transition",
            details={"from": current, "to": target},
        )
