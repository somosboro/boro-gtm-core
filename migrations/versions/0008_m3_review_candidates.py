"""m3: durable review candidates, and a CONFIRM that must carry provenance

A second independent audit found that "reviewable" meant "the caller knows an
evidence UUID", and that a low-confidence observation left no durable trace at
all — `low_confidence_deferred` was transient state on a `PipelineResult`, so
after the process ended nothing connected the `INSUFFICIENT_EVIDENCE` gap to
the evidence a reviewer was supposed to look at. D3 was passing without being
implemented.

`research_review_candidates` is that missing link: one row per observation
awaiting a human, carrying why it is waiting, which question raised it and
which attribute it concerns. It is the queue, and it is what makes an evidence
item reviewable — rather than the caller's knowledge of an id.

The `CONFIRM` half of the review CHECK is also closed here. `0007` constrained
only rejections; a raw writer could record a confirmation with no human
extraction behind it, which is a confirmation that nobody made.

`0001`–`0007` are published on this branch and left byte-identical.

Revision ID: 0008_m3_candidates
Revises: 0007_m3_review
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0008_m3_candidates'
down_revision = '0007_m3_review'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'research_review_candidates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('evidence_item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attempt_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('company_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attribute_key', sa.String(length=128), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column('extractor_confidence', sa.Numeric(6, 4), nullable=True),
        sa.Column('raised_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reason IN ('SAMPLED_REQUIRES_CONFIRMATION','LOW_CONFIDENCE_REQUIRES_REVIEW')",
            name=op.f('ck_research_review_candidates_reason_vocabulary'),
        ),
        sa.ForeignKeyConstraint(['evidence_item_id'], ['research_evidence_items.id'],
                                name=op.f('fk_candidate_evidence_item'),
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['run_id'], ['operational_research_runs.id'],
                                name=op.f('fk_candidate_run'), ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['attempt_id'], ['operational_research_attempts.id'],
                                name=op.f('fk_candidate_attempt'), ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'],
                                name=op.f('fk_candidate_company'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_research_review_candidates')),
        # One candidate per observation per question: raising the same question
        # again must not grow the queue.
        sa.UniqueConstraint('evidence_item_id', 'run_id',
                            name=op.f('uq_candidate_identity')),
    )
    # Names prefixed `ix_review_candidates_*`. The first draft used
    # `ix_candidates_company`, which PostgreSQL rejected: index names are
    # schema-global and M2's `entity_resolution_candidates` already owns it.
    op.create_index('ix_review_candidates_company', 'research_review_candidates',
                    ['company_id'])
    op.create_index('ix_review_candidates_reason', 'research_review_candidates',
                    ['reason'])
    op.create_index('ix_review_candidates_attribute', 'research_review_candidates',
                    ['run_id', 'attribute_key'])
    op.execute("""
        CREATE TRIGGER research_review_candidates_append_only
        BEFORE UPDATE OR DELETE ON research_review_candidates
        FOR EACH ROW EXECUTE FUNCTION gtm_reject_update();
    """)

    # A confirmation with no human extraction is a confirmation nobody made.
    op.create_check_constraint(
        op.f('ck_research_evidence_reviews_confirmation_has_provenance'),
        'research_evidence_reviews',
        "decision <> 'CONFIRM' OR human_extraction_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('ck_research_evidence_reviews_confirmation_has_provenance'),
        'research_evidence_reviews', type_='check')
    op.execute("DROP TRIGGER research_review_candidates_append_only "
               "ON research_review_candidates")
    op.drop_index('ix_review_candidates_attribute',
                  table_name='research_review_candidates')
    op.drop_index('ix_review_candidates_reason',
                  table_name='research_review_candidates')
    op.drop_index('ix_review_candidates_company',
                  table_name='research_review_candidates')
    op.drop_table('research_review_candidates')
