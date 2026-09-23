# M2 — Acceptance Criteria

**Status:** revision 5 (final). Implementation authorized. These scenarios are specified *before*
implementation, as M0/M1 were. None is implemented as a test yet.

Each states a **Given / When / Then** and the invariant it protects. An
implementation is not done until every scenario passes against live PostgreSQL
from an empty schema.

Scenarios marked **[r2]** were added in revision 2, **[r3]** in revision 3 and
**[r4]** in revision 4 and **[r5]** in revision 5, each covering the
invariants resolved in that revision.

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

### A5a [r4] — One version may hold many bodies
**When** the schema is inspected
**Then** `raw_body_sha256` lives on `provider_record_bodies`, not on
`provider_record_versions`; the relationship is **1:N**; and the unique
constraint is `(provider_record_version_id, raw_body_sha256)`.
*Protects:* the revision-3 placement, which silently asserted one body per
version while identity was defined semantically.

### A5b [r4] — Identical bytes re-fetched do not duplicate a body
**Given** a version with one body at `raw_body_sha256 = H`
**When** the identical bytes are fetched again
**Then** no second body row is created, and a sighting records the observation.

### A5c [r4] — Semantic mutation creates a version; reformatting does not
**Given** a version `V1`
**When** the provider returns semantically different content
**Then** a **new version** `V2` is created with its own body. **When** it
instead returns the same content reformatted, `V1` is unchanged and gains a
second body.

### A5d [r4] — Pruning bodies never changes version identity or history
**Given** a version with three bodies
**When** all bodies are pruned by retention
**Then** `canonical_payload_hash`, `parsed_payload`, sightings, decisions and
claims are all unchanged, and the version still resolves to the same company.

### A16 [r5] — Strategy and version participate in version identity
**When** the schema is inspected
**Then** the version uniqueness key is
`(provider_entity_id, canonicalization_strategy, canonicalization_version,
canonical_payload_hash)` — not the hash alone.

### A17 [r5] — Same strategy, same payload → no new version
**Given** entity `E` with a version under `JSON_CANONICAL_V1`
**When** the identical semantic payload is ingested under the same strategy and
version
**Then** no new version is created and a sighting is recorded.

### A18 [r5] — Same strategy, semantic mutation → new version
**Given** the same entity and strategy
**When** the semantic payload changes
**Then** a new version is created under the same entity.

### A19 [r5] — Different strategy version → distinct version even on hash equality
**Given** entity `E` with a version whose `canonical_payload_hash = H` under
strategy version `1`
**When** the same payload is ingested under strategy version `2` and the
canonicalization happens to produce the **identical hash `H`**
**Then** a **second, distinct version** exists, differing only in
`canonicalization_version`.
*Protects:* the revision-4 key, under which a strategy migration could silently
fail to create a version, or two strategies could be merged into one.

### A20 [r5] — Historical versions remain interpretable
**Given** versions written under two different strategy versions
**Then** each carries the strategy and version under which its hash was
computed, so an old digest is never re-interpreted under a newer algorithm.

### A13 [r4] — Canonicalization strategy is declared and stamped
**Given** a JSON provider and a CSV provider
**Then** each declares its own `canonicalization_strategy` and version; every
`provider_record_versions` row stamps both; and the JSON provider's strategy is
`JSON_CANONICAL_V1`, the algorithm M0 uses for snapshots.
*Protects:* the assumption that M0's canonical JSON is universal.

### A14 [r4] — A non-JSON provider may not borrow the JSON canonicalizer
**Given** an adapter whose payload is CSV, XML or HTML
**When** it declares `canonicalization_strategy = 'JSON_CANONICAL_V1'`
**Then** registration is rejected: the strategy must match the declared media
type.

### A15 [r4] — Changing a strategy is a visible, versioned event
**Given** a provider whose canonicalization version changes
**When** a previously-seen payload is re-ingested
**Then** it produces a **new version** stamped with the new strategy version,
the old version is untouched, and the change is attributable — never silent
churn.

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
**Then** `has_identity_collision` holds for the entity and resolution routes it
to `AMBIGUOUS` with `signals.reason = "identity_collision"` — it may neither
auto-match **nor create**.
*Protects:* a colliding derived key is weaker evidence than no key at all.
*Revised in r6:* the predicate is derived rather than stored, and blocking
creation is part of the guarantee — see revision 6, items 26–27.
*Test:* `test_a_colliding_derived_key_blocks_auto_matching`.

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

### B3d [r4] — Two rebuilds at different wall-clock times are identical
**Given** a populated registry with unchanged evidence
**When** every derived projection is rebuilt, the clock is advanced (by days,
across a relationship's and a presence's `effective_to` boundary), and it is
rebuilt again
**Then** the two rebuilds are compared by **exact normalized row-set equality**
— row for row, not by hash — and are identical.
*Protects:* the determinism contract. A digest is recorded in
`projection_runs` as telemetry, but the assertion is on the rows, so a failure
names the differing row (M2-ADR-030).

### B3e [r4] — A date-filtered view may legitimately change
**Given** the same unchanged projection
**When** the clock crosses a relationship's `effective_to`
**Then** `current_company_relationships` returns **fewer** rows, while
`company_relationships` and its digest are **unchanged**.
*Protects:* the separation between a time-independent projection and a view
that applies the date.

### B3f [r4] — Operational metadata cannot invalidate rebuild equality
**Given** two rebuilds producing identical projection digests
**Then** `projection_runs` holds two rows with different `started_at`,
`completed_at` and `triggered_by`, **and no projection table contains any
timestamp of its own**.
*Protects:* the revision-3 `last_projected_at` contradiction.

### B3g [r4] — No derived projection has a surrogate key
**When** the schema is inspected
**Then** every derived projection's primary key is a natural key composed of
stored columns; none is a generated UUID.
*Protects:* a surrogate key would differ on every rebuild.

### B3h [r4] — Projection array columns are sorted
**Given** a projection row whose `derived_from_claim_ids` draws on several
claims
**Then** the array is stored in ascending order, so two rebuilds cannot differ
by ordering alone.

### B3i [r4] — Tie-breaks do not depend on prior projection state
**Given** two claims that tie on human review, fact type, provider trust and
recency
**When** the projection is rebuilt from an **empty** table
**Then** the claim with the lowest `claim.id` wins, `projection_conflict` is
set, and the result equals a rebuild performed over a pre-populated table.
*Protects:* the revision-3 rule "keep the current projected value", which had
no meaning on a rebuild from empty.

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

### C11a [r4] — A valid chain within one entity is accepted
**Given** provider entity `A`
**When** decisions `A1 → A2 → A3` are written, each superseding the previous
**Then** all three commit, `A3` is the head, and the full chain is retrievable
in order.

### C11b [r4] — A decision of entity B cannot supersede a decision of entity A
**Given** decision `A1` belonging to provider entity `A`
**When** a decision belonging to entity `B` attempts
`supersedes_decision_id = A1.id`
**Then** the database rejects it on `fk_supersedes_same_entity` — the composite
foreign key requires the referenced row to share the same `provider_entity_id`.
*Protects:* the revision-3 hole — the partial indexes constrained chain *shape*
but not chain *ownership*, so B could have spliced itself into A's history,
leaving A headless and B forked.

### C11c [r4] — The invariant is declarative, not procedural
**When** cross-entity supersession is attempted through raw SQL, bypassing all
application code
**Then** it still fails.
*Protects:* correctness that does not depend on every writer remembering a
protocol.

### C11d [r4] — Cycles are unconstructible
**Given** any sequence of writes
**Then** no cycle exists in the supersession graph, because every insert must
reference an already-committed row and rows are never updated.

### C12 [r3] — The heads table is a cache, not the invariant
**Given** a populated registry
**When** `entity_resolution_heads` is truncated and rebuilt from decisions
**Then** it is byte-identical, it agrees row-for-row with the
`current_entity_resolutions` view, and resolution correctness was never
dependent on it.

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

### J7a [r4] — The relationship projection stores intervals, not a snapshot
**When** the schema is inspected
**Then** `company_relationships` has `effective_from` and `effective_to`
columns, contains relationships whose interval has already closed, and its
rebuild reads no clock.
*Protects:* the revision-3 "currently-valid" framing, which made content
depend on wall-clock time.

### J7b [r4] — The current view applies the date, the projection does not
**Given** a relationship with `effective_to = 2027-06-30`
**When** queried on 2027-06-01 and again on 2027-07-01
**Then** `current_company_relationships` includes it, then excludes it, while
`company_relationships` returns the identical row both times.

### J7c [r4] — `as_of` reproduces a historical answer exactly
**Given** the same relationship
**When** queried with `?as_of=2027-06-01` at any later date
**Then** the answer is the same as the current view returned on that date.

### J7 [r3] — Relationship projection is rebuildable
**Given** a populated registry
**When** `company_relationships` is truncated and rebuilt from relationship
claims
**Then** it is byte-identical, containing exactly the effective, non-retracted,
currently-valid relationships.

## K. Temporal market presence

### K1 [r5] — A company enters a market
**Given** a claim asserting `OPERATES` in DE from 2024-01-01
**Then** `company_market_presences` holds one row with
`effective_from = 2024-01-01`, `effective_to IS NULL`, and the current view
includes it.

### K2 [r5] — A company exits a market
**Given** a claim asserting the DE presence ended 2027-06-30
**Then** the projection row carries `effective_to = 2027-06-30`; the current
view excludes it after that date; and no row was deleted.

### K3 [r5] — A company leaves and later re-enters
**Given** DE presence from 2024-01-01 to 2026-01-01, then again from 2027-01-01
**Then** **two** rows exist for the same `(company_id, market_id,
presence_type)`, distinguished by `effective_from` — re-entry is a new
interval, not a correction of the old one.
*Protects:* the reason `effective_from` is part of the primary key.

### K4 [r5] — Presence type changes are two intervals
**Given** a company that served IE remotely from 2024-01-01 and opened a branch
on 2026-06-01
**Then** one row has `SERVES_REMOTELY` with `effective_to = 2026-06-01` and a
second has `BRANCH` with `effective_from = 2026-06-01`; `presence_type` was
never edited in place.

### K5 [r5] — An as-of query reproduces prior state
**Given** the history above
**When** queried with `?as_of=2025-01-01`
**Then** the answer equals what the current view returned on that date.

### K6 [r5] — CURRENT_DATE influences only the view
**Given** unchanged evidence
**When** the projection is rebuilt before and after an `effective_to` boundary
passes
**Then** `company_market_presences` is row-for-row identical both times, while
`current_company_market_presences` returns fewer rows after the boundary.

### K7 [r5] — Provider silence never closes a presence
**Given** a company with an open DE presence
**When** a later discovery run returns no record for that company at all
**Then** the presence remains open, no `effective_to` is written, and no
negative claim is created.
*Protects:* absence of evidence is not evidence of absence — the same rule that
stops a missing value becoming a zero in M0.

### K8 [r5] — The presence projection is rebuildable
**Given** a populated registry
**When** `company_market_presences` is truncated and rebuilt
**Then** it is row-for-row identical, intervals included.

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
* B3d–B3i run in CI, including a rebuild with the clock advanced across a
  relationship boundary, so determinism is proven rather than asserted.
* C11b is exercised through raw SQL, not only through application code.
* A19 and K3 are exercised, since both encode a key that would otherwise look
  redundant.


---

## Revision 6 — scenarios added after implementation

Each of these exists because implementing revision 5 produced a wrong result
that no revision-5 scenario would have caught. They are listed with the test
that executes them; all run against real PostgreSQL and none touches the
network.

### L. Run completion is durable

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| L1 | A run fails mid-fetch, is then normalized, and resolution is requested | `PARTIAL_FETCH_NOT_RESOLVABLE` (409). Normalizing must not make an incomplete fetch resolvable | `test_a_partial_run_refuses_to_write_anything_canonical` |
| L2 | The same run, created with `allow_partial_resolution=true` | Resolution proceeds | `test_partial_resolution_is_possible_only_when_explicitly_opted_into` |
| L3 | A failed fetch | Evidence already fetched is retained; nothing canonical is written | `test_a_failed_fetch_keeps_the_evidence_it_paid_for` |
| L4 | A page is fetched twice (interrupted run resumed) | No duplicate query row, version or body; second fetch creates nothing | `test_a_resumed_fetch_does_not_duplicate_evidence` |
| L5 | One provider's run fails | Another provider's run is unaffected | `test_one_provider_failing_does_not_affect_another` |
| L6 | A second run fetches the same unchanged records | It still *sees* them (via sightings) and reports `unchanged`, not `created`; no new decision and no duplicate claim is written | `test_a_second_run_sees_the_same_records_and_changes_nothing` |
| L7 | A run fetches, then the worker dies before resolving | A later run over the same records resolves that evidence rather than stranding it | `test_a_run_that_died_before_resolving_can_be_resolved_later` |

### M. Uninterpretable records

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| M1 | One record on a page cannot be normalized | The other records on that page are stored normally; the failure is recorded on `provider_record_normalizations.error` with a null payload | `test_a_bad_record_does_not_abort_normalization_of_the_rest` |
| M2 | The same run is then resolved | The uninterpretable record resolves to `AMBIGUOUS`, not to a new company | same test |
| M3 | A candidate with neither name nor domain | `AMBIGUOUS` with `reason = no_identifying_attributes`; no company is created | same test |

### N. Domain policy is enforced on write

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| N1 | Two unrelated firms on `*.wixsite.com` | Two companies; the shared domain is projected as `GROUP` for both and `IDENTITY` for neither | `test_shared_platform_domain_does_not_merge_distinct_companies` |
| N2 | The blocklist, exhaustively | No blocklisted host is an identity domain; a real domain is | `test_hosting_domains_never_carry_identity` |

### O. Concurrency on real connections

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| O1 | Two workers resolve the same unresolved entity concurrently | One decision, one company, no orphan company from the loser; both callers see the same decision id | `test_two_workers_racing_to_create_produce_one_company` |
| O2 | Two reviewers supersede the same head concurrently | One supersession survives; the loser is refused with `IntegrityError`, not silently re-parented | `test_two_reviewers_racing_to_supersede_produce_one_chain` |
| O3 | A second worker resolves an already-resolved entity | Nothing new is written | `test_a_second_worker_finds_the_entity_already_resolved` |

### P. Registry and null semantics

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| P1 | A `vertical` claim typed `FACT` | Rejected: a classification is an interpretation, never an observation | `test_registry_rejects_a_fact_type_the_attribute_does_not_allow` |
| P2 | A `NOT_AVAILABLE` claim | Stored with SQL `NULL` value, null fact type, null confidence; satisfies `ck_company_claims_availability_consistency`; projects nothing | `test_a_not_available_claim_is_not_a_fact` |
| P3 | The M1 firewall allowlist | Covers every M0/M1 table, including `observation_sources` | `test_the_firewall_watches_every_m1_evidence_table` |

### Q. Derived-key collisions (A10, re-specified)

| # | Scenario | Expected | Test |
| --- | --- | --- | --- |
| Q1 | Two firms with different names on one corporate domain, under a `DERIVED_STABLE_KEY` provider | One entity, two versions, `AMBIGUOUS` with `reason = identity_collision`, **zero** companies created | `test_a_colliding_derived_key_blocks_auto_matching` |
| Q2 | The same name seen twice under one derived key | Not a collision; resolves normally | `test_one_derived_entity_with_a_renamed_record_is_not_a_collision` |
| Q3 | Two different names under one **native** provider id | Not a collision: the provider asserted they are one object | `test_a_native_id_entity_cannot_collide_this_way` |
| Q4 | An unchanged `AMBIGUOUS` entity re-resolved | No new decision; the chain does not grow | `test_re_resolving_an_ambiguous_entity_does_not_grow_the_chain` |
