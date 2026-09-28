"""m3 lifecycle tiebreak: make "latest event" deterministic in the database too

`gtm_m3_signal_event_transition` and the `current_operational_research_gaps`
view both resolve "the current status" with `ORDER BY occurred_at DESC, id
DESC`. The id is a random UUID, so two events at the same instant — which the
pipeline produces every run, raising a signal and recording `OPEN` in one call —
are ordered arbitrarily.

M3-ADR-053 fixed this in the services by breaking ties on lifecycle position.
Leaving the trigger unfixed left **two statements of one invariant that can
disagree**: the service read `ACKNOWLEDGED` and the trigger read `OPEN` for the
same rows, so a legal transition was rejected. That is the failure mode this
project has hit repeatedly, and the fix is to make both agree by construction.

Both lifecycle graphs are DAGs, so "furthest along" is a total order:

    RAISED → ATTEMPTED → RESOLVED | ABANDONED
    OPEN   → ACKNOWLEDGED → ACTIONED | DISMISSED

`0004_m3` is left byte-identical; it is published in this branch and protected
by the migration manifest.

Revision ID: 0006_m3_tiebreak
Revises: 0005_m3_retention
"""

from __future__ import annotations

from alembic import op

revision = '0006_m3_tiebreak'
down_revision = '0005_m3_retention'
branch_labels = None
depends_on = None

#: Position in the review lifecycle, as a SQL expression.
_REVIEW_RANK = """
    CASE status
        WHEN 'ACTIONED' THEN 2
        WHEN 'DISMISSED' THEN 2
        WHEN 'ACKNOWLEDGED' THEN 1
        ELSE 0
    END
"""

#: Position in the gap lifecycle.
_GAP_RANK = """
    CASE event_kind
        WHEN 'RESOLVED' THEN 2
        WHEN 'ABANDONED' THEN 2
        WHEN 'ATTEMPTED' THEN 1
        ELSE 0
    END
"""


def upgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION gtm_m3_signal_event_transition() RETURNS trigger AS $$
        DECLARE latest text;
        BEGIN
            -- Ties on occurred_at break on lifecycle position, never on the
            -- row id, so this agrees with the service that calls it.
            SELECT status INTO latest
            FROM identity_review_signal_events
            WHERE occurrence_id = NEW.occurrence_id
            ORDER BY occurred_at DESC, {_REVIEW_RANK} DESC, id DESC LIMIT 1;
            IF latest IS NULL THEN
                IF NEW.status <> 'OPEN' THEN
                    RAISE EXCEPTION 'a review occurrence must open with OPEN, got %',
                        NEW.status;
                END IF;
            ELSIF NOT (CASE latest
            WHEN 'ACKNOWLEDGED' THEN NEW.status IN ('ACTIONED','DISMISSED')
            WHEN 'ACTIONED' THEN FALSE
            WHEN 'DISMISSED' THEN FALSE
            WHEN 'OPEN' THEN NEW.status IN ('ACKNOWLEDGED','DISMISSED')
            ELSE FALSE END) THEN
                RAISE EXCEPTION 'illegal signal transition % -> %', latest, NEW.status;
            END IF;
            UPDATE identity_review_signal_occurrences
               SET is_open = (NEW.status NOT IN ('ACTIONED','DISMISSED'))
             WHERE id = NEW.occurrence_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute(f"""
        CREATE OR REPLACE FUNCTION gtm_m3_gap_event_transition() RETURNS trigger AS $$
        DECLARE latest text;
        BEGIN
            SELECT event_kind INTO latest
            FROM operational_research_gap_events
            WHERE gap_id = NEW.gap_id
            ORDER BY occurred_at DESC, {_GAP_RANK} DESC, id DESC LIMIT 1;
            IF latest IS NULL THEN
                IF NEW.event_kind <> 'RAISED' THEN
                    RAISE EXCEPTION 'a gap must open with RAISED, got %',
                        NEW.event_kind;
                END IF;
            ELSIF NOT (CASE latest
            WHEN 'ABANDONED' THEN FALSE
            WHEN 'ATTEMPTED' THEN NEW.event_kind IN ('ABANDONED','ATTEMPTED','RESOLVED')
            WHEN 'RAISED' THEN NEW.event_kind IN ('ABANDONED','ATTEMPTED','RESOLVED')
            WHEN 'RESOLVED' THEN FALSE
            ELSE FALSE END) THEN
                RAISE EXCEPTION 'illegal gap transition % -> %', latest,
                    NEW.event_kind;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # Both views are recreated from 0004's definitions with only the ORDER BY
    # changed, so nothing else about them can drift.
    op.execute("DROP VIEW current_operational_research_gaps")
    op.execute(f"""
        CREATE VIEW current_operational_research_gaps AS
        SELECT g.*,
               s.current_status,
               s.attempt_count,
               s.attempted_source_count,
               s.last_attempt_at,
               s.resolved_by_claim_id
        FROM operational_research_gaps g
        LEFT JOIN LATERAL (
            SELECT
              (SELECT event_kind FROM operational_research_gap_events e
                WHERE e.gap_id = g.id
                ORDER BY occurred_at DESC, {_GAP_RANK} DESC, id DESC LIMIT 1)
                AS current_status,
              count(*) FILTER (WHERE e2.event_kind = 'ATTEMPTED') AS attempt_count,
              count(DISTINCT e2.source_id) FILTER (WHERE e2.event_kind = 'ATTEMPTED')
                AS attempted_source_count,
              max(e2.occurred_at) FILTER (WHERE e2.event_kind = 'ATTEMPTED')
                AS last_attempt_at,
              (SELECT resolved_by_claim_id FROM operational_research_gap_events e3
                WHERE e3.gap_id = g.id AND e3.event_kind = 'RESOLVED'
                ORDER BY occurred_at DESC, id DESC LIMIT 1) AS resolved_by_claim_id
            FROM operational_research_gap_events e2 WHERE e2.gap_id = g.id
        ) s ON TRUE;
    """)

    op.execute("DROP VIEW current_identity_review_occurrences")
    op.execute(f"""
        CREATE VIEW current_identity_review_occurrences AS
        SELECT o.*,
               (SELECT status FROM identity_review_signal_events e
                 WHERE e.occurrence_id = o.id
                 ORDER BY occurred_at DESC, {_REVIEW_RANK} DESC, id DESC LIMIT 1)
                 AS current_status
        FROM identity_review_signal_occurrences o;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION gtm_m3_signal_event_transition() RETURNS trigger AS $$
        DECLARE latest text;
        BEGIN
            SELECT status INTO latest
            FROM identity_review_signal_events
            WHERE occurrence_id = NEW.occurrence_id
            ORDER BY occurred_at DESC, id DESC LIMIT 1;
            IF latest IS NULL THEN
                IF NEW.status <> 'OPEN' THEN
                    RAISE EXCEPTION 'a review occurrence must open with OPEN, got %',
                        NEW.status;
                END IF;
            ELSIF NOT (CASE latest
            WHEN 'ACKNOWLEDGED' THEN NEW.status IN ('ACTIONED','DISMISSED')
            WHEN 'ACTIONED' THEN FALSE
            WHEN 'DISMISSED' THEN FALSE
            WHEN 'OPEN' THEN NEW.status IN ('ACKNOWLEDGED','DISMISSED')
            ELSE FALSE END) THEN
                RAISE EXCEPTION 'illegal signal transition % -> %', latest, NEW.status;
            END IF;
            UPDATE identity_review_signal_occurrences
               SET is_open = (NEW.status NOT IN ('ACTIONED','DISMISSED'))
             WHERE id = NEW.occurrence_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION gtm_m3_gap_event_transition() RETURNS trigger AS $$
        DECLARE latest text;
        BEGIN
            SELECT event_kind INTO latest
            FROM operational_research_gap_events
            WHERE gap_id = NEW.gap_id
            ORDER BY occurred_at DESC, id DESC LIMIT 1;
            IF latest IS NULL THEN
                IF NEW.event_kind <> 'RAISED' THEN
                    RAISE EXCEPTION 'a gap must open with RAISED, got %',
                        NEW.event_kind;
                END IF;
                RETURN NEW;
            END IF;
            IF NOT (CASE latest
            WHEN 'ABANDONED' THEN FALSE
            WHEN 'ATTEMPTED' THEN NEW.event_kind IN ('ABANDONED','ATTEMPTED','RESOLVED')
            WHEN 'RAISED' THEN NEW.event_kind IN ('ABANDONED','ATTEMPTED','RESOLVED')
            WHEN 'RESOLVED' THEN FALSE
            ELSE FALSE END) THEN
                RAISE EXCEPTION 'illegal gap transition % -> %', latest, NEW.event_kind;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("DROP VIEW current_identity_review_occurrences")
    op.execute("""
        CREATE VIEW current_identity_review_occurrences AS
        SELECT o.*,
               (SELECT status FROM identity_review_signal_events e
                 WHERE e.occurrence_id = o.id
                 ORDER BY occurred_at DESC, id DESC LIMIT 1) AS current_status
        FROM identity_review_signal_occurrences o;
    """)
    op.execute("DROP VIEW current_operational_research_gaps")
    op.execute("""
        CREATE VIEW current_operational_research_gaps AS
        SELECT g.*,
               s.current_status,
               s.attempt_count,
               s.attempted_source_count,
               s.last_attempt_at,
               s.resolved_by_claim_id
        FROM operational_research_gaps g
        LEFT JOIN LATERAL (
            SELECT
              (SELECT event_kind FROM operational_research_gap_events e
                WHERE e.gap_id = g.id ORDER BY occurred_at DESC, id DESC LIMIT 1)
                AS current_status,
              count(*) FILTER (WHERE e2.event_kind = 'ATTEMPTED') AS attempt_count,
              count(DISTINCT e2.source_id) FILTER (WHERE e2.event_kind = 'ATTEMPTED')
                AS attempted_source_count,
              max(e2.occurred_at) FILTER (WHERE e2.event_kind = 'ATTEMPTED')
                AS last_attempt_at,
              (SELECT resolved_by_claim_id FROM operational_research_gap_events e3
                WHERE e3.gap_id = g.id AND e3.event_kind = 'RESOLVED'
                ORDER BY occurred_at DESC, id DESC LIMIT 1) AS resolved_by_claim_id
            FROM operational_research_gap_events e2 WHERE e2.gap_id = g.id
        ) s ON TRUE;
    """)
