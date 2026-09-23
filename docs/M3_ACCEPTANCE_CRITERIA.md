# M3 — Acceptance Criteria

**Status:** design, revision 2. **Not implemented.** No test below exists.

Scenarios M3 must satisfy before it is considered done. Every scenario marked
**MUST** runs against real PostgreSQL. No scenario may require public internet
access: source providers are fixtures reading local files, exactly as M2 shipped.

Companion to [M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md).

---

## A. Source and artifact identity

### A1 [MUST] — Repeated identical retrieval appends an event, nothing else
**Given** a source fetched once, producing one body and one artifact
**When** the identical URL is fetched again on two later dates, returning
identical bytes each time
**Then** exactly one `research_artifact_bodies` row, one `research_artifacts`
row and one `research_sources` row exist, **three** `research_fetch_events`
rows exist, and no evidence row was updated.
**And** the system can answer "we saw these exact bytes on Sep 1, Sep 8 and
Sep 20" from those events alone.
*Protects:* revision 1 promised this and had nowhere to record the second
retrieval.

### A2 [MUST] — Cosmetic change: one artifact, two versions
**Given** a page whose only change between fetches is a build hash in a script
tag and a footer copyright year
**When** it is fetched again
**Then** a **new version** is recorded (bytes differ) under the **same
artifact** (canonical content is unchanged), and no new claims are asserted.
*Protects:* a rotating banner must not look like a change of fact.

### A3 [MUST] — Semantic change: new artifact and new version
**Given** a services page previously stating "24-hour emergency service"
**When** it is refetched and now states "emergency service weekdays only"
**Then** a new artifact **and** a new version are created, the prior artifact
is untouched, and the prior claim remains with its original `observed_at`.
*Protects:* history is not rewritten by the present.

### A4 [MUST] — Same bytes at two URLs converge at the body
**Given** the same PDF served at `/docs/maint.pdf` and `/files/maint.pdf`
**When** both are fetched
**Then** two `research_sources` rows and two `research_fetch_events` rows
exist, both pointing at **one** `research_artifact_bodies` row, and a
`MIRROR_CANDIDATE` edge is recorded between the sources.
**And** the 2 MB payload is stored once.
*Protects:* "mirrored at two URLs" is a finding worth keeping, while the
document is stored and recognised once.

### A5 [MUST] — A redirect is recorded as an edge, not a mutation
**Given** `/about-us` returns 301 to `/company/about`
**When** it is fetched
**Then** both sources exist, a `REDIRECTS_TO` edge links them carrying the
observing fetch event, the fetch event records `final_url`, and neither source
row is updated or merged.

### A6 [MUST] — A declared canonical URL is evidence, not an instruction
**Given** a page whose `<link rel="canonical">` points at an unrelated host
**When** it is captured
**Then** the canonical edge is recorded and **no** merge occurs.
*Protects:* a page may misdeclare its canonical URL.

### A7 [MUST] — Tracking parameters do not create a new source
**Given** `/services` and `/services?utm_source=nl&gclid=x`
**When** both are discovered
**Then** one source exists, because the locator policy strips tracking
parameters, and its `locator_policy_version` is recorded.

### A8 [MUST] — A disappearing page does not erase its evidence
**Given** an artifact captured from a source that now returns 410
**When** the source is refetched
**Then** the failure is recorded as `fetch_outcome = 'GONE'`, the prior
artifact, versions and claims all remain, and claims are **not** invalidated.
*Protects:* the page existed and we saw it; that stays true.

### A9 [MUST] — One body, two canonicalization versions, two artifacts
**Given** a body already derived under `HTML_TEXT_V1` into artifact A
**When** the **same body** is derived under `HTML_TEXT_V2`
**Then** a second `research_artifact_derivations` row exists pointing at a
distinct artifact B, the V1 derivation and artifact A are byte-identical to
before, **no refetch occurs** and no new body is created.
*Protects:* revision 1's single `version → artifact` pointer made this
unrepresentable.

### A10 [MUST] — A text extractor upgrade needs no refetch
**Given** a body with a text derivation at policy `v1`
**When** text policy `v2` is applied
**Then** a second `research_text_derivations` row exists, the `v1` derivation
is unchanged, **no new body and no new artifact** are created, and no fetch
occurs.
*Protects:* revision 1 put `extracted_text` and its policy version on the same
append-only row as the bytes, so this required an UPDATE.

### A11 [MUST] — A failed fetch is an event with no body
**Given** a source returning 403 eleven times
**When** the fetches complete
**Then** eleven `research_fetch_events` rows exist with `body_id IS NULL` and
`fetch_outcome = 'DENIED'`, and no body, artifact or claim is created.
*Protects:* "we tried and were refused" must be distinguishable from "we never
looked".

### A12 [MUST] — One source serves two research runs without losing provenance
**Given** a trade-association page already fetched for company A
**When** company B's research discovers the same URL a year later
**Then** one `research_sources` row is reused, two
`research_source_discoveries` rows exist naming different attempts, and each
company's evidence traces to its own fetch event.
*Protects:* revision 1's `discovered_by_run_id` could record only the first.

### A13 [MUST] — One source discovered by two methods records both
**Given** a page found via the sitemap and independently via a search provider
**When** both discoveries are recorded
**Then** two `research_source_discoveries` rows exist with different
`discovery_method` values, and one source row.

### A14 [MUST] — A redirect learned later mutates nothing
**Given** a source created six months ago with no known redirect
**When** a later fetch observes a 301 to a new location
**Then** a `REDIRECTS_TO` edge is appended, the source row is byte-identical to
before, and a subsequent change of redirect target appends a **second** edge
without removing the first.

---

## B. Evidence locators

### B1 [MUST] — An HTML claim cites an exact span
**Given** a services page stating "24-hour emergency HVAC service"
**When** `emergency_service = true` is asserted
**Then** the evidence link carries a locator with a CSS path, a heading path,
the quote and its sha256, and the claim resolves back to that exact text.

### B2 [MUST] — A PDF claim cites a page and span
**Given** a licence PDF whose page 2 names a certification
**When** `certification` is asserted
**Then** the locator carries `page: 2`, a section, character offsets, the quote
and its hash.

### B3 [MUST] — A job-posting claim cites the field and span
**Given** a posting whose description reads "team of more than 80 technicians"
**When** `technician_count` is asserted
**Then** the locator names the field, the character span, the quote and its hash.

### B4 [MUST] — A locator survives reprocessing by quote hash
**Given** a claim whose locator's CSS path no longer matches after a site
restructure, while the quoted sentence is still present
**When** the locator is resolved
**Then** it resolves by quote hash, and `locator_status = 'RESOLVED'`.
*Protects:* locators must not depend on line numbers from arbitrary parsing.

### B5 [MUST] — A rotted locator weakens, never deletes
**Given** a claim whose quote no longer appears in the retained text
**When** the locator is resolved
**Then** `locator_status = 'UNRESOLVED'`, the claim persists, and the claim is
not invalidated.

---

## C. Claims, contradictions and inference

### C1 [MUST] — Conflicting sources coexist
**Given** the site says 120 technicians, a posting says 80+, a profile says
51–100
**When** all three are extracted
**Then** three claims exist, none overwritten, and the projection reports
`min 51`, `max 120`, `contradiction = true` with all three claim ids.
*Protects:* confidence must not silently erase disagreement.

### C2 [MUST] — Precedence is deterministic
**Given** the contradiction in C1
**When** the profile is rebuilt three times
**Then** the selected `_best` value and the contributing ids are identical
every time.
*Protects:* "keep the current value" is not a rule.

### C3 [MUST] — An inference is never FACT
**Given** `emergency_service = true` and three branches
**When** the dispatch rule runs
**Then** `dispatch_centralization` is asserted with `fact_type = 'INFERENCE'`
and a recorded rule id and version, and an attempt to assert it as `FACT` is
rejected by the registry.

### C4 [MUST] — A claim cannot exist without evidence
**Given** an attempt to insert an M3-owned claim with no `claim_evidence_links`
row
**When** the transaction commits
**Then** the deferred trigger rejects it.
*Protects:* an unsourced research claim must be unrepresentable, not merely
discouraged.

### C5 [MUST] — One claim, several supporting artifacts
**Given** the same statement found on the services page and in a PDF brochure
**When** the claim is asserted
**Then** one claim has two evidence links, each with its own locator.

### C6 [MUST] — One artifact, several claims
**Given** a page stating both emergency service and a service area
**When** extraction runs
**Then** two claims exist, both linking the same artifact version at different
locators.

### C7 [MUST] — Two independent lineages produce two claims
**Given** two independent sources both stating "24/7 emergency service"
**When** both are extracted
**Then** **two** claims exist with different `lineage_key` values and therefore
different assertion fingerprints, and the profile reports
`corroborating_lineage_count = 2`.
*Protects:* the two claims may differ in fact type, confidence, source trust
and source date; merging them would destroy all four.

### C8 [MUST] — The same lineage asserted twice produces one claim
**Given** one extraction rerun at the same extractor version over one text
derivation
**When** assertion runs again
**Then** no duplicate claim is created, because the assertion fingerprint is
identical.

### C8a [MUST] — A newer extractor over the same lineage does not double-count
**Given** a claim asserted from lineage L by extractor v1
**When** extractor v2 reads the same lineage and produces the **same** value
**Then** **no second claim is created**; a new `claim_evidence_links` row is
appended to the existing claim naming the v2 extraction, and
`corroborating_lineage_count` remains 1.
*Protects:* revision 1 created a new claim per extractor version, so
re-extracting one page three times looked like threefold corroboration.

### C8b [MUST] — A newer extractor disagreeing with itself creates a claim
**Given** the same lineage L and extractor v2 producing a **different** value
**When** assertion runs
**Then** a second claim exists — a real disagreement within one lineage — and
the projection reports a contradiction.

### C8c [MUST] — One assertion may need several spans
**Given** an inference drawn from three job postings and a services page
**When** it is asserted
**Then** **one** claim exists with four `claim_evidence_links` rows, each with
its own locator, and `corroborating_lineage_count` counts one lineage.

### C9 [MUST] — Absence is never false
**Given** a complete research run that found no ERP evidence
**When** the profile and gaps are produced
**Then** no `erp` claim exists with value `false`, `0` or "none"; an
`erp` gap of kind `NO_EVIDENCE` exists instead.
*Protects:* the rule M3 is under the most pressure to break.

### C10 [MUST] — A stated negative is a fact
**Given** a page stating "we do not offer emergency service"
**When** it is extracted
**Then** `emergency_service = false` is asserted as `FACT`.
*Protects:* silence and denial are different, and both are representable.

### C11 [MUST] — The provenance walk always terminates at bytes or a hash
**Given** any M3 claim
**When** the chain claim → link → extraction → version → artifact → source is
walked
**Then** every hop resolves, and the terminal node yields either the retained
body or its sha256.

---

## D. Extraction

### D1 [MUST] — Extractor provenance is complete
**Given** a model-assisted extraction
**When** it is stored
**Then** extractor kind, id, version, model provider, model name, model
version, prompt template version, output schema version, determinism and
timestamp are all recorded.

### D2 [MUST] — Model confidence does not become claim confidence
**Given** an extraction with `extractor_confidence = 0.99` over a third-party
blog post
**When** the claim is asserted
**Then** its `fact_type` is `PROXY` (determined by the source) and its
`confidence` is computed from evidence type and source trust, not from 0.99.
*Protects:* a model cannot upgrade weak evidence by being sure.

### D3 [MUST] — A low-confidence extraction yields a gap, not a claim
**Given** an extraction below the review threshold
**When** assertion runs
**Then** no claim is created; a review candidate and an
`INSUFFICIENT_EVIDENCE` gap are.

### D4 [MUST] — Re-extraction adds evidence, never rewrites, never duplicates
**Given** a text derivation already extracted at extractor version 1
**When** extractor version 2 runs over the same derivation
**Then** a new `research_extractions` row exists, the version-1 extraction is
byte-identical to before, and for every value both versions agree on, the
existing claim gains an evidence link rather than a twin (C8a). Values only
v2 produces become new claims.

### D5 [MUST] — Re-running the same extractor version is a no-op
**Given** an extraction at `(artifact_version, extractor, version, prompt)`
**When** the identical extraction is run again
**Then** no new row is created.

### D6 [MUST] — A human review appends
**Given** a model claim a reviewer rejects
**When** the review is recorded
**Then** a `HUMAN` extraction and a new claim are appended; the model's
extraction and claim are unchanged.

---

## E. Temporal and staleness

### E1 [MUST] — A retrieval date never becomes a fact date
**Given** an undated page fetched today
**When** claims are asserted
**Then** `source_published_at` is NULL, granularity is `UNDATED`,
`period_granularity` is `SNAPSHOT` and `observed_at` is NULL.
*Protects:* the most common way an evidence system starts lying.

### E2 [MUST] — A stated publication date is preserved at its own granularity
**Given** a posting stating "Posted March 2026"
**When** it is captured
**Then** `source_published_at = 2026-03-01` with granularity `MONTH`, and no
day-level precision is invented.

### E3 [MUST] — An old fact is not rewritten to false
**Given** a `certification` claim observed in 2019, past its horizon
**When** staleness is evaluated
**Then** the claim is unchanged, `staleness = 'STALE'` appears on the projected
row, and a `STALE_EVIDENCE` gap is raised.

### E4 [MUST] — Undated evidence reports unknown age
**Given** a claim whose source carries no date
**When** staleness is evaluated
**Then** `staleness = 'UNKNOWN_AGE'`, not `FRESH` and not `STALE`.

### E5 [MUST] — The projection reads no clock
**Given** a profile rebuilt, then rebuilt again with the system clock advanced
by 400 days
**When** the two projections are compared
**Then** they are byte-identical; only the `current_*` view differs.

### E6 [MUST] — A removed posting keeps its claim
**Given** a job posting whose page now 404s
**When** research reruns
**Then** `removed_observed_at` is recorded, the `hiring_signal` claim persists
with its original `observed_at`, and only its staleness changes.

---

## F. Research gaps and coverage

### F1 [MUST] — Gap identity is deterministic
**Given** a company with no FSM evidence
**When** research runs three times under one policy version
**Then** exactly one `field_service_management` gap row exists.

### F2 [MUST] — A gap means unknown, not absent
**Given** any gap row
**When** it is read through the API
**Then** it states insufficient evidence and never asserts the company lacks
the capability.

### F3 [MUST] — Eleven attempts leave the gap parent byte-identical
**Given** one attribute never attempted and one attempted across eleven sources
**When** both gaps are read
**Then** eleven `operational_research_gap_events` rows of kind `ATTEMPTED`
exist for the second, the derived `attempted_source_count` distinguishes them,
and **both gap parent rows are byte-identical to when they were raised**.
*Protects:* revision 1 put a changing counter on a row it declared
append-only.

### F4 [MUST] — A gap closes by an event, with zero UPDATE to the parent
**Given** an open gap later satisfied by a claim
**When** it is resolved
**Then** a `RESOLVED` event is appended carrying the closing claim id, the
derived status becomes resolved, and the gap parent row is unchanged.

### F5 [MUST] — Coverage excludes non-applicable attributes from both sides
**Given** an attribute the policy marks not-applicable for this vertical
**When** coverage is computed
**Then** it leaves numerator **and** denominator untouched.
*Protects:* unknown must not be scored as zero.

### F6 [MUST] — Coverage, confidence and contradiction stay separate
**Given** any research profile
**When** it is read
**Then** three distinct numbers are present and no combined figure exists.

---

## G. Runs, idempotency and concurrency

### G1 [MUST] — A run that has not extracted asserts nothing
**Given** a run whose fetch failed before completing
**When** assertion is requested
**Then** it is refused with 409 unless `allow_partial_assertion` was set at
creation.
*Protects:* inherits M2-ADR-031 — the gate is a durable timestamp, never
`status`.

### G2 [MUST] — A later stage cannot launder an incomplete earlier stage
**Given** a run whose fetch never completed
**When** extraction runs and sets `status = 'EXTRACTING'`
**Then** assertion is still refused, because the gate reads
`extraction_completed_at` and `fetch_completed_at`, not `status`.

### G3 [MUST] — A retry advances the same run
**Given** a `PARTIAL` run
**When** it is retried
**Then** the same run id advances; no second run is created.

### G4 [MUST] — A policy change creates a new run
**Given** a completed run under policy `v1`
**When** research is requested under `v2`
**Then** a new run is created and the `v1` run is untouched.

### G5 [MUST] — Two workers fetching one URL produce one version
**Given** two connections fetching the same URL with identical bytes
**When** both commit
**Then** one version row exists and neither worker sees a raw `IntegrityError`.
*Runs against two real connections.*

### G6 [MUST] — Two workers extracting one artifact produce one extraction
*Two real connections.*

### G7 [MUST] — Two workers raising one gap produce one gap
*Two real connections.*

### G8 [MUST] — Two concurrent runs on one company are prevented
**Given** a live run for a company under a policy
**When** a second is requested
**Then** it is refused by the partial unique index, and the caller receives a
domain error, not an `IntegrityError`.

### G9 [MUST] — A failed fetch is retried, then recorded
**Given** a source returning 503 three times
**When** the fetch job exhausts its retries
**Then** the failure is recorded as data, the run may still reach `PARTIAL`,
and other sources are unaffected.

### G10 [MUST] — One bad source does not lose the others' evidence
**Given** a page that cannot be parsed among nine that can
**When** the run proceeds
**Then** the nine are captured and extracted, and the failure is recorded
against its own source.
*Protects:* M2 shipped the opposite defect once.

---

## G-bis. Logical runs and execution attempts

### G11 [MUST] — A PARTIAL attempt is never reopened
**Given** an attempt that ended `PARTIAL`
**When** research is retried
**Then** a **new** attempt with `attempt_number + 1` is created on the same
run, the `PARTIAL` attempt is byte-identical to before, and any attempt to
transition it out of a terminal state is rejected by the trigger.
*Protects:* revision 1 called `PARTIAL` terminal and simultaneously had retry
reopen it.

### G12 [MUST] — Evidence remembers which attempt captured it
**Given** evidence captured by an attempt that later failed
**When** the evidence is read
**Then** its fetch events, discoveries and extractions still name that attempt.

### G13 [MUST] — A different target attribute set is a different run
**Given** a completed run for company X under policy `v2` across 18 attributes
**When** research is requested for the same company and policy across 19
attributes
**Then** `target_set_hash` differs, a **new** logical run is created, and the
original run and its attempts are untouched.
*Protects:* revision 1 asserted this in prose while leaving the target set out
of the run key.

### G14 [MUST] — The same question reuses the run
**Given** the same company, policy version and target set
**When** research is requested again
**Then** the existing run is reused and a new attempt executes it.

### G15 [MUST] — Only one attempt may be live per run
**Given** a run with a live attempt
**When** a second attempt is requested
**Then** it is refused by the partial unique index, and the caller receives a
domain error rather than an `IntegrityError`.

## H. Firewalls

### H1 [MUST] — M2 is unchanged by a research run
**Given** a fingerprint of every M2 table's row count
**When** a full research run completes
**Then** the fingerprint is identical.
*The strong form, as M2 already does for M1.*

### H2 [MUST] — M3 cannot create a company
**Given** research discovering an apparently unknown subsidiary
**When** the run completes
**Then** no `companies` row is created; an `identity_review_signals` row is.

### H3 [MUST] — M3 cannot alter a resolution decision
**Given** an attempted update to `entity_resolution_decisions`
**When** it executes
**Then** the append-only trigger rejects it.

### H4 [MUST] — An identity conflict raises a signal and stops
**Given** a site stating "a division of Other Co"
**When** it is extracted
**Then** a `POSSIBLE_PARENT` signal is raised with evidence links, and no
merge, no domain change and no decision occurs.

### H5 [MUST] — A GROUP domain is never crawled as the company's site
**Given** a company whose only domain is `krause.wixsite.com`, role `GROUP`
**When** discovery runs
**Then** the host is not crawled, a `NO_CRAWLABLE_SEED` gap is raised, and
other source kinds proceed.
*Protects:* attributing another company's pages is worse than missing evidence.

### H6 [MUST] — No M4 scoring field exists
**Given** the full M3 schema
**When** column names are inspected
**Then** none matches `lead_score`, `qualification_score`, `icp_fit_score`,
`pain_score`, `priority_score`, `recommend_contact`, `sales_ready`, `tier` or
`grade`.
*Enforced as a test, so the boundary fails a build rather than eroding.*

### H7 [MUST] — No person record is created
**Given** a posting naming a recruiter and a case study quoting a manager
**When** extraction runs
**Then** no person, contact or email row exists, and the names are redacted
from `extracted_text` with `redaction_policy_version` recorded.

---

## I. Retention

### I1 [MUST] — A pruned body leaves provenance intelligible
**Given** an artifact version whose `raw_body` is pruned by policy
**When** a claim citing it is read
**Then** the source, retrieval time, status, both hashes,
`source_published_at`, `body_retention = 'PRUNED'` and `pruned_at` are all
present, and the evidence link still carries its quote and quote hash.
*Protects:* deletion must not silently invalidate history.

### I2 [MUST] — Pruning is the only permitted mutation
**Given** an append-only artifact version
**When** any UPDATE other than the one-way body prune is attempted
**Then** the trigger rejects it.

### I3 [MUST] — Evidence rows are never deleted by retention
**Given** retention running over an old run
**When** it completes
**Then** no `claim_evidence_links`, `research_extractions` or claim row is
deleted; only body columns are nulled.

---

## J. Technology and job-posting evidence typing

### J1 [MUST] — A script fingerprint cannot exceed HYPOTHESIS
**Given** a vendor script tag detected on a company site
**When** the technology claim is asserted
**Then** its fact type is `HYPOTHESIS`, and `FACT` is rejected by the registry.

### J2 [MUST] — A job mention of a tool cannot exceed PROXY
**Given** a posting for a "ServiceNow administrator"
**When** the claim is asserted
**Then** its fact type is at most `PROXY` and the value records
`evidence_class = 'JOB_DESCRIPTION_MENTION'`.
*Protects:* hiring an administrator does not prove operational adoption.

### J3 [MUST] — An explicit company statement may be FACT
**Given** a page stating "we run ServiceTitan"
**When** the claim is asserted
**Then** `FACT` is permitted with
`evidence_class = 'EXPLICIT_COMPANY_STATEMENT'`.

### J4 [MUST] — Operating-model attributes cannot be FACT
**Given** an attempt to assert `dispatch_centralization` as `FACT`
**When** validation runs
**Then** the registry rejects it.

---

## K. Definition of done

* Every **MUST** scenario is an executable test against real PostgreSQL.
* No test requires network access; all providers are fixtures.
* M0/M1/M2 suites pass unchanged, and M0 reference reproduction still reports
  `max_score_delta` 0.0 and `rank_mismatches` 0.
* The M2 fingerprint test (H1) runs in CI.
* The M4-boundary test (H6) runs in CI.
* Profile rebuild determinism (E5, C2) runs in CI with an advanced clock.
* Concurrency scenarios (G5–G8) use two real connections, never one session.
* At least one fixture provider exists per artifact kind: HTML, PDF, JSON and
  job posting.
* Migrations verified from an empty database, and `db check --strict` clean.
* An append-only audit test: for every table declared append-only in the schema
  graph, an UPDATE to any column other than a documented one-way prune is
  rejected by the database.
* A terminal-state test: no attempt may transition out of `COMPLETED`,
  `PARTIAL` or `FAILED`.

**Scenario count: 80 (all MUST)** — A:14, B:5, C:14, D:6, E:6, F:6, G:15, H:7, I:3, J:4.
