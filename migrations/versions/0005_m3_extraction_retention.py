"""m3 extraction retention: make model raw_output prunable

Design §26 assigns model `raw_output` a retention class — "default 90 days;
hash permanent" — but `0004_m3` gave `research_extractions` a blanket
`gtm_reject_update()` trigger and no retention columns, so the class it
mandates was unrepresentable. Pruning was refused outright:

    RestrictViolation: relation research_extractions is append-only:
    UPDATE is not permitted

This adds the two columns and swaps the blanket rejection for the same
parameterised one-way prune that bodies and text derivations already use, so
exactly one mutation is permitted and every other UPDATE is still refused by
the database.

`0004_m3` is deliberately left untouched: it is published in this branch and
protected by the migration manifest. Rewriting it to hide the defect would make
the manifest a formality. See M3-ADR-052.

Revision ID: 0005_m3_retention
Revises: 0004_m3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005_m3_retention'
down_revision = '0004_m3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'research_extractions',
        sa.Column('raw_output_retention', sa.String(length=16), nullable=False,
                  server_default='RETAINED'),
    )
    op.add_column(
        'research_extractions',
        sa.Column('raw_output_pruned_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        'ck_extraction_raw_output_retention_vocabulary',
        'research_extractions',
        "raw_output_retention IN ('RETAINED','PRUNED')",
    )
    # A pruned payload must say so, and a retained one must not claim a date.
    op.create_check_constraint(
        'ck_extraction_raw_output_prune_consistency',
        'research_extractions',
        "(raw_output_retention = 'RETAINED' AND raw_output_pruned_at IS NULL) OR "
        "(raw_output_retention = 'PRUNED' AND raw_output_pruned_at IS NOT NULL)",
    )
    op.create_index(
        'ix_extractions_raw_output_retention', 'research_extractions',
        ['raw_output_retention'],
    )

    op.execute("DROP TRIGGER research_extractions_one_way_prune ON research_extractions")
    op.execute("""
        CREATE TRIGGER research_extractions_one_way_prune
        BEFORE UPDATE ON research_extractions
        FOR EACH ROW EXECUTE FUNCTION
            gtm_m3_one_way_prune('raw_output', 'raw_output_retention');
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER research_extractions_one_way_prune ON research_extractions")
    op.execute("""
        CREATE TRIGGER research_extractions_one_way_prune
        BEFORE UPDATE ON research_extractions
        FOR EACH ROW EXECUTE FUNCTION gtm_reject_update();
    """)
    op.drop_index('ix_extractions_raw_output_retention',
                  table_name='research_extractions')
    op.drop_constraint('ck_extraction_raw_output_prune_consistency',
                       'research_extractions', type_='check')
    op.drop_constraint('ck_extraction_raw_output_retention_vocabulary',
                       'research_extractions', type_='check')
    op.drop_column('research_extractions', 'raw_output_pruned_at')
    op.drop_column('research_extractions', 'raw_output_retention')
