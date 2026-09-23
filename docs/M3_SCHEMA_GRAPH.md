# M3 — Proposed Schema Graph

**Status:** design, revision 1. **Not implemented.** No table below exists.

Companion to [M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md).

---

## 1. The graph

```
                        ── M2, read-only to M3 ──────────────────
                        companies ◀──────────────┐
                            ▲                    │
                            │ subject_company_id │ company_id
                            │                    │
      ┌─────────────────────┴──────────┐         │
      │      company_claims (M2-owned) │         │
      │      append-only ledger        │         │
      └───────────▲────────────────────┘         │
                  │ claim_id                     │
                  │                              │
      ┌───────────┴────────────┐                 │
      │  claim_evidence_links  │  N:M + locator  │
      └───────────┬────────────┘                 │
                  │ extraction_id                │
                  ▼                              │
      ┌────────────────────────┐                 │
      │  research_extractions  │ append-only     │
      └───────────┬────────────┘                 │
                  │ artifact_version_id          │
                  ▼                              │
   ┌──────────────────────────────┐              │
   │ research_artifact_versions   │ append-only  │
   │ raw bytes + retrieval facts  │              │
   └───────┬──────────────┬───────┘              │
           │ artifact_id  │ source_id            │
           ▼              ▼                      │
 ┌───────────────────┐  ┌──────────────────┐     │
 │ research_artifacts│  │ research_sources │     │
 │ semantic identity │  │ where we looked  │     │
 └───────────────────┘  └────────┬─────────┘     │
                                 │ run_id        │
                                 ▼               │
                    ┌──────────────────────────┐ │
                    │ operational_research_runs├─┘
                    └──────────┬───────────────┘
                               │
              ┌────────────────┼──────────────────┐
              ▼                ▼                  ▼
  ┌────────────────────┐ ┌──────────────┐ ┌────────────────────────┐
  │ operational_       │ │ identity_    │ │ operational_research_  │
  │ research_gaps      │ │ review_      │ │ profiles               │
  │ append-only        │ │ signals      │ │ PROJECTION, truncatable│
  └─────────┬──────────┘ └──────┬───────┘ └────────────────────────┘
            │ *_gap_events             │ *_signal_events
            ▼  append-only status log  ▼  append-only status log
                                │ handed back to M2's review queue
                                ▼
                        (M2 human review, unchanged)

  discovery_jobs (M2-owned queue) ── reused unchanged, no new queue
  attribute_definitions (M2-owned) ── + owner_milestone column, registry M3-1.0
```

**Eleven new tables**, specified in ten sections below — the two structurally identical event logs share one. Two M2 tables are touched: `attribute_definitions` gains
one additive nullable column, and `company_claims` gains a deferred constraint
trigger. Nothing else in M0/M1/M2 changes.

---

## 2. Tables not created, and why

The prompt listed candidates. Three are deliberately absent:

| Candidate | Verdict |
| --- | --- |
| `research_queries` | **Not created.** M2 needed `discovery_queries` because a provider search is a paged, parameterised call worth recording separately. M3's discovery produces *candidates*, and a candidate's provenance is fully carried by `research_sources.discovery_method` + `discovered_from_source_id`. A separate table would hold only a duplicate of the run's parameters. |
| A separate M3 claims table | **Not created.** §5 of the design: one ledger. Creating M3 claims beside `company_claims` would be the "two competing sources of truth" the brief warns against. |
| `REBUILD_RESEARCH_PROFILE` job row | **Not created.** The profile is a synchronous derived rebuild, not queued work (design §24). |

---

## 3. Table specifications

Legend — **Mutability:** `append-only` (BEFORE UPDATE trigger rejects),
`projection` (truncatable, rebuilt from evidence), `mutable` (configuration).

### 3.1 `operational_research_runs`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | mutable — stage timestamps written once, status descriptive |
| PK | `id` (uuid) |
| Natural uniqueness | partial unique on `(company_id, research_policy_version) WHERE status NOT IN ('COMPLETED','PARTIAL','FAILED')` — at most one live run per company per policy |
| Key FKs | `company_id → companies` (RESTRICT), `vertical_id → verticals` (RESTRICT, nullable) |
| Truncatable | No — it is the provenance root of every artifact |
| Temporal | `created_at`, `started_at`, `discovery_completed_at`, `fetch_completed_at`, `extraction_completed_at`, `completed_at` |

Durable stage timestamps, **not** `status`, gate canonical writes
(design §4.1, inheriting M2-ADR-031).

### 3.2 `research_sources`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (normalized_locator, locator_policy_version)` |
| Key FKs | `discovered_by_run_id → operational_research_runs`, `discovered_from_source_id → research_sources` (self, nullable), `resolves_to_source_id → research_sources` (self, nullable) |
| Truncatable | No |
| Temporal | `first_discovered_at` |

`normalized_locator` is the versioned normalization of the URL (design §3.1).
The policy version is in the key: a normalization change produces new sources
rather than silently reinterpreting old ones.

Sources are **never merged**. Two URLs serving one document converge at the
artifact, not here.

### 3.3 `research_artifacts`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (canonicalization_strategy, canonicalization_version, canonical_content_hash)` |
| Key FKs | none — an artifact is content, independent of where it was found |
| Truncatable | No |
| Temporal | `first_seen_at` |

The strategy and its version are **inside** the identity key. A hash is
meaningless without the algorithm that produced it (M2-ADR-028, learned once
already).

### 3.4 `research_artifact_versions`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (source_id, raw_body_sha256)` |
| Key FKs | `artifact_id → research_artifacts` (RESTRICT), `source_id → research_sources` (RESTRICT), `fetched_by_run_id → operational_research_runs` |
| Truncatable | No — but `raw_body` is prunable in place (§3.4.1) |
| Temporal | `retrieved_at` (when we fetched), `source_published_at` + `source_published_granularity` (what the source says, NULL when unstated) |

Columns: `http_status`, `final_url`, `content_type`, `content_length`,
`raw_body_sha256`, `raw_body` (nullable), `extracted_text` (nullable),
`extraction_policy_version`, `language`, `title`, `etag`, `last_modified`,
`fetch_outcome`, `body_retention` (`RETAINED | PRUNED`), `pruned_at`.

`CHECK (source_published_at IS NOT NULL) = (source_published_granularity <> 'UNDATED')`
— the same shape as M0's `observed_at` rule, making invented publication dates
unrepresentable.

#### 3.4.1 Pruning and append-only

`raw_body` must become NULL under retention (design §26) while the table is
append-only. The trigger therefore permits exactly one transition — `raw_body`
non-NULL → NULL, together with `body_retention` → `'PRUNED'` and `pruned_at`
→ now — and rejects every other UPDATE. This is the one place M3 allows a
column to change, it is one-way, and it is enforced in the database rather than
by convention.

*Alternative considered and rejected:* moving bytes to a separate
`research_artifact_bodies` table and DELETEing rows there. It keeps the
append-only rule pure, at the cost of a fourth table and a join on every read
for a column that is NULL most of the time. The one-way trigger is the smaller
correct thing. Recorded as M3-ADR-008.

### 3.5 `research_extractions`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only (`raw_output` prunable by the same one-way rule) |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (artifact_version_id, extractor_id, extractor_version, prompt_template_version)` |
| Key FKs | `artifact_version_id → research_artifact_versions` (RESTRICT), `run_id → operational_research_runs` |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `extractor_kind` (`RULE | PARSER | MODEL | HUMAN`), `extractor_id`,
`extractor_version`, `model_provider`, `model_name`, `model_version`,
`prompt_template_version`, `output_schema_version`, `determinism`,
`temperature`, `extractor_confidence`, `observations` (JSONB), `status`,
`error`, `raw_output`, `raw_output_sha256`.

`extractor_confidence` is stored and **is not** an input to the claim's
confidence (design §9.1).

### 3.6 `claim_evidence_links`

The table that closes M2's provenance gaps. N:M between a claim and its
evidence.

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (claim_id, artifact_version_id, locator_hash)` |
| Key FKs | `claim_id → company_claims` (RESTRICT), `artifact_version_id → research_artifact_versions` (RESTRICT), `extraction_id → research_extractions` (RESTRICT) |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `locator` (JSONB, design §8), `locator_hash` (sha256 of the canonical
locator, so the unique key is indexable), `quote`, `quote_sha256`,
`support_kind` (`DIRECT_STATEMENT | DERIVED | CORROBORATING`).

`RESTRICT` on all three FKs is deliberate: evidence cannot be deleted out from
under a claim. Retention prunes *bodies*, never rows.

### 3.7 `operational_research_gaps`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only rows; status changes appended to `operational_research_gap_events`, never updated in place |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (company_id, attribute_key, research_policy_version, gap_kind)` |
| Key FKs | `company_id → companies` (RESTRICT), `raised_by_run_id → operational_research_runs`, `resolved_by_claim_id → company_claims` (nullable) |
| Truncatable | No — "we once did not know this" is a finding |
| Temporal | `first_raised_at`, `last_attempt_at` |

Columns: `gap_kind`, `attempted_source_count`, `notes`.

Deterministic identity mirrors M1's research gaps, so re-running produces the
same rows rather than duplicates.

### 3.8 `identity_review_signals`

| Property | Value |
| --- | --- |
| Owner | **M3** — written by M3, consumed by M2's review queue |
| Mutability | append-only rows; status appended to `identity_review_signal_events` |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (company_id, signal_kind, related_company_id, evidence_digest)` |
| Key FKs | `company_id → companies` (RESTRICT), `related_company_id → companies` (RESTRICT, nullable), `raised_by_run_id → operational_research_runs` |
| Truncatable | No |
| Temporal | `raised_at` |

A **queue**, not an instruction. M3 never acts on it; M2's existing human-review
path does (design §20.1).

### 3.9 `operational_research_gap_events` and `identity_review_signal_events`

Two structurally identical append-only event logs. Both parent tables carry a
**status that changes over time**, and both parents are append-only — so the
status cannot live on the parent row.

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (parent_id, status, occurred_at)` |
| Key FKs | `parent_id → operational_research_gaps` / `→ identity_review_signals` (RESTRICT) |
| Truncatable | No |
| Temporal | `occurred_at` |

Columns: `status`, `actor`, `note`, `occurred_at`, and for gap events
`resolved_by_claim_id` (nullable).

Gap statuses: `OPEN → IN_PROGRESS → RESOLVED | ABANDONED`.
Signal statuses: `OPEN → ACKNOWLEDGED → ACTIONED | DISMISSED`.

Current status is the latest event, exposed through
`open_operational_research_gaps` and `unresolved_identity_review_signals`
(§7). This is the same shape M2 uses for resolution chains: the state changes,
the history does not, and nothing is updated in place.

*Alternative considered and rejected:* a mutable `status` column on the parent.
It would make two of M3's evidence tables mutable for the convenience of one
column, and it would lose the reviewer's trail — who acknowledged a signal and
when is exactly the audit question these tables exist to answer.

---

### 3.10 `operational_research_profiles`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **projection** — truncatable, rebuilt from claims alone |
| PK | `company_id` (natural key, no surrogate) |
| Natural uniqueness | the PK |
| Key FKs | `company_id → companies` |
| Truncatable | **Yes** — truncate and rebuild must reproduce it byte-identically |
| Temporal | none stored; no clock is read during rebuild |

Columns: per-attribute projected values and envelopes
(`technician_count_min/_max/_best`), `contradiction` flags,
`derived_from_claim_ids` (sorted arrays), `coverage`, `confidence`,
`contradiction_rate`, `required_attribute_count`, `covered_attribute_count`.

**No timestamps, no staleness verdict, no surrogate key.** Staleness is a
`current_operational_research_profiles` **view** applying `now()` at query
time — the split M2 already uses so projections stay deterministic while views
stay current (M2-ADR-024).

`derived_from_claim_ids` is sorted, so rebuilds are byte-identical.

---

## 4. Changes to M2-owned tables

Exactly two, both additive.

### 4.1 `attribute_definitions` — one column

`owner_milestone VARCHAR(8) NULL` (`'M2' | 'M3'`), backfilled to `'M2'` for the
existing registry. Needed so the M3 taxonomy is separable from M2's and so the
trigger in §4.2 can tell them apart.

### 4.2 `company_claims` — one deferred constraint trigger

Rejects at COMMIT any claim whose attribute is owned by M3 and which has no row
in `claim_evidence_links`. An unsourced research claim becomes unrepresentable
rather than merely discouraged — M2 already demonstrated what happens when an
invariant relies on every writer remembering it (M2-ADR-032).

**No existing column, CHECK or index is altered.** M3 claims use the
`subject_company_id` attribution path, which already satisfies
`exactly_one_attribution_path`.

---

## 5. Reused unchanged

| Table | Use |
| --- | --- |
| `companies` | Read. FK target. Never written by M3 |
| `company_claims` | Written via the `subject_company_id` path |
| `company_domains` | Read, to decide crawl seeds (design §16) |
| `attribute_definitions` | M3 registry version `M3-1.0` |
| `discovery_jobs` | The existing SKIP LOCKED queue, no new queue |
| `markets`, `verticals` | Read-only FK targets for context |

---

## 6. Index plan

```
research_sources            UNIQUE (normalized_locator, locator_policy_version)
                            INDEX  (discovered_by_run_id)
                            INDEX  (resolves_to_source_id) WHERE NOT NULL

research_artifacts          UNIQUE (canonicalization_strategy,
                                    canonicalization_version,
                                    canonical_content_hash)

research_artifact_versions  UNIQUE (source_id, raw_body_sha256)
                            INDEX  (artifact_id)
                            INDEX  (fetched_by_run_id)
                            INDEX  (retrieved_at DESC)
                            INDEX  (body_retention) WHERE body_retention='RETAINED'

research_extractions        UNIQUE (artifact_version_id, extractor_id,
                                    extractor_version, prompt_template_version)
                            INDEX  (run_id)

claim_evidence_links        UNIQUE (claim_id, artifact_version_id, locator_hash)
                            INDEX  (artifact_version_id)
                            INDEX  (extraction_id)

operational_research_gaps   UNIQUE (company_id, attribute_key,
                                    research_policy_version, gap_kind)
                            INDEX  (company_id, gap_kind)

identity_review_signals     UNIQUE (company_id, signal_kind,
                                    related_company_id, evidence_digest)

operational_research_runs   UNIQUE (company_id, research_policy_version)
                              WHERE status NOT IN ('COMPLETED','PARTIAL','FAILED')
                            INDEX  (status) WHERE status NOT IN ('COMPLETED','FAILED')

operational_research_gap_events    UNIQUE (parent_id, status, occurred_at)
                                   INDEX  (parent_id, occurred_at DESC)

identity_review_signal_events      UNIQUE (parent_id, status, occurred_at)
                                   INDEX  (parent_id, occurred_at DESC)

operational_research_profiles      PK (company_id)
```

---

## 7. Views

Everything time-varying lives in a view, never in a projection.

| View | Purpose |
| --- | --- |
| `current_operational_research_profiles` | Profile + staleness computed against `now()` |
| `current_research_sources` | Latest version and fetch outcome per source |
| `unresolved_identity_review_signals` | Signals whose latest event is not `ACTIONED`/`DISMISSED` |
| `open_operational_research_gaps` | Gaps whose latest event is not `RESOLVED` |

---

## 8. Ownership summary

| Table | Owner | Mutability | Truncatable |
| --- | --- | --- | --- |
| `operational_research_runs` | M3 | mutable (one-way stage stamps) | No |
| `research_sources` | M3 | append-only | No |
| `research_artifacts` | M3 | append-only | No |
| `research_artifact_versions` | M3 | append-only + one-way prune | No |
| `research_extractions` | M3 | append-only + one-way prune | No |
| `claim_evidence_links` | M3 | append-only | No |
| `operational_research_gaps` | M3 | append-only | No |
| `operational_research_gap_events` | M3 | append-only | No |
| `identity_review_signals` | M3 (consumed by M2) | append-only | No |
| `identity_review_signal_events` | M3 | append-only | No |
| `operational_research_profiles` | M3 | **projection** | **Yes** |
| `companies`, `company_claims`, `company_domains` | **M2** | unchanged | per M2 |
| `discovery_jobs` | **M2** | unchanged | per M2 |
