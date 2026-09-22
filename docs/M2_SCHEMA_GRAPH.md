# M2 — Schema Graph

**Status:** design only, revision 3. No migrations. Read alongside
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
║                 │ 1:1    │ 1:N    │ 1:N               ║     │
║                 ▼        ▼        ▼                   ║     │
║   provider_record_  provider_   provider_record_      ║     │
║        bodies      record_norm   sightings            ║     │
║     (prunable)     alizations                         ║     │
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
║ DERIVED PROJECTIONS — fully truncatable and rebuildable                   ║
║   company_profiles · company_names · company_domains                      ║
║   company_locations · company_market_presences · company_verticals        ║
║   company_relationships · entity_resolution_heads                         ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

## 2. What can and cannot be truncated

| Tier | Truncatable | Consequence of truncating |
| --- | --- | --- |
| `companies` | **No** | Every claim, decision and relationship claim reaches a company through it. UUIDs are minted by judgement, not computed, so they cannot be regenerated identically |
| Evidence tables | **No** | This *is* the record. Nothing else can reconstruct it |
| `provider_record_bodies` | **Prunable** — by documented retention policy only | Digests, parsed payload and the evidence chain survive; only literal bytes are lost |
| Derived projections | **Yes, entirely** | Rebuilt byte-identically from evidence + registry version + policy version |

### The rebuild invariant

> All **derived projections** may be truncated and rebuilt byte-identically,
> while **identity anchors, claims, decisions and relationship claims are never
> truncated.**

Given fixed evidence, a fixed `attribute_registry_version` and a fixed
`identity_policy_version`, the projection function is deterministic and total.
Asserted by acceptance scenario B3.

## 3. Tables, ownership and cardinality

### Identity anchor

| Table | Owns | Key constraint |
| --- | --- | --- |
| `companies` | Canonical identity and lifecycle only | self-FK `merged_into_company_id`; **no domain uniqueness here** |

### Evidence — append-only

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `discovery_providers` | Registry, capabilities, trust tier, `identity_capability`, `key_fields`, `key_algorithm_version` | — | `UNIQUE (provider_key)` |
| `discovery_runs` | One execution in one M1 context, lifecycle state, `allow_partial_resolution` | provider 1:N | — |
| `discovery_queries` | Exact query, params, cursor, page, result count | run 1:N | — |
| `provider_entities` | Provider-side identity, `external_id_kind`, `identity_collision` | provider 1:N | **`UNIQUE (provider_id, provider_external_id)`** |
| `provider_record_versions` | Immutable observation: `parsed_payload`, `canonical_payload_hash`, `raw_body_sha256`, `retrieved_at` | entity 1:N · query 1:N | **`UNIQUE (provider_entity_id, canonical_payload_hash)`** |
| `provider_record_bodies` | Literal response bytes | version 1:1 | PK `provider_record_version_id` |
| `provider_record_normalizations` | Normalizer output per version | version 1:N | `UNIQUE (version_id, normalizer_version)` |
| `provider_record_sightings` | "Re-confirmed unchanged on date Y" | version 1:N | `UNIQUE (version_id, discovery_query_id)` |
| `entity_resolution_candidates` | A considered pairing with signals and score | version 1:N · company 1:N | — |
| `entity_resolution_decisions` | Outcome, method, rationale, actor, policy version | entity 1:N, **linear chain** | `uq_resolution_root` + `uq_resolution_supersedes` — see §7. Together they force exactly one effective head (M2-ADR-018) |
| `company_claims` | One typed assertion against the registry | version 1:N *or* company 1:N | `CHECK` exactly one attribution path |
| `company_relationship_claims` | Typed, time-bounded relationship assertion | company N:N | `UNIQUE (supersedes_claim_id)`; `CHECK (from < to)` for `SISTER_OF` |

### Derived projections — truncatable

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `company_profiles` | Canonical business attributes, `derived_from_claim_ids`, `projection_conflict` | company 1:1 | PK `company_id` |
| `company_names` | Name variants and types | company 1:N | `UNIQUE (company_id, name_normalized, name_type)`; trigram index for retrieval |
| `company_domains` | Domains and roles | company 1:N | **`UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'`** |
| `company_locations` | Physical/registered places only | company 1:N · market 1:N | `INDEX (market_id)` |
| `company_market_presences` | Operating geography | company N:N market | `UNIQUE (company_id, market_id, presence_type)` |
| `company_verticals` | Vertical membership with evidence | company N:N vertical | `UNIQUE (company_id, vertical_id)` |
| `company_relationships` | Effective, non-retracted, currently-valid relationships | company N:N | `UNIQUE (from_company_id, to_company_id, relationship_type)` |
| `entity_resolution_heads` | O(1) head lookup cache | entity 1:1 | PK `provider_entity_id` |

### Registry

| Table | Owns | Notes |
| --- | --- | --- |
| `attribute_definitions` | The versioned attribute contract (design §5.2) | Configuration tier: seeded from code as M0 seeds `scoring_models`, rebuildable from that seed. Claims reference the *version*, not the row. `UNIQUE (registry_version, attribute_key)` |

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
| `entity_resolution_heads` **demoted** to a cache | The invariant is now declarative (two partial unique indexes), not a mutable table |
| `PARENT_OF`, `FRANCHISOR_OF`, `ACQUIRER_OF` **removed** as stored types | Derivable from the canonical direction; storing both risks disagreement |
| `FORMERLY` **removed** from relationship types | It is a name claim, not a relationship |

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
                               INDEX  (identity_collision) WHERE identity_collision
provider_record_versions       UNIQUE (provider_entity_id, canonical_payload_hash)
                               INDEX  (discovery_query_id)
                               INDEX  (provider_entity_id, retrieved_at DESC)
provider_record_sightings      UNIQUE (provider_record_version_id, discovery_query_id)

entity_resolution_decisions    uq_resolution_root
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
company_market_presences       UNIQUE (company_id, market_id, presence_type)
                               INDEX  (market_id, presence_type)
company_verticals              UNIQUE (company_id, vertical_id)
company_relationships          UNIQUE (from_company_id, to_company_id, relationship_type)

attribute_definitions          UNIQUE (registry_version, attribute_key)
discovery_runs                 INDEX  (status) WHERE status IN ('PENDING','PARTIAL_FETCH')
```

`pg_trgm` is required for candidate *retrieval*. It is never a decision
mechanism (design §6.5).

## 8. Job queue

Per M2-ADR-010, a PostgreSQL job table consumed with `FOR UPDATE SKIP LOCKED`.
Its shape is left to implementation: it is a queue, not a domain concept, and
nothing in this graph depends on it.
