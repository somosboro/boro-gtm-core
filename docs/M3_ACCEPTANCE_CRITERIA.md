# M3 — Acceptance Criteria

**Status:** design, revision 1. **Not implemented.** No test below exists.

Scenarios M3 must satisfy before it is considered done. Every scenario marked
**MUST** runs against real PostgreSQL. No scenario may require public internet
access: source providers are fixtures reading local files, exactly as M2 shipped.

Companion to [M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md).

---

## A. Source and artifact identity

### A1 [MUST] — The same URL, unchanged, is not re-recorded
**Given** a source fetched once, producing one artifact and one version
**When** the identical URL is fetched again and the bytes are identical
**Then** no new artifact, no new version and no new source row is created; only
the retrieval is recorded as a sighting.
*Protects:* re-running research must not inflate the evidence base.

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

### A4 [MUST] — Same content at two URLs converges at the artifact
**Given** the same PDF served at `/docs/maint.pdf` and `/files/maint.pdf`
**When** both are fetched
**Then** two `research_sources` rows exist, two versions exist, and both point
at **one** `research_artifacts` row.
*Protects:* "mirrored at two URLs" is a finding worth keeping, while the
document is recognised as one document.

### A5 [MUST] — A redirect is recorded, not silently followed
**Given** `/about-us` returns 301 to `/company/about`
**When** it is fetched
**Then** both sources exist, `resolves_to_source_id` links them, the version
records `final_url`, and neither source is deleted or merged.

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

### A9 [MUST] — Canonicalization version participates in identity
**Given** an artifact stored under `HTML_TEXT_V1`
**When** the same bytes are canonicalized under `HTML_TEXT_V2` producing an
identical hash
**Then** two distinct artifacts exist, because the contract differs.
*Protects:* a hash is meaningless without the algorithm that produced it.

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

### C7 [MUST] — The same claim from two sources is kept twice
**Given** two independent sources both stating "24/7 emergency service"
**When** both are extracted
**Then** **two** claims exist, not one.
*Protects:* independent corroboration is the strongest state M3 reaches and
must be representable.

### C8 [MUST] — The same claim from one source twice is kept once
**Given** one extraction rerun at the same extractor version over one artifact
version
**When** assertion runs again
**Then** no duplicate claim is created.

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

### D4 [MUST] — Re-extraction under a new extractor adds, never rewrites
**Given** an artifact version already extracted at extractor version 1
**When** extractor version 2 runs over the same version
**Then** a new extraction and new claims exist, the version-1 extraction and
its claims are byte-identical to before, and the profile prefers the newer
extractor.

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

### F3 [MUST] — Never-looked is distinguishable from looked-and-failed
**Given** one attribute never attempted and one attempted across eleven sources
**When** both gaps are read
**Then** `attempted_source_count` distinguishes them.

### F4 [MUST] — A gap closes by resolution, not deletion
**Given** an open gap later satisfied by a claim
**When** the gap is resolved
**Then** the row persists with a resolution event and the closing claim id.

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

**Scenario count: 67 (all MUST)** — A:9, B:5, C:11, D:6, E:6, F:6, G:10, H:7, I:3, J:4.
