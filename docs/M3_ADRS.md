# M3 — Architecture Decision Records

**Status:** design, revision 1. **Not implemented.**

Decisions taken while designing M3 Operational Research. Numbered
`M3-ADR-NNN`, independent of M0/M1's `ADR-NNN` and M2's `M2-ADR-NNN`.

---

## M3-ADR-001 — M3 produces evidence, never judgement

**Status:** accepted

### Context

M3 collects operational facts that exist precisely because someone wants to
sell to these companies. Every attribute in the taxonomy is commercially
interesting, and the shortest path from "they publish PDF work-order forms" to
"they are a good prospect" is one column.

That column would be load-bearing immediately, impossible to remove later, and
wrong: it answers a question about BoRo, not about the company.

### Decision

M3 outputs evidence, claims, gaps and coverage. It never outputs a score, a
grade, a tier or a recommendation. The boundary is enforced by a test asserting
that no M3 column name matches a forbidden vocabulary
(`lead_score`, `qualification_score`, `icp_fit_score`, `pain_score`,
`priority_score`, `recommend_contact`, `sales_ready`, `tier`, `grade`), so the
boundary fails a build rather than eroding across a quarter of reasonable
commits.

Coverage and confidence are exported because M4 needs them. Their
interpretation is not M3's.

### Consequences

* M4 can weigh a PROXY heavily if it chooses; M3 hands it over honestly
  labelled rather than pre-promoted.
* Attributes like `digital_maturity = high` are unrepresentable, which is the
  point.
* A reviewer asking "why does research not just flag the good ones" has a
  written answer.

---

## M3-ADR-002 — One claim ledger, with richer provenance beside it

**Status:** accepted

### Context

The M2 design says M3 "may attach findings as `company_claims`". Audited
against the live schema, `company_claims` already provides exactly what M3
needs on the *value* side — typed shadows, availability, the five fact types,
confidence, temporal granularity, append-only enforcement — and three gaps on
the *provenance* side:

1. attribution to a research artifact (the only FK is to
   `provider_record_versions`),
2. one claim supported by several artifacts,
3. an exact evidence span, and any record of what extractor produced it.

Two options. **A:** extend `company_claims` into a unified ledger. **B:**
create M3 observation tables and promote validated results into
`company_claims`.

### Decision

**Option A′.** `company_claims` remains the single source of truth for what we
assert about a company. The three gaps are closed *beside* the claim, not by
forking it:

* `claim_evidence_links` — N:M between claim and evidence, carrying the
  locator and the extraction.
* `research_extractions` — what read the artifact, with model and prompt
  version.

M3 claims use the existing `subject_company_id` attribution path, which already
satisfies `exactly_one_attribution_path`. **No released CHECK constraint is
altered.**

### Consequences

* One place answers "what do we believe about this company".
* Option B was rejected because it sounds safer and is not: two ledgers drift,
  and every consumer — including M4 — must learn which to read and when.
* One additive column (`attribute_definitions.owner_milestone`) and one
  deferred trigger on `company_claims` are required. See M3-ADR-003.

---

## M3-ADR-003 — An unsourced research claim must be unrepresentable

**Status:** accepted

### Context

M2 shipped a defect where the domain identity policy was enforced only on the
read path, so the invariant held only as long as every future reader remembered
to re-apply it (M2-ADR-032). M3 has the same shape of risk: a research claim
with no evidence is worthless and indistinguishable from a sourced one.

Enforcement by service-layer convention was considered and rejected on exactly
that precedent.

### Decision

A deferred constraint trigger on `company_claims` rejects, at COMMIT, any claim
whose attribute is owned by M3 and which has no row in `claim_evidence_links`.
`attribute_definitions` gains one additive nullable column, `owner_milestone`,
so the trigger can tell M2 and M3 attributes apart.

### Consequences

* Deferred rather than immediate, because claim and link are written in one
  transaction and the order should not matter.
* M2 claims are untouched: the trigger only fires for M3-owned attributes.
* This is the only change M3 makes to an M2-owned table's behaviour.

---

## M3-ADR-004 — A model's confidence is not evidential strength

**Status:** accepted

### Context

M3 benefits from LLM-assisted extraction, and every such system drifts toward
treating model confidence as fact confidence. A model that is 0.99 sure it read
a sentence has said something about *reading*, not about *the company*.

### Decision

**The source determines the fact type. The extractor determines only whether we
read the source correctly.**

`company_claims.confidence` is computed from evidence type and source trust,
exactly as M0 computes it. `research_extractions.extractor_confidence` is
stored, exposed and **is not an input** to the claim's confidence. An
extraction below the review threshold produces no claim — it produces a review
candidate and a gap.

### Consequences

* The same sentence yields `FACT` on the company's own site and `PROXY` on an
  unaffiliated blog, regardless of how certain the model was.
* A better model improves *coverage*, never evidential strength — which is the
  correct incentive.

---

## M3-ADR-005 — Source location identity is separate from content identity

**Status:** accepted

### Context

A naïve design treats a URL as a document. Reality: redirects, canonical tags,
language variants, tracking parameters, PDFs mirrored at several paths, content
moved to a new URL, dynamic job URLs, pages that disappear.

### Decision

Three tiers, mirroring M2's provider model:

* `research_sources` — a versioned **normalized locator**; where we looked.
* `research_artifacts` — identity is `(strategy, strategy_version,
  canonical_content_hash)`; what we got semantically.
* `research_artifact_versions` — identity is `(source_id, raw_body_sha256)`;
  what we got literally.

Sources are never merged; two URLs serving one document converge at the
artifact.

### Consequences

* "The same document is mirrored at two URLs" stays queryable.
* A cosmetic edit creates a version, not an artifact; a semantic edit creates
  both.
* Reusing M2's shape is deliberate — the problems are the same, and a second
  vocabulary would cost without paying.

---

## M3-ADR-006 — Canonicalization is content-type specific and versioned

**Status:** accepted

### Context

M2 learned that a hash is meaningless without the algorithm that produced it,
and had to put the canonicalization contract inside version identity
(M2-ADR-028). M3 faces a harder version of the problem: HTML needs boilerplate
stripping that JSON does not, and PDFs need page segmentation that neither does.

### Decision

Four strategies — `HTML_TEXT_V1`, `PDF_TEXT_V1`, `JSON_CANONICAL_V1`
(reused verbatim from M0), `PLAINTEXT_V1` — each versioned, each declared
valid only for specific media types, and the strategy plus its version are part
of artifact identity from day one.

### Consequences

* A policy improvement produces new artifacts rather than silently
  reinterpreting old ones, and old artifacts stay interpretable under the
  policy that produced them.
* Boilerplate stripping is the risky part and is an open question (design §29).

---

## M3-ADR-007 — Staleness is computed, never stored, and never mutates a fact

**Status:** accepted

### Context

Operational facts age fast: headcount, branches, software. The tempting move is
a `is_current` flag maintained by a sweep. That makes an old fact *false*,
which it is not — it was true when observed.

M2 already established that a projection reading the wall clock cannot rebuild
deterministically (M2-ADR-024).

### Decision

Four distinct times: `retrieved_at`, `source_published_at`, `observed_at`,
`valid_from`/`valid_to`. A retrieval date is never copied into a fact date.

Staleness is derived at read time from the registry's per-attribute horizon and
surfaces as `FRESH | AGING | STALE | UNKNOWN_AGE` in a `current_*` **view**.
The stored projection holds dates, never a verdict, so rebuilds are
byte-identical with the clock advanced.

An old claim is never rewritten. A `STALE_EVIDENCE` gap is raised instead.

### Consequences

* `UNKNOWN_AGE` is a real state: a claim from an undated page has no computable
  staleness, and saying so beats assuming.
* Per-attribute horizons mean `acquisition` never goes stale while
  `hiring_field_roles` goes stale in 180 days, instead of one rule for all.

---

## M3-ADR-008 — Retention prunes bodies in place, by a one-way trigger

**Status:** accepted

### Context

Raw HTML and PDF bodies dominate storage. They must be prunable. But
`research_artifact_versions` is append-only, and "append-only except when we
feel like it" is not a property.

Two options: (a) a one-way mutation permitted by the trigger; (b) a separate
`research_artifact_bodies` table whose rows are DELETEd.

### Decision

**(a).** The trigger permits exactly one transition — `raw_body` non-NULL →
NULL together with `body_retention → 'PRUNED'` and `pruned_at → now` — and
rejects every other UPDATE.

Option (b) keeps the append-only rule pure at the cost of a fourth table and a
join on every read for a column that is NULL most of the time. The one-way
trigger is the smaller correct thing.

### Consequences

* Pruning never deletes a row, so provenance stays walkable: source, times,
  status, both hashes and `source_published_at` all survive.
* The evidence link keeps its own quote and quote hash, so the exact supporting
  text remains readable from the claim after the body is gone.
* The permitted transition is narrow, one-way and enforced in the database.

---

## M3-ADR-009 — Identity conflicts raise a signal; M3 never acts on them

**Status:** accepted

### Context

Research will find identity conflicts: "a division of X", two companies sharing
a phone number, a site announcing an acquisition. M3 has the evidence and is
the worst-placed component to act on it — it sees one company at a time and
has no view of the resolution chain.

### Decision

`identity_review_signals` is append-only, written by M3 and consumed by M2's
existing human-review path. M3 never creates, merges or splits a company, never
alters a resolution decision and never changes a domain role.

### Consequences

* M2's `AMBIGUOUS` review flow already appends rather than mutates; this feeds
  it rather than inventing a second review mechanism.
* An M2 row-count fingerprint test across a full research run proves the
  firewall, the same way M2 proves its M1 firewall today.

---

## M3-ADR-010 — A GROUP domain is never a crawl seed

**Status:** accepted

### Context

M2 demotes shared hosting domains (`wixsite.com`, `business.site`, …) to
`GROUP` because thousands of unrelated firms share them (M2-ADR-032). A crawler
seeded with the host would capture other companies' pages and attribute them
here.

### Decision

A domain is crawlable as "this company's website" only when its role is
`IDENTITY`, or it is `ALTERNATE` with a human override, or an explicit human
seed exists. A `GROUP` domain may be crawled only as a **path prefix** under an
explicit seed, never at the host root. With no crawlable seed, a
`NO_CRAWLABLE_SEED` gap is raised and other source kinds proceed.

### Consequences

* Missing evidence is accepted in exchange for never manufacturing false
  evidence — the right trade for an evidence system.
* Companies on shared platforms will have lower coverage, visibly and for a
  stated reason.

---

## M3-ADR-011 — Evidence class caps fact type for technology claims

**Status:** accepted

### Context

Technology evidence ranges from "we run ServiceTitan" on the company's own
page to a vendor script tag. Treating these alike would let a marketing pixel
assert an operational platform.

### Decision

`evidence_class` is part of the technology attribute's **value**, and the
registry caps the fact type per class: `EXPLICIT_COMPANY_STATEMENT`,
`CUSTOMER_PORTAL_BRANDING` and `INTEGRATION_DOC` may reach `FACT`;
`JOB_DESCRIPTION_MENTION` and `THIRD_PARTY_TECH_DATABASE` cap at `PROXY`;
`SCRIPT_FINGERPRINT` and `EMPLOYEE_PROFILE_MENTION` cap at `HYPOTHESIS`.

### Consequences

* A script tag proves a script loaded on a web page — nothing about the field
  workforce.
* The cap is enforced by the registry, not by asking extractors to be modest,
  which is the same mechanism that stopped M2 recording a directory category as
  a fact.

---

## M3-ADR-012 — Contradictions project as an envelope, never a silent winner

**Status:** accepted

### Context

Three sources will say 120, 80+ and 51–100 technicians. A scalar projection
must pick one, and picking one quietly destroys the most useful signal: that
the sources disagree.

### Decision

Quantity attributes are **RANGE**-valued. The projection stores `min`, `max`,
a `contradiction` flag, the contributing claim ids, and a separately-labelled
`_best` chosen by a deterministic precedence rule (fact type, source trust,
`source_published_at` with NULLs last, `retrieved_at`, then lowest claim id).

### Consequences

* A consumer reading only `_best` has chosen to ignore `contradiction`; the
  data did not hide it.
* The final tiebreak is the claim id, not "keep the current value" — M2 proved
  that rule is not deterministic on rebuild-from-empty (M2-ADR-024).

---

## M3-ADR-013 — Reuse M2's job queue; add no new infrastructure

**Status:** accepted

### Context

M3 adds fetching and extraction, both long-running and both wanting retries.
The reflex is a dedicated queue.

### Decision

Reuse `discovery_jobs` and its PostgreSQL `FOR UPDATE SKIP LOCKED` claiming
unchanged. **No Redis.** Four job types — `DISCOVER_SOURCES`,
`FETCH_ARTIFACT`, `EXTRACT_ARTIFACT`, `ASSERT_CLAIMS` — because each has a
genuinely different failure and retry profile.

`REBUILD_RESEARCH_PROFILE` is deliberately **not** a job: the profile is a
derived projection rebuilt synchronously, and queuing it would create a state
where claims exist and the profile silently lags.

### Consequences

* No second datastore, no second operational surface.
* If PostgreSQL ever proves genuinely insufficient, that is a new ADR with
  measurements — not an assumption made in advance.
