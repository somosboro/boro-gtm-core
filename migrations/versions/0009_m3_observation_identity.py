"""m3: a review candidate is one observation, and a review names the candidate

A third independent audit found the review *target* was defined correctly — one
attribute at one span — while its persisted *identity* was still one evidence
item per run. Those are not the same thing, and the difference loses work.

Reproduced on this branch: the workforce extractor emits two observations at one
span,

    "58 field technicians"  ->  technician_count = 58
                            ->  field_workforce_present = true

which converge on one evidence item, because evidence identity is keyed on the
locator. `UNIQUE (evidence_item_id, run_id)` then admitted only the first, so
`field_workforce_present` silently vanished from the review queue —
`ON CONFLICT DO NOTHING` swallowed it. The same happened to
`fleet_size`/`fleet_presence`.

A review row keyed on the evidence item was ambiguous for the same reason: a
rejection could not say *which* observation the reviewer disbelieved.

This migration:

* gives a candidate an `observation_fingerprint` and keys it
  `UNIQUE (run_id, observation_fingerprint)`, so one candidate is one exact
  machine observation awaiting one human decision;
* moves `research_evidence_reviews` onto `review_candidate_id`, so the durable
  record names the observation reviewed and the evidence is derived through it
  — one source of truth rather than two that can disagree (M3-ADR-064).

`0001`–`0008` are published on this branch and left byte-identical.

Revision ID: 0009_m3_observation
Revises: 0008_m3_candidates
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0009_m3_observation'
down_revision = '0008_m3_candidates'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Candidates: one row per observation.
    # ------------------------------------------------------------------
    op.add_column(
        'research_review_candidates',
        sa.Column('observation_fingerprint', sa.String(length=64), nullable=True),
    )
    # The append-only trigger refuses UPDATE, correctly — a backfill is a schema
    # operation, not application code, so the migration disables it explicitly
    # for the duration and puts it back. Doing this visibly is the point: the
    # trigger is not something a service may switch off.
    op.execute("ALTER TABLE research_review_candidates "
               "DISABLE TRIGGER research_review_candidates_append_only")
    # Backfill deterministically from what SQL can see. It is prefixed `legacy:`
    # because it is *not* the Python fingerprint — that is computed from the
    # canonical observation, which SQL cannot reconstruct. A legacy row therefore
    # stays in the queue under its old identity until it is decided, and a later
    # attempt raises the correctly-fingerprinted candidate beside it. Nothing is
    # lost, and the two are visibly distinguishable.
    op.execute("""
        UPDATE research_review_candidates
           SET observation_fingerprint =
               'legacy:' || substr(
                   encode(sha256((evidence_item_id::text || ':' ||
                                  attribute_key)::bytea), 'hex'), 1, 56)
         WHERE observation_fingerprint IS NULL
    """)
    op.execute("ALTER TABLE research_review_candidates "
               "ENABLE TRIGGER research_review_candidates_append_only")
    op.alter_column('research_review_candidates', 'observation_fingerprint',
                    nullable=False)
    op.drop_constraint('uq_candidate_identity', 'research_review_candidates',
                       type_='unique')
    op.create_unique_constraint(
        op.f('uq_candidate_observation'), 'research_review_candidates',
        ['run_id', 'observation_fingerprint'],
    )
    op.create_index('ix_review_candidates_evidence', 'research_review_candidates',
                    ['evidence_item_id'])

    # ------------------------------------------------------------------
    # Reviews: name the candidate, derive the evidence through it.
    # ------------------------------------------------------------------
    op.add_column(
        'research_evidence_reviews',
        sa.Column('review_candidate_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("ALTER TABLE research_evidence_reviews "
               "DISABLE TRIGGER research_evidence_reviews_append_only")
    # Deterministic: the candidate on that evidence with the lowest attribute
    # key. Every review written under 0008 was gated by a candidate, so this
    # resolves; if a row does not, the NOT NULL below fails the migration rather
    # than dropping it.
    op.execute("""
        UPDATE research_evidence_reviews r
           SET review_candidate_id = (
                 SELECT c.id FROM research_review_candidates c
                  WHERE c.evidence_item_id = r.evidence_item_id
                  ORDER BY c.attribute_key, c.id
                  LIMIT 1)
         WHERE r.review_candidate_id IS NULL
    """)
    op.execute("ALTER TABLE research_evidence_reviews "
               "ENABLE TRIGGER research_evidence_reviews_append_only")
    op.alter_column('research_evidence_reviews', 'review_candidate_id',
                    nullable=False)
    op.create_foreign_key(
        op.f('fk_review_candidate'), 'research_evidence_reviews',
        'research_review_candidates', ['review_candidate_id'], ['id'],
        ondelete='RESTRICT',
    )
    op.create_index('ix_reviews_candidate', 'research_evidence_reviews',
                    ['review_candidate_id'])

    # The evidence item is now derived through the candidate. Keeping both would
    # be two sources of truth that can disagree, which is what made a rejection
    # ambiguous in the first place.
    op.drop_constraint('uq_review_identity', 'research_evidence_reviews',
                       type_='unique')
    op.drop_index('ix_reviews_evidence_item', table_name='research_evidence_reviews')
    op.drop_constraint('fk_review_evidence_item', 'research_evidence_reviews',
                       type_='foreignkey')
    op.drop_column('research_evidence_reviews', 'evidence_item_id')
    op.create_unique_constraint(
        op.f('uq_review_identity'), 'research_evidence_reviews',
        ['review_candidate_id', 'actor', 'reviewed_at'],
    )


def downgrade() -> None:
    op.drop_constraint(op.f('uq_review_identity'), 'research_evidence_reviews',
                       type_='unique')
    op.add_column(
        'research_evidence_reviews',
        sa.Column('evidence_item_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("ALTER TABLE research_evidence_reviews "
               "DISABLE TRIGGER research_evidence_reviews_append_only")
    op.execute("""
        UPDATE research_evidence_reviews r
           SET evidence_item_id = (
                 SELECT c.evidence_item_id FROM research_review_candidates c
                  WHERE c.id = r.review_candidate_id)
    """)
    op.execute("ALTER TABLE research_evidence_reviews "
               "ENABLE TRIGGER research_evidence_reviews_append_only")
    op.alter_column('research_evidence_reviews', 'evidence_item_id', nullable=False)
    op.create_foreign_key(
        op.f('fk_review_evidence_item'), 'research_evidence_reviews',
        'research_evidence_items', ['evidence_item_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_index('ix_reviews_evidence_item', 'research_evidence_reviews',
                    ['evidence_item_id'])
    op.create_unique_constraint(
        'uq_review_identity', 'research_evidence_reviews',
        ['evidence_item_id', 'actor', 'reviewed_at'],
    )
    op.drop_index('ix_reviews_candidate', table_name='research_evidence_reviews')
    op.drop_constraint(op.f('fk_review_candidate'), 'research_evidence_reviews',
                       type_='foreignkey')
    op.drop_column('research_evidence_reviews', 'review_candidate_id')

    op.drop_index('ix_review_candidates_evidence',
                  table_name='research_review_candidates')
    op.drop_constraint(op.f('uq_candidate_observation'),
                       'research_review_candidates', type_='unique')
    op.create_unique_constraint(
        'uq_candidate_identity', 'research_review_candidates',
        ['evidence_item_id', 'run_id'],
    )
    op.drop_column('research_review_candidates', 'observation_fingerprint')
