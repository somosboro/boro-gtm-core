# M2 — Architecture Decision Records

**Status:** design only. These record decisions taken while designing M2. None
is implemented. They are numbered in their own `M2-` series so they cannot be
confused with the accepted ADR-001 … ADR-020 governing shipped code.

---

## M2-ADR-001 — Canonical company identity is market- and vertical-agnostic

**Status:** Proposed

### Context
A company may operate in several markets, hold several locations, and belong to
several verticals. Keying a company to one market and one vertical — the
obvious shape when the only consumer is a market-by-market discovery run —
breaks on the first multinational contractor, and forces duplicate rows that
then have to be reconciled.

### Decision
`companies` carries no `market_id` and no `vertical_id`. Market presence and
vertical membership are separate evidence-bearing relationships:
`company_market_presences` and `company_verticals`.

### Consequences
* "One company, branches in three countries" is one row plus three presences.
* Market-filtered queries join through presence, which is slightly more work
  and dramatically more correct.
* This mirrors ADR-002: just as a score is not an attribute of a market, a
  market is not an attribute of a company.

---

## M2-ADR-002 — Presence and location are different concepts

**Status:** Proposed

### Context
A Belfast contractor may serve the Republic of Ireland without any Irish site.
Modelling presence as "has a location in that market" would erase real
commercial reach; modelling location as "wherever it operates" would invent
buildings that do not exist.

### Decision
Two tables. `company_locations` records physical or registered places.
`company_market_presences` records the claim of operating in a market, typed
(`HEADQUARTERED` / `BRANCH` / `OPERATES` / `SERVES_REMOTELY`) and carrying its
own evidence and confidence.

### Consequences
* Presence can exist without a location, and vice versa.
* M1 contexts consume presence, not location.
* Slightly more schema; no fabricated addresses.

---

## M2-ADR-003 — Fuzzy name similarity is a retrieval mechanism, never a decision

**Status:** Proposed

### Context
Trigram similarity over company names is the cheapest way to find candidates
and the most dangerous way to decide. "Schmidt Kältetechnik GmbH" and "Schmidt
Kaeltetechnik GmbH" may be one company or two unrelated family firms.

### Decision
Fuzzy similarity may only *retrieve* candidates. A merge requires a
corroborating non-name signal: exact normalized domain, a trusted external
identifier, or a strong composite of geography and contact details. Scores
between the two configured thresholds produce an `AMBIGUOUS` decision that
creates nothing and merges nothing.

### Consequences
* A human-review queue is required from day one, not bolted on later.
* Some duplicates will persist until better evidence arrives. That is the
  intended failure mode — the same discipline that keeps M1 coverage honest
  rather than inventing vertical density.

---

## M2-ADR-004 — Provider records are append-only and content-hashed

**Status:** Proposed

### Context
M0 already established that evidence is append-only and content-addressed
(ADR-003, ADR-017, ADR-018). Provider output is evidence.

### Decision
`provider_records` stores the verbatim payload plus its SHA-256, is unique on
`(provider_id, provider_external_id)` and on `(provider_id, payload_hash)`, and
is protected by the same `BEFORE UPDATE` rejection trigger used for
`market_observations`.

### Consequences
* Re-ingestion is a natural no-op.
* A provider changing its answer creates a new record rather than destroying
  the old one, so drift is observable.
* Resolution can be re-run over stored raw records without re-paying providers.

---

## M2-ADR-005 — Discovery counts never become FACT

**Status:** Proposed · **the load-bearing M1/M2 boundary**

### Context
It is tempting to treat "provider returned 2,400 US HVAC companies" as a
measurement of market density. It is not. It measures the provider's index,
its language and geography coverage, and the query used. The bias systematically
favours large English-language markets — exactly the direction that would
flatter a predetermined conclusion.

M1 currently represents vertical density as a **research gap**. That is
accurate. Replacing an honest gap with a biased number would be a regression
disguised as progress.

### Decision
1. A discovery run never writes to `market_observations`.
2. Promotion to M1 is explicit, human-authorised, logged and reversible.
3. A promoted count enters as `fact_type = PROXY`, never `FACT`, carrying
   provider, adapter version, query definition, retrieval date, coverage
   assumption, calibration (if any) and confidence.
4. Uncalibrated counts support within-provider, within-market trends only.
   Cross-market density claims require calibration against an independent
   anchor, itself evidenced.

### Consequences
* Provider-derived estimates likely live in their own `market_density_proxies`
  space before any promotion.
* M1 vertical-density coverage stays low until genuinely better evidence
  exists. That is the correct outcome, not a shortcoming to engineer around.

---

## M2-ADR-006 — PostgreSQL constraints over distributed coordination

**Status:** Proposed

### Context
Concurrent workers may resolve the same real company simultaneously. The
textbook reflex is a lock service or a deduplicating queue.

### Decision
Rely on the database, in this order: a partial unique index on
`primary_domain`; `INSERT ... ON CONFLICT DO NOTHING ... RETURNING` with a
re-read on conflict; and, only for the narrow non-domain case, a transactional
advisory lock on the normalized-name + market hash.

### Consequences
* No new infrastructure, consistent with ADR-001 and the M1 research-gap
  detector, which already proves this pattern under a real two-connection race.
* Correctness is enforced where the data lives rather than in application
  convention.

---

## M2-ADR-007 — Provider normalization is a pure function

**Status:** Proposed

### Context
M0/M1 hold a hard rule that no test touches the network. Provider integration
is the obvious place for that rule to erode.

### Decision
`ProviderAdapter.normalize(raw) -> CandidateCompany` performs no I/O and no
database access. Fetching is separate and returns raw payloads; normalization
and resolution operate on stored records.

### Consequences
* Adapter behaviour is testable from recorded fixtures with zero network.
* A normalization fix is replayable over historical records.
* The three-stage split (fetch / normalize / resolve) becomes the natural job
  boundary.

---

## M2-ADR-008 — Entity resolution decisions are append-only and explainable

**Status:** Proposed

### Context
"Why are these two companies the same?" must be answerable months later,
especially where a merge turns out to be wrong.

### Decision
`entity_resolution_decisions` is append-only. Each row records the decision,
the method (`DETERMINISTIC` / `CANDIDATE_AUTO` / `HUMAN_REVIEW`), the full
signal vector, a rationale and the actor. A reversal writes a new row
referencing the old one via `superseded_by_id`.

### Consequences
* Merge history is reconstructable.
* Automatic decisions are auditable with the same rigour as human ones.
* A future ML-based matcher gets a labelled corpus for free — which is also why
  no ML matcher is proposed for the first implementation.

---

## M2-ADR-009 — Reuse M0's evidence vocabulary rather than inventing a second one

**Status:** Proposed

### Context
M2 needs to express how strongly a company attribute is believed. M0 already
has a five-member fact-type vocabulary, an availability flag, temporal
granularity and one confidence algorithm.

### Decision
`company_evidence` and `company_verticals` reuse `FactType`, the same
`availability` semantics and the same confidence algorithm. A provider-asserted
domain is `FACT`; a keyword-inferred vertical is `INFERENCE`; an employee-count
band is `ESTIMATE`.

### Consequences
* One vocabulary across the whole engine; a consumer learns it once.
* M3 can attach to the same structure without a third dialect.
* Any change to the confidence algorithm applies uniformly, by construction.
