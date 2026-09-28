"""m3 remediation: durable review, terminal uniqueness, and an honest prune

Three corrections an independent audit proved against the running branch.

**1. The one-way prune could launder any other mutation.**
`gtm_m3_one_way_prune` checked only that the payload went to NULL and the state
went RETAINED → PRUNED, then returned NEW. Everything else in the same UPDATE
went through. Demonstrated on the branch:

    UPDATE research_artifact_bodies
       SET raw_body = NULL, body_retention = 'PRUNED', pruned_at = now(),
           raw_body_sha256 = 'deadbeef'                 -- laundered

That is the terminal node of every provenance walk being rewritten under cover
of a legal retention transition. The function now proves every other column is
unchanged, by comparing the whole row minus the three columns the transition is
allowed to touch.

**2. Terminal siblings could both land.**
`ACTIONED`/`DISMISSED` and `RESOLVED`/`ABANDONED` are alternatives, not a
progression, so they share a lifecycle rank and the transition triggers let two
concurrent transactions insert one each. Reproduced on the branch: a gap ended
with both RESOLVED and ABANDONED. A partial unique index makes a second
terminal event unrepresentable rather than merely unlikely.

**3. Review had nowhere to live.**
`POST /research-claims/{id}/review` cannot express the workflow it claimed: a
sampled reading awaiting confirmation has no claim yet, and a rejection
persisted nothing at all — no actor, no time, no rationale, no record that a
human looked. `research_evidence_reviews` is the smallest append-only record
that fixes both, keyed on the evidence item, which exists from the moment the
sampled reading is recorded.

`0004`–`0006` are left byte-identical.

Revision ID: 0007_m3_review
Revises: 0006_m3_tiebreak
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0007_m3_review'
down_revision = '0006_m3_tiebreak'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. A prune may change three columns and nothing else.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION gtm_m3_one_way_prune() RETURNS trigger AS $$
        DECLARE
            payload_col text := TG_ARGV[0];
            state_col   text := TG_ARGV[1];
            stamp_col   text := TG_ARGV[2];
            old_payload_null boolean;
            new_payload_null boolean;
            old_state   text;
            new_state   text;
            old_stamp_null boolean;
            new_stamp_null boolean;
            rest_old    jsonb;
            rest_new    jsonb;
        BEGIN
            EXECUTE format('SELECT ($1).%I IS NULL', payload_col)
                INTO old_payload_null USING OLD;
            EXECUTE format('SELECT ($1).%I IS NULL', payload_col)
                INTO new_payload_null USING NEW;
            EXECUTE format('SELECT ($1).%I', state_col) INTO old_state USING OLD;
            EXECUTE format('SELECT ($1).%I', state_col) INTO new_state USING NEW;
            EXECUTE format('SELECT ($1).%I IS NULL', stamp_col)
                INTO old_stamp_null USING OLD;
            EXECUTE format('SELECT ($1).%I IS NULL', stamp_col)
                INTO new_stamp_null USING NEW;

            IF NOT (old_payload_null = false AND new_payload_null = true
                    AND old_state = 'RETAINED' AND new_state = 'PRUNED'
                    AND old_stamp_null = true AND new_stamp_null = false) THEN
                RAISE EXCEPTION
                    'append-only: % permits only a one-way prune of %',
                    TG_TABLE_NAME, payload_col;
            END IF;

            -- Everything the transition is not allowed to touch must be
            -- identical. Without this the legal prune was a vehicle for any
            -- other mutation in the same UPDATE.
            rest_old := (to_jsonb(OLD) - payload_col - state_col - stamp_col);
            rest_new := (to_jsonb(NEW) - payload_col - state_col - stamp_col);
            IF rest_old IS DISTINCT FROM rest_new THEN
                RAISE EXCEPTION
                    'append-only: a prune of %.% may not change any other column',
                    TG_TABLE_NAME, payload_col;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    for table, payload, state, stamp in (
        ('research_artifact_bodies', 'raw_body', 'body_retention', 'pruned_at'),
        ('research_text_derivations', 'extracted_text', 'text_retention', 'pruned_at'),
        ('research_extractions', 'raw_output', 'raw_output_retention',
         'raw_output_pruned_at'),
    ):
        op.execute(f"DROP TRIGGER {table}_one_way_prune ON {table}")
        op.execute(f"""
            CREATE TRIGGER {table}_one_way_prune
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION
                gtm_m3_one_way_prune('{payload}', '{state}', '{stamp}');
        """)

    # ------------------------------------------------------------------
    # 2. At most one terminal event per lifecycle.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE UNIQUE INDEX uq_gap_terminal_event
        ON operational_research_gap_events (gap_id)
        WHERE event_kind IN ('RESOLVED','ABANDONED');
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_signal_terminal_event
        ON identity_review_signal_events (occurrence_id)
        WHERE status IN ('ACTIONED','DISMISSED');
    """)

    # ------------------------------------------------------------------
    # 3. Durable human review, keyed on something that exists beforehand.
    # ------------------------------------------------------------------
    op.create_table(
        'research_evidence_reviews',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('evidence_item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('decision', sa.String(length=16), nullable=False),
        sa.Column('actor', sa.String(length=128), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('human_extraction_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('resulting_claim_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('CONFIRM','REJECT')",
                           name=op.f('ck_research_evidence_reviews_decision_vocabulary')),
        # A rejection asserts nothing, so it may not carry a claim; a
        # confirmation that produced one names it.
        sa.CheckConstraint(
            "decision = 'CONFIRM' OR "
            "(human_extraction_id IS NULL AND resulting_claim_id IS NULL)",
            name=op.f('ck_research_evidence_reviews_rejection_asserts_nothing'),
        ),
        sa.ForeignKeyConstraint(['evidence_item_id'], ['research_evidence_items.id'],
                                name=op.f('fk_review_evidence_item'),
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['human_extraction_id'], ['research_extractions.id'],
                                name=op.f('fk_review_human_extraction'),
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['resulting_claim_id'], ['company_claims.id'],
                                name=op.f('fk_review_resulting_claim'),
                                ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_research_evidence_reviews')),
        sa.UniqueConstraint('evidence_item_id', 'actor', 'reviewed_at',
                            name=op.f('uq_review_identity')),
    )
    op.create_index('ix_reviews_evidence_item', 'research_evidence_reviews',
                    ['evidence_item_id'])
    op.create_index('ix_reviews_decision', 'research_evidence_reviews', ['decision'])
    op.execute("""
        CREATE TRIGGER research_evidence_reviews_append_only
        BEFORE UPDATE OR DELETE ON research_evidence_reviews
        FOR EACH ROW EXECUTE FUNCTION gtm_reject_update();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER research_evidence_reviews_append_only "
               "ON research_evidence_reviews")
    op.drop_index('ix_reviews_decision', table_name='research_evidence_reviews')
    op.drop_index('ix_reviews_evidence_item', table_name='research_evidence_reviews')
    op.drop_table('research_evidence_reviews')
    op.execute("DROP INDEX uq_signal_terminal_event")
    op.execute("DROP INDEX uq_gap_terminal_event")
    op.execute("""
        CREATE OR REPLACE FUNCTION gtm_m3_one_way_prune() RETURNS trigger AS $$
        DECLARE
            payload_col text := TG_ARGV[0];
            state_col   text := TG_ARGV[1];
            old_payload text;
            new_payload text;
            old_state   text;
            new_state   text;
        BEGIN
            EXECUTE format('SELECT ($1).%I IS NULL', payload_col)
                INTO old_payload USING OLD;
            EXECUTE format('SELECT ($1).%I IS NULL', payload_col)
                INTO new_payload USING NEW;
            EXECUTE format('SELECT ($1).%I', state_col) INTO old_state USING OLD;
            EXECUTE format('SELECT ($1).%I', state_col) INTO new_state USING NEW;
            IF old_payload = 'false' AND new_payload = 'true'
               AND old_state = 'RETAINED' AND new_state = 'PRUNED' THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'append-only: % permits only a one-way prune of %',
                TG_TABLE_NAME, payload_col;
        END;
        $$ LANGUAGE plpgsql;
    """)
    for table, payload, state in (
        ('research_artifact_bodies', 'raw_body', 'body_retention'),
        ('research_text_derivations', 'extracted_text', 'text_retention'),
        ('research_extractions', 'raw_output', 'raw_output_retention'),
    ):
        op.execute(f"DROP TRIGGER {table}_one_way_prune ON {table}")
        op.execute(f"""
            CREATE TRIGGER {table}_one_way_prune
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION
                gtm_m3_one_way_prune('{payload}', '{state}');
        """)
