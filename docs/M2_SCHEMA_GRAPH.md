# M2 — Schema Graph

**Status:** design only. No migrations. This is the proposed table graph with
ownership and cardinality, to be read alongside
[M2_COMPANY_DISCOVERY_DESIGN.md](M2_COMPANY_DISCOVERY_DESIGN.md).

---

## 1. The graph

```
                        ┌──────────────────────┐
                        │ discovery_providers  │  registry + trust tier (config)
                        └──────────┬───────────┘
                                   │ 1
                     ┌─────────────┼─────────────┐
                     │ N                         │ N
          ┌──────────▼─────────┐      ┌──────────▼─────────┐
          │  discovery_runs    │      │ provider_entities  │  provider-side identity
          │  lifecycle state   │      │  UNIQUE(provider,  │  UNIQUE(provider_id,
          └──────────┬─────────┘      │         ext_id)    │         external_id)
                     │ 1              └──────────┬─────────┘
                     │ N                         │ 1
          ┌──────────▼─────────┐                 │ N
          │ discovery_queries  │      ┌──────────▼──────────────┐
          │ params + cursor    │─────▶│ provider_record_versions│  IMMUTABLE
          └──────────┬─────────┘  1:N │ UNIQUE(entity, hash)    │  observations
                     │                └──────────┬──────────────┘
                     │ 1:N                       │ 1
                     │           ┌───────────────┼───────────────┐
          ┌──────────▼───────────▼──┐            │ N             │ N
          │provider_record_sightings│  ┌─────────▼──────────┐    │
          │ re-confirmed, unchanged │  │entity_resolution_  │    │
          └─────────────────────────┘  │     candidates     │    │
                                       └─────────┬──────────┘    │
                                                 │               │
                                       ┌─────────▼───────────────▼──┐
                                       │entity_resolution_decisions │ IMMUTABLE
                                       │ supersedes_decision_id ────┼──┐
                                       │ UNIQUE(supersedes_id)      │◀─┘ self-ref
                                       └─────────┬──────────────────┘
                                                 │ 1
                    ┌────────────────────────────┼────────────────┐
                    │ N                          │ N              │
         ┌──────────▼─────────┐        ┌─────────▼────────┐       │
         │  company_claims    │        │    companies     │◀──────┘
         │  IMMUTABLE         │───────▶│   PROJECTION     │
         │  attribute + value │  N:1   │   mutable        │
         └────────────────────┘        └─────────┬────────┘
                    │                            │ 1
                    │ projects into              │
                    │                ┌───────────┼───────────┬───────────┬──────────┐
                    │                │ N         │ N         │ N         │ N        │ N
                    │      ┌─────────▼──┐ ┌──────▼─────┐ ┌───▼──────┐ ┌──▼───────┐ ┌▼──────────────┐
                    └─────▶│company_    │ │company_    │ │company_  │ │company_  │ │company_       │
                           │  names     │ │  domains   │ │locations │ │verticals │ │market_        │
                           │ PROJECTION │ │ PROJECTION │ │PROJECTION│ │PROJECTION│ │  presences    │
                           └────────────┘ └────────────┘ └──────────┘ └──────────┘ │  PROJECTION   │
                                                                                    └───────────────┘
                           ┌──────────────────────┐
                           │ company_relationships│  company ──▶ company, typed
                           │ IMMUTABLE            │  (subsidiary, franchise, …)
                           └──────────────────────┘
```

## 2. Tables, ownership and cardinality

### Provider layer — append-only

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `discovery_providers` | Provider registry, capabilities, trust tier, active flag | — | `UNIQUE (provider_key)` |
| `discovery_runs` | One execution against one provider in one M1 context, plus lifecycle state | provider 1:N runs | — |
| `discovery_queries` | The exact query issued: params, cursor, page, result count | run 1:N queries | — |
| `provider_entities` | Stable provider-side identity | provider 1:N entities | **`UNIQUE (provider_id, provider_external_id)`** |
| `provider_record_versions` | One immutable observation of an entity | entity 1:N versions · query 1:N versions | **`UNIQUE (provider_entity_id, payload_hash)`** |
| `provider_record_sightings` | "Re-confirmed unchanged on date Y" | version 1:N sightings | `UNIQUE (version_id, query_id)` |

### Decision layer — append-only

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `entity_resolution_candidates` | A considered pairing with its signal vector and score | version 1:N candidates · company 1:N candidates | — |
| `entity_resolution_decisions` | The outcome, with method, rationale, actor, policy version | entity 1:N decisions · self-referencing chain | **`UNIQUE (supersedes_decision_id)`**, `CHECK (id <> supersedes_decision_id)` |

### Claim layer — append-only

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `company_claims` | One atomic assertion: attribute, value, fact type, confidence, provenance | company 1:N · version 1:N · decision 1:N | `INDEX (company_id, attribute_key)` |
| `company_relationships` | Typed company-to-company links | company N:N company | `UNIQUE (from_company_id, to_company_id, relationship_type)` |

### Projection layer — mutable, rebuildable from claims

| Table | Owns | Cardinality | Key constraint |
| --- | --- | --- | --- |
| `companies` | Canonical projection: name, primary domain, status, merge pointer | — | *(no domain uniqueness — see below)* |
| `company_names` | Name variants and their types | company 1:N | `UNIQUE (company_id, name_normalized, name_type)`, trigram index for retrieval |
| `company_domains` | Domains and their roles | company 1:N | **`UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'`** |
| `company_locations` | Physical/registered places only | company 1:N · market 1:N | `INDEX (market_id)` |
| `company_market_presences` | Operating geography | company N:N market | `UNIQUE (company_id, market_id, presence_type)` |
| `company_verticals` | Vertical membership with evidence | company N:N vertical | `UNIQUE (company_id, vertical_id)` |

## 3. Cross-milestone foreign keys

M2 references M1 but **never** writes to it.

| M2 column | → M1 table |
| --- | --- |
| `discovery_runs.market_id` / `.vertical_id` / `.icp_id` / `.channel_id` | `markets`, `verticals`, `icps`, `channels` |
| `company_locations.market_id` | `markets` |
| `company_market_presences.market_id` | `markets` |
| `company_verticals.vertical_id` | `verticals` |

No M2 table is referenced by any M0/M1 table. The dependency is one-directional
by design — M1 must remain complete and correct with M2 absent, which is what
keeps the density boundary (§11 of the design) enforceable.

## 4. Tables removed, and why

| Table | Fate | Reasoning |
| --- | --- | --- |
| `provider_records` | **Split** into `provider_entities` + `provider_record_versions` | One table cannot be unique per external id *and* keep a row per payload change |
| `company_evidence` | **Replaced** by `company_claims` | It recorded which source spoke but not what it said — a strict subset of claims, with worse completeness |
| `discovery_results` | Not created | Identical responsibility to `provider_record_versions` |
| `provider_external_ids` | Not created | A two-column unique constraint on `provider_entities`, not a table |
| `company_aliases` | Not created | Same responsibility as `company_names` |

## 5. Tables added beyond the requested list

Flagged explicitly, with justification and a deferral note.

| Table | Justification | Deferrable? |
| --- | --- | --- |
| `company_relationships` | The identity definition (design §1.3) requires expressing "related but distinct" for holding groups, franchises and acquisitions. Without it, the only way to record a real connection is a merge — which would be wrong | **No.** It is what prevents the franchise false-merge |
| `provider_record_sightings` | M0's confidence algorithm discounts stale evidence, so "last confirmed" is needed; a mutable `last_seen_at` would break append-only | **Yes.** Defer if recency tracking is not needed in the first cut; nothing else depends on it |

## 6. Mutability summary

| Append-only (evidence) | Mutable (projections) |
| --- | --- |
| `provider_entities` | `companies` |
| `provider_record_versions` | `company_names` |
| `provider_record_sightings` | `company_domains` |
| `discovery_queries` | `company_locations` |
| `entity_resolution_candidates` | `company_market_presences` |
| `entity_resolution_decisions` | `company_verticals` |
| `company_claims` | `discovery_runs.status` *(lifecycle only)* |
| `company_relationships` | |

Append-only tables get the `BEFORE UPDATE` rejection trigger already used by
`market_snapshots` and `market_observations` (ADR-018).

**The integrity invariant:** truncating every projection table and recomputing
from `company_claims` + `entity_resolution_decisions` + the identity policy
version must reproduce byte-identical projections. This is an acceptance
scenario, not an aspiration.

## 7. Indexes likely required

```
provider_entities            UNIQUE (provider_id, provider_external_id)
provider_record_versions     UNIQUE (provider_entity_id, payload_hash)
                             INDEX  (discovery_query_id)
                             INDEX  (provider_entity_id, retrieved_at DESC)
provider_record_sightings    UNIQUE (provider_record_version_id, discovery_query_id)
entity_resolution_decisions  UNIQUE (supersedes_decision_id)
                             INDEX  (provider_entity_id, decided_at DESC)
                             INDEX  (company_id) WHERE company_id IS NOT NULL
                             INDEX  (decision) WHERE decision = 'AMBIGUOUS'   -- review queue
entity_resolution_candidates INDEX  (provider_record_version_id)
                             INDEX  (candidate_company_id)
company_claims               INDEX  (company_id, attribute_key, observed_at DESC)
                             INDEX  (provider_record_version_id)
company_names                UNIQUE (company_id, name_normalized, name_type)
                             INDEX  gin_trgm_ops (name_normalized)  -- retrieval only
company_domains              UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'
                             INDEX  (domain_normalized)             -- candidate lookup
company_market_presences     UNIQUE (company_id, market_id, presence_type)
                             INDEX  (market_id, presence_type)
company_verticals            UNIQUE (company_id, vertical_id)
                             INDEX  (vertical_id, fact_type)
company_relationships        UNIQUE (from_company_id, to_company_id, relationship_type)
discovery_runs               INDEX  (status) WHERE status IN ('PENDING','PARTIAL_FETCH')
```

`pg_trgm` is required for candidate *retrieval*. It is never a decision
mechanism (design §6.4).

## 8. Job queue

Per M2-ADR-010, the worker queue is a PostgreSQL table consumed with
`FOR UPDATE SKIP LOCKED` — no Redis, no new infrastructure. Its shape is
deliberately left to implementation; it is a queue, not a domain concept, and
nothing in this graph depends on it.
