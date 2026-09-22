# M2 — Company Discovery and Entity Resolution

**Status:** design only. No M2 code, migrations or tables exist in this
repository, and none should be created from this document without a separate
implementation decision.

**Milestone position:** M2 sits between M1 (contextual market intelligence) and
M3 (operational research). M1 answers *"which market × vertical × ICP × channel
× ticket contexts are worth pursuing?"* M2 answers *"which real companies exist
in a chosen context, and are we sure they are distinct companies?"*

M2 is **not** "scrape companies". Scraping is one possible provider
implementation. M2 is the canonical-identity problem: given noisy, overlapping,
partially wrong records from several providers, maintain a defensible registry
of real companies with traceable evidence for every claim.

---

## 1. The problem M2 actually solves

A real company is not a row in a provider's index. It:

* operates in **multiple markets** (a German HVAC group with Austrian branches)
* has **multiple locations** (headquarters, depots, service branches)
* fits **multiple verticals** (mechanical contracting *and* refrigeration)
* appears through **multiple providers**, each with its own identifiers
* has **multiple names** (legal entity, trading name, former name, localisations)
* has **multiple domains** (primary, country TLDs, legacy, marketing microsites)

A schema that keys a company to one market and one vertical is therefore wrong
at the first real data. The canonical company must be **market-agnostic and
vertical-agnostic**; market presence and vertical membership are *relationships
carrying their own evidence*, not columns.

## 2. Pipeline

```
Discovery Request        what to look for, in which M1 context
   ↓
Provider Adapter         provider-specific I/O, isolated
   ↓
Raw Provider Record      verbatim payload + hash, append-only
   ↓
Normalization            provider schema → canonical candidate shape
   ↓
Candidate Matching       find plausible existing canonical companies
   ↓
Entity Resolution        deterministic / candidate / ambiguous decision
   ↓
Canonical Company        market-agnostic identity
   ↓
Market Presence          where it operates, with evidence
   ↓
Vertical Association     what it does, with evidence and confidence
   ↓
Company Evidence         every attribute traceable to a provider record
```

Each arrow is a boundary where provenance is attached, never discarded.

## 3. Proposed data model

Tables are proposed on responsibility, not because a name was suggested. Where
a candidate table from the brief is **not** proposed, §3.10 says why.

### 3.1 `companies` — canonical identity

The market-agnostic, vertical-agnostic anchor.

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `canonical_name text` | Display name; a projection of the best-evidenced alias, not a source of truth |
| `primary_domain text null` | Normalized registrable domain; the strongest identity signal available |
| `legal_form text null` | GmbH, Ltd, S.L.U. — evidence, not identity |
| `founded_year int null` | NULL means unknown |
| `employee_count_min/max int null` | Range, because providers disagree |
| `status text` | `ACTIVE` / `MERGED` / `DISSOLVED` / `UNVERIFIED` |
| `merged_into_company_id uuid null` | Set when superseded; the row is never deleted |
| `first_seen_at`, `last_confirmed_at timestamptz` | |

Deliberately **absent**: `market_id`, `vertical_id`, `score`, `icp_fit`. A
company is not owned by a market, and scores live in runs (ADR-002).

`UNIQUE (primary_domain) WHERE primary_domain IS NOT NULL AND status <> 'MERGED'`
is the one hard identity constraint — see §5.1.

### 3.2 `company_names` — aliases with provenance

Justified as a separate table: a company genuinely has many names, each seen by
a different provider at a different time, and the "best" name is a judgement
that changes as evidence arrives.

`(company_id, name_normalized, name_type)` where `name_type ∈ {LEGAL, TRADING,
FORMER, LOCALIZED, PROVIDER_DISPLAY}`, plus `first_seen_at`, `last_seen_at`,
`is_primary`. Normalization (case-folding, legal-suffix stripping, diacritic
folding) is stored **alongside** the raw name, never instead of it.

### 3.3 `company_domains`

`(company_id, domain_normalized)` with `domain_type ∈ {PRIMARY, ALTERNATE,
REDIRECT, COUNTRY_TLD, DEFUNCT}`, `verified_at`, `evidence_id`.

Separate from `companies.primary_domain` because a company may hold a dozen
domains while exactly one serves as identity.

### 3.4 `company_locations`

One row per physical or registered place.

`company_id`, `location_type ∈ {HEADQUARTERS, BRANCH, DEPOT, REGISTERED_OFFICE,
SERVICE_AREA}`, `market_id` → M1 `markets`, `region`, `city`, `postal_code`,
`address_raw`, `address_normalized`, `latitude/longitude` (nullable),
`is_primary`, `evidence_id`.

Coordinates stay NULL unless a provider supplies them. Geocoding is a later,
separately-evidenced step.

### 3.5 `company_market_presences`

**Distinct from `company_locations`** and the reason both exist: presence is a
*claim about operating in a market*, which can be true without a location row
(a UK contractor serving Ireland from Belfast).

`company_id`, `market_id`, `presence_type ∈ {HEADQUARTERED, BRANCH, OPERATES,
SERVES_REMOTELY}`, `confidence numeric`, `evidence_id`, `first_seen_at`.

`UNIQUE (company_id, market_id, presence_type)`.

This is the join M1 contexts use, and the table that makes "one company with
branches in multiple markets" representable without duplicating the company.

### 3.6 `company_verticals`

`company_id`, `vertical_id` → M1 `verticals`, `classification_method ∈
{PROVIDER_TAXONOMY, NAICS_MAPPING, KEYWORD_INFERENCE, HUMAN_REVIEW}`,
`fact_type` (reusing M0's five-member vocabulary), `confidence numeric`,
`evidence_id`, `is_primary`.

`UNIQUE (company_id, vertical_id)`. Multiple verticals per company are normal,
not an error.

A keyword-inferred vertical is `INFERENCE`, never `FACT`. This reuses the
existing evidence taxonomy rather than inventing a parallel one.

### 3.7 Provider layer

| Table | Responsibility |
| --- | --- |
| `discovery_providers` | Registry: key, name, capabilities, rate limits, trust tier, `is_active` |
| `discovery_runs` | One execution: provider, M1 context FKs, status, started/completed, cost, error |
| `discovery_queries` | The *exact* query issued — parameters, pagination cursor, result count |
| `provider_records` | One raw result, append-only: `raw_payload jsonb`, `raw_payload_sha256`, `retrieved_at`, `source_url`, `provider_external_id`, `normalized jsonb`, `resolved_company_id`, `resolution_decision_id` |

`discovery_results` from the brief is **not** proposed as a separate table: it
would duplicate `provider_records`. One raw record *is* the result.

`provider_external_ids` is **not** proposed either — it collapses into
`UNIQUE (provider_id, provider_external_id)` on `provider_records`, plus a
`company_provider_links` view when a company-centric lookup is wanted. Adding a
third table for a two-column tuple is normalization theatre.

`discovery_runs` carries `vertical_id`, `icp_id`, `market_id`, `channel_id` as
real FKs, exactly as `score_runs` does — the ADR-014 pattern, reused.

### 3.8 Entity resolution layer

| Table | Responsibility |
| --- | --- |
| `entity_resolution_candidates` | A considered pairing: `provider_record_id`, `candidate_company_id`, `match_signals jsonb`, `score numeric`, `tier` |
| `entity_resolution_decisions` | The outcome, append-only: `decision ∈ {MATCHED, CREATED_NEW, AMBIGUOUS, REJECTED, MERGED}`, `method ∈ {DETERMINISTIC, CANDIDATE_AUTO, HUMAN_REVIEW}`, `rationale text`, `signals jsonb`, `decided_by`, `decided_at`, `superseded_by_id` |

Decisions are **never updated**. A reversal writes a new decision referencing
the old one, so "why is this company merged?" is always answerable.

### 3.9 `company_evidence`

The generic provenance link, mirroring M0's `observation_sources`:
`company_id`, `attribute` (e.g. `primary_domain`, `employee_count`,
`vertical:commercial_hvac`), `provider_record_id`, `fact_type`, `confidence`,
`observed_at`, `period_granularity`.

This is what makes "which provider told us this company has 40 employees, when,
and how sure are we?" a single query — and it reuses M0's evidence vocabulary
rather than inventing a second one.

### 3.10 Tables deliberately not proposed

| Suggested | Why not |
| --- | --- |
| `discovery_results` | Identical responsibility to `provider_records` |
| `provider_external_ids` | A unique constraint, not a table |
| `company_aliases` *(separate from names)* | Same responsibility as `company_names` |

## 4. Provider abstraction

Provider-specific schemas must not leak into the canonical domain. The boundary:

```
ProviderAdapter (interface)
  .capabilities() -> supported filters, geography granularity, rate limits
  .search(query: DiscoveryQuery) -> Iterator[RawProviderRecord]
  .normalize(raw) -> CandidateCompany       # pure, deterministic, testable
```

Rules:

1. **Adapters never write canonical tables.** They emit raw records and
   normalized candidates; the resolution service alone writes `companies`.
2. **`normalize` is a pure function** of the raw payload — no I/O, no database.
   That makes provider normalization testable from fixtures with zero network,
   matching the existing M0/M1 test discipline.
3. **Raw payloads are append-only** and hashed, exactly like `market_snapshots`.
   Re-fetching the same record yields the same hash and is a no-op.
4. **Provider trust is configuration**, stored on `discovery_providers` and
   versioned, so a provider's weight in resolution is an auditable decision.
5. A provider outage produces a `FAILED` `discovery_run` and **no canonical
   writes**. Existing entities are never degraded by a provider being down.

## 5. Entity resolution strategy

Three tiers, with deliberately different authority.

### 5.1 Deterministic matches — may auto-merge

* Exact normalized **primary registrable domain** match
* Exact `(provider_id, provider_external_id)` already resolved to a company
* Exact verified national company-registry identifier

Domain equality is the only *name-free* signal strong enough to stand alone.
Enforced by the partial unique index in §3.1, so a race cannot create two
companies on one domain: the loser catches the violation and re-reads.

### 5.2 Candidate matches — score, then gate

Signals, each contributing to a composite score with stated weights:

| Signal | Indicative weight |
| --- | --- |
| Normalized legal name equality within same market | high |
| Normalized name + same city/postal code | high |
| Shared phone number | medium |
| Shared address (normalized) | medium |
| Fuzzy name similarity (trigram) | low |
| Same vertical + same market | very low, tie-break only |

Two thresholds, both configuration:

* `auto_match_threshold` — above this, `CANDIDATE_AUTO` match, with the full
  signal vector persisted in the decision.
* `ambiguous_threshold` — between the two, the record is parked as
  `AMBIGUOUS` for human review. It does **not** create a company and does
  **not** merge one.

Below `ambiguous_threshold`: `CREATED_NEW`.

### 5.3 Fuzzy similarity alone never merges

This is the load-bearing rule. Trigram similarity on names is a *retrieval*
mechanism for finding candidates, never a *decision* mechanism. "Schmidt
Kältetechnik GmbH" and "Schmidt Kaeltetechnik GmbH" may be one company or two
family firms in different towns; without a corroborating domain, address or
identifier, the honest answer is `AMBIGUOUS`.

This mirrors M0/M1's central discipline: **an unknown stays unknown rather than
being guessed into a confident-looking answer.**

### 5.4 Merges are reversible

A merge sets `status = MERGED` and `merged_into_company_id` on the loser; it
never deletes. Evidence and provider records follow the survivor by reference,
so an incorrect merge can be reversed by a new decision without data loss.

## 6. Idempotency and concurrency

**Same provider record imported twice → one canonical company.**
`UNIQUE (provider_id, provider_external_id)` plus
`UNIQUE (raw_payload_sha256, provider_id)` make re-ingestion a no-op. This is
the `INSERT ... ON CONFLICT DO NOTHING` pattern already used by the M1 research-gap
detector.

**Two workers discovering the same company concurrently.** Preferred mechanism,
in order:

1. The partial unique index on `primary_domain` — the database arbitrates.
2. `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`, then re-read on conflict.
3. For the narrow non-domain case, a transactional advisory lock keyed on the
   normalized-name + market hash, held only for the resolution decision.

No distributed lock service. No queue-level deduplication. PostgreSQL
constraints plus short transactions are sufficient at this scale and are
already the house pattern.

**Discovery-run reproducibility.** A run stores its provider, adapter version,
exact query parameters, pagination cursors and result count. Re-running it
produces a *new* run (runs are immutable) whose records can be diffed against
the previous run. Provider results legitimately change over time; the design
makes that change **visible** rather than silently overwriting.

## 7. The M1 ↔ M2 boundary — discovery counts are not density

This is the most important constraint in the design.

If provider A returns 2,400 US HVAC companies and 800 German ones, that does
**not** establish that the US has three times the HVAC density. It establishes
that *this provider, with this query, on this date, indexed those counts*.
Provider coverage, indexing depth, language handling, geography granularity and
query phrasing all bias the result — typically in favour of large
English-language markets, which is precisely the bias that would flatter a
US-first conclusion.

Therefore:

1. A discovery count **never** writes to `market_observations` automatically.
2. If a count is promoted to an M1 observation, it enters as `fact_type = PROXY`
   — never `FACT` — carrying:
   * `provider_id` and adapter version
   * the exact `discovery_query` definition
   * `retrieved_at` and `period_granularity = DATE`
   * a stated coverage assumption (what the provider is believed to index)
   * a calibration factor, if one has been established against a known-good
     national business register, with its own evidence
   * a confidence derived from the existing M0 algorithm
3. Promotion is an **explicit, logged, human-authorised action**, not a side
   effect of a discovery run.
4. Uncalibrated counts are usable for *within-provider, within-market* trend
   comparison only. Cross-market density claims require calibration evidence.
5. Until calibrated, the honest M1 representation of vertical density remains
   what it is today: **a research gap**.

A dedicated `market_density_proxies` table is the likely mechanism, keeping
provider-derived estimates in their own space with their own methodology
fields, and requiring a deliberate promotion step before any of it reaches
`market_observations`.

## 8. Mutable vs append-only

| Append-only | Mutable |
| --- | --- |
| `provider_records` | `companies` (attribute projections) |
| `discovery_runs`, `discovery_queries` | `company_names.is_primary` |
| `entity_resolution_decisions` | `companies.status`, `merged_into_company_id` |
| `company_evidence` | `company_market_presences.confidence` |

The rule mirrors M0: **evidence is append-only, projections are mutable.** A
company's employee count may be corrected; the provider record that claimed the
old number is never rewritten. The `BEFORE UPDATE` trigger pattern from
ADR-018 extends naturally to `provider_records` and
`entity_resolution_decisions`.

## 9. Proposed API surface

All under `/api/v1`, following existing conventions and the established error
contract.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/discovery-providers` | Registry and capabilities |
| `POST` | `/discovery-runs` | Start a run for an M1 context |
| `GET` | `/discovery-runs[/{id}]` | Status, query, counts, cost |
| `GET` | `/discovery-runs/{id}/records` | Raw provider records with provenance |
| `GET` | `/companies` | Filter by market, vertical, presence, status |
| `GET` | `/companies/{id}` | Canonical company with evidence summary |
| `GET` | `/companies/{id}/evidence` | Every attribute, with provider and date |
| `GET` | `/companies/{id}/locations` | |
| `GET` | `/companies/{id}/market-presences` | |
| `GET` | `/companies/{id}/verticals` | With `fact_type` and confidence |
| `GET` | `/companies/{id}/provider-records` | Which providers saw it, and when |
| `GET` | `/entity-resolution/decisions` | Filter by decision, method, run |
| `GET` | `/entity-resolution/ambiguous` | The human-review queue |
| `POST` | `/entity-resolution/decisions/{id}/review` | Record a human decision |

New error codes: `PROVIDER_UNAVAILABLE`, `RESOLUTION_AMBIGUOUS`,
`DUPLICATE_PROVIDER_RECORD`, `MERGE_CONFLICT`.

## 10. Job and worker boundaries

* A discovery run is a **job**, not a request-thread — providers are slow and
  rate-limited.
* Fetch, normalize and resolve are **separate stages**, each independently
  retryable. A resolution bug must be re-runnable against already-fetched raw
  records without re-paying for provider calls.
* Normalization and resolution are **pure enough to test from fixtures**, with
  no network, preserving the existing "no test touches the internet" rule.
* Redis plus a lightweight worker, as the existing architecture already
  anticipates. No Kafka, no Kubernetes.

## 11. How M3 consumes M2

M3 (Operational Research) takes a canonical company and gathers operational
evidence — fleet size, service contracts, dispatch tooling, job-posting
signals. It depends on M2 for:

* a **stable company id** that survives re-discovery
* **market presence**, to know which jurisdiction's rules apply
* **vertical association with confidence**, to pick the right research playbook
* **provider records**, as seed URLs and identifiers
* an explicit **ambiguity flag**, so research effort is not spent on entities
  that may be duplicates

M3 must be able to attach its findings to the same `company_evidence`
structure, which is why that table is generic over `attribute` rather than
enumerating M2-specific columns.

## 12. Explicit non-goals for M2

* No people, buyers, contacts or email discovery — that is M5.
* No outbound, sequences or campaigns — that is M6.
* No CRM synchronisation.
* No scoring of companies — qualification is M4, and it will follow the same
  score/confidence/coverage discipline.
* No automatic promotion of discovery counts into M1 (§7).
* No web scraping of company websites — that is M3 operational research; M2
  consumes structured provider output.
* No machine-learned entity resolution in the first implementation.
  Deterministic rules plus scored candidates first; a model only once there is
  a labelled corpus of human decisions to train and evaluate against.

## 13. Design questions, answered

1. **Canonical identity?** A UUID anchored on normalized primary domain where
   available; otherwise a resolution decision backed by corroborating signals.
2. **Independent of markets/verticals?** `companies` carries no market or
   vertical column; both are evidence-bearing relationships.
3. **Multiple locations?** `company_locations`, typed, optionally geocoded.
4. **Multiple countries?** `company_market_presences`, distinct from locations
   because serving a market does not require a site in it.
5. **Vertical classification?** `company_verticals` with method, `fact_type`
   and confidence; multiple verticals are normal.
6. **Provider-specific records?** `provider_records`, append-only, hashed,
   holding verbatim payload plus a normalized projection.
7. **Cross-provider dedup?** Deterministic domain/identifier match first,
   scored candidates second, human review third.
8. **Avoiding false merges?** Fuzzy similarity never decides; merges require a
   corroborating non-name signal; all merges are reversible.
9. **Ambiguous cases?** Persisted as `AMBIGUOUS` decisions in a review queue —
   they create nothing and merge nothing.
10. **Automatic vs human?** Deterministic auto; high-scoring candidates auto
    with full signal capture; the middle band always human.
11. **Evidence for an attribute?** A `company_evidence` row pointing at the
    `provider_record` that asserted it, with fact type, confidence and date.
12. **Mutable vs append-only?** §8.
13. **Run reproducibility?** Immutable runs storing provider, adapter version,
    exact query and cursors; re-runs are new runs and are diffable.
14. **Idempotency?** Unique constraints on provider external id and payload
    hash, with `ON CONFLICT DO NOTHING`.
15. **Indexes/constraints?** §14.
16. **API boundaries?** §9.
17. **Job boundaries?** §10.
18. **Non-goals?** §12.
19. **M3 consumption?** §11.
20. **Density proxies without poisoning M1?** §7.

## 14. Likely indexes and constraints

```
companies                      UNIQUE (primary_domain) WHERE primary_domain IS NOT NULL
                                                         AND status <> 'MERGED'
                               INDEX (status)
company_names                  UNIQUE (company_id, name_normalized, name_type)
                               INDEX gin_trgm (name_normalized)      -- retrieval only
company_domains                UNIQUE (domain_normalized)
company_locations              INDEX (market_id), INDEX (company_id, location_type)
company_market_presences       UNIQUE (company_id, market_id, presence_type)
                               INDEX (market_id, presence_type)
company_verticals              UNIQUE (company_id, vertical_id)
                               INDEX (vertical_id, fact_type)
provider_records               UNIQUE (provider_id, provider_external_id)
                               UNIQUE (provider_id, raw_payload_sha256)
                               INDEX (resolved_company_id)
                               INDEX (discovery_run_id)
entity_resolution_decisions    INDEX (decision, method)
                               INDEX (provider_record_id)
company_evidence               INDEX (company_id, attribute)
                               INDEX (provider_record_id)
```

`pg_trgm` is required for candidate retrieval. It is a retrieval index, not a
decision mechanism (§5.3).

---

See [M2_ACCEPTANCE_CRITERIA.md](M2_ACCEPTANCE_CRITERIA.md) for the scenarios an
implementation must satisfy, and [M2_ADRS.md](M2_ADRS.md) for the decisions
taken while producing this design.
