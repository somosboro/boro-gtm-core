# M2 — Schema Graph

**Status:** design, revision 5 (final). Implementation authorized. No migrations. Read alongside
[M2_COMPANY_DISCOVERY_DESIGN.md](M2_COMPANY_DISCOVERY_DESIGN.md).

---

## 1. Three tiers

The single most important structural fact: tables fall into three tiers with
different truncation rules.

```
╔═══════════════════════════════════════════════════════════════════════════╗
║ IDENTITY ANCHOR — never truncated, UUIDs are minted and referenced        ║
║   companies (id, created_at, identity_policy_version,                     ║
║              lifecycle_status, merged_into_company_id)                    ║
╚═══════════════════════════════════════════════════════════════════════════╝
                                   ▲
                 referenced by     │      referenced by
        ┌──────────────────────────┴──────────────────────────┐
        │                                                     │
╔═══════╧═══════════════════════════════════════════════╗     │
║ EVIDENCE — append-only, never truncated               ║     │
║                                                       ║     │
║  discovery_providers                                  ║     │
║        │ 1:N                                          ║     │
║        ├──▶ discovery_runs ──1:N──▶ discovery_queries ║     │
║        │                                   │          ║     │
║        └──▶ provider_entities              │          ║     │
║                 │ 1:N                      │          ║     │
║                 ▼                          │          ║     │
║           provider_record_versions ◀───────┘          ║     │
║                 │ 1:N    │ 1:N    │ 1:N               ║     │
║                 ▼        ▼        ▼                   ║     │
║   provider_record_  provider_   provider_record_      ║     │
║        bodies      record_norm   sightings            ║     │
║     (prunable,     alizations                         ║     │
║      N per version)                                   ║     │
║                                                       ║     │
║  entity_resolution_candidates                         ║     │
║        │                                              ║     │
║        ▼                                              ║     │
║  entity_resolution_decisions ──self-ref chain──┐      ║     │
║        │  supersedes_decision_id ◀─────────────┘      ║─────┘
║        │                                              ║
║  company_claims ─────────────────────────────────────╫──────┐
║  company_relationship_claims ────────────────────────╫──────┤
╚═══════════════════════════════════════════════════════╝      │
                                   │                           │
                          projected into                       │
                                   ▼                           │
╔═══════════════════════════════════════════════════════════════╧═══════════╗
║ DERIVED PROJECTIONS — fully truncatable, deterministic, natural keys only ║
║   company_profiles · company_names · company_domains                      ║
║   company_locations · company_market_presences · company_verticals        ║
║   company_relationships (intervals) · entity_resolution_heads             ║
╚═══════════════════════════════════════════════════════════════════════════╝
                                   │
                          date filters applied by
                                   ▼
╔═══════════════════════════════════════════════════════════════════════════╗
║ VIEWS — may legitimately vary with wall-clock time                        ║
║   current_company_relationships · current_company_market_presences        ║
║   company_relationships_bidirectional · company_claims_effective          ║
║   current_entity_resolutions                                              ║
╚═══════════════════════════════════════════════════════════════════════════╝

╔═══════════════════════════════════════════════════════════════════════════╗
║ OPERATIONAL — outside every determinism claim                             ║
║   projection_runs (timing, row counts, per-table content digests)         ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

## 2. What can and cannot be truncated

| Tier | Truncatable | Consequence of truncating |
| --- | --- | --- |
| `companies` | **No** | Every claim, decision and relationship claim reaches a company through it. UUIDs are minted by judgement, not computed, so they cannot be regenerated identically |
| Evidence tables | **No** | This *is* the record. Nothing else can reconstruct it |
| `provider_record_bodies` | **Prunable** — by documented retention policy only | Digests, parsed payload and the evidence chain survive; only literal bytes are lost |
| Derived projections | **Yes, entirely** | Rebuilt byte-identically from evidence + registry version + policy version |
| `projection_runs` | **Yes, independently** | Operational telemetry only; outside every determinism claim |
| Views | n/a | Not stored. May legitimately return different rows as the date moves |

### The rebuild invariant

> All **derived projections** may be truncated and rebuilt byte-identically,
> while **identity anchors, claims, decisions and relationship claims are never
> truncated.**

Given fixed evidence, a fixed `attribute_registry_version` and a fixed
`identity_policy_version`, the projection function is **pure** — it reads no
clock, no sequence and no prior projection state.

Equality is compared over **all columns of every projection table**, because
nothing non-deterministic is stored in one: no rebuild timestamps (they live in
`projection_runs`), no surrogate keys (projections use natural keys), sorted
arrays, and interval columns rather than `now()`-filtered snapshots. The
mechanical check is a per-table canonical digest:

```sql
SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM <projection> t;
```

Asserted by acceptance scenarios B3 and B3d–B3f.

## 3. Tables, ownership and cardinality

### Identity anchor

| Table | Owns | Key constraint |
| --- | --- | --- |
| `companies` | Canonical identity and lifecycle only | self-FK `merged_into_company_id`; **no domain uniqueness here** |

### Evidence — append-only

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `discovery_providers` | Registry, capabilities, trust tier, `identity_capability`, `key_fields`, `key_algorithm_version` | — | `UNIQUE (provider_key)` |
| `discovery_runs` | One execution in one M1 context, lifecycle state, `allow_partial_resolution`, and `fetch_completed_at` — the durable gate on canonical writes (M2-ADR-031) | provider 1:N | — |
| `discovery_queries` | Exact query, params, cursor, page, result count | run 1:N | — |
| `provider_entities` | Provider-side identity, `external_id_kind`, `key_algorithm_version`. Collision is derived, never stored (M2-ADR-034) | provider 1:N | **`UNIQUE (provider_id, provider_external_id)`** |
| `provider_record_versions` | Immutable **semantic** observation: `parsed_payload`, `canonical_payload_hash`, `canonicalization_strategy` + `_version`, `retrieved_at` | entity 1:N · query 1:N | **`UNIQUE (provider_entity_id, canonicalization_strategy, canonicalization_version, canonical_payload_hash)`** |
| `provider_record_bodies` | Distinct **byte** representations of one semantic version: `raw_body`, `raw_body_sha256`, `content_type`, `retrieved_at`, `discovery_query_id` | version **1:N** | **`UNIQUE (provider_record_version_id, raw_body_sha256)`** |
| `provider_record_normalizations` | Normalizer output per version | version 1:N | `UNIQUE (version_id, normalizer_version)` |
| `provider_record_sightings` | "Re-confirmed unchanged on date Y" | version 1:N | `UNIQUE (version_id, discovery_query_id)` |
| `entity_resolution_candidates` | A considered pairing with signals and score | version 1:N · company 1:N | — |
| `entity_resolution_decisions` | Outcome, method, rationale, actor, policy version | entity 1:N, **linear chain within one entity** | `uq_resolution_root` + `uq_resolution_supersedes` + `fk_supersedes_same_entity` — see §7 (M2-ADR-018, M2-ADR-025) |
| `company_claims` | One typed assertion against the registry | version 1:N *or* company 1:N | `CHECK` exactly one attribution path |
| `company_relationship_claims` | Typed, time-bounded relationship assertion | company N:N | `UNIQUE (supersedes_claim_id)`; `CHECK (from < to)` for `SISTER_OF` |

### Derived projections — truncatable

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `company_profiles` | Canonical business attributes, `derived_from_claim_ids`, `projection_conflict` | company 1:1 | PK `company_id` |
| `company_names` | Name variants and types | company 1:N | `UNIQUE (company_id, name_normalized, name_type)`; trigram index for retrieval |
| `company_domains` | Domains and roles | company 1:N | **`UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'`** |
| `company_locations` | Physical/registered places only | company 1:N · market 1:N | `INDEX (market_id)` |
| `company_market_presences` | Operating geography with **validity intervals** — never a `now()`-filtered snapshot | company N:N market | PK `(company_id, market_id, presence_type, effective_from)` — interval start is in the key so leave-and-re-enter is two rows |
| `company_verticals` | Vertical membership with evidence | company N:N vertical | `UNIQUE (company_id, vertical_id)` |
| `company_relationships` | Effective, non-retracted relationships with **validity intervals** (`effective_from`, `effective_to`) — never a `now()`-filtered snapshot | company N:N | PK `(from_company_id, to_company_id, relationship_type)` |
| `entity_resolution_heads` | O(1) head lookup cache | entity 1:1 | PK `provider_entity_id` |

### Registry

| Table | Owns | Notes |
| --- | --- | --- |
| `attribute_definitions` | The versioned attribute contract (design §5.2) | Configuration tier: seeded from code as M0 seeds `scoring_models`, rebuildable from that seed. Claims reference the *version*, not the row. `UNIQUE (registry_version, attribute_key)` |

### Operational

| Table | Owns | Notes |
| --- | --- | --- |
| `projection_runs` | One rebuild: `started_at`, `completed_at`, `attribute_registry_version`, `identity_policy_version`, `row_counts jsonb`, `content_digests jsonb`, `triggered_by` | Deliberately outside every determinism claim. This is where rebuild timestamps live so that no projection has to carry one |

## 4. Cross-milestone foreign keys

M2 references M1 but **never** writes to it.

| M2 column | → M1 table |
| --- | --- |
| `discovery_runs.market_id` / `.vertical_id` / `.icp_id` / `.channel_id` | `markets`, `verticals`, `icps`, `channels` |
| `company_locations.market_id`, `company_market_presences.market_id` | `markets` |
| `company_verticals.vertical_id` | `verticals` |
| `company_claims.value_ref_id` *(for `REFERENCE` attributes)* | `verticals`, `markets` |

No M0/M1 table references any M2 table. The dependency is one-directional by
design — M1 must remain complete and correct with M2 absent, which is what
keeps the density boundary enforceable.

## 5. Changes from revision 2

| Change | Reason |
| --- | --- |
| `companies` **split** into anchor + `company_profiles` | A truncatable projection cannot hold referenced UUIDs |
| `company_claims.company_id` **removed** | Attribution derives through the effective decision, so a corrected resolution never rewrites a claim |
| `company_relationships` **became** `company_relationship_claims` + a projection | Relationships start, end and get corrected |
| `provider_record_bodies` **added** | JSONB is not byte-faithful; bodies must be separable and prunable |
| `attribute_definitions` **added** | A generic `attribute_key` with no contract is untyped EAV |
| `entity_resolution_heads` **demoted** to a cache | The invariant is declarative (indexes + composite FK), not a mutable table |
| `PARENT_OF`, `FRANCHISOR_OF`, `ACQUIRER_OF` **removed** as stored types | Derivable from the canonical direction; storing both risks disagreement |
| `FORMERLY` **removed** from relationship types | It is a name claim, not a relationship |

## 5a. Changes in revision 4

| Change | Reason |
| --- | --- |
| `provider_record_bodies` **1:1 → 1:N**, `raw_body_sha256` moved onto it | Identity is *semantic*, so one version may legitimately have many byte-different bodies. Keeping the byte digest on the version silently asserted the opposite |
| `canonicalization_strategy` + `_version` **added** to providers and versions | M0's canonical-JSON algorithm is not universal; CSV/XML/HTML need their own, declared and versioned |
| `company_profiles.last_projected_at` **removed** | A wall-clock value inside a projection makes byte-identical rebuild impossible by construction |
| `projection_runs` **added** | Somewhere for rebuild telemetry to live that is not a projection |
| `company_relationships` stores **intervals**, not a current snapshot | `now()`-dependent content cannot satisfy the determinism contract |
| `current_company_relationships` **view added** | The date filter belongs in a view, where varying over time is correct |
| **No surrogate keys** in the derived tier | A generated UUID differs on every rebuild |
| `fk_supersedes_same_entity` **added** | Nothing previously stopped entity B superseding entity A's decision |

## 5b. Changes in revision 5

| Change | Reason |
| --- | --- |
| Version identity gains `canonicalization_strategy` + `_version` | A canonical hash is meaningless without the algorithm that produced it. Keying on the hash alone could silently merge two strategies, or fail to create a version on a strategy migration |
| `company_market_presences` gains `effective_from` / `effective_to`, with the interval start in the key | A company must be able to leave a market, and to leave and re-enter |
| `current_company_market_presences` view added | The date predicate belongs in a view |
| `md5(string_agg(...))` demoted to telemetry | The contract is normalized row-set equality; the digest is SHA-256 over canonical row serialization, for drift detection only |

## 6. Tables removed or never created

| Table | Fate | Reasoning |
| --- | --- | --- |
| `provider_records` | Split (rev 2) | Cannot be unique per external id *and* keep a row per payload change |
| `company_evidence` | Replaced by `company_claims` (rev 2) | Recorded which source spoke but not what it said |
| `discovery_results` | Never created | Identical responsibility to `provider_record_versions` |
| `provider_external_ids` | Never created | A two-column unique constraint, not a table |
| `company_aliases` | Never created | Same responsibility as `company_names` |

## 7. Indexes likely required

```
companies                      INDEX  (lifecycle_status) WHERE lifecycle_status <> 'ACTIVE'

provider_entities              UNIQUE (provider_id, provider_external_id)
provider_record_versions       UNIQUE (provider_entity_id, canonicalization_strategy,
                                       canonicalization_version, canonical_payload_hash)
                               INDEX  (discovery_query_id)
                               INDEX  (provider_entity_id, retrieved_at DESC)
provider_record_bodies         UNIQUE (provider_record_version_id, raw_body_sha256)
                               INDEX  (provider_record_version_id, retrieved_at DESC)
provider_record_sightings      UNIQUE (provider_record_version_id, discovery_query_id)

entity_resolution_decisions    PRIMARY KEY (id)
                               uq_decision_entity      UNIQUE (id, provider_entity_id)
                                 -- exists only to serve the composite FK below
                               fk_supersedes_same_entity
                                 FOREIGN KEY (supersedes_decision_id, provider_entity_id)
                                 REFERENCES  entity_resolution_decisions (id, provider_entity_id)
                                 -- makes cross-entity supersession unrepresentable
                               uq_resolution_root
                                 UNIQUE (provider_entity_id) WHERE supersedes_decision_id IS NULL
                               uq_resolution_supersedes
                                 UNIQUE (supersedes_decision_id) WHERE supersedes_decision_id IS NOT NULL
                               -- together: one root + one child each = one linear
                               -- chain per entity = exactly one effective head
                               INDEX  (company_id) WHERE company_id IS NOT NULL
                               INDEX  (decision) WHERE decision = 'AMBIGUOUS'      -- review queue
entity_resolution_candidates   INDEX  (provider_record_version_id)
                               INDEX  (candidate_company_id)
entity_resolution_heads        PRIMARY KEY (provider_entity_id)
                               INDEX  (company_id)

company_claims                 INDEX  (provider_record_version_id)
                               INDEX  (subject_company_id) WHERE subject_company_id IS NOT NULL
                               INDEX  (attribute_key, observed_at DESC)
company_relationship_claims    UNIQUE (supersedes_claim_id) WHERE supersedes_claim_id IS NOT NULL
                               INDEX  (from_company_id, relationship_type)
                               INDEX  (to_company_id, relationship_type)

company_profiles               PRIMARY KEY (company_id)
company_names                  UNIQUE (company_id, name_normalized, name_type)
                               INDEX  gin_trgm_ops (name_normalized)   -- retrieval only
company_domains                UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'
                               INDEX  (domain_normalized)              -- candidate lookup
company_market_presences       PRIMARY KEY (company_id, market_id, presence_type,
                                            effective_from)
                               INDEX  (market_id, presence_type)
                               INDEX  (effective_from, effective_to)
company_verticals              UNIQUE (company_id, vertical_id)
company_relationships          PRIMARY KEY (from_company_id, to_company_id, relationship_type)
                               INDEX  (effective_from, effective_to)   -- view predicate

attribute_definitions          UNIQUE (registry_version, attribute_key)
discovery_runs                 INDEX  (status) WHERE status IN ('PENDING','PARTIAL_FETCH')
```

`pg_trgm` is required for candidate *retrieval*. It is never a decision
mechanism (design §6.5).

## 8. Job queue

Per M2-ADR-010, a PostgreSQL job table consumed with `FOR UPDATE SKIP LOCKED`.
Its shape is left to implementation: it is a queue, not a domain concept, and
nothing in this graph depends on it.
