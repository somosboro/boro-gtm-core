# M2 — Company Discovery and Entity Resolution

**Status:** design only, revision 2. No M2 code, migrations or tables exist in
this repository, and none should be created from this document without a
separate implementation authorization.

**Revision 2** corrects internal contradictions found in review. The changes are
substantive, not cosmetic — see [§0](#0-what-changed-in-revision-2).

**Milestone position:** M2 sits between M1 (contextual market intelligence) and
M3 (operational research). M1 answers *"which market × vertical × ICP × channel
× ticket contexts are worth pursuing?"* M2 answers *"which real commercial
organizations exist in a chosen context, and are we sure they are distinct?"*

M2 is **not** "scrape companies". Scraping is one possible provider
implementation. M2 is the canonical-identity problem: given noisy, overlapping,
partially wrong records from several providers, maintain a defensible registry
of commercial organizations with traceable evidence for every claim.

---

## 0. What changed in revision 2

| # | Contradiction in revision 1 | Correction |
| --- | --- | --- |
| 1 | "Company" was undefined, and domain equality silently merged legally distinct organizations | Explicit identity unit: **commercial operating organization** (§1), with a domain policy that no longer auto-merges (§8) |
| 2 | `provider_records` was `UNIQUE (provider_id, external_id)` *and* had to keep a new row per changed payload — impossible | Split into `provider_entities` (identity) and `provider_record_versions` (immutable observations) (§4.1) |
| 3 | Immutable raw records carried mutable `resolved_company_id` / `resolution_decision_id` | Resolution state removed from raw records entirely; it lives only in decisions (§4.1, §6) |
| 4 | Decisions were "append-only" but received `superseded_by_id` — an UPDATE | Direction reversed: `new.supersedes_decision_id = old.id`. Old rows are never touched (§6.2) |
| 5 | `company_evidence` recorded *which source*, never *what it said* | Replaced by `company_claims`, holding the normalized value; `companies` becomes a derived projection (§5) |
| 6 | `SERVICE_AREA` was a physical location type | Removed. Locations are physical/registered only; operating geography is presence (§5.6) |
| 7 | "No canonical writes on provider outage" vs "independently retryable stages" | Explicit run lifecycle and per-stage transaction boundaries (§7) |
| 8 | A universal `UNIQUE (primary_domain)` assumption | Replaced by an explicit domain-identity policy and a narrower constraint (§8) |
| 9 | Redis assumed without evaluation | Evaluated against PostgreSQL `FOR UPDATE SKIP LOCKED`; PostgreSQL chosen (§10, M2-ADR-010) |
| — | `provider_records`, `company_evidence` | Removed as tables; responsibilities reassigned (§4.1, §5.5) |

---

## 1. The canonical identity unit

### 1.1 Definition

> A **company** is a *canonical commercial operating organization* — the unit
> BoRo would sell to, contract with and deliver to as **one account**.

It is explicitly **not** necessarily one legal entity, and not one provider
record, and not one domain.

The operational test, applied in this order:

1. **Commercial autonomy.** Does it make its own buying decision for an
   operations project? If a regional unit cannot sign, it is not a company.
2. **Operational coherence.** Does it run one operation — its own work orders,
   dispatch, technicians, service contracts?
3. **Account identity.** Would two BoRo reps working these separately be
   duplicating each other, or covering genuinely different accounts?

If all three say "one", it is one company, regardless of how many legal
entities, domains or provider records sit underneath.

### 1.2 Why not the legal entity

A legal entity is a *jurisdictional artifact*. A single German operating
business may hold a GmbH plus a property-holding entity plus a dormant
predecessor. Targeting the legal entity produces three "companies" where one
account exists. Conversely, one legal entity may run two genuinely separate
businesses.

Legal entities are still recorded — as `company_claims` with
`attribute_key = 'legal_entity'` and registry identifiers — but they are
*evidence about* a company, not the identity of one.

### 1.3 Implications by structure

| Structure | Modelling | Reasoning |
| --- | --- | --- |
| **Branch / depot** | **Same company.** A `company_locations` row plus a `company_market_presences` row | A branch has no independent buying authority |
| **Subsidiary, operationally autonomous** | **Separate company**, linked by `company_relationships (SUBSIDIARY_OF)` | It buys, contracts and delivers on its own |
| **Subsidiary, administratively controlled** | **Same company**, recorded as a `legal_entity` claim | Signing happens at the parent |
| **Holding group** | **Separate company only if it is itself a target.** Usually it is not; it exists as a `PARENT_OF` relationship node | A holding company has no field operations to re-architect |
| **Franchise — franchisee** | **Separate company**, `FRANCHISE_OF` the franchisor | Each franchisee buys independently; this is the single most common false-merge trap |
| **Franchise — franchisor** | **Separate company** | Sells to franchisees; a different account and a different motion |
| **Multiple legal entities on one domain** | **Depends on the test above.** Domain equality is a *candidate* signal, never an automatic merge (§8) | A group domain covers many autonomous businesses |
| **Regional operating entity** | **Separate company** if it holds its own P&L and buying authority; otherwise a presence | "Acme UK" vs "Acme DACH" are usually separate accounts |

### 1.4 `company_relationships`

The identity definition is unusable without a way to say *"these are related but
distinct"* — that is precisely what prevents a bad merge from being the only
way to express a real connection.

`company_relationships`: `from_company_id`, `to_company_id`,
`relationship_type ∈ {SUBSIDIARY_OF, PARENT_OF, FRANCHISE_OF, FRANCHISOR_OF,
SISTER_OF, ACQUIRED_BY, FORMERLY}`, `confidence`, `claim_id`, `created_at`.

Append-only, with `UNIQUE (from_company_id, to_company_id, relationship_type)`.

This is an addition beyond the table list in the correction brief. It is
justified by §1.3: without it, holding groups and franchises can only be
represented by merging or by silent duplication, and both are wrong.

### 1.5 Identity policy is explicit and versioned

The rules above are **configuration**, stored as a versioned
`identity_policy_version` referenced by every resolution decision. Changing the
identity policy is therefore a decision with a date, not a silent behaviour
change — the same discipline as `scoring_models.version` in M0.

---

## 2. Pipeline

```
Discovery Request          M1 context: market × vertical × ICP × channel × ticket
   ↓
Provider Adapter           provider-specific I/O, isolated
   ↓
Provider Entity            stable provider-side identity (provider + external id)
   ↓
Provider Record Version    immutable observation: payload + hash + retrieved_at
   ↓
Normalization              pure function: provider schema → candidate shape
   ↓
Candidate Matching         retrieve plausible existing companies
   ↓
Resolution Decision        append-only, explainable, supersedable
   ↓
Company Claims             what was asserted, by whom, when — immutable
   ↓
Canonical Projections      companies / names / domains / locations /
                           presences / verticals — derived, rebuildable
```

Every arrow attaches provenance. No arrow discards it.

## 3. Layer separation

Three layers, with different mutability rules:

| Layer | Tables | Mutability |
| --- | --- | --- |
| **Provider (raw)** | `provider_entities`, `provider_record_versions`, `provider_record_sightings`, `discovery_runs`, `discovery_queries` | Append-only |
| **Decision** | `entity_resolution_candidates`, `entity_resolution_decisions` | Append-only |
| **Claim (evidence)** | `company_claims`, `company_relationships` | Append-only |
| **Projection (canonical)** | `companies`, `company_names`, `company_domains`, `company_locations`, `company_market_presences`, `company_verticals` | Mutable, and **fully rebuildable** from claims + decisions |

The load-bearing invariant: **every mutable row is derivable. Nothing that
constitutes evidence is ever mutated.** Deleting all projections and rebuilding
from claims must produce the identical result.

## 4. Provider layer

### 4.1 Identity and versions, separated

Revision 1 required one table to be simultaneously unique per external id and
to grow a row per payload change. That is impossible. Split:

**`provider_entities`** — the stable provider-side identity.

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `provider_id` → `discovery_providers` | |
| `provider_external_id text` | The provider's own key |
| `first_seen_at timestamptz` | |
| `UNIQUE (provider_id, provider_external_id)` | The identity constraint |

**`provider_record_versions`** — immutable observations of that entity.

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `provider_entity_id` → `provider_entities` | |
| `payload_hash char(64)` | SHA-256 of the canonical payload |
| `raw_payload jsonb` | Verbatim |
| `normalized_payload jsonb null` | Output of the pure normalizer; NULL until normalized |
| `normalizer_version text null` | Which normalizer produced it |
| `source_url text null` | |
| `retrieved_at timestamptz` | When this content was first seen |
| `discovery_query_id` → `discovery_queries` | Which query surfaced it first |
| `UNIQUE (provider_entity_id, payload_hash)` | Idempotency |

Behaviour:

* **Identical payload → no new version.** The unique constraint plus
  `ON CONFLICT DO NOTHING` makes re-ingestion a genuine no-op.
* **Changed payload → new version, same entity.** History accumulates; nothing
  is overwritten.
* **No resolution state lives here.** There is no `resolved_company_id` and no
  `resolution_decision_id`. Correcting a resolution must never require touching
  raw evidence (§6).

`normalized_payload` is the one column written after insert, exactly once,
by the normalize stage. Two honest options, decided at implementation time:
either relax the append-only trigger to permit the single NULL → value
transition, or move normalization into its own
`provider_record_normalizations` table keyed by
`(version_id, normalizer_version)`. **The second is preferred** — it keeps the
raw table strictly immutable and lets a normalizer upgrade re-derive without
destroying the previous output. Recorded as an open question in §13.

**`provider_record_sightings`** — "the provider re-confirmed this, unchanged, on
date Y".

`provider_record_version_id`, `discovery_query_id`, `seen_at`. Append-only.

Also an addition beyond the brief's list. Justified: M0's confidence algorithm
already discounts stale evidence, so "last confirmed" is needed — and a mutable
`last_seen_at` column would violate append-only. If recency tracking is
deferred, this table can be too; nothing else depends on it.

### 4.2 Runs and queries

**`discovery_runs`** — one execution. Carries `provider_id`, the M1 context as
real FKs (`market_id`, `vertical_id`, `icp_id`, `channel_id` — the ADR-014
pattern), `status` (§7), `adapter_version`, timing, cost, error.

**`discovery_queries`** — the exact query issued: parameters, pagination cursor,
page number, result count. One run has many queries; reproducibility lives here.

### 4.3 Provider abstraction

```
ProviderAdapter
  .capabilities()  -> supported filters, geography granularity, rate limits
  .search(query)   -> Iterator[RawRecord]      # I/O, may fail or partially fail
  .normalize(raw)  -> CandidateCompany         # pure, deterministic, testable
```

1. **Adapters never write canonical tables.** They emit raw records. Only the
   resolution service writes companies.
2. **`normalize` is pure** — no I/O, no database. Testable from fixtures with
   zero network, preserving the existing M0/M1 discipline.
3. **Raw payloads are append-only and hashed**, like `market_snapshots`.
4. **Provider trust is versioned configuration** on `discovery_providers`.

## 5. Claims — the evidence model

### 5.1 The problem with revision 1

`company_evidence` recorded *that* provider A said something about
`employee_count`, but not *what it said*. It could not answer the question it
existed to answer.

### 5.2 `company_claims`

One row per atomic assertion. Append-only. The source of truth for everything
canonical.

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `company_id` → `companies` | Which company this is asserted about |
| `attribute_key text` | `employee_count`, `primary_domain`, `legal_name`, `vertical`, `market_presence`, `location`, `legal_entity`, … |
| `value_jsonb jsonb` | The normalized claimed value |
| `value_numeric numeric null` | Typed shadow for numeric attributes, for indexing and range queries |
| `value_text text null` | Typed shadow for scalar text attributes |
| `unit text null` | |
| `fact_type text null` | M0 vocabulary: FACT / PROXY / ESTIMATE / INFERENCE / HYPOTHESIS |
| `availability text` | `OBSERVED` / `NOT_AVAILABLE`, reusing M0's semantics |
| `confidence numeric null` | From M0's single confidence algorithm |
| `provider_record_version_id` → `provider_record_versions` | Exactly which observation asserted it |
| `resolution_decision_id` → `entity_resolution_decisions` | Which decision attached it to this company |
| `observed_at date null` | Only when the provider states a date |
| `period_granularity text` | `DATE` / `YEAR` / `SNAPSHOT` / `UNDATED` — M0's vocabulary |
| `created_at timestamptz` | Ingest time |

The same CHECK constraints M0 uses apply: `availability = NOT_AVAILABLE` implies
NULL value and NULL fact type; `observed_at` present **iff** granularity is
`DATE`. Unknown is never zero, and a year is never widened into a date.

This answers the required question directly:

```sql
SELECT p.provider_id, c.value_numeric, c.observed_at, c.fact_type, c.confidence
FROM company_claims c
JOIN provider_record_versions v ON v.id = c.provider_record_version_id
JOIN provider_entities p        ON p.id = v.provider_entity_id
WHERE c.company_id = $1 AND c.attribute_key = 'employee_count'
ORDER BY c.observed_at DESC NULLS LAST, c.created_at DESC;
```

> Provider A said 40 on 2026-03-01 · Provider A said 45 on 2026-07-01 ·
> Provider B said 42 on 2026-06-15

All three rows persist. None overwrites another.

### 5.3 Projection rules

`companies` and the other canonical tables are **mutable projections** computed
from claims by a deterministic, documented function.

Precedence for a single-valued attribute, applied in order:

1. **Human review wins.** A claim whose decision method is `HUMAN_REVIEW`
   outranks everything.
2. **Fact type.** FACT > PROXY > ESTIMATE > INFERENCE > HYPOTHESIS.
3. **Provider trust tier**, from versioned `discovery_providers` configuration.
4. **Recency.** `observed_at` first, then `retrieved_at`.
5. **Stability tie-break.** If still tied, keep the current projected value and
   set `projection_conflict = true` on the projection row.

For **range-valued** attributes (`employee_count_min/max`, revenue bands), do
**not** pick a winner among equally-ranked claims. Project the envelope — min of
mins, max of maxes — and record the contributing claim ids. Providers
disagreeing is information, not noise, and collapsing it to one number would be
the same error as collapsing score and coverage.

A projection row records `derived_from_claim_ids`, so any canonical value is one
join from its justification.

### 5.4 Conflict handling and rebuild

* Projection is a **pure function of claims + decisions + policy version**.
* Rebuilding is always legal: truncate projections, recompute, get identical
  output. This is the integrity test, and an acceptance scenario.
* A **claim is never edited to fix a projection.** A wrong projection means the
  precedence rules or the provider trust tier is wrong; both are versioned
  configuration.
* A retracted claim is expressed as a new claim with
  `availability = NOT_AVAILABLE` and a superseding decision — never a delete.

### 5.5 What `company_claims` replaces

`company_evidence` is **removed**. Its responsibility — linking an attribute to
its source — is a strict subset of `company_claims`, which also carries the
value. Keeping both would mean two tables answering "where did this come from?"
with different completeness.

### 5.6 Locations vs presence — corrected

**`company_locations`** — physical or registered places only:

`location_type ∈ {HEADQUARTERS, BRANCH, DEPOT, REGISTERED_OFFICE}`

`SERVICE_AREA` is **removed**. A service area is not a place; treating it as one
fabricates buildings from coverage statements, which is exactly the class of
invention M0's provenance rules exist to prevent.

**`company_market_presences`** — operating geography:

`presence_type ∈ {HEADQUARTERED, BRANCH, OPERATES, SERVES_REMOTELY}`

A location implies a presence; a presence does **not** imply a location. A
Belfast contractor serving Ireland has an IE presence of type `SERVES_REMOTELY`
and **no** Irish location row.

Sub-national coverage ("serves Bavaria") has no home in this model; M1 markets
are national. Recorded as an open question in §13 rather than forced into a
location row.

### 5.7 The other projections

* **`company_names`** — `name_raw`, `name_normalized`, `name_type ∈ {LEGAL,
  TRADING, FORMER, LOCALIZED, PROVIDER_DISPLAY}`, `is_primary`, derived from
  `legal_name` / `trading_name` claims. Normalization is stored *alongside* the
  raw name, never instead of it.
* **`company_domains`** — `domain_normalized`, `domain_role ∈ {IDENTITY,
  ALTERNATE, REDIRECT, COUNTRY_TLD, DEFUNCT, GROUP}`, `verified_at` (§8).
* **`company_verticals`** — `vertical_id` → M1 `verticals`,
  `classification_method`, `fact_type`, `confidence`, `is_primary`. A
  keyword-inferred vertical is `INFERENCE`, never `FACT`.

## 6. Entity resolution

### 6.1 Candidates

`entity_resolution_candidates`: `provider_record_version_id`,
`candidate_company_id`, `match_signals jsonb`, `score numeric`, `tier`,
`created_at`. Append-only. Retained even when rejected — a rejected candidate is
evidence about *why* a decision was made.

### 6.2 Decisions, and supersession in the correct direction

Revision 1 claimed append-only decisions while writing `superseded_by_id` onto
old rows. Reversed:

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `provider_entity_id` → `provider_entities` | What is being resolved |
| `provider_record_version_id` → `provider_record_versions` | Which observation triggered this decision |
| `company_id uuid null` | The resolved company; NULL for `AMBIGUOUS` / `REJECTED` |
| `decision text` | `MATCHED` / `CREATED_NEW` / `AMBIGUOUS` / `REJECTED` / `MERGED` / `SPLIT` |
| `method text` | `DETERMINISTIC` / `CANDIDATE_AUTO` / `HUMAN_REVIEW` |
| **`supersedes_decision_id uuid null`** | **Points backwards** to the decision this replaces |
| `identity_policy_version text` | Which policy was in force |
| `signals jsonb` | The full signal vector |
| `rationale text` | |
| `decided_by text` | Actor or `system:<component>` |
| `decided_at timestamptz` | |

Constraints:

* `UNIQUE (supersedes_decision_id)` — a decision can be superseded at most once,
  which prevents forked history.
* `CHECK (id <> supersedes_decision_id)`.
* Append-only trigger, as with M0's evidence tables. **No row is ever updated.**

### 6.3 Querying effective resolution

The current resolution for a provider entity is the latest decision that nothing
supersedes:

```sql
SELECT d.*
FROM entity_resolution_decisions d
WHERE d.provider_entity_id = $1
  AND NOT EXISTS (
        SELECT 1 FROM entity_resolution_decisions s
        WHERE s.supersedes_decision_id = d.id
      )
ORDER BY d.decided_at DESC
LIMIT 1;
```

Exposed as a view, `current_entity_resolutions`, with a partial index on
`supersedes_decision_id` to keep the anti-join cheap. If the chain ever grows
long enough to matter, the view becomes materialized and is refreshed by the
resolve stage — a performance change with no semantic change.

### 6.4 Three tiers of authority

**Deterministic — may auto-match.**
* Exact verified national registry identifier
* An existing non-superseded decision for the same `provider_entity_id`
* Exact `IDENTITY`-role domain match **that passes the domain policy** (§8)

**Candidate — scored, then gated.**

| Signal | Indicative weight |
| --- | --- |
| Normalized legal name equality within same market | high |
| Normalized name + same city/postal code | high |
| Shared phone number | medium |
| Shared normalized address | medium |
| Shared `GROUP`-role domain | low — presence of a group, not identity |
| Fuzzy name similarity (trigram) | low |
| Same vertical + same market | very low, tie-break only |

Two configured thresholds: above `auto_match_threshold` →
`CANDIDATE_AUTO`, with the full signal vector persisted. Between the two →
`AMBIGUOUS`, which creates nothing and merges nothing. Below → `CREATED_NEW`.

**Fuzzy similarity alone never merges.** Trigram similarity is a *retrieval*
mechanism, never a *decision* mechanism. Without a corroborating domain,
address or identifier, the honest answer is `AMBIGUOUS` — the same discipline
that keeps M1 coverage honest instead of inventing vertical density.

### 6.5 Merges and splits without touching evidence

A merge writes a `MERGED` decision and updates **projections only**:
`companies.status = 'MERGED'`, `merged_into_company_id` set. Claims and provider
versions are untouched; claims are re-projected onto the survivor.

Reversal writes a `SPLIT` decision whose `supersedes_decision_id` points at the
merge. Projections are rebuilt. **No raw evidence is rewritten in either
direction**, which is what makes the reversal safe.

## 7. Run lifecycle and transaction semantics

### 7.1 States

```
PENDING → FETCHING → ┬→ FETCHED ──→ NORMALIZING → RESOLVING → COMPLETED
                     ├→ PARTIAL_FETCH ─┘ (only if allow_partial_resolution)
                     └→ FAILED
```

| State | Meaning |
| --- | --- |
| `PENDING` | Requested, not started |
| `FETCHING` | Provider calls in progress |
| `PARTIAL_FETCH` | Provider failed mid-run; some raw versions were persisted |
| `FETCHED` | All expected pages retrieved |
| `NORMALIZING` | Pure normalization over persisted versions |
| `RESOLVING` | Decisions and projections being written |
| `COMPLETED` | Terminal success |
| `FAILED` | Terminal failure; raw evidence retained |

`PARTIAL_FETCH` and `FAILED` are distinct: partial means usable raw evidence
exists, failed means the run produced nothing trustworthy. Eight states is the
minimum that keeps "raw persisted but not resolved" expressible; collapsing
`PARTIAL_FETCH` into `FAILED` would lose exactly the distinction that makes
retry safe.

### 7.2 Transaction boundaries

| Stage | Transaction | On failure |
| --- | --- | --- |
| Fetch, per page | One transaction per page: insert versions + sightings + query row, commit | Committed pages survive; run → `PARTIAL_FETCH` |
| Normalize, per version | One transaction per version | Other versions unaffected; unnormalizable version flagged |
| Resolve, per provider entity | One short transaction: candidates + decision + claims + projection update | That entity is skipped; others proceed |

Fetch commits incrementally **on purpose**: raw evidence already paid for must
not be lost to a later failure. Resolve is per-entity and short, so the
canonical registry is never left half-written for a single company.

### 7.3 The policy, stated unambiguously

> **A run that has not reached `FETCHED` does not write to the canonical
> registry**, unless the run was explicitly created with
> `allow_partial_resolution = true`.

This resolves revision 1's contradiction. Both halves are now true:

* raw provider evidence from a partial fetch **is** persisted and retained;
* canonical companies, claims and projections are **not** touched by an
  incomplete fetch, because a truncated result set is not evidence of absence —
  and treating it as such would let a provider outage silently shrink a market.

`allow_partial_resolution` exists for the legitimate case of a very large run
where partial results are still worth resolving. It is opt-in, recorded on the
run, and visible in the API.

### 7.4 Retry

Retry is **resumable, not restarting**. A `PARTIAL_FETCH` run retries from the
last recorded `discovery_queries` cursor. Already-persisted versions are
re-observed as sightings, not duplicated, because the payload hash is unchanged.
Normalize and resolve can be re-run independently over stored versions without
re-paying the provider.

## 8. Domain identity policy

### 8.1 Normalization

Lowercase · strip trailing dot · IDN → punycode · strip `www.` · reduce to the
**registrable domain** (eTLD+1) using the Public Suffix List. `shop.acme.co.uk`
and `www.acme.co.uk` both normalize to `acme.co.uk`. Subdomains are **never**
identity on their own.

### 8.2 Why universal uniqueness is wrong

* **Shared corporate domains.** A group runs `group.com` across eight autonomous
  operating companies. Unique-per-domain would force seven bad merges.
* **Subsidiaries on the parent domain** — same problem.
* **Free and hosting domains.** `wixsite.com`, `business.site`, marketplace
  profile domains — thousands of unrelated firms share these. Blocklisted:
  never identity, never even a candidate signal.
* **Domain changes.** A rebrand moves `old.de` → `new.de`. Both belong to one
  company, across time.
* **Redirects.** A redirect may mean "same company, new domain" *or* "acquired,
  now points at the acquirer" — which is a `company_relationships
  (ACQUIRED_BY)`, **not** a merge.
* **Acquisitions.** Two companies that genuinely existed remain two companies
  with a relationship and a date. Merging them destroys history that the M1
  context may still need.

### 8.3 The three-way test

A domain match is:

| Strength | Conditions |
| --- | --- |
| **Deterministic** (may auto-match) | Registrable domain, `domain_role = IDENTITY`, not on the generic/hosting blocklist, not flagged `GROUP`, **and** no conflicting strong signal (different verified registry IDs, or different countries with separately-registered legal entities) |
| **Strong candidate** (scored, may reach `AMBIGUOUS`) | Domain shared but marked `GROUP`, or names/locations materially differ, or the match is on a subdomain |
| **Insufficient** (contributes nothing) | Blocklisted generic/hosting domain, parked domain, or a marketplace profile URL |

### 8.4 The constraint, corrected

The revision-1 `UNIQUE (primary_domain) ON companies` is **removed**. In its
place:

```
company_domains  UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'
```

A company may hold many domains; at most one company may claim a given domain
as its *identity*. Group and alternate domains are freely shared. This keeps the
race protection that made deterministic matching safe (§9.2) without asserting
that every domain belongs to exactly one organization.

`companies.primary_domain` remains as a **projection** of the `IDENTITY` row,
for convenience — not as a constraint.

## 9. Concurrency and idempotency

### 9.1 Idempotency

| Event | Mechanism | Result |
| --- | --- | --- |
| Same external id, identical payload | `UNIQUE (provider_entity_id, payload_hash)` + `ON CONFLICT DO NOTHING` | No new version; a sighting is recorded |
| Same external id, changed payload | Same constraint, different hash | New immutable version, same entity |
| Same entity resolved again, unchanged | Existing non-superseded decision found | No new decision |
| Re-run of a completed run | Runs are immutable | New run, diffable against the old |

### 9.2 Concurrency

Preferred mechanisms, in order:

1. `UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'` — the database
   arbitrates the deterministic case.
2. `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`, re-read on conflict — the
   pattern already proven under a real two-connection race by M1's research-gap
   detector.
3. For the non-domain case, a transactional advisory lock keyed on
   `hash(normalized_name, market_id)`, held only for the resolution decision.

No distributed lock service. PostgreSQL constraints plus short transactions are
sufficient at this scale and are the established house pattern (ADR-001).

## 10. Worker architecture

Revision 1 assumed Redis without evaluating it. Evaluated:

| | PostgreSQL `FOR UPDATE SKIP LOCKED` | Redis + worker |
| --- | --- | --- |
| New infrastructure | None — PostgreSQL is already required | A second datastore to run, monitor, back up |
| Transactional with writes | Yes — enqueue and domain write share one transaction | No — dual-write, needs outbox to be correct |
| Job durability | ACID by construction | Needs persistence configuration |
| Visibility | Plain SQL over a table | Separate tooling |
| Throughput ceiling | Thousands/sec — orders of magnitude above M2's need | Much higher, unneeded |
| Operational burden for one developer | Low | Meaningfully higher |

**Decision: PostgreSQL `FOR UPDATE SKIP LOCKED`.** Recorded as M2-ADR-010.

M2's real workload is tens to low thousands of provider calls per run, bounded
by provider rate limits — nowhere near a queue's throughput limits. The
transactional advantage is the decisive one: a discovery job that enqueues
follow-up work in the same transaction as its raw writes cannot drift out of
sync, which with Redis would require an outbox pattern to achieve.

Revisit if and only if: sustained throughput exceeds a few thousand jobs/second,
or fan-out/pub-sub delivery becomes a requirement, or workers must run where
PostgreSQL is not reachable. None applies at M2.

## 11. The M1 ↔ M2 boundary — discovery counts are not density

Unchanged from revision 1, and still the most important constraint here.

If provider A returns 2,400 US HVAC companies and 800 German ones, that does
**not** establish that the US has three times the density. It establishes that
*this provider, with this query, on this date, indexed those counts*. Coverage,
indexing depth, language handling and query phrasing all bias the result —
typically toward large English-language markets, precisely the direction that
would flatter a US-first conclusion.

1. A discovery count **never** writes to `market_observations` automatically.
2. Promotion enters as `fact_type = PROXY` — never `FACT` — carrying provider,
   adapter version, exact query definition, `retrieved_at`, coverage assumption,
   calibration (if established against an independent register, itself
   evidenced) and a confidence from M0's algorithm.
3. Promotion is explicit, human-authorised, logged and reversible.
4. Uncalibrated counts support within-provider, within-market trends only.
5. Until calibrated, the honest M1 representation of vertical density remains
   what it is today: **a research gap**.

A `market_density_proxies` table keeps provider-derived estimates in their own
space with their own methodology fields, requiring a deliberate promotion step
before anything reaches `market_observations`.

## 12. API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/discovery-providers` | Registry and capabilities |
| `POST` | `/discovery-runs` | Start a run for an M1 context |
| `GET` | `/discovery-runs[/{id}]` | Status, lifecycle state, counts, cost |
| `POST` | `/discovery-runs/{id}/retry` | Resume from the last cursor |
| `GET` | `/discovery-runs/{id}/queries` | Exact queries and cursors |
| `GET` | `/provider-entities/{id}/versions` | Full observation history |
| `GET` | `/companies` | Filter by market, vertical, presence, status |
| `GET` | `/companies/{id}` | Canonical projection |
| `GET` | `/companies/{id}/claims` | Every claim, with provider, value and date |
| `GET` | `/companies/{id}/claims?attribute_key=employee_count` | The claim history for one attribute |
| `GET` | `/companies/{id}/locations` | Physical/registered only |
| `GET` | `/companies/{id}/market-presences` | Operating geography |
| `GET` | `/companies/{id}/verticals` | With `fact_type` and confidence |
| `GET` | `/companies/{id}/relationships` | Parent, franchise, acquisition links |
| `GET` | `/entity-resolution/decisions` | Filter by decision, method, run |
| `GET` | `/entity-resolution/decisions/{id}/chain` | Full supersession chain |
| `GET` | `/entity-resolution/ambiguous` | The human-review queue |
| `POST` | `/entity-resolution/decisions` | Record a human decision (supersedes by id) |

New error codes: `PROVIDER_UNAVAILABLE`, `RESOLUTION_AMBIGUOUS`,
`IDENTITY_POLICY_CONFLICT`, `MERGE_CONFLICT`, `PARTIAL_FETCH_NOT_RESOLVABLE`.

## 13. Open design questions

Genuinely unresolved, listed rather than papered over:

1. **Normalized payload storage.** Column on the immutable version (requiring a
   narrow append-only exception) versus a separate
   `provider_record_normalizations` table. The separate table is preferred; the
   cost is one more join on a hot path.
2. **Sub-national service coverage.** "Serves Bavaria" has no home. M1 markets
   are national. Options: a `company_service_areas` table keyed to a geography
   vocabulary M1 does not yet have, or deferring to M3.
3. **Projection rebuild cost.** Full rebuild is the integrity guarantee, but at
   scale it needs to be incremental. Trigger-based or job-based incremental
   projection is undesigned.
4. **Identity policy migration.** When the policy version changes, existing
   decisions were made under the old one. Re-resolving everything is expensive;
   leaving it is inconsistent. A migration strategy is undesigned.
5. **Provider trust calibration.** Trust tiers are currently a judgement. Making
   them evidence-based requires a labelled corpus of human decisions — which
   M2 will produce but does not yet have.
6. **Cross-provider claim conflicts at equal rank.** The envelope rule handles
   ranges; genuinely contradictory scalars (two different registry IDs) fall to
   the stability tie-break and a conflict flag. Whether that should instead force
   `AMBIGUOUS` is unresolved.

## 14. How M3 consumes M2

M3 (Operational Research) takes a canonical company and gathers operational
evidence — fleet size, service contracts, dispatch tooling, hiring signals. It
depends on M2 for a **stable company id**, **market presence**, **vertical
association with confidence**, **provider versions** as seed URLs, and an
explicit **ambiguity flag** so research effort is not spent on possible
duplicates.

M3 attaches its findings as `company_claims` with the same structure — which is
why claims are generic over `attribute_key` rather than enumerating M2 columns.

## 15. Explicit non-goals for M2

* No people, buyers, contacts or email discovery — M5.
* No outbound, sequences or campaigns — M6.
* No CRM synchronisation.
* No scoring of companies — qualification is M4, and it will follow the same
  score/confidence/coverage discipline.
* No automatic promotion of discovery counts into M1 (§11).
* No web scraping of company websites — that is M3; M2 consumes structured
  provider output.
* No machine-learned entity resolution in the first implementation.
  Deterministic rules plus scored candidates first; a model only once there is a
  labelled corpus of human decisions to train and evaluate against.

---

See [M2_SCHEMA_GRAPH.md](M2_SCHEMA_GRAPH.md) for the table graph and
cardinalities, [M2_ACCEPTANCE_CRITERIA.md](M2_ACCEPTANCE_CRITERIA.md) for the
scenarios an implementation must satisfy, and [M2_ADRS.md](M2_ADRS.md) for the
decisions behind this revision.
