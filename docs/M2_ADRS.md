# M2 — Architecture Decision Records

**Status:** design only, revision 2. None is implemented. Numbered in their own
`M2-` series so they cannot be confused with the accepted ADR-001 … ADR-020
governing shipped code.

M2-ADR-001 … 009 were written for revision 1. Those superseded by the
correction pass are marked and cross-referenced; the originals are retained
rather than rewritten, so the reasoning trail survives.

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
resolves correction item 2

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

**Status:** Accepted (design) · corrects M2-ADR-008 · resolves correction
items 3 and 4

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

**Status:** Accepted (design) · extends M2-ADR-009 · resolves correction item 5

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
