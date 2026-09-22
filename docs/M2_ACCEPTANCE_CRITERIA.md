# M2 — Acceptance Criteria

**Status:** design only. These scenarios are specified *before* implementation,
as M0/M1 were. None of them is implemented as a test yet.

Each scenario states a **Given / When / Then** and the invariant it protects.
An implementation is not done until every MUST scenario passes against a live
PostgreSQL database from an empty schema.

---

## A. Idempotency

### A1 — The same provider record imported twice yields one canonical company
**Given** a provider record with external id `P-123` resolved to company `C`
**When** the same record is ingested again, with an identical payload hash
**Then** no new company, no new provider record, and no new resolution decision
are created, and the run reports the record as already known.
*Protects:* re-running discovery must be free of side effects.

### A2 — A changed payload under the same external id is a new observation, not a new company
**Given** provider record `P-123` → company `C`, employee count 40
**When** the provider returns `P-123` again with employee count 45
**Then** a **new** `provider_records` row is written (different payload hash),
company `C` is not duplicated, and a new `company_evidence` row records the new
claim without deleting the old one.
*Protects:* evidence accumulates; it is never overwritten.

### A3 — A re-run is a new, diffable run
**Given** discovery run `R1` returned 200 records
**When** the identical query is run again as `R2`
**Then** `R1` remains immutable, `R2` records its own timing and counts, and the
two are diffable to show what the provider's index changed.
*Protects:* provider drift is visible rather than silent.

## B. Cross-provider entity resolution

### B1 — The same company from two providers resolves to one canonical company
**Given** provider A returns `schmidt-kaelte.de` and provider B returns
`schmidt-kaelte.de` under a different external id
**When** both are resolved
**Then** exactly one company exists, both provider records point at it, and two
`entity_resolution_decisions` exist — one `CREATED_NEW`, one `MATCHED` with
method `DETERMINISTIC` and the domain signal recorded.
*Protects:* deterministic identity works across providers.

### B2 — Similar names with different domains are NOT merged
**Given** "Schmidt Kältetechnik GmbH" at `schmidt-kaelte.de` in Munich
**And** "Schmidt Kaeltetechnik GmbH" at `schmidt-kt.at` in Vienna
**When** both are ingested
**Then** **two** companies exist. The decision for the second is `CREATED_NEW`
or `AMBIGUOUS` — never an automatic merge.
*Protects:* the central rule — fuzzy name similarity alone never merges.

### B3 — A high-scoring candidate match is auto-matched with its signals recorded
**Given** an existing company with a normalized legal name, city and phone
**When** a provider record matches on all three but supplies no domain
**Then** the composite score exceeds `auto_match_threshold`, the decision is
`CANDIDATE_AUTO`, and the full signal vector is persisted and inspectable.
*Protects:* automatic decisions remain explainable after the fact.

### B4 — A mid-band match stays ambiguous
**Given** a record matching only on normalized name within the same market
**When** the composite score lands between the thresholds
**Then** the decision is `AMBIGUOUS`; **no company is created and none merged**;
the record appears in the human-review queue.
*Protects:* ambiguity is preserved rather than guessed.

### B5 — A human review decision supersedes without erasing
**Given** an `AMBIGUOUS` decision
**When** a reviewer resolves it as `MATCHED`
**Then** a new decision row is written with method `HUMAN_REVIEW` and
`superseded_by_id` set on the original; the original is still readable.
*Protects:* decisions are append-only and auditable.

### B6 — An incorrect merge is reversible
**Given** companies `C1` and `C2` merged in error
**When** the merge is reversed
**Then** both companies are `ACTIVE` again, their evidence and provider records
are correctly re-attached, and no rows were deleted at any point.
*Protects:* merges never destroy data.

## C. Multi-market and multi-vertical reality

### C1 — One company with branches in multiple markets
**Given** a German company with an Austrian and a Swiss branch
**When** all three locations are discovered
**Then** **one** company exists with three `company_locations` rows and three
`company_market_presences` rows, and it is returned by a market filter for
DE, AT and CH.
*Protects:* a company is not keyed to one market.

### C2 — A company serving a market with no site there
**Given** a Belfast contractor that serves Ireland with no Irish location
**When** presence evidence is recorded
**Then** a `company_market_presences` row exists for IE with
`presence_type = SERVES_REMOTELY` and **no** corresponding location row.
*Protects:* presence and location are genuinely different concepts.

### C3 — One company in multiple verticals
**Given** a company classified as both `mechanical_contractors` and
`refrigeration`
**When** both classifications are recorded
**Then** two `company_verticals` rows exist, each with its own
`classification_method`, `fact_type` and confidence, and neither is discarded.
*Protects:* a company is not keyed to one vertical.

### C4 — An inferred vertical is never a FACT
**Given** a vertical assigned by keyword inference
**Then** its `fact_type` is `INFERENCE` and its confidence reflects that.
*Protects:* M0's evidence vocabulary is reused honestly.

## D. Provenance

### D1 — Every company attribute traces to a provider record
**Given** any company attribute with a value
**When** its evidence is queried
**Then** a `company_evidence` row names the provider record, provider, retrieval
date and fact type that support it.
*Protects:* no claim without provenance — the M0 principle, extended.

### D2 — Raw payloads are preserved verbatim and are immutable
**Given** an ingested provider record
**When** an `UPDATE` is attempted on `provider_records`
**Then** the database rejects it, exactly as it does for `market_observations`.
*Protects:* append-only evidence.

### D3 — Unknown attributes stay NULL
**Given** a provider that supplies no employee count
**Then** `employee_count_min/max` are NULL, not 0, and no evidence row claims a
value.
*Protects:* unknown is never zero.

## E. Failure isolation

### E1 — A provider outage does not corrupt existing entities
**Given** an established set of canonical companies
**When** a provider times out mid-run
**Then** the run is `FAILED`, partial raw records are retained for inspection,
**no canonical company is modified or deleted**, and no confidence is degraded.
*Protects:* availability problems are not evidence.

### E2 — A malformed provider payload fails that record, not the run
**Given** one unparseable record in a page of 100
**Then** that record is stored raw and marked unnormalizable; the other 99
resolve normally; the failure is visible on the run.
*Protects:* one bad row cannot block a pipeline.

### E3 — A resolution bug is re-runnable without re-paying providers
**Given** fetched raw records and a corrected resolution rule
**When** resolution is re-run over the existing records
**Then** new decisions supersede old ones with no new provider calls.
*Protects:* the fetch/normalize/resolve stage separation.

## F. The M1 boundary — the one that matters most

### F1 — A discovery count NEVER silently becomes an M1 observation
**Given** a completed discovery run returning 2,400 US HVAC companies
**When** the run completes
**Then** **no row is written to `market_observations`**, and M1 contextual
coverage for that market × vertical is unchanged.
*Protects:* provider bias cannot leak into market intelligence.

### F2 — A promoted count enters as PROXY with full methodology
**Given** an explicit, authorised promotion of a discovery count
**Then** the resulting `market_observations` row has `fact_type = PROXY` —
never `FACT` — and carries provider, adapter version, query definition,
retrieval date, coverage assumption and confidence.
*Protects:* the honesty of the evidence taxonomy.

### F3 — Promotion is explicit and logged
**Given** any promotion
**Then** it is attributable to a human actor with a timestamp and rationale, and
is reversible.
*Protects:* no silent data-quality drift.

### F4 — Uncalibrated counts cannot be compared across markets
**Given** uncalibrated counts for two markets
**When** a cross-market density comparison is requested
**Then** the API declines or flags it as uncalibrated, in the same spirit as
`INSUFFICIENT_COVERAGE`.
*Protects:* the exact false conclusion §7 of the design warns about.

## G. Concurrency

### G1 — Concurrent workers do not create duplicate companies
**Given** two workers resolving the same domain simultaneously
**When** both attempt to create the company
**Then** exactly one company exists; the loser re-reads the winner's row; no
`IntegrityError` reaches the caller.
*Protects:* the pattern already proven by the M1 research-gap detector.

### G2 — Concurrent merges do not corrupt the graph
**Given** two workers merging overlapping company sets
**Then** the result is a consistent merge chain with no cycles and no orphaned
evidence.

## H. Scope containment

### H1 — No people, campaigns or CRM tables exist
**When** the schema is inspected after M2
**Then** no table for people, contacts, emails, sequences, campaigns or CRM
sync exists — mirroring the existing `test_no_m2_tables_exist` guard, inverted
for M3+.

### H2 — M0/M1 guarantees are untouched
**When** the full M0/M1 suite runs after M2 is implemented
**Then** reference reproduction still reports max score delta `0.0` and `0` rank
mismatches.
*Protects:* the non-negotiable invariant.

---

## Definition of done for M2

* All MUST scenarios above pass against live PostgreSQL from an empty schema.
* No test requires network access; provider adapters are tested from recorded
  fixtures.
* Normalization functions are pure and independently tested.
* `pytest` and `ruff` green, with M0/M1's full suite still passing unchanged.
* Migrations verified from an empty database.
* At least two provider adapters exist, so the abstraction is proven against
  more than one shape of data.
* An `AMBIGUOUS` review queue is reachable through the API.
* A documented promotion path for density proxies exists — and is **not**
  wired to run automatically.
