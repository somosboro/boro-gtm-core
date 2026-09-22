# M2 — Acceptance Criteria

**Status:** design only, revision 3. These scenarios are specified *before*
implementation, as M0/M1 were. None is implemented as a test yet.

Each states a **Given / When / Then** and the invariant it protects. An
implementation is not done until every scenario passes against live PostgreSQL
from an empty schema.

Scenarios marked **[r2]** were added in revision 2; **[r3]** in revision 3,
covering the design invariants resolved there.

---

## A. Provider versioning and idempotency

### A1 [r2] — Same external id, identical payload → no new version
**Given** provider entity `P-123` with one version at payload hash `H`
**When** the provider returns the byte-identical payload again
**Then** no new `provider_record_versions` row is created, a
`provider_record_sightings` row records the re-confirmation, and no new
resolution decision is written.
*Protects:* re-running discovery is free of side effects.

### A2 [r2] — Same external id, changed payload → new immutable version
**Given** entity `P-123` at version `H1` claiming `employee_count = 40`
**When** the provider returns a payload claiming 45, hashing to `H2`
**Then** a **second** version row exists under the **same** `provider_entity_id`,
version `H1` is byte-identical to before, and both versions remain queryable in
retrieval order.
*Protects:* the revision-1 impossibility — unique-per-external-id while keeping
history.

### A3 [r2] — A version is never mutated
**Given** any persisted `provider_record_versions` row
**When** an `UPDATE` is attempted
**Then** the database rejects it, exactly as for `market_observations`.
*Protects:* append-only raw evidence.

### A5 [r3] — Identity is the canonical digest, not the raw bytes
**Given** a provider that returns the same object with different whitespace and
key ordering on two calls
**When** both responses are ingested
**Then** exactly **one** `provider_record_versions` row exists (same
`canonical_payload_hash`), **two** `provider_record_bodies` rows exist with
different `raw_body_sha256`, and a sighting records the second observation.
*Protects:* a provider reformatting its JSON must not manufacture a version —
the same rule that makes a reindented source file a no-op import in M0.

### A6 [r3] — Raw bytes are preserved, and JSONB is not called verbatim
**Given** any ingested record
**Then** `provider_record_bodies.raw_body` equals the literal response bytes,
`raw_body_sha256` matches them, and `parsed_payload` is documented as a parsed
representation rather than a verbatim one.

### A7 [r3] — Pruning bodies preserves the evidence chain
**Given** a retention policy that prunes `provider_record_bodies`
**When** bodies are pruned
**Then** `provider_record_versions` is untouched — both digests, the parsed
payload, sightings, decisions and claims all survive and remain queryable.

### A8 [r3] — A NATIVE external id is used as-is
**Given** a provider declaring `identity_capability = NATIVE_EXTERNAL_ID`
**Then** its entities carry `external_id_kind = 'NATIVE'` and the provider's own
id verbatim.

### A9 [r3] — A DERIVED key is never presented as provider-issued
**Given** a provider declaring `DERIVED_STABLE_KEY` with
`key_fields = [registrable_domain]`
**When** entities are created
**Then** each carries `external_id_kind = 'DERIVED'` and the
`key_algorithm_version` in force, and no API response describes the key as a
provider identifier.

### A10 [r3] — A derived-key collision blocks auto-matching
**Given** two genuinely different organizations producing the same derived key
**When** their versions land under one provider entity with materially
disagreeing non-key identity fields
**Then** the entity is flagged `identity_collision = true` and resolution routes
it to `AMBIGUOUS` — it may never auto-match.
*Protects:* a colliding derived key is weaker evidence than no key at all.

### A11 [r3] — Changed key fields create a new entity, not a mutated one
**Given** a `DERIVED_STABLE_KEY` entity whose participating fields change
**When** the record is re-ingested
**Then** a **new** provider entity is created, the old one is untouched, and the
two are linked only by both resolving to the same company — no mutable pointer
is invented.

### A12 [r3] — A CONTENT_ONLY provider cannot express object change
**Given** a provider declaring `CONTENT_ONLY`
**When** its payload for the same real object changes
**Then** a new entity is created (`external_id_kind = 'CONTENT'`), and the
design records that such providers cannot track change over time.

### A4 — A re-run is a new, diffable run
**Given** run `R1` returned 200 records
**When** the identical query runs again as `R2`
**Then** `R1` is unchanged, `R2` has its own timing and counts, and the two are
diffable to show what the provider's index changed.
*Protects:* provider drift is visible, never silent.

## B. Claims and projections

### B1 [r2] — Multiple historical claims for one attribute all persist
**Given** provider A claims `employee_count = 40` on 2026-03-01, provider A
later claims 45 on 2026-07-01, provider B claims 42 on 2026-06-15
**When** all three are ingested
**Then** **three** `company_claims` rows exist, each naming its
`provider_record_version_id`, value, fact type, confidence and `observed_at`;
none overwrites another; and the full history is returned in date order.
*Protects:* the revision-1 gap — evidence must record what was said, not only
who said it.

### B2 [r2] — The canonical projection changes while claim history stays intact
**Given** the three claims above
**When** the projection is recomputed
**Then** `companies` reflects the winning value per the documented precedence,
`derived_from_claim_ids` names the contributing claims, and **all three claim
rows are unchanged**.
*Protects:* projections are derived; evidence is not.

### B3 [r3] — Derived projections rebuild byte-identically; anchors survive
**Given** a populated registry
**When** every **derived projection** table is truncated — `company_profiles`,
`company_names`, `company_domains`, `company_locations`,
`company_market_presences`, `company_verticals`, `company_relationships`,
`entity_resolution_heads` — and recomputed from evidence, the
`attribute_registry_version` and the `identity_policy_version`
**Then** the projections are byte-identical to before, **and every
`companies.id` is unchanged**.
*Protects:* the revised rebuild invariant — projections are derived, identity
anchors are minted.

### B3a [r3] — Identity anchors are not truncatable
**Given** a populated registry
**When** truncating `companies` is attempted
**Then** it fails on foreign keys from evidence tables, and no rebuild path
exists that would regenerate the same UUIDs.
*Protects:* the boundary between minted identity and computed projection.

### B3b [r3] — `companies` holds no business attributes
**When** the schema is inspected
**Then** `companies` has no `canonical_name`, `primary_domain`,
`employee_count_*`, `legal_form`, `founded_year`, `market_id` or `vertical_id`
column; all appear on `company_profiles` or another projection.

### B3c [r3] — Anchor lifecycle reconciles against decisions
**Given** a registry containing merged companies
**When** the reconciliation query in design §3.2 is run
**Then** it returns zero rows — every `lifecycle_status` agrees with the
effective `MERGED` / `SPLIT` decision.
*Protects:* the one cached identity-level fact stays truthful.

### B8 [r3] — `value_jsonb` and typed shadows never disagree
**Given** every claim in the database
**When** each shadow is recomputed from `value_jsonb` using its registry
definition's extractor
**Then** the recomputed value equals the stored shadow for every row.

### B9 [r3] — A claim outside the registry is rejected
**Given** the active `attribute_registry_version`
**When** a claim is written with an `attribute_key` absent from it, or a
`value_jsonb` failing that version's `value_schema`, or a `unit` or `fact_type`
outside the declared sets
**Then** the write is rejected with `ATTRIBUTE_NOT_IN_REGISTRY`.
*Protects:* the contract that stops claims becoming untyped EAV.

### B10 [r3] — Registry strategy drives projection behaviour
**Given** `employee_count` declared `ENVELOPE` and `legal_name` declared
`HIGHEST_PRECEDENCE`
**When** two equally-ranked providers disagree on both
**Then** `employee_count` projects a min/max envelope while `legal_name`
projects a single winner with `projection_conflict` set if unresolvable.

### B4 [r2] — Equally-ranked disagreement projects an envelope, not a winner
**Given** two providers of equal trust tier and equal fact type claiming
`employee_count` 40 and 60
**When** the projection runs
**Then** `employee_count_min = 40` and `employee_count_max = 60` — not a single
averaged or arbitrarily chosen figure.
*Protects:* disagreement is information, not noise.

### B5 [r2] — Human review outranks every automatic claim
**Given** a human-reviewed claim and a higher-trust provider claim disagreeing
**Then** the human claim wins the projection.

### B6 — Unknown attributes stay NULL
**Given** a provider supplying no employee count
**Then** the claim records `availability = NOT_AVAILABLE` with NULL value and
NULL fact type; `companies.employee_count_min/max` are NULL, not 0.
*Protects:* unknown is never zero.

### B7 — A retraction is a new claim, never a delete
**Given** a claim later retracted by its provider
**Then** a new claim with `availability = NOT_AVAILABLE` supersedes it in the
projection; the original row still exists.

## C. Entity resolution and supersession

### C1 [r2] — Correcting a resolution never modifies the old decision
**Given** decision `D1` resolving entity `P-123` to company `C1`
**When** a correction resolves it to `C2`
**Then** a new decision `D2` exists with `D2.supersedes_decision_id = D1.id`,
**`D1` is byte-identical to before**, and no row was updated.
*Protects:* the revision-1 contradiction — append-only rows that received
`superseded_by_id`.

### C2 [r2] — Effective resolution is the latest non-superseded decision
**Given** a chain `D1 ← D2 ← D3`
**When** the current resolution is queried
**Then** `D3` is returned, and the full chain is retrievable in order.
*Protects:* current state is derivable, not stored.

### C3 [r2] — A decision cannot be superseded twice
**Given** decision `D1` already superseded by `D2`
**When** a second decision attempts to supersede `D1`
**Then** the unique constraint rejects it.
*Protects:* forked history.

### C4 [r2] — Raw records carry no resolution state
**When** the schema is inspected
**Then** `provider_record_versions` has no `resolved_company_id` and no
`resolution_decision_id` column, and re-resolving requires no write to any
provider table.
*Protects:* immutable evidence never needs mutation to correct a decision.

### C9 [r3] — Exactly one head after two concurrent *initial* decisions
**Given** provider entity `P` with no decisions
**When** two workers concurrently write a root decision for `P`
**Then** exactly one commits; the other fails on `uq_resolution_root`, re-reads
the head, and either writes nothing or supersedes it. Exactly one effective
head exists, and no `IntegrityError` reaches the caller.
*Protects:* the revision-2 gap — `UNIQUE (supersedes_decision_id)` did not stop
two concurrent roots, because NULLs do not collide.

### C10 [r3] — Exactly one head after two concurrent *superseding* decisions
**Given** entity `P` with head `D1`
**When** two workers concurrently write decisions superseding `D1`
**Then** exactly one commits; the other fails on `uq_resolution_supersedes`,
re-reads the new head and re-evaluates against it. Exactly one effective head
exists.

### C11 [r3] — The decision graph is a linear chain
**Given** any provider entity with decisions
**Then** exactly one decision has `supersedes_decision_id IS NULL`, no decision
is superseded twice, and the anti-join for the head returns exactly one row
without needing `ORDER BY ... LIMIT 1`.

### C12 [r3] — The heads table is a cache, not the invariant
**Given** a populated registry
**When** `entity_resolution_heads` is truncated and rebuilt from decisions
**Then** it is byte-identical, and resolution correctness was never dependent
on it.

### C13 [r3] — Corrected resolution moves claim history without rewriting it
**Given** provider entity `P` resolved to company `A` by decision `D1`, with
claims written under `D1`
**When** `D2` supersedes `D1`, resolving `P` to company `B`
**Then** every claim row is **byte-identical** to before; `B`'s rebuilt
projection includes those claims; `A`'s rebuilt projection excludes them; and
the claims remain queryable as history via `resolution_decision_id = D1`.
*Protects:* the revision-2 contradiction — claims carrying `company_id` would
have had to be rewritten.

### C14 [r3] — Superseded-decision claims never contaminate a current projection
**Given** the state above
**When** `A`'s projection is rebuilt
**Then** no value in it derives from a claim whose provider entity resolves
elsewhere under the effective decision.

### C5 — Same company from two providers resolves to one company
**Given** provider A and provider B both returning `schmidt-kaelte.de` as an
identity domain
**Then** exactly one company exists, both provider entities resolve to it, and
two decisions exist — one `CREATED_NEW`, one `MATCHED` with method
`DETERMINISTIC` and the domain signal recorded.

### C6 — Similar names, different domains → not merged
**Given** "Schmidt Kältetechnik GmbH" at `schmidt-kaelte.de` in Munich, and
"Schmidt Kaeltetechnik GmbH" at `schmidt-kt.at` in Vienna
**Then** **two** companies exist; the second decision is `CREATED_NEW` or
`AMBIGUOUS`, never an automatic merge.
*Protects:* fuzzy name similarity alone never merges.

### C7 — A mid-band match stays ambiguous
**Given** a record matching only on normalized name within the same market
**Then** the decision is `AMBIGUOUS`, **no company is created and none merged**,
and the record appears in the review queue.

### C8 [r2] — A merge is reversible without rewriting raw evidence
**Given** companies `C1` and `C2` merged in error
**When** a `SPLIT` decision supersedes the merge
**Then** both are `ACTIVE`, claims re-project correctly, **no
`provider_record_versions` or `company_claims` row was modified**, and no row
was deleted.
*Protects:* reversibility without evidence loss.

## D. Identity policy

### D1 [r2] — A shared group domain does not merge autonomous companies
**Given** two operationally autonomous subsidiaries both presenting
`group.com`, with different registry identifiers and different countries
**Then** they are **two** companies; the domain is recorded with
`domain_role = 'GROUP'` on both; the match contributes a low-weight candidate
signal and does not auto-merge.
*Protects:* the revision-1 universal domain-uniqueness assumption.

### D2 [r2] — Company domain migration keeps one company
**Given** company `C` on `old.de`
**When** the provider later reports `new.de` for the same provider entity with
a redirect from `old.de`
**Then** one company remains; `company_domains` holds `new.de` as `IDENTITY`
and `old.de` as `REDIRECT`; both transitions are claim-backed with dates.

### D3 [r2] — A blocklisted hosting domain is never identity
**Given** two unrelated firms both on `something.wixsite.com`
**Then** neither claims it as `IDENTITY`, the shared domain contributes nothing
to matching, and two companies exist.

### D4 [r2] — Acquisition is a relationship, not a merge
**Given** `acquired.de` redirecting to `acquirer.de`
**Then** two companies remain, linked by `company_relationships (ACQUIRED_BY)`
with a date — the acquired company's history stays intact.

### D5 [r2] — A branch is not a company
**Given** a provider record for "Acme GmbH — Hamburg Branch"
**When** it resolves to existing company "Acme GmbH"
**Then** one company exists with an additional `company_locations` row of type
`BRANCH` and a DE `company_market_presences` row — **not** a second company.

### D6 [r2] — A franchisee is a company
**Given** two franchisees of the same franchisor, separate buying authority,
same brand name, different domains and cities
**Then** **three** companies exist (two franchisees plus the franchisor), linked
by `FRANCHISE_OF` relationships.
*Protects:* the most common false-merge trap.

### D7 [r2] — Resolution records the identity policy version in force
**Given** any decision
**Then** `identity_policy_version` is populated, so a later policy change does
not silently reinterpret past decisions.

## E. Locations and presence

### E1 [r2] — Service coverage never fabricates a location
**Given** a provider stating a company "serves Ireland" with no Irish address
**Then** an IE `company_market_presences` row of type `SERVES_REMOTELY` exists
and **no** `company_locations` row is created.
*Protects:* the removal of `SERVICE_AREA` from physical locations.

### E2 [r2] — `SERVICE_AREA` is not a location type
**When** the schema is inspected
**Then** `company_locations.location_type` permits only `HEADQUARTERS`,
`BRANCH`, `DEPOT`, `REGISTERED_OFFICE`.

### E3 — One company with branches in multiple markets
**Given** a German company with Austrian and Swiss branches
**Then** **one** company, three location rows, three presence rows, returned by
a market filter for DE, AT and CH.

### E4 — One company in multiple verticals
**Given** a company classified as both `mechanical_contractors` and
`refrigeration`
**Then** two `company_verticals` rows, each with its own method, fact type and
confidence; neither discarded.

### E5 — An inferred vertical is never a FACT
**Given** a vertical assigned by keyword inference
**Then** its `fact_type` is `INFERENCE`.

## F. Run lifecycle

### F1 [r2] — A partial fetch preserves raw evidence
**Given** a run that fetched 3 of 10 pages before the provider timed out
**Then** the run is `PARTIAL_FETCH`, the 3 pages of versions and their query
rows are **persisted and committed**, and the failure is visible on the run.
*Protects:* evidence already paid for is never discarded.

### F2 [r2] — A partial fetch writes nothing canonical by default
**Given** the same run, created without `allow_partial_resolution`
**Then** **no** company, claim, decision or projection row was created or
modified by it.
*Protects:* a truncated result set is not evidence of absence; a provider
outage must not silently shrink a market.

### F3 [r2] — Opt-in partial resolution is explicit and visible
**Given** a run created with `allow_partial_resolution = true` that reaches
`PARTIAL_FETCH`
**Then** resolution proceeds, and the flag is recorded on the run and returned
by the API.

### F4 [r2] — Retry resumes rather than restarts
**Given** a `PARTIAL_FETCH` run
**When** it is retried
**Then** fetching resumes from the last recorded cursor; already-persisted
payloads produce sightings, not duplicate versions; the run can reach
`COMPLETED`.

### F5 [r2] — Resolution is re-runnable without re-paying the provider
**Given** persisted versions and a corrected resolution rule
**When** resolution is re-run over stored versions
**Then** new decisions supersede old ones and **no provider call is made**.

### F6 — A malformed payload fails that record, not the run
**Given** one unparseable record among 100
**Then** it is stored raw and flagged unnormalizable; the other 99 resolve; the
failure is visible on the run.

### F7 [r2] — A provider outage never degrades existing entities
**Given** an established registry
**When** a provider times out mid-run
**Then** no existing company, claim or confidence value is modified or deleted.
*Protects:* availability problems are not evidence.

## G. Concurrency

### G1 — Concurrent workers do not create duplicate companies
**Given** two workers resolving the same identity domain simultaneously
**Then** exactly one company exists, the loser re-reads the winner's row, and no
`IntegrityError` reaches the caller.

### G2 [r2] — Concurrent ingestion of the same payload creates one version
**Given** two workers ingesting the identical payload for one provider entity
**Then** exactly one `provider_record_versions` row exists.

### G3 — Concurrent merges do not corrupt the graph
**Given** two workers merging overlapping company sets
**Then** the result is a consistent merge chain with no cycles and no orphaned
claims.

## H. The M1 boundary — the one that matters most

### H1 — A discovery count NEVER silently becomes an M1 observation
**Given** a completed run returning 2,400 US HVAC companies
**Then** **no row is written to `market_observations`**, and M1 contextual
coverage for that market × vertical is unchanged.
*Protects:* provider bias cannot leak into market intelligence.

### H2 — A promoted count enters as PROXY with full methodology
**Given** an explicit, authorised promotion
**Then** the resulting observation has `fact_type = PROXY` — never `FACT` — and
carries provider, adapter version, query definition, retrieval date, coverage
assumption and confidence.

### H3 — Promotion is explicit, logged and reversible
**Given** any promotion
**Then** it is attributable to a human actor with timestamp and rationale, and
can be reversed.

### H4 — Uncalibrated counts cannot be compared across markets
**Given** uncalibrated counts for two markets
**When** a cross-market density comparison is requested
**Then** the API declines or flags it as uncalibrated, in the spirit of
`INSUFFICIENT_COVERAGE`.

## I. Scope containment

### I1 — No people, campaigns or CRM tables exist
**When** the schema is inspected after M2
**Then** no table for people, contacts, emails, sequences, campaigns or CRM sync
exists.

### I2 — M0/M1 guarantees are untouched
**When** the full M0/M1 suite runs after M2 is implemented
**Then** reference reproduction still reports max score delta `0.0` and `0` rank
mismatches, and all pre-existing tests pass unchanged.
*Protects:* the non-negotiable invariant.

---

## J. Temporal relationships

### J1 [r3] — A relationship that ended is representable without UPDATE
**Given** company `A` was a franchisee of `F` until 2027-06-30
**Then** one `ASSERTED` `FRANCHISE_OF` claim exists with
`valid_to = 2027-06-30`; the projection excludes it from current
relationships; and no row was updated.

### J2 [r3] — A sequence of relationships is representable
**Given** company `B` was a subsidiary of `X`, then acquired by `Y` on
2027-03-01
**Then** two claims exist — `(B, X, SUBSIDIARY_OF, valid_to = 2027-03-01)` and
`(B, Y, ACQUIRED_BY, valid_from = 2027-03-01)` — both immutable, and
`?as_of=2026-01-01` returns the first while the current view returns the second.

### J3 [r3] — A wrong relationship claim is retracted, not deleted
**Given** an erroneous `SUBSIDIARY_OF` claim
**When** it is corrected
**Then** a new `RETRACTED` claim exists with `supersedes_claim_id` pointing at
it, the original row is byte-identical, and the projection drops the
relationship.

### J4 [r3] — Inverse relationship types are not stored
**When** the schema is inspected
**Then** `PARENT_OF`, `FRANCHISOR_OF` and `ACQUIRER_OF` are absent from stored
`relationship_type` values, and the bidirectional view derives them from the
canonical direction.
*Protects:* two stored directions that could disagree.

### J5 [r3] — `SISTER_OF` is stored once
**Given** a symmetric sister relationship between `A` and `B`
**Then** exactly one row exists, with `from_company_id < to_company_id`
enforced by a CHECK, and both companies see it through the bidirectional view.

### J6 [r3] — `FORMERLY` is a name, not a relationship
**When** the schema is inspected
**Then** `FORMERLY` is absent from `relationship_type`, and a former name
appears in `company_names` with `name_type = FORMER`.

### J7 [r3] — Relationship projection is rebuildable
**Given** a populated registry
**When** `company_relationships` is truncated and rebuilt from relationship
claims
**Then** it is byte-identical, containing exactly the effective, non-retracted,
currently-valid relationships.

## Definition of done for M2

* Every scenario above passes against live PostgreSQL from an empty schema.
* No test requires network access; provider adapters are tested from recorded
  fixtures.
* Normalization functions are pure and independently tested.
* The full-rebuild scenario (B3) runs in CI, not only locally.
* `pytest` and `ruff` green, with M0/M1's existing suite passing unchanged.
* Migrations verified from an empty database.
* At least two provider adapters exist, so the abstraction is proven against
  more than one shape of data.
* The `AMBIGUOUS` review queue is reachable through the API.
* A documented promotion path for density proxies exists — and is **not** wired
  to run automatically.
* The derived-projection rebuild (B3) runs in CI, not only locally, and asserts
  that every `companies.id` is unchanged.
* The attribute registry is seeded and versioned, and B8/B9 enforce the
  contract on every write.
* At least one provider adapter of each `identity_capability` is exercised, so
  the derived-key and content-only paths are not theoretical.
