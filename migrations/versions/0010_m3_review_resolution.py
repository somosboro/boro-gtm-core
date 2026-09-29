"""m3: one occurrence per candidate, one operative decision, one reconciled account

Three findings, all of which show up as a BoRo operator being misled.

**A candidate collapsed across evidence origins.** `0009` keyed a candidate
`UNIQUE (run_id, observation_fingerprint)`, which correctly separated two
readings of one span but merged the *same* reading found on different sources.
Reproduced on this branch: the pipeline made **30** `raise_candidate` calls and
landed **14** — eight observations were each attempted from three distinct
sources (`meridianmechanical.com`, a second path on it, and the unrelated
`meridian-mechanical.net`) and collapsed to one candidate each. Those are three
different publishers. Rejecting the reading on one of them is not a judgement
about the other two, and sixteen questions never reached a reviewer at all.
Identity becomes `UNIQUE (evidence_item_id, observation_fingerprint)`: one
observation *occurrence*. The run is deliberately not in the key — a reviewer's
answer about a source does not expire because a later run saw it again.

**Every decision acted.** A second reviewer confirming an observation another
reviewer had already rejected ran the whole confirmation again: another HUMAN
extraction, another evidence item, another claim. The documented rule is that
the first decision is operative and later ones are recorded dissent. `is_operative`
plus `UNIQUE (review_candidate_id) WHERE is_operative` makes that the schema's
rule rather than the service's intention, and makes two simultaneous first
reviewers impossible rather than unlikely.

**A closed question still read as open.** An operative confirmation created the
claim and left the `INSUFFICIENT_EVIDENCE` gap open, the company profile saying
nothing was known, and plan coverage counting the attribute as uncovered. The
service now resolves the gap and rebuilds both projections synchronously, and the
gap ledger has to be able to say a *human* closed it: `attempt_id` becomes
nullable, `resolved_by_review_id` is added, and a CHECK requires exactly one
actor per event. Borrowing the attempt that raised the gap would have recorded a
machine run doing something a person did days later.

**Candidate provenance is no longer stored.** `company_id`, `run_id` and
`attempt_id` were columns; all three follow from `evidence_item_id` through
single-valued foreign keys, so keeping them was three ways for a queued row to
name the wrong account. They are dropped and derived. A mismatch is now
unrepresentable rather than merely detectable.

Fingerprints are recomputed, because `lineage_tag` joins the hash: it decides how
an observation groups when a claim is asserted, so two readings differing only in
the tag are different review questions.

`0001`–`0009` are published on this branch and left byte-identical.

Revision ID: 0010_m3_review_resolution
Revises: 0009_m3_observation
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_m3_review_resolution"
down_revision = "0009_m3_observation"
branch_labels = None
depends_on = None

#: The hash 0009 wrote, and the hash this migration writes. Both are inlined
#: rather than imported: a migration has to keep working when the service moves
#: on, and importing today's function would silently change what this file did.
_OLD_FIELDS = ("attribute_key", "value", "unit", "fact_type", "locator_hash",
               "support_kind")
_NEW_FIELDS = (*_OLD_FIELDS, "lineage_tag")


def _canonical(payload: object) -> str:
    """Byte-for-byte `policies.sha256_json`, copied rather than imported.

    `default=str` and the absence of `ensure_ascii=False` are load-bearing: the
    hashes already on disk were produced by exactly this spelling.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")
    ).hexdigest()


def _locator_hash(locator: dict) -> str:
    return _canonical(locator)


def _fingerprint(stored: dict, fields: tuple[str, ...]) -> str:
    payload = {
        "attribute_key": stored["attribute_key"],
        "value": stored["value"],
        "unit": stored.get("unit"),
        "fact_type": stored["fact_type"],
        "locator_hash": _locator_hash(stored["locator"]),
        "support_kind": stored.get("support_kind", "DIRECT_STATEMENT"),
        "lineage_tag": stored.get("lineage_tag"),
    }
    return _canonical({k: payload[k] for k in fields})


def upgrade() -> None:
    connection = op.get_bind()

    # --- gap events may name a human actor ---------------------------------
    op.alter_column("operational_research_gap_events", "attempt_id",
                    existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column(
        "operational_research_gap_events",
        sa.Column("resolved_by_review_id", postgresql.UUID(as_uuid=True),
                  nullable=True),
    )
    op.create_foreign_key(
        "fk_gap_event_review", "operational_research_gap_events",
        "research_evidence_reviews", ["resolved_by_review_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "gap_event_has_one_actor", "operational_research_gap_events",
        "(attempt_id IS NOT NULL) <> (resolved_by_review_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "only_a_resolution_is_human", "operational_research_gap_events",
        "resolved_by_review_id IS NULL OR event_kind = 'RESOLVED'",
    )

    # --- a review is operative or dissent ----------------------------------
    op.add_column(
        "research_evidence_reviews",
        sa.Column("is_operative", sa.Boolean(), nullable=True),
    )
    # The append-only trigger is not something a service may switch off. A
    # migration may, for exactly as long as the backfill takes.
    op.execute("ALTER TABLE research_evidence_reviews "
               "DISABLE TRIGGER research_evidence_reviews_append_only")
    # Existing rows: the earliest decision per candidate is the one that acted.
    # Every row 0009 left behind was written under the old rule, where each
    # decision ran in full, so "earliest" is the only defensible reading.
    op.execute("""
        UPDATE research_evidence_reviews r
           SET is_operative = (r.id = (
                   SELECT o.id FROM research_evidence_reviews o
                    WHERE o.review_candidate_id = r.review_candidate_id
                    ORDER BY o.reviewed_at, o.id
                    LIMIT 1))
    """)
    op.execute("ALTER TABLE research_evidence_reviews "
               "ENABLE TRIGGER research_evidence_reviews_append_only")
    op.alter_column("research_evidence_reviews", "is_operative",
                    existing_type=sa.Boolean(), nullable=False)
    op.create_index(
        "uq_review_operative", "research_evidence_reviews",
        ["review_candidate_id"], unique=True,
        postgresql_where=sa.text("is_operative"),
    )
    op.create_check_constraint(
        "dissent_asserts_nothing", "research_evidence_reviews",
        "is_operative OR "
        "(human_extraction_id IS NULL AND resulting_claim_id IS NULL)",
    )
    op.drop_constraint("confirmation_has_provenance", "research_evidence_reviews",
                       type_="check")
    op.create_check_constraint(
        "confirmation_has_provenance", "research_evidence_reviews",
        "decision <> 'CONFIRM' OR NOT is_operative "
        "OR human_extraction_id IS NOT NULL",
    )

    # --- candidate identity is one observation occurrence -------------------
    op.execute("ALTER TABLE research_review_candidates "
               "DISABLE TRIGGER research_review_candidates_append_only")

    # Recompute fingerprints: `lineage_tag` now participates. A row is matched
    # by its OLD fingerprint against the observations persisted on its own
    # extraction, so the rewrite is deterministic and names the same reading.
    # `legacy:`-prefixed rows predate 0009's fingerprint entirely and have no
    # honest observation to match; they keep their marker rather than acquiring
    # a hash that would collide with a real reading.
    rows = connection.execute(sa.text("""
        SELECT c.id, c.observation_fingerprint, e.observations
          FROM research_review_candidates c
          JOIN research_evidence_items i ON i.id = c.evidence_item_id
          JOIN research_extractions e ON e.id = i.extraction_id
         WHERE c.observation_fingerprint NOT LIKE 'legacy:%'
    """)).all()
    for candidate_id, old, observations in rows:
        payload = (observations or {}).get("observations", [])
        match = next(
            (o for o in payload if _fingerprint(o, _OLD_FIELDS) == old), None
        )
        if match is None:
            # The queue and the ledger already disagreed before this migration.
            # Marking it is honest; inventing a fingerprint would not be, and
            # the service refuses to review such a row rather than guessing.
            connection.execute(
                sa.text("UPDATE research_review_candidates "
                        "SET observation_fingerprint = :f WHERE id = :i"),
                {"f": f"unmatched:{old[:54]}", "i": candidate_id},
            )
            continue
        connection.execute(
            sa.text("UPDATE research_review_candidates "
                    "SET observation_fingerprint = :f WHERE id = :i"),
            {"f": _fingerprint(match, _NEW_FIELDS), "i": candidate_id},
        )

    op.drop_constraint("uq_candidate_observation", "research_review_candidates",
                       type_="unique")
    op.create_unique_constraint(
        "uq_candidate_occurrence", "research_review_candidates",
        ["evidence_item_id", "observation_fingerprint"],
    )

    # Provenance is derived from here on. Dropping the columns is what makes an
    # inconsistent row unrepresentable rather than merely rejectable.
    op.drop_index("ix_review_candidates_company",
                  table_name="research_review_candidates")
    op.drop_index("ix_review_candidates_attribute",
                  table_name="research_review_candidates")
    op.create_index("ix_review_candidates_attribute",
                    "research_review_candidates", ["attribute_key"])
    for column in ("company_id", "run_id", "attempt_id"):
        op.drop_column("research_review_candidates", column)

    op.execute("ALTER TABLE research_review_candidates "
               "ENABLE TRIGGER research_review_candidates_append_only")


def downgrade() -> None:
    op.execute("ALTER TABLE research_review_candidates "
               "DISABLE TRIGGER research_review_candidates_append_only")
    op.add_column("research_review_candidates",
                  sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("research_review_candidates",
                  sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("research_review_candidates",
                  sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("""
        UPDATE research_review_candidates c
           SET attempt_id = p.attempt_id,
               run_id     = p.run_id,
               company_id = p.company_id
          FROM (SELECT i.id AS evidence_item_id, a.id AS attempt_id,
                       r.id AS run_id, r.company_id
                  FROM research_evidence_items i
                  JOIN research_fetch_events f ON f.id = i.fetch_event_id
                  JOIN operational_research_attempts a ON a.id = f.attempt_id
                  JOIN operational_research_runs r ON r.id = a.run_id) p
         WHERE p.evidence_item_id = c.evidence_item_id
    """)
    op.execute("ALTER TABLE research_review_candidates "
               "ENABLE TRIGGER research_review_candidates_append_only")
    for column, target in (("company_id", "companies"),
                           ("run_id", "operational_research_runs"),
                           ("attempt_id", "operational_research_attempts")):
        op.alter_column("research_review_candidates", column,
                        existing_type=postgresql.UUID(as_uuid=True), nullable=False)
        op.create_foreign_key(
            f"fk_candidate_{column}", "research_review_candidates", target,
            [column], ["id"], ondelete="RESTRICT",
        )
    op.drop_index("ix_review_candidates_attribute",
                  table_name="research_review_candidates")
    op.create_index("ix_review_candidates_attribute",
                    "research_review_candidates", ["run_id", "attribute_key"])
    op.create_index("ix_review_candidates_company",
                    "research_review_candidates", ["company_id"])
    op.drop_constraint("uq_candidate_occurrence", "research_review_candidates",
                       type_="unique")
    op.create_unique_constraint(
        "uq_candidate_observation", "research_review_candidates",
        ["run_id", "observation_fingerprint"],
    )

    op.drop_constraint("confirmation_has_provenance", "research_evidence_reviews",
                       type_="check")
    op.create_check_constraint(
        "confirmation_has_provenance", "research_evidence_reviews",
        "decision <> 'CONFIRM' OR human_extraction_id IS NOT NULL",
    )
    op.drop_constraint("dissent_asserts_nothing", "research_evidence_reviews",
                       type_="check")
    op.drop_index("uq_review_operative", table_name="research_evidence_reviews")
    op.drop_column("research_evidence_reviews", "is_operative")

    op.drop_constraint("only_a_resolution_is_human",
                       "operational_research_gap_events", type_="check")
    op.drop_constraint("gap_event_has_one_actor",
                       "operational_research_gap_events", type_="check")
    op.drop_constraint("fk_gap_event_review", "operational_research_gap_events",
                       type_="foreignkey")
    op.drop_column("operational_research_gap_events", "resolved_by_review_id")
    op.alter_column("operational_research_gap_events", "attempt_id",
                    existing_type=postgresql.UUID(as_uuid=True), nullable=False)
