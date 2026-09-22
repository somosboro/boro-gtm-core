# M2 — Architecture Decision Records

**Status:** design only, revision 3. None is implemented. Numbered in their own
`M2-` series so they cannot be confused with the accepted ADR-001 … ADR-020
governing shipped code.

M2-ADR-001 … 009 were written for revision 1, 010 … 017 for revision 2, and
018 … 023 for revision 3. Superseded decisions are marked and cross-referenced;
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

**Status:** Accepted (design) · extends M2-ADR-013 · resolves invariant 2

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
