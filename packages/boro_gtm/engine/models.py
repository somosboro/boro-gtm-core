"""Commercial engine tables (CE-ADR-001).

PostgreSQL is the single source of commercial truth. Every row that decides
whether BoRo may write to somebody — a target's state, a suppression, a message
already sent — lives here, and every state change is an append-only event.

Accounts are keyed by the same registrable-domain rules M2 uses
(`normalize_domain`, `domain_role`), so "the same company twice" means the same
thing in discovery and in outreach. An account may point at an M2 company, but
does not have to: a job post is not a provider record, and forcing it through
M2's provider-version chain would add provenance machinery without adding
truth (CE-ADR-002).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from boro_gtm.core.db import Base, new_uuid, utcnow
from boro_gtm.engine.states import (
    LANES,
    MESSAGE_STATUSES,
    OPEN_STATES,
    STATES,
    STEPS,
    SUPPRESSION_KINDS,
    SUPPRESSION_REASONS,
)


def _vocab(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CeAccount(Base):
    """One company BoRo might sell to, by registrable domain."""

    __tablename__ = "ce_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2))
    m2_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 nullable=False)


class CeSignal(Base):
    """A public observation that justifies a commercial target.

    One row per source item, ever: re-reading a page that still lists the same
    job post is the same signal (`UNIQUE (source_key, external_id)`).
    """

    __tablename__ = "ce_signals"
    __table_args__ = (
        UniqueConstraint("source_key", "external_id", name="uq_signal_source_item"),
        CheckConstraint(_vocab("lane", LANES), name="lane_vocabulary"),
        CheckConstraint(
            "kind IN ('EXPLICIT_DEMAND','PARTNER_LISTING','TRIGGER','HISTORIC_TRIGGER','IMPORT')",
            name="kind_vocabulary"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    lane: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    company_domain: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ce_accounts.id", ondelete="RESTRICT"))
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey("ce_source_runs.id"))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class CeSourceRun(Base):
    """What one pass over one source did, including what it could not find."""

    __tablename__ = "ce_source_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','COMPLETED','EXHAUSTED','BLOCKED','FAILED')",
                        name="status_vocabulary"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    lane: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queries: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results_inspected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expired: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cursor_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cursor_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CeSourceCursor(Base):
    """Where a source left off, so the next run continues instead of repeating."""

    __tablename__ = "ce_source_cursors"

    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeContact(Base):
    """A publicly published channel for a person or role. Never guessed."""

    __tablename__ = "ce_contacts"
    __table_args__ = (UniqueConstraint("email", name="uq_contact_email"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ce_accounts.id", ondelete="RESTRICT"), nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    is_generic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Where the address was published. Required: an address without a public
    #: source is a guess, and guesses are never sent to.
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeTarget(Base):
    """One commercial attempt at one account, through one lane."""

    __tablename__ = "ce_targets"
    __table_args__ = (
        CheckConstraint(_vocab("state", STATES), name="state_vocabulary"),
        CheckConstraint(_vocab("lane", LANES), name="lane_vocabulary"),
        CheckConstraint("score IS NULL OR (score BETWEEN 0 AND 100)", name="score_range"),
        # One open pursuit per company. A second signal about an account that is
        # already being worked attaches to it; it does not start a parallel one.
        Index("uq_target_open_per_account", "account_id", unique=True,
              postgresql_where=text("state IN (" + ", ".join(repr(s) for s in OPEN_STATES) + ")")),
        Index("ix_targets_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ce_accounts.id", ondelete="RESTRICT"), nullable=False)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ce_signals.id"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ce_contacts.id"))
    lane: Mapped[str] = mapped_column(String(32), nullable=False)
    segment: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(2))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    score_components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    priority: Mapped[str | None] = mapped_column(String(8))
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_from_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ce_targets.id"))
    state_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeTargetEvent(Base):
    """Append-only history of every state transition."""

    __tablename__ = "ce_target_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ce_targets.id", ondelete="RESTRICT"), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeSuppression(Base):
    """An address or domain BoRo must not write to again."""

    __tablename__ = "ce_suppressions"
    __table_args__ = (
        UniqueConstraint("kind", "value", name="uq_suppression"),
        CheckConstraint(_vocab("kind", SUPPRESSION_KINDS), name="kind_vocabulary"),
        CheckConstraint(_vocab("reason", SUPPRESSION_REASONS), name="reason_vocabulary"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeMessage(Base):
    """One planned or sent email in a sequence.

    `UNIQUE (target_id, step)` is what makes a duplicate send unrepresentable:
    a target has one INITIAL, one F1 and one F2, whatever a worker does.
    `rfc822_message_id` is fixed before sending, so a worker that died
    mid-send can ask Gmail whether the message went out (CE-ADR-004).
    """

    __tablename__ = "ce_messages"
    __table_args__ = (
        UniqueConstraint("target_id", "step", name="uq_message_step"),
        UniqueConstraint("rfc822_message_id", name="uq_message_rfc822"),
        CheckConstraint(_vocab("step", STEPS), name="step_vocabulary"),
        CheckConstraint(_vocab("status", MESSAGE_STATUSES), name="status_vocabulary"),
        Index("ix_messages_due", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ce_targets.id", ondelete="RESTRICT"), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ce_contacts.id"), nullable=False)
    step: Mapped[str] = mapped_column(String(8), nullable=False)
    lane: Mapped[str] = mapped_column(String(32), nullable=False)
    segment: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(2))
    language: Mapped[str] = mapped_column(String(2), nullable=False)
    variant: Mapped[str] = mapped_column(String(64), nullable=False)
    offer: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rfc822_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: When a worker committed SENDING. A row stuck SENDING is reconciled only
    #: once this is old enough that no live worker can still be mid-call.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeMailEvent(Base):
    """One Gmail message, ingested once (`UNIQUE gmail_message_id`)."""

    __tablename__ = "ce_mail_events"
    __table_args__ = (
        UniqueConstraint("gmail_message_id", name="uq_mail_event_gmail_id"),
        CheckConstraint("direction IN ('IN','OUT')", name="direction_vocabulary"),
        Index("ix_mail_events_thread", "gmail_thread_id"),
        Index("ix_mail_events_from", "from_email"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rfc822_message_id: Mapped[str | None] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    from_email: Mapped[str | None] = mapped_column(String(320))
    to_emails: Mapped[list[str]] = mapped_column(ARRAY(String(320)), nullable=False, default=list)
    subject: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classification: Mapped[str | None] = mapped_column(String(32))
    classification_source: Mapped[str | None] = mapped_column(String(16))
    escalate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False,
                                                          default=list)
    target_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ce_targets.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ce_messages.id"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeDeal(Base):
    """What a target is worth once it is an opportunity. Revenue lives here."""

    __tablename__ = "ce_deals"

    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ce_targets.id", ondelete="RESTRICT"), primary_key=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    proposal_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeProblemSignal(Base):
    """A problem a company described, kept so repeated pains become queryable."""

    __tablename__ = "ce_problem_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    problem_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ce_accounts.id"), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ce_targets.id"))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    willingness_to_pay: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeControl(Base):
    """Operational switches the engine reads and may set itself (e.g. a pause)."""

    __tablename__ = "ce_controls"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CeWorkerHeartbeat(Base):
    __tablename__ = "ce_worker_heartbeats"

    worker: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_task: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class CeTaskRun(Base):
    """Every scheduled task execution, with its error if it failed."""

    __tablename__ = "ce_task_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','OK','FAILED','SKIPPED')", name="status_vocabulary"),
        Index("ix_task_runs_task", "task", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    worker: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
