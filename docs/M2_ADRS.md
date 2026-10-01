# M2 — Architecture Decision Records

**Status:** revision 5 (final). Implementation authorized. None is implemented. Numbered in their own
`M2-` series so they cannot be confused with the accepted ADR-001 … ADR-020
governing shipped code.

M2-ADR-001 … 009 were written for revision 1, 010 … 017 for revision 2,
018 … 023 for revision 3, 024 … 027 for revision 4, and 028 … 030 for
revision 5. Superseded decisions are marked and cross-referenced;
originals are retained rather than rewritten, so the reasoning trail survives.

---

## M2-ADR-001 — Canonical company identity is market- and vertical-agnostic

**Status:** Accepted (design) · extended by M2-ADR-011

A company may operate in several markets, hold several locations and belong to
several verticals. `companies` therefore carries no `market_id` and no
`vertical_id`; market presence and vertical membership are separate
evidence-bearing relationships.

This mirrors ADR-002: just as a score is not an attribute of a market, a market
is not an attribute of a company.

---

## M2-ADR-002 — Presence and location are different concepts

**Status:** Accepted (design) · amended by M2-ADR-015

Two tables. `company_locations` records physical or registered places;
`company_market_presences` records the claim of operating in a market. Presence
can exist without a location, and vice versa.

---

## M2-ADR-003 — Fuzzy name similarity is a retrieval mechanism, never a decision

**Status:** Accepted (design)

Trigram similarity may only *retrieve* candidates. A merge requires a
corroborating non-name signal. Scores between the two configured thresholds
produce an `AMBIGUOUS` decision that creates nothing and merges nothing.

Some duplicates will persist until better evidence arrives. That is the intended
failure mode — the same discipline that keeps M1 coverage honest rather than
inventing vertical density.

---

## M2-ADR-004 — Provider records are append-only and content-hashed

**Status:** Accepted (design) · **superseded in structure by M2-ADR-012**

The principle stands: provider output is evidence, therefore append-only and
content-addressed. The single-table shape it proposed does not — see
M2-ADR-012.

---

## M2-ADR-005 — Discovery counts never become FACT

**Status:** Accepted (design) · **the load-bearing M1/M2 boundary**

A discovery run never writes to `market_observations`. Promotion to M1 is
explicit, human-authorised, logged and reversible, and enters as
`fact_type = PROXY` carrying provider, adapter version, query definition,
retrieval date, coverage assumption, calibration and confidence.

M1 currently represents vertical density as a research gap. That is accurate.
Replacing an honest gap with a provider-biased number would be a regression
disguised as progress.

---

## M2-ADR-006 — PostgreSQL constraints over distributed coordination

**Status:** Accepted (design) · extended by M2-ADR-010

Rely on the database for concurrency: a partial unique index, then
`INSERT ... ON CONFLICT DO NOTHING ... RETURNING` with a re-read, then a
transactional advisory lock for the narrow non-domain case. No new
infrastructure, consistent with ADR-001.

---

## M2-ADR-007 — Provider normalization is a pure function

**Status:** Accepted (design)

`normalize(raw) -> CandidateCompany` performs no I/O and no database access.
Fetching is separate and returns raw payloads; normalization and resolution
operate on stored records. Adapter behaviour is testable from fixtures with zero
network, and a normalization fix is replayable over historical records.

---

## M2-ADR-008 — Entity resolution decisions are append-only and explainable

**Status:** Accepted (design) · **direction corrected by M2-ADR-013**

Decisions record the outcome, method, signal vector, rationale and actor, and
are never updated. The supersession *mechanism* it proposed was self-
contradictory — see M2-ADR-013.

---

## M2-ADR-009 — Reuse M0's evidence vocabulary rather than inventing a second one

**Status:** Accepted (design) · extended by M2-ADR-014

`company_claims` and `company_verticals` reuse `FactType`, `availability`,
`period_granularity` and the single confidence algorithm. One vocabulary across
the whole engine; a consumer learns it once.

---

# Revision 2 decisions

## M2-ADR-010 — PostgreSQL `FOR UPDATE SKIP LOCKED` instead of Redis

**Status:** Accepted (design) · resolves correction item 9

### Context
Revision 1 stated "Redis plus a lightweight worker" without evaluating it. The
architecture rules require the smallest thing consistent with a modular
monolith, one developer and PostgreSQL already being present.

### Options

| | PostgreSQL `SKIP LOCKED` | Redis + worker |
| --- | --- | --- |
| New infrastructure | None | A second datastore to run, monitor, back up |
| Transactional with domain writes | **Yes** — one transaction | No — dual-write, needs an outbox to be correct |
| Durability | ACID by construction | Needs explicit persistence configuration |
| Visibility | Plain SQL | Separate tooling |
| Throughput ceiling | Thousands/sec | Far higher, and unneeded |
| Burden for one developer | Low | Meaningfully higher |

### Decision
A PostgreSQL job table consumed with `SELECT ... FOR UPDATE SKIP LOCKED`.
**Redis is removed from the M2 design.**

M2's workload is tens to low thousands of provider calls per run, bounded by
provider rate limits — orders of magnitude below where a dedicated queue earns
its operational cost. The decisive factor is transactionality: a job that
enqueues follow-up work in the same transaction as its raw writes cannot drift
out of sync, which with Redis would require an outbox pattern to achieve the
same guarantee.

### Revisit if
Sustained throughput exceeds a few thousand jobs/second, **or** fan-out/pub-sub
delivery becomes a requirement, **or** workers must run where PostgreSQL is not
reachable. None applies at M2.

### Consequences
* No new infrastructure in M2, consistent with ADR-001.
* The queue is an implementation detail; no domain table depends on it.

---

## M2-ADR-011 — A company is a commercial operating organization, not a legal entity

**Status:** Accepted (design) · resolves correction item 1

### Context
Revision 1 never defined "company", then let exact domain equality merge
records. Without a definition, every resolution rule is arbitrary — and the
undefined unit silently defaulted to "whatever the provider indexed".

### Decision
> A company is the canonical **commercial operating organization** — the unit
> BoRo would sell to, contract with and deliver to as one account.

Tested by commercial autonomy, operational coherence and account identity, in
that order. Legal entities are recorded as *claims about* a company, never as
its identity.

Consequences by structure: branches and depots are locations, not companies;
operationally autonomous subsidiaries and franchisees **are** companies, linked
by `company_relationships`; holding companies are usually relationship nodes
rather than targets.

### Consequences
* `company_relationships` becomes necessary — without it, the only way to record
  a real corporate connection is a merge, which would be wrong.
* The identity policy is versioned and stamped on every decision, so changing it
  is a dated decision rather than a silent behaviour change.
* Franchise networks — the most common false-merge trap — are representable
  correctly.

---

## M2-ADR-012 — Provider identity and provider observations are separate tables

**Status:** Accepted (design) · supersedes the structure in M2-ADR-004 ·
**extended by M2-ADR-019** (payload fidelity) and **M2-ADR-020** (providers
without stable external ids)

### Context
Revision 1 required `provider_records` to be `UNIQUE (provider_id,
external_id)` while also creating a new row whenever the payload changed. Those
two requirements cannot both hold.

### Decision
Split the concerns:

* **`provider_entities`** — stable provider-side identity,
  `UNIQUE (provider_id, provider_external_id)`.
* **`provider_record_versions`** — immutable observations,
  `UNIQUE (provider_entity_id, payload_hash)`.

Identical payload → no new version (a sighting is recorded instead). Changed
payload → new version under the same entity. Nothing is ever overwritten.

### Consequences
* Provider drift becomes first-class history rather than a lost update.
* `provider_record_sightings` is introduced so "re-confirmed unchanged on date
  Y" does not require a mutable `last_seen_at`. It is deferrable if recency
  tracking is not needed in the first cut.
* One open question remains: where the normalized payload lives, given the raw
  table is strictly immutable. A separate
  `provider_record_normalizations` table is preferred (design §13.1).

---

## M2-ADR-013 — Supersession points backwards, from new decision to old

**Status:** Accepted (design) · corrects M2-ADR-008 · **extended by M2-ADR-018**
(which closes the concurrent-root gap this ADR left open)

### Context
Revision 1 declared decisions append-only, then wrote `superseded_by_id` onto
the old row — an UPDATE. It also stored `resolved_company_id` on immutable raw
records, so correcting a resolution would have required mutating evidence.

### Decision
1. **`new_decision.supersedes_decision_id = old_decision.id`.** The old row is
   never touched. `UNIQUE (supersedes_decision_id)` prevents forked history.
2. **Raw provider tables carry no resolution state.** No
   `resolved_company_id`, no `resolution_decision_id`.
3. Effective resolution is **derived**: the latest decision for a provider
   entity that nothing supersedes, exposed as a `current_entity_resolutions`
   view.

### Consequences
* Correcting a resolution is purely additive.
* A full decision chain is reconstructable in both directions.
* "Why is this company merged?" and "what did we think before?" are both
  answerable months later.
* The anti-join needs a partial index; if chains ever grow long enough to
  matter, the view becomes materialized — a performance change with no semantic
  change.

---

## M2-ADR-014 — Evidence records claims, and canonical fields are projections

**Status:** Accepted (design) · extends M2-ADR-009 · **identity framing superseded
by M2-ADR-023**, typing contract **extended by M2-ADR-022**

### Context
Revision 1's `company_evidence` recorded *which source* spoke about an
attribute but not *what it said*. It could not answer "provider A said 40 on
date X, then 45 on date Y" — the question it existed for.

### Decision
* **`company_claims`** holds the normalized value: `attribute_key`,
  `value_jsonb` plus typed shadows, `fact_type`, `availability`, `confidence`,
  `provider_record_version_id`, `resolution_decision_id`, `observed_at`,
  `period_granularity`. Append-only.
* **`company_evidence` is removed** — a strict subset with worse completeness.
* **`companies` and every other canonical table are projections**, mutable and
  fully rebuildable from claims + decisions + identity policy version.
* Precedence: human review, then fact type, then provider trust tier, then
  recency, then a stability tie-break with a conflict flag.
* Range-valued attributes project an **envelope** across equally-ranked claims
  rather than picking a winner.

### Consequences
* Claim history is complete and immutable; correcting a projection never edits
  evidence.
* A wrong projection means the precedence rules or trust tiers are wrong — both
  versioned configuration, both fixable without touching data.
* Rebuild-equals-original becomes a testable invariant (acceptance B3).
* The envelope rule preserves provider disagreement as information, mirroring
  M0's refusal to collapse score and coverage into one number.

---

## M2-ADR-015 — Service coverage is presence, never a physical location

**Status:** Accepted (design) · amends M2-ADR-002 · resolves correction item 6

### Context
Revision 1 listed `SERVICE_AREA` among physical `company_locations` types,
which would have fabricated buildings out of coverage statements.

### Decision
`company_locations.location_type` is restricted to `HEADQUARTERS`, `BRANCH`,
`DEPOT`, `REGISTERED_OFFICE`. Operating geography lives in
`company_market_presences`, including `SERVES_REMOTELY`.

### Consequences
* "Serves Ireland from Belfast" is a presence with no Irish location — correct,
  and now the only representable option.
* Sub-national coverage ("serves Bavaria") has no home, since M1 markets are
  national. Recorded as an open question rather than forced into a location row.

---

## M2-ADR-016 — A run must reach FETCHED before touching the canonical registry

**Status:** Accepted (design) · resolves correction item 7

### Context
Revision 1 said a provider outage produces a `FAILED` run with no canonical
writes, while also describing independently retryable stages that could write
canonically. Both could not be true.

### Decision
Eight lifecycle states: `PENDING`, `FETCHING`, `PARTIAL_FETCH`, `FETCHED`,
`NORMALIZING`, `RESOLVING`, `COMPLETED`, `FAILED`.

> A run that has not reached `FETCHED` does not write to the canonical
> registry, unless created with `allow_partial_resolution = true`.

Transaction boundaries: fetch commits per page (raw evidence already paid for
must survive later failure); normalize commits per version; resolve commits per
provider entity in one short transaction.

### Consequences
* Both halves are now true: partial raw evidence **is** retained, and the
  canonical registry **is not** touched by an incomplete fetch.
* A truncated result set is not treated as evidence of absence, so a provider
  outage cannot silently shrink a market.
* Retry resumes from the last cursor rather than restarting; unchanged payloads
  produce sightings, not duplicates.
* `PARTIAL_FETCH` must stay distinct from `FAILED` — collapsing them would lose
  exactly the distinction that makes retry safe.

---

## M2-ADR-017 — Domain equality is graded, not a universal unique key

**Status:** Accepted (design) · resolves correction item 8

### Context
Revision 1 placed `UNIQUE (primary_domain)` on `companies` and treated exact
domain equality as a deterministic merge. That assumes one domain implies one
organization, which is false for group domains, subsidiaries on a parent
domain, hosting/marketplace domains, and post-acquisition redirects.

### Decision
Normalize to the registrable domain (eTLD+1 via the Public Suffix List);
subdomains are never identity. Then grade the match:

* **Deterministic** — `IDENTITY`-role registrable domain, not blocklisted, not
  `GROUP`, and no conflicting strong signal.
* **Strong candidate** — shared `GROUP` domain, materially different
  names/locations, or a subdomain match.
* **Insufficient** — blocklisted hosting/generic domain, parked domain, or a
  marketplace profile URL.

The constraint moves from `companies` to
`company_domains UNIQUE (domain_normalized) WHERE domain_role = 'IDENTITY'`.
`companies.primary_domain` survives only as a projection.

### Consequences
* Race protection for the deterministic case is preserved.
* Group domains and shared hosting no longer force false merges.
* Domain migration and acquisition are expressible: one company changing
  domains, or two companies linked by `ACQUIRED_BY` — never a silent merge.


---

# Revision 3 decisions

## M2-ADR-018 — Two partial unique indexes guarantee one effective resolution head

**Status:** Accepted (design) · extends M2-ADR-013 · **completed by M2-ADR-025**
(which adds the missing same-entity constraint)

### Context
`UNIQUE (supersedes_decision_id)` stops a decision being superseded twice, but
PostgreSQL permits multiple NULLs in a unique index — so two concurrent workers
could each write a *root* decision for the same provider entity, producing two
independent chains and therefore two effective heads.

### Options

| Option | Assessment |
| --- | --- |
| Transactional advisory lock on `provider_entity_id` | Works, but correctness depends on every writer remembering to take it. A forgotten lock fails silently, and ad-hoc SQL bypasses it entirely |
| Mutable `entity_resolution_heads` table as the invariant | Works, but makes correctness depend on mutable state that the rebuild invariant then has to carve an exception for |
| **Two partial unique indexes** | **Chosen** |

### Decision
```sql
CREATE UNIQUE INDEX uq_resolution_root
    ON entity_resolution_decisions (provider_entity_id)
    WHERE supersedes_decision_id IS NULL;

CREATE UNIQUE INDEX uq_resolution_supersedes
    ON entity_resolution_decisions (supersedes_decision_id)
    WHERE supersedes_decision_id IS NOT NULL;
```

One root per entity, and each node superseded at most once, means the decision
graph is a **linear chain** — therefore exactly one head, by construction.

`entity_resolution_heads` is retained, demoted to a **derived cache** for O(1)
lookup, maintained in the same transaction and fully rebuildable.

### Consequences
* The invariant is declarative and enforced for every writer, including ad-hoc
  SQL — not merely for code that remembers a protocol.
* Both races resolve through the same `ON CONFLICT` / re-read pattern already
  proven under a real two-connection race by M1's research-gap detector.
* The head anti-join returns exactly one row, so `ORDER BY ... LIMIT 1` is no
  longer needed for correctness.
* Decisions remain strictly append-only.

---

## M2-ADR-019 — Raw bytes are byte-faithful; identity is the canonical digest

**Status:** Accepted (design) · resolves invariant 3

### Context
Revision 2 described the stored JSONB as a "verbatim" raw payload. JSONB
preserves neither original bytes, whitespace, key ordering nor duplicate keys,
so the claim was false — and an evidence system that misdescribes its own
fidelity is worse than one that stores less.

### Decision
Policy A, byte-faithful, with three artifacts doing three jobs:

| Artifact | Location | Job |
| --- | --- | --- |
| `raw_body bytea` | `provider_record_bodies` | Literal response bytes |
| `raw_body_sha256` | `provider_record_versions` | Fidelity auditing |
| `parsed_payload jsonb` | `provider_record_versions` | Queryable representation |
| `canonical_payload_hash` | `provider_record_versions` | **Identity and idempotency** |

The word "verbatim" is removed from any description of JSONB.

**Identity stays on the canonical semantic digest**, computed exactly as M0
computes snapshot identity (ADR-017). A provider reformatting its JSON must not
manufacture a spurious version — the same reasoning that makes a reindented
source file a no-op import in M0.

Bodies live in their own table so a retention or redaction policy can prune
them without touching the append-only version row. Pruning a prunable table is
not an `UPDATE` on an immutable one, and the design says so explicitly rather
than quietly permitting `SET raw_body = NULL`.

### Consequences
* Two byte-different bodies with the same canonical hash are one version with
  two bodies — both facts preserved.
* Body retention becomes a policy question (open question 5) rather than a
  silent data-loss path.

---

## M2-ADR-020 — Provider identity capability is explicit

**Status:** Accepted (design) · resolves invariant 4

### Context
Revision 2 assumed `UNIQUE (provider_id, provider_external_id)` was always
meaningful. Many useful sources — directory scrapes, association member lists,
search results — issue no durable identifier at all.

### Decision
`discovery_providers.identity_capability ∈ {NATIVE_EXTERNAL_ID,
DERIVED_STABLE_KEY, CONTENT_ONLY}`, with
`provider_entities.external_id_kind ∈ {NATIVE, DERIVED, CONTENT}` recording how
each key was obtained.

`DERIVED_STABLE_KEY` requires declared `key_fields`, a stamped
`key_algorithm_version`, and explicit collision handling: an entity whose
non-key identity fields materially disagree is flagged `identity_collision` and
**must route to `AMBIGUOUS`** rather than auto-match. When key fields change, a
**new** entity is created; the two are linked only by both resolving to the same
company, because claiming the provider said they were the same object would be
false.

`CONTENT_ONLY` entities are keyed by canonical payload hash and **cannot express
"the same object changed"** — a limitation stated in the design rather than
hidden, and one their trust tier should reflect.

### Consequences
* A derived key is never presented as a provider identifier.
* A colliding derived key is treated as *weaker* evidence than no key, which is
  the honest ordering.
* Content-only providers are usable for candidate discovery but not for
  tracking change over time.

---

## M2-ADR-021 — Relationships are time-bounded claims, and inverses are derived

**Status:** Accepted (design) · resolves invariant 5

### Context
Revision 2's `company_relationships` was append-only with a uniqueness
constraint, so it could not express a relationship that ends, one that is later
corrected, or a sequence such as "subsidiary of X, then acquired by Y" — every
one of which would have required an `UPDATE`.

It also stored both directions (`SUBSIDIARY_OF` and `PARENT_OF`), which is two
rows that can disagree.

### Decision
* `company_relationship_claims` becomes the evidence table: `valid_from`,
  `valid_to`, `assertion ∈ {ASSERTED, RETRACTED}`, `supersedes_claim_id`
  (`UNIQUE`), plus the standard provenance columns.
* `company_relationships` becomes a **derived projection** of effective,
  non-retracted, currently-valid relationships.
* Only the canonical direction is stored: `SUBSIDIARY_OF`, `FRANCHISE_OF`,
  `ACQUIRED_BY`, and the symmetric `SISTER_OF` (stored once, with
  `from_company_id < to_company_id` enforced by CHECK). `PARENT_OF`,
  `FRANCHISOR_OF` and `ACQUIRER_OF` are exposed through a bidirectional view.
* `FORMERLY` is removed — "formerly known as" is a name claim belonging in
  `company_names` with `name_type = FORMER`. It was a relationship only by
  accident of vocabulary.

### Consequences
* All three required statements are representable with no `UPDATE`.
* A relationship ending is an interval bound; a relationship being wrong is a
  retraction. Neither is an erasure.
* Historical queries (`?as_of=DATE`) become possible, which M3 will need.
* Two stored directions can no longer disagree, because only one is stored.

---

## M2-ADR-022 — A versioned attribute registry replaces untyped EAV

**Status:** Accepted (design) · extends M2-ADR-014 · resolves invariant 6

### Context
`company_claims` with a free-text `attribute_key`, a `value_jsonb` and two
typed shadow columns is an untyped entity-attribute-value store. Nothing
defined what an attribute means, what shape its value takes, which shadow is
authoritative, or how conflicts project — so every consumer would have had to
re-derive that knowledge and they would have disagreed.

### Decision
A versioned `attribute_definitions` registry, held in code as configuration and
persisted at seed time exactly as M0 persists `scoring_models`. Per attribute:
`value_kind` (SCALAR / RANGE / SET), `value_type`, `allowed_units`,
`allowed_fact_types`, `cardinality`, `projection_strategy`,
`conflict_strategy`, `shadow_column`, `value_schema`, `target_projection`,
`index_strategy`.

Every claim stamps the `attribute_registry_version` it was written against. A
claim whose key is absent from that version, or whose value fails its schema,
is rejected at write time with `ATTRIBUTE_NOT_IN_REGISTRY`.

`value_jsonb` and its typed shadow must never disagree. Preferred enforcement is
a PostgreSQL **generated column**, which makes disagreement unrepresentable
rather than merely forbidden; where the shape varies, the registry's declared
extractor populates the shadow and acceptance scenario B8 asserts agreement
across every row.

### Consequences
* Projection and conflict behaviour become properties of the attribute, not of
  the code that happens to read it.
* `employee_count` projecting an envelope while `legal_name` projects a single
  winner is configuration, not a special case.
* Registry migration becomes a real open question (open question 1) — which is
  better than having no contract to migrate.

---

## M2-ADR-023 — Company identity is a durable anchor; attribution is derived

**Status:** Accepted (design) · supersedes the projection framing in
M2-ADR-014 · resolves invariants 1 and 7

### Context
Two linked contradictions:

* Revision 2 called `companies` a mutable projection while claims, decisions and
  relationships held foreign keys to it, and simultaneously required all
  projections to be truncatable and byte-identically rebuildable. Rebuilding
  would have had to regenerate UUIDs, breaking every reference.
* Claims carried `company_id`, so correcting a resolution from company A to
  company B would have required rewriting immutable claims.

### Decision
**Identity anchor.** `companies` holds only `id`, `created_at`,
`identity_policy_version`, `lifecycle_status` and `merged_into_company_id`. It
is **never truncated**. Business attributes move to `company_profiles` and the
other derived tables.

There is deliberately **no `created_by_decision_id` column** — it would create
a circular foreign key (`companies → decisions → companies`). The creating
decision is derived by query.

`lifecycle_status` stays on the anchor because it governs whether an id may be
referenced as a live target, so it must be readable without running a
projection. It is maintained transactionally and **reconcilable** against
decisions — a consistency check, not a rebuild input (acceptance B3c).

**Derived attribution.** `company_id` is **removed** from provider-sourced
claims. Attribution runs `claim → version → entity → effective decision →
company`. A claim carries `subject_company_id` only when it is directly
attributed (human or derived), with a CHECK permitting exactly one attribution
path.

**Projection input rule.** A rebuild consumes only claims whose provider entity
resolves, under the selected identity policy version, to an effective
non-superseded decision naming that company — plus directly-attributed claims.

**Revised rebuild invariant.** All *derived projections* may be truncated and
rebuilt byte-identically, while identity anchors, claims, decisions and
relationship claims are never truncated.

### Consequences
* Correcting a resolution moves the entire claim history to the new company
  with **zero writes** to `company_claims`.
* The old company's projection loses those claims on the next rebuild — no
  contamination — while they stay queryable as history via
  `resolution_decision_id`.
* `resolution_decision_id` on a claim becomes purely historical: "what was in
  force when this was written". It is never used for attribution.
* Company UUIDs are exempt from the rebuild invariant because they are *minted*
  by judgement, not computed. Making them deterministic would require hashing an
  identity that is by definition a judgement call, reintroducing exactly the
  false-merge problem the identity policy exists to prevent.


---

# Revision 4 decisions

## M2-ADR-024 — Projections are pure; time and telemetry live outside them

**Status:** Accepted (design) · refines M2-ADR-023 · resolves invariant 1

### Context
Revision 3 claimed derived projections rebuild byte-identically, while
simultaneously storing `company_profiles.last_projected_at` and describing
`company_relationships` as holding "currently-valid" relationships. Both make
projection content depend on wall-clock time, so the same evidence would rebuild
differently tomorrow.

A third, subtler violation: precedence rule 5 resolved ties by "keeping the
current projected value" — which has no meaning when rebuilding from an empty
table, making the projection depend on its own prior state.

### Decision
The projection function `P` is **pure**. Its only inputs are the evidence
tables, `attribute_registry_version` and `identity_policy_version`. It reads no
clock, no sequence, no random source and no prior projection.

Five consequences, each removing a specific hazard:

| Hazard | Resolution |
| --- | --- |
| Rebuild timestamps | Moved to `projection_runs` — a new operational table outside every determinism claim |
| Surrogate keys | Forbidden in the derived tier; projections use natural keys |
| Array ordering | Arrays stored sorted |
| Wall-clock filtering | Projections store intervals; date predicates move into views |
| Tie-breaks on prior state | Replaced by lowest `claim.id` — a total order over stored, immutable values |

**The equality contract.** Comparison is over *all* columns of every projection
table, because nothing non-deterministic is stored in one. Mechanically, a
per-table canonical digest:

```sql
SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM <projection> t;
```

`projection_runs` records these digests per rebuild, so drift is detectable
without keeping a second copy of the data.

**What may still vary with time.** Views — `current_company_relationships`,
`company_claims_effective`, `current_entity_resolutions`. A view returning
different rows tomorrow is correct and says nothing about the projection
beneath it.

### Consequences
* Determinism becomes testable rather than asserted (scenarios B3d–B3i).
* "No persisted projection may depend on `now()`" becomes a general rule, not a
  relationship special case.
* `projection_runs` gives rebuild telemetry a home, which is why no projection
  needs a timestamp of its own.

---

## M2-ADR-025 — Supersession is confined to one provider entity by a composite FK

**Status:** Accepted (design) · completes M2-ADR-018 · resolves invariant 2

### Context
`uq_resolution_root` and `uq_resolution_supersedes` constrain the *shape* of a
decision chain — one root, one child each. Neither constrains its *ownership*.
Nothing prevented a decision belonging to provider entity **B** from superseding
a decision belonging to entity **A**, which would splice two histories into one
chain: A would be left headless (its root superseded by a foreign decision) and
B would hold two chains.

### Decision
A composite self-foreign-key makes the illegal state unrepresentable:

```sql
ALTER TABLE entity_resolution_decisions
  ADD CONSTRAINT uq_decision_entity UNIQUE (id, provider_entity_id);

ALTER TABLE entity_resolution_decisions
  ADD CONSTRAINT fk_supersedes_same_entity
  FOREIGN KEY  (supersedes_decision_id, provider_entity_id)
  REFERENCES entity_resolution_decisions (id, provider_entity_id);
```

The referenced row must match on **both** columns, so a superseding decision can
only point at a decision of the same entity. `UNIQUE (id, provider_entity_id)`
is redundant against the primary key; it exists solely because PostgreSQL
requires a unique constraint covering the referenced columns.

### Consequences
* The full invariant is now: one root per entity, one child per decision, all
  within one entity, no self-reference — therefore exactly one linear chain and
  exactly one head, by construction.
* It holds for raw SQL, not only for code that remembers a protocol — which is
  the same reason the root index was chosen over an advisory lock.
* Cycles are unconstructible rather than merely forbidden: every insert
  references an already-committed row, and rows are never updated.

---

## M2-ADR-026 — Byte fidelity belongs to the body, not the version

**Status:** Accepted (design) · corrects M2-ADR-019 · resolves invariant 3

### Context
M2-ADR-019 established that version identity is the **canonical semantic**
digest, so a provider reformatting its JSON does not create a new version. It
then placed `raw_body_sha256` on `provider_record_versions` — a column that can
hold exactly one value per version, silently asserting one body per version.
The two statements cannot both be true.

### Decision
Byte fidelity moves to the body, and the relationship becomes **1:N**:

```
provider_record_versions  1 ──── N  provider_record_bodies
  canonical_payload_hash              raw_body, raw_body_sha256,
  parsed_payload                      content_type, retrieved_at,
  canonicalization_strategy           discovery_query_id
  UNIQUE (entity, canonical_hash)     UNIQUE (version_id, raw_body_sha256)
```

| Event | Version | Body |
| --- | --- | --- |
| Identical bytes re-fetched | unchanged | deduped |
| Reformatted, same semantics | **unchanged** | **new body** |
| Semantic change | **new version** | new body under it |
| Retention pruning | untouched | removed |

`provider_record_bodies` keeps a surrogate `id` because it is evidence; the
no-surrogate-key rule applies only to the derived tier.

### Consequences
* "Which exact bytes did this provider send, and when?" is answerable for every
  distinct representation, not just the most recent.
* Pruning bodies cannot alter semantic identity or history.
* Bodies being retained is what keeps re-canonicalization possible if a
  strategy changes (M2-ADR-027).

---

## M2-ADR-027 — Canonicalization is declared per provider and versioned

**Status:** Accepted (design) · extends M2-ADR-019 · resolves invariant 4

### Context
M0's canonical-JSON algorithm — sorted keys, tight separators,
`ensure_ascii=False`, `allow_nan=False` — is defined for JSON documents.
Revision 3 assumed it applied to all provider payloads. Providers return CSV
exports, XML feeds, HTML pages and binary formats, none of which have a
meaningful sorted-key form.

### Decision
Each provider declares `canonicalization_strategy` and
`canonicalization_version` in `discovery_providers`, and every
`provider_record_versions` row stamps both, so a hash is always interpretable.

`JSON_CANONICAL_V1` is defined as M0's algorithm; JSON providers reuse it
directly. Non-JSON sources must declare an explicit, versioned strategy before
their adapter is written, and registration is rejected if a declared strategy
does not match the payload's media type.

**No provider-specific algorithm is specified here.** Inventing an HTML or CSV
canonicalization in the abstract, with no provider to validate it against,
would be guessing — and a wrong canonicalizer silently corrupts identity for
every record it touches.

### Consequences
* No adapter can quietly borrow the JSON algorithm for a payload it does not
  fit.
* Changing a strategy changes canonical hashes, so previously-seen payloads
  produce **new versions**. That is a migration event, recorded as open
  question 9, and retained bodies are what make re-canonicalization possible.
* The strategy is stamped on the version, so a hash computed under an old
  strategy stays interpretable after the strategy changes.


---

# Revision 5 decisions

## M2-ADR-028 — The canonicalization contract is part of version identity

**Status:** Accepted · resolves invariant 13

### Context
M2-ADR-027 made canonicalization per-provider and versioned, stamping
`canonicalization_strategy` and `canonicalization_version` on every version. But
version identity remained `(provider_entity_id, canonical_payload_hash)`.

A canonical hash is only meaningful relative to the algorithm that produced it,
so that key was under-specified in two directions:

* a strategy migration might **not** produce a new version, if the new algorithm
  happened to yield the same digest — contradicting ADR-027's promise that a
  strategy change is a visible, dated event;
* two different strategies colliding on one hash would be **silently merged**
  into one version, making the stored strategy stamp false for at least one of
  them.

### Decision
```sql
UNIQUE (provider_entity_id,
        canonicalization_strategy,
        canonicalization_version,
        canonical_payload_hash)
```

| Same entity, and… | Result |
| --- | --- |
| same strategy + version, same semantic payload | No new version; sighting recorded |
| same strategy + version, semantic mutation | New version |
| different strategy or version | **Distinct version**, even if the hash is equal |

### Consequences
* A strategy migration is guaranteed to produce new versions, at the database
  level rather than by convention.
* Historical versions stay interpretable: each carries the contract under which
  its hash was computed, so an old digest is never re-read under a newer
  algorithm.
* Retained bodies (ADR-026) remain what makes re-canonicalization possible.

---

## M2-ADR-029 — Market presence is temporal

**Status:** Accepted · extends M2-ADR-002 and M2-ADR-021 · resolves invariant 14

### Context
Revision 4 fixed temporality for relationships but left
`company_market_presences` timeless, so "we no longer operate in Ireland" was
unsayable. Carrying that as an open question was acceptable while presence was
notional; it is not acceptable once presence is a durable projection that M1
contexts and M3 research both consume.

### Decision
Presence follows the relationship pattern exactly: an append-only claim shape
carrying `valid_from` / `valid_to` / `assertion`, a deterministic interval
projection, and a `current_company_market_presences` view applying
`CURRENT_DATE`.

The projection key is
`(company_id, market_id, presence_type, effective_from)` — the interval start is
**in the key**, so a company that leaves a market and later re-enters has two
rows rather than one overwritten row. Re-entry is a new interval, not a
correction.

A presence-type change (`SERVES_REMOTELY` → `BRANCH`) closes one interval and
opens another; it is never an in-place edit, which would destroy the history of
how the market was previously served.

**Silence is not exit.** An `effective_to` is written only from a claim that
positively asserts an end, or from a human decision. A provider that stops
returning a company has said nothing about that company — it may have changed
its index, its coverage or simply failed.

### Consequences
* Enter, exit, re-enter and presence-type change are all representable without
  `UPDATE`.
* `?as_of=DATE` reproduces exactly what the current view returned on that day.
* The "absence of evidence is not evidence of absence" rule now holds in M2
  exactly as it holds in M0, where a missing value never becomes a zero.

---

## M2-ADR-030 — The rebuild contract is row-set equality; digests are telemetry

**Status:** Accepted · refines M2-ADR-024 · resolves invariant 15

### Context
M2-ADR-024 illustrated the rebuild equality check with
`md5(string_agg(t::text, ...))`. Convenient for eyeballing a table by hand, but
unfit as a durable contract: `::text` rendering is PostgreSQL-version-dependent,
and MD5 is unsuitable for anything that must survive.

### Decision
* **The contract is exact normalized row-set equality.** Acceptance tests
  compare the rows of two rebuilds directly, not a hash of them — so a failure
  reports *which row differs*, not merely that something did.
* **A digest exists separately as operational telemetry**, computed as SHA-256
  over a canonical row serialization and recorded per table per rebuild in
  `projection_runs`, so production drift is detectable without storing a second
  copy of the data.
* Nothing in the domain or on the wire may depend on any particular digest
  algorithm.

### Consequences
* Determinism failures are diagnosable, not just detectable.
* The digest algorithm can change without touching the contract.
* The canonical serialization reuses M0's discipline for snapshot identity
  rather than inventing a second one.


---

## M2-ADR-031 — The gate on canonical writes is a durable fact, not a status column

**Status:** accepted (revision 6, found during implementation)

### Context

Revision 5 gated canonical writes on `discovery_runs.status ∈ {FETCHED,
NORMALIZING, RESOLVING}`. An acceptance test for "a partial fetch writes
nothing canonical" passed when run against a freshly failed run and *failed*
when the pipeline ran normally, because `normalize_run` sets
`status = NORMALIZING`. Normalizing a failed run therefore promoted it into the
resolvable set. The guard protected against nothing that the pipeline itself
did not undo one step later.

This is the general failure mode of gating on a mutable status column: the
column is owned by whichever stage last wrote it, so a guard reading it trusts
every later stage to preserve a meaning it never agreed to.

### Decision

`discovery_runs.fetch_completed_at` is set **only** when a fetch reaches its
end without raising. It is never cleared and never set by another stage. The
resolvability guard reads it, not `status`.

`RESOLVABLE_STATUSES` is renamed `POST_FETCH_STATUSES` and demoted to
documentation, so no future reader mistakes it for the guard.

The same reasoning applies to the resolution race: a worker that mints a
company and then loses the race on `uq_resolution_root` deletes that company on
the conflict path. Its existence was speculative and uncommitted, so nothing
could reference it; leaving it behind would put an identity in the registry
that no evidence and no decision points at.

### Consequences

* An incomplete fetch cannot be laundered into a complete one by any
  subsequent stage.
* `status` is free to be purely descriptive, which is what it is good at.
* A losing racer leaves no trace: one entity, one company, one decision.

---

## M2-ADR-032 — The domain identity policy is enforced where the projection is written

**Status:** accepted (revision 6, found during implementation)

### Context

§8 states that a shared hosting domain (`wixsite.com`, `business.site`, …)
never carries identity. Revision 5 enforced this in `_deterministic_match` —
the read path. The projection was free to write `domain_role = 'IDENTITY'` for
such a domain, and did.

Nothing merged incorrectly, because the only reader re-applied the policy. That
is precisely the problem: the invariant held by convention across every future
reader rather than by construction in the stored data. The second reader to
forget would produce a false merge, and the stored data would have justified it.

### Decision

The policy is applied at write time, in two places:

1. `write_claims_for_candidate` authors the domain claim with
   `role = IDENTITY` only when `is_identity_domain` holds, and `GROUP`
   otherwise.
2. `_project_domains` applies the same test again, regardless of what the claim
   asserted, before writing `company_domains`.

The projection is the authoritative gate; the claim-level test only keeps the
evidence honest.

### Consequences

* `company_domains` cannot contain a blocklisted domain marked `IDENTITY`,
  whatever a provider claimed.
* The domain still appears as `GROUP`, so it remains a weak corroborating
  signal and is not silently discarded.
* A future reader that forgets the policy cannot produce a false merge from
  stored data, because the stored data no longer supports one.

---

## M2-ADR-033 — A record that cannot be interpreted is still evidence

**Status:** accepted (revision 6, found during implementation)

### Context

`ingest_record` ran the adapter's pure stage — parse, normalize, canonicalize —
before touching the database. A `normalize` that raised therefore aborted the
record, and because `fetch` wrapped the whole page in one `try`, it also
discarded every record after it on that page. One malformed row cost an entire
paid page of raw evidence.

Worse, when the record *was* stored, resolution re-derived the candidate by
calling `normalize` again, which raised again.

### Decision

Normalization failure is data, not an exception path:

* `ingest_record` catches it, stores the bytes, the parsed payload and the
  version as usual, and writes `provider_record_normalizations.error` with an
  empty normalized payload.
* `fetch` isolates each record in a savepoint, so a record that cannot be
  stored at all costs only itself; the count surfaces as
  `FetchReport.record_errors`.
* `_candidate_from_version` returns an empty candidate for a version already
  known to be uninterpretable, rather than re-raising.
* Resolution parks an empty candidate as `AMBIGUOUS` with
  `reason = no_identifying_attributes` instead of minting an anonymous company.

### Consequences

* Raw evidence survives adapter bugs, and a later `normalizer_version` can
  re-derive it — which is the entire point of storing bytes.
* A company is never created from a record nobody can read.
* The failure is visible: it is a stored error string and a counter, not a
  silently dropped row.


---

## M2-ADR-034 — Derived-key collision is a predicate, not a column

**Status:** accepted (revision 6, found during implementation)

### Context

Revision 5 specified `provider_entities.identity_collision` as stored state,
set when more than one version under a derived entity showed materially
disagreeing identity fields.

Implementing it exposed a contradiction with two other accepted decisions.
`provider_entities` is append-only (M2-ADR-018's rejection trigger), and a
collision becomes visible only when the **second** version lands — after the
entity row exists. The flag could therefore never be set at the moment it
became true. In the shipped code it was written once, as `false`, and the two
branches that read it were unreachable. The guarantee A10 promised did not
exist.

### Decision

Collision is computed, not stored:

```
has_identity_collision(entity) :=
    entity.external_id_kind = 'DERIVED'
    AND count(DISTINCT normalize_name(legal_name)) > 1
        over the entity's successful normalizations
```

Only `DERIVED` entities qualify. A native id is the provider's own assertion
that these payloads describe one object — disagreeing names there are a rename
or a data error, not an identity collision. A content hash makes every
differing payload its own entity, so a collision is unrepresentable.

A colliding entity resolves to `AMBIGUOUS` **unconditionally**: it may neither
auto-match nor create. Revision 5's implementation only blocked matching, so an
entity with no retrieval candidates fell through and created a company —
minting a canonical identity for whichever of the two firms happened to be
queried first, which is exactly the false-identity outcome A10 exists to
prevent.

### Consequences

* The guarantee is enforced by the code path that resolves, not by a flag some
  earlier writer had to remember to set.
* It costs one indexed query per resolution of a derived entity, which is the
  same order as the candidate retrieval already performed.
* A future collision signal (disagreeing postal codes, registry ids) extends
  the predicate without a migration.
* The predicate is monotone in evidence: it can only become true as versions
  accumulate, and it is re-evaluated on every resolution rather than frozen.

---

## M2-ADR-040 — One production provider, chosen explicitly, and no marketplace

**Status:** accepted (BoRo-first live discovery)

### Context

M2 shipped the whole discovery architecture and no production provider: every
adapter declared `is_fixture = True`, and a purity test asserted that nothing in
the package could reach the network. BoRo cannot supply its own account list
forever, so the engine has to find companies itself.

The tempting response is a provider marketplace — a registry, a plugin protocol,
a credential store, a capability negotiation layer. That is a platform for users
who do not exist. BoRo needs one provider that works.

### Decision

One adapter: `GooglePlacesAdapter`, on the official Places API (New)
`places:searchText` endpoint. Not Google Maps HTML, not a browser, not a generic
crawler.

* `provider_key = "google_places"`, `NATIVE_EXTERNAL_ID` on the place id,
  `JSON_CANONICAL_V1`, `application/json`, `is_fixture = False`.
* Seeded by `seed_production_providers`, separate from the fixture seeder, so
  `is_fixture` stays a real distinction a reader can trust rather than a name
  they have to recognise.
* Selection is explicit at the call site and in the CLI. No registry lookup by
  string, no entry points, no configuration naming a class.

The network firewall test changes shape rather than being deleted. It used to
assert that *no* discovery module imports a network client; it now asserts that
only the one named module does, and separately that the adapter's pure stages —
`parse`, `normalize`, `canonicalize`, `derive_key` — never touch the client. The
guarantee worth having was never "M2 has no provider"; it was "network access
lives in one place and the pure stages cannot acquire it".

The old test also counted `urllib.parse` as a network client, which flagged a URL
parser. `urllib` is a network client through `request` and `error`, and the
detector now says so.

### Consequences

* Adding a second provider means writing a class and naming it in one allowlist.
  A registry earns its keep when there is a second customer, not a second file.
* **Generalisable:** a firewall that forbids the thing you have now decided to
  build has to be rewritten to forbid what you still want forbidden. Deleting it
  loses the guarantee; leaving it fails honestly and makes you choose.

---

## M2-ADR-041 — A stored record is one place, not one page

**Status:** accepted (BoRo-first live discovery)

### Context

Places returns pages: `{"places": [...], "nextPageToken": "..."}`. The obvious
thing is to store the page as the record body. It is also wrong twice over.

Semantic identity in M2 is the canonical hash of the stored payload. A page
containing one place found by two different query intents would hash differently
each time — so the same business would become two versions, and the convergence
M2 exists to provide would not happen. And a page body cannot be attributed to a
place, so nothing downstream could say which business the bytes described.

### Decision

One `RawRecord` per place, whose body is the place object alone, canonicalized.
Two queries finding one listing converge on one provider entity and one version,
and both sightings survive — because a sighting links a version to a
`DiscoveryQuery`, which is exactly the "which query saw this" relation.

Page-level provenance therefore lives on the query row, not in the body: one
`DiscoveryQuery` per provider page, carrying the plan version, the intent, the
metro, the page index and the page token.

That required a small orchestrator rather than `runs.fetch`, whose page loop
assumes a run is one query chunked by storage size and which sets
`fetch_completed_at` when it returns. Calling it once per provider page would
have marked an unfinished plan complete — laundering a partial fetch, which is
the one thing M2's fetch gate exists to prevent. The orchestrator reuses
`ingest_record`, `normalize_run` and `resolve_run` unchanged; there is no second
dedupe path and no canonical write outside M2.

`discovery_queries` is append-only, so the query row is written *after* its page
returns, with `result_count` already set. A row that records what the query
returned is the more honest one anyway.

### Consequences

* A record is attributable to a business, a query, a metro and a page.
* **Generalisable:** the unit of storage should be the unit of identity. When
  they differ, deduplication silently stops working and nothing reports it.

---

## M2-ADR-042 — A provider display name is a trading name, not a legal name

**Status:** accepted (BoRo-first live discovery)

### Context

`CandidateCompany.legal_name` flows into a `legal_name` claim written with
`FactType.FACT`. In M2's semantics that is an assertion about legal
registration.

A Google Places `displayName` is the name a business trades under, set by
whoever manages its Business Profile. "Big Mechanical" is very often not
"Big Mechanical Holdings, LLC", and nothing in a Places response establishes a
registered entity.

### Decision

The display name maps to `trading_names`, and `legal_name` is left unset. The
claim writer already records trading names as `trading_name`, which is what this
is.

No new naming subsystem, no change to `CandidateCompany`, no new fact type. The
smallest correct mapping was to use the field that already meant this.

Two consequences follow from leaving `legal_name` empty, and both are correct:

* `CompanyProfile.canonical_name` derives from `legal_name` claims, so a
  Places-only company has no canonical name until a provider supplies a legal
  one. The company is still identified by its domain and named by its trading
  name, which is what Places actually knows.
* `resolution._score_candidates` keys name signals on `legal_name`, so a
  Places-only candidate contributes no name signal — it matches on domain or it
  does not match. Given that name signals can never reach the auto-match
  threshold alone, this loses nothing and removes a way to be wrong.

### Consequences

* **Generalisable:** when a provider's field is nearly the one you have, the
  cost of the mismatch is a false fact that looks exactly like a true one.
  Mapping to the weaker, accurate field is almost always right.

---

## M2-ADR-046 — A contractor's listed website is often a directory

**Status:** accepted (BoRo-first live discovery)

### Context

`websiteUri` is the bridge from a listing to a canonical identity and then to M3
first-party research. For small and mid-market contractors it very often points
somewhere that is not the company: a Facebook page, a Yelp profile, an Angi or
HomeAdvisor listing, a HousecallPro booking page, a Wix subdomain.

M2's `DOMAIN_BLOCKLIST` held twelve hosts, aimed at site builders and social
networks. It did not cover review sites, lead brokers, job boards or booking
platforms.

If any of those became an identity domain, every contractor listed on that
directory would deterministically match the same company — the exact
wrong-company merge this milestone treats as P0, arrived at through the strongest
signal M2 has.

### Decision

Extend the blocklist to 56 hosts across four groups: site builders and hosts,
social and video, directories and lead brokers, job boards, and booking
platforms. The policy itself is unchanged — a blocklisted domain is recorded as
`GROUP` and can never carry identity — this is data, not mechanism.

### Consequences

* A contractor whose only listed website is a Yelp profile produces no identity
  domain, so it is parked as `AMBIGUOUS` and researched by nobody. That is
  correct: M2 does not know who it is, and guessing is how accounts get crossed.
* The list will need adding to. It is a list, and that is the cheapest kind of
  thing to be wrong about.
* **Generalisable:** the strongest deterministic signal is also the most
  dangerous one to feed bad data into. Blocklists guarding an identity rule are
  worth more attention than the rule.

---

## M2-ADR-047 — Intra-run domain duplicates, and why they are the safe direction

**Status:** accepted (BoRo-first live discovery; **measured limitation**)

### Context

Google Places returns one listing per physical location, so a contractor with
three branches is three places. All three usually carry the same
`websiteUri`, and the intended outcome is one canonical company.

`_deterministic_match` finds an existing company by reading `CompanyDomain` —
which is a **projection**, rebuilt from the claim ledger after resolution. So
within a single run the second listing cannot see the first listing's company
yet, and two companies are created.

Measured: two branch listings in one run produce two companies. The projection's
partial unique index `uq_identity_domain` then grants the domain `IDENTITY` on
one and demotes it to `GROUP` on the other. Across runs the match works
correctly — the limitation is intra-run ordering, not the matching rule.

### Decision

Record it. Do not force convergence.

The alternatives were worse. Rebuilding projections between individual
resolutions would truncate and rebuild every derived table per record.
Re-running `resolve_run` does not help: both entities already have heads, and
merging two existing companies is a `MERGED` decision M2 deliberately does not
automate. Adding franchise or branch-merging logic is precisely what this phase
was told not to invent without a demonstrated defect.

What matters is the direction of the error. M2 **under-merges**: it produces a
duplicate rather than joining two organisations that might not be one. The
wrong-company merge count stays zero, and the database still guarantees one
identity holder per domain.

### Consequences

* The duplicate is visible, not silent: the run reports
  `canonical_companies`, `companies_with_identity_domain` and
  `companies_without_domain`, and a duplicated contractor shows up as a company
  with no identity domain.
* The demoted duplicate is **not researchable** by M3, which only accepts
  `IDENTITY`, `ALTERNATE`, `REDIRECT` and `COUNTRY_TLD`. One real company
  appears twice and is researched once. That is the honest outcome of not
  knowing.
* A second run over the same metro converges them, because by then the
  projection exists.
* **Generalisable:** when a deterministic match reads a derived view, it is only
  as current as the last rebuild. Either the match reads the ledger or the
  staleness is a documented property — pretending it is neither is how duplicates
  become merges.

---

## M2-ADR-048 — The holdout is evaluation data, and discovery runs blind

**Status:** accepted (BoRo-first live discovery)

### Context

BoRo has an existing outbound cohort. Using it to build queries, pick metros or
seed domains would make any recall number meaningless: the engine would be
scored on finding what it was told.

### Decision

The query plan is built from the market definition and a fixed metro
configuration, and from nothing else. A test reads the plan module's source and
fails if it references a company table, a cohort file, CSV parsing or a database
session — so the isolation is structural rather than a convention someone
remembers.

The holdout is read by a separate offline evaluator, after a run, and is never
ingested into M2. Recovery is an exact normalized **domain** match and nothing
else. Name similarity is computed and reported as a diagnostic, and never counted:
counting it would turn M2's one firm refusal — that a shared name is not identity
— into a success metric.

A holdout row with no domain is reported as unscored rather than as a miss.
Blaming discovery for a gap in the evaluation data would flatter the
alternative.

### Consequences

* Recall is over the whole holdout, with the unscored rows stated separately.
* Companies discovered outside the holdout are counted, because finding accounts
  BoRo does not already know is the point of the exercise and a recall-only
  metric would treat them as noise.
* **Generalisable:** an evaluation set that touches the system it evaluates
  stops being an evaluation set, and nothing about the resulting number warns
  you.

---

## M2-ADR-049 — A run report answers what *this run* did

**Status:** accepted (pre-flight correction, before the first paid request)

### Context

`_fill_resolution_counts` scoped resolution decisions by `provider_id` and read
`canonical_companies`, `companies_with_identity_domain` and
`companies_without_domain` straight off the whole database.

Reproduced before fixing: run A discovered two companies, run B discovered one,
and B's report said **three** for every company metric. An operator reading it
after the second metro would have concluded discovery was three times as
productive as it was, and the number would have grown monotonically no matter
what any individual run found.

### Decision

Every primary metric is scoped to the run through the provenance M2 already
maintains for exactly this question:

```
DiscoveryRun → DiscoveryQuery → ProviderRecordSighting
             → ProviderRecordVersion → ProviderEntity
             → EntityResolutionHead.company_id
```

Attribution runs through **sightings**, not `provider_id` and not
`ProviderRecordVersion.discovery_query_id` — a second run over unchanged
evidence creates no new versions, so keying on the version's own query would
report that it saw nothing.

The created / matched / ambiguous / unchanged / claims counts come from
`resolve_run`'s return value rather than being recomputed. It already counts what
the run *did* instead of what the heads happen to say: an entity resolved by an
earlier run has a `CREATED_NEW` head and created nothing now, and recomputing
from heads reported a creation that did not happen.

Database-wide figures remain available as `cumulative_canonical_companies` and
`cumulative_companies_with_identity_domain`. The two semantics never share a
field name.

### Consequences

* `companies_without_domain` is renamed `companies_without_identity_domain`,
  because within a run that is what it measures.
* **Generalisable:** a metric computed from "everything matching this provider"
  answers a question nobody asked, and it answers it with a number that only ever
  rises. Scope follows the provenance you already keep.

---

## M2-ADR-050 — Breadth before depth

**Status:** accepted (pre-flight correction, before the first paid request)

### Context

The scheduler walked the plan depth-first: every page of step one, then every
page of step two. With a bounded query budget that means pagination on the
earliest metros consumes the budget and the later metros are never asked at all.

For market discovery that is the wrong priority. The first page of Milwaukee is
worth more than the third page of Dallas, and a run that returns 60 Dallas
contractors and nothing from 24 other metros has not discovered a market.

### Decision

Round-based scheduling. Round 1 is page 1 of **every** planned step, in plan
order. Round *n* > 1 revisits only the steps that came back with a next-page
token, in the same order. No second page is requested until every first page has
been attempted.

Ordering inside a round is the plan's declared order, so a run remains
reproducible and a budget cut removes the same steps every time.

### Consequences

* A budget equal to the step count buys exactly one page of everything, which is
  now the default (M2-ADR-051).
* `first_page_queries_issued` and `pagination_queries_issued` are reported
  separately, so an operator can see which kind of request the money went on.
* **Generalisable:** when a budget is smaller than the plan, the traversal order
  *is* the sampling strategy. Depth-first quietly samples one region thoroughly
  and the rest not at all.

---

## M2-ADR-051 — The default budget buys breadth, and a short budget says so

**Status:** accepted (pre-flight correction, before the first paid request)

### Context

`QueryBudget.max_queries` defaulted to 50. The full plan is 25 metros × 5
intents = **125** first-page searches. So `--metros all` advertised national
coverage and could only ever attempt the first 50 steps — and because the
scheduler was depth-first, far fewer than 50 distinct steps at that.

Nothing in the plan output or the run report said this. `status = COMPLETED`
meant "finished without raising", and it read as "asked everything".

### Decision

`max_queries = None` by default, meaning *exactly one page of every planned
step*. Smoke buys 10, phase B buys 50, the full plan buys 125. Pagination is
opt-in: an operator raises the number explicitly and the plan reports the
leftover as `pagination_capacity`.

The plan states four things before any spend: `first_page_queries`,
`max_queries`, `first_page_coverage_possible` and `pagination_capacity`. An
explicitly supplied budget below full first-page coverage emits a `WARNING`
naming the shortfall and the number that would fix it.

The run report separates coverage from success:
`planned_first_page_queries`, `first_page_queries_issued`,
`first_page_coverage_complete`, `pagination_queries_issued` and
`budget_stopped_at`. A run can be `COMPLETED` with
`first_page_coverage_complete = false`, and those are different facts.

No new database status vocabulary: hitting an intended budget is not a provider
failure, and `PARTIAL_FETCH` means the provider or the network failed.

### Consequences

* The phase lists are prefixes of one ordered metro tuple, so the default budget
  and the phase definition cannot drift apart.
* **Generalisable:** a default that cannot execute the thing it is the default
  for is a lie with a number in it. Either the default fits the plan or the plan
  reports that it does not.

---

## M2-ADR-052 — No partial-resolution control on the live path

**Status:** accepted (pre-flight correction, before the first paid request)

### Context

`discovery run-live` accepted `--allow-partial` and did:

```python
if allow_partial:
    pass
```

It read as a control over whether an incomplete run could write canonical
companies. It controlled nothing.

### Decision

Removed from the live command. A provider or network failure leaves the run
`PARTIAL_FETCH` with its evidence retained and nothing canonical written, which
is the behaviour production wants and the only behaviour the live path offers.

The fixture path keeps its `--allow-partial`, where it is wired to
`allow_partial_resolution` on the run and exists for internal M2 work. New
partial-live semantics are not invented in this phase.

### Consequences

* **Generalisable:** a flag that looks like a safety control and does nothing is
  worse than its absence — it answers the question an operator was right to ask,
  wrongly.
