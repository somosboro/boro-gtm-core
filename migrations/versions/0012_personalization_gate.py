"""personalization gate: research briefs, drafts, scoreboard events (CE-ADR-022)

Replies came to emails that named a person, cited a checkable fact and asked a
specific question; generic info@/contacto@ sends bounced and got no answer. A
first email now needs an APPROVED research brief.

* `ce_research_briefs`: one per target and recipient; status NEEDS_RESEARCH →
  READY_TO_DRAFT → DRAFTED → APPROVED → SENT, or MANUAL_OUTREACH.
* `ce_draft_generations`: every model draft with its prompt, model and verdict.
* `ce_score_events`: the weekly scoreboard's countable steps.
* `ce_messages.brief_id`: which brief a personalized email came from.

Data, nothing deleted: every READY target gets a NEEDS_RESEARCH brief seeded
from its current contact (its state stays READY; the planner now needs an
APPROVED brief). toolstore.cl is suppressed as a domain on Amaru's instruction.

Downgrade drops the three tables and the column, and removes only the
toolstore.cl row this migration added.

Revision ID: 0012_personalization_gate
Revises: 0011_commercial_engine
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_personalization_gate"
down_revision = "0011_commercial_engine"
branch_labels = None
depends_on = None

BRIEF_STATUSES = ("NEEDS_RESEARCH", "READY_TO_DRAFT", "DRAFTED", "APPROVED", "SENT",
                  "MANUAL_OUTREACH")
SCORE_EVENTS = ("RESEARCHED", "DRAFTED", "APPROVED", "SENT", "BOUNCED", "REPLIED",
                "POSITIVE_REPLY", "CONVERSATION", "NAMED_NEED", "PROPOSAL",
                "MANUAL_SENT", "MANUAL_ACCEPTED", "MANUAL_REPLIED")
TOOLSTORE_EVIDENCE = ("0012: suppressed on Amaru's instruction (2026-11-13 personalization "
                      "brief); BoRo's last send there 2026-11-11 per Gmail history")


def _vocab(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "ce_research_briefs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("recipient_name", sa.Text(), nullable=True),
        sa.Column("recipient_role", sa.Text(), nullable=True),
        sa.Column("recipient_email", sa.String(length=320), nullable=True),
        sa.Column("email_source_url", sa.Text(), nullable=True),
        sa.Column("fact_text", sa.Text(), nullable=True),
        sa.Column("fact_source_url", sa.Text(), nullable=True),
        sa.Column("fact_date", sa.Date(), nullable=True),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("why_now", sa.Text(), nullable=True),
        sa.Column("proof_case_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("batch", sa.String(length=64), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_vocab("status", BRIEF_STATUSES),
                           name=op.f("ck_ce_research_briefs_status_vocabulary")),
        sa.ForeignKeyConstraint(["account_id"], ["ce_accounts.id"], ondelete="RESTRICT",
                                name=op.f("fk_ce_research_briefs_account_id_ce_accounts")),
        sa.ForeignKeyConstraint(["target_id"], ["ce_targets.id"], ondelete="RESTRICT",
                                name=op.f("fk_ce_research_briefs_target_id_ce_targets")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ce_research_briefs")),
        sa.UniqueConstraint("target_id", "recipient_email", name="uq_brief_target_recipient"),
    )
    op.create_index("ix_briefs_status", "ce_research_briefs", ["status"], unique=False)

    op.create_table(
        "ce_draft_generations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("brief_id", sa.UUID(), nullable=False),
        sa.Column("brief_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("validator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["brief_id"], ["ce_research_briefs.id"], ondelete="RESTRICT",
                                name=op.f("fk_ce_draft_generations_brief_id_ce_research_briefs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ce_draft_generations")),
    )
    op.create_index(op.f("ix_ce_draft_generations_brief_id"), "ce_draft_generations",
                    ["brief_id"], unique=False)

    op.create_table(
        "ce_score_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("brief_id", sa.UUID(), nullable=True),
        sa.Column("target_id", sa.UUID(), nullable=True),
        sa.Column("batch", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_vocab("kind", SCORE_EVENTS),
                           name=op.f("ck_ce_score_events_kind_vocabulary")),
        sa.ForeignKeyConstraint(["brief_id"], ["ce_research_briefs.id"], ondelete="RESTRICT",
                                name=op.f("fk_ce_score_events_brief_id_ce_research_briefs")),
        sa.ForeignKeyConstraint(["target_id"], ["ce_targets.id"], ondelete="RESTRICT",
                                name=op.f("fk_ce_score_events_target_id_ce_targets")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ce_score_events")),
    )
    op.create_index("ix_score_events_kind_at", "ce_score_events", ["kind", "at"], unique=False)

    op.add_column("ce_messages", sa.Column("brief_id", sa.UUID(), nullable=True))
    op.create_foreign_key(op.f("fk_ce_messages_brief_id_ce_research_briefs"), "ce_messages",
                          "ce_research_briefs", ["brief_id"], ["id"], ondelete="RESTRICT")

    # Every READY target needs research before it can be written to.
    op.execute("""
        INSERT INTO ce_research_briefs
            (id, target_id, account_id, recipient_name, recipient_role, recipient_email,
             email_source_url, status, batch, reasons, warnings, created_at, updated_at)
        SELECT gen_random_uuid(), t.id, t.account_id, c.full_name, c.role, c.email,
               c.source_url, 'NEEDS_RESEARCH', NULL,
               '["migrated from READY by 0012: needs research"]'::jsonb, '[]'::jsonb,
               now(), now()
        FROM ce_targets t LEFT JOIN ce_contacts c ON c.id = t.contact_id
        WHERE t.state = 'READY'
        ON CONFLICT ON CONSTRAINT uq_brief_target_recipient DO NOTHING
    """)
    op.execute(sa.text("""
        INSERT INTO ce_suppressions (kind, value, reason, evidence_ref, created_at)
        VALUES ('DOMAIN', 'toolstore.cl', 'MANUAL', :evidence, now())
        ON CONFLICT ON CONSTRAINT uq_suppression DO NOTHING
    """).bindparams(evidence=TOOLSTORE_EVIDENCE))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM ce_suppressions WHERE kind = 'DOMAIN' AND value = 'toolstore.cl' "
        "AND evidence_ref = :evidence").bindparams(evidence=TOOLSTORE_EVIDENCE))
    op.drop_constraint(op.f("fk_ce_messages_brief_id_ce_research_briefs"), "ce_messages",
                       type_="foreignkey")
    op.drop_column("ce_messages", "brief_id")
    op.drop_index("ix_score_events_kind_at", table_name="ce_score_events")
    op.drop_table("ce_score_events")
    op.drop_index(op.f("ix_ce_draft_generations_brief_id"), table_name="ce_draft_generations")
    op.drop_table("ce_draft_generations")
    op.drop_index("ix_briefs_status", table_name="ce_research_briefs")
    op.drop_table("ce_research_briefs")
