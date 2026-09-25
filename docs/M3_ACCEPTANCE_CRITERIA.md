# M3 — Acceptance Criteria

**Status:** revision 5 — reconciled with the canonical commercial ontology.
Sections A–N are implemented as schema-invariant tests; section O covers the
reconciliation. Scenarios requiring the acquisition and extraction services are
not yet executable.

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
**Then** two claims exist, both linking the same extraction at different
locators.

### C7 [MUST] — Two independent lineages produce two claims
**Given** two independent sources both stating "24/7 emergency service"
**When** both are extracted
**Then** **two** claims exist with different `lineage_key` values and therefore
different assertion fingerprints, and the profile reports
`corroborating_publisher_count = 2`.
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
`corroborating_publisher_count` remains 1.
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
its own locator, and `corroborating_publisher_count` counts one lineage.

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
**Given** an extraction at `(text_derivation, extraction_contract_hash)`
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
**Then** its fetch events and discoveries still name that attempt directly, and
its extractions name it through `research_attempt_extractions` — extractions
are reusable results and do not own an attempt (M3-ADR-021).

### G13 [MUST] — A different target attribute set is a different run
**Given** a completed run for company X under policy `v2` across 18 attributes
**When** research is requested for the same company and policy across 19
attributes
**Then** `research_plan_hash` differs, a **new** logical run is created, and the
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
**Given** a `research_artifact_bodies` row whose `raw_body` is pruned by policy
**When** a claim citing it is read
**Then** the body reports its hash, byte length, `body_retention = 'PRUNED'`
and `pruned_at`; its fetch event still carries the source, the retrieval time
and the HTTP status; its artifact still carries `source_published_at`; and the
evidence link still carries its quote and quote hash.
*Protects:* deletion must not silently invalidate history.

### I2 [MUST] — Pruning is the only permitted mutation
**Given** an append-only body row
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

## L. Exact provenance, lineage and execution identity (revision 3)

### L1 [MUST] — One body fetched from two sources resolves to one intended source
**Given** the same PDF fetched from `/docs/maint.pdf` and `/files/maint.pdf`,
producing one body and two fetch events
**When** a claim is asserted from the first retrieval
**Then** its evidence link names exactly one `fetch_event_id` and one
`source_id`, and the provenance walk returns a **single** source — not both.
*Protects:* revision 2's walk fanned out across every retrieval of a body.

### L2 [MUST] — A link cannot cite an extraction and a fetch of different bodies
**Given** an extraction over body X and a fetch event that produced body Y
**When** an evidence link citing both is inserted
**Then** the composite foreign keys reject it.
*Protects:* the binding is proven by the database, not by the writer's care.

### L3 [MUST] — Two sources serving identical bytes do not corroborate
**Given** a company page and a third-party scrape serving byte-identical text,
both asserting `emergency_service = true`
**When** claims are asserted and the profile is built
**Then** **two claims** exist — the lineages differ by `source_id` — and
`corroborating_publisher_count` is **1**, because the artifact is the same
document.
*Protects:* "it is all over the internet" must not read as corroboration.

### L4 [MUST] — A company page and a registry do corroborate
**Given** the company site and a government registry stating the same fact in
different documents
**When** the profile is built
**Then** `corroborating_publisher_count` is **2**.

### L5 [MUST] — Two pages on one site do not corroborate
**Given** `/about` and `/services` both stating the fact
**When** the profile is built
**Then** two claims exist and `corroborating_publisher_count` is **1** — one
publisher saying it twice.

### L6 [MUST] — An extraction reused by a second attempt is not duplicated
**Given** attempt 1 created extraction E over a text derivation
**When** attempt 2 needs the identical extraction contract
**Then** **one** `research_extractions` row exists and **two**
`research_attempt_extractions` rows exist, with `usage_role` `CREATED` and
`REUSED`, and "which attempts used E?" returns both.
*Protects:* revision 2 stored one `attempt_id` on a reusable result.

### L7 [MUST] — A model version change is a distinct extraction contract
**Given** an extraction at prompt template v3 and model version `2026-01`
**When** the same prompt runs against model version `2026-06`
**Then** the `extraction_contract_hash` differs, a second extraction row
exists, and the first is byte-identical to before — no collision and no UPDATE.
*Protects:* revision 2's key omitted every model field while storing them.

### L8 [MUST] — A redaction policy upgrade is a distinct text derivation
**Given** a text derivation at text policy v1 and redaction policy v1
**When** redaction policy v2 is applied to the same body
**Then** a second `research_text_derivations` row exists, the first is
unchanged, and no refetch occurs.
*Protects:* revision 2 keyed on the text policy alone while storing the
redaction policy beside it.

### L9 [MUST] — One byte string, two declared content types
**Given** identical bytes served as `text/plain` by one host and `text/html`
by another
**When** both are fetched
**Then** **one** `research_artifact_bodies` row exists carrying **no** content
type, and two fetch events carry the two `declared_content_type` values.
*Protects:* a globally deduplicated row must not hold a per-retrieval fact.

### L10 [MUST] — The sniffed type names its classifier version
**Given** a body classified under classifier policy v1
**When** classifier v2 runs over the same body
**Then** two `research_body_classifications` rows exist and the v1 row is
unchanged.

### L11 [MUST] — The same question with different execution seeds is one run
**Given** a run for company X, policy v2, target set T, executed last month
**When** research runs again with a different projected identity-domain seed
**Then** the **same** logical run is reused, a new attempt records the new
`attempt_seed_inputs`, and no immutable run column is overwritten.
*Protects:* revision 2 kept seeds on the immutable run row outside its key.

### L12 [MUST] — A different vertical is a different question
**Given** a run for company X, policy v2, target set T, vertical A
**When** research is requested for the same company and target set under
vertical B
**Then** `research_plan_hash` differs and a **new** run is created.

### L13 [MUST] — A source fetched three times in one attempt records three attempts
**Given** a gap whose source is fetched three times within one attempt
**When** the gap events are read
**Then** **three** `ATTEMPTED` events exist, each naming its own
`fetch_event_id`, `attempt_count` is 3, `attempted_source_count` is 1, and
`last_attempt_at` is the **third** retrieval's time.
*Protects:* revision 2's key permitted one event per source per attempt, so
`last_attempt_at` reported the first.

### L14 [MUST] — A signal with a NULL related company cannot duplicate
**Given** a `POSSIBLE_CEASED_TRADING` signal with `related_company_id` NULL
**When** the identical signal is raised on a later attempt
**Then** one signal row exists, because the unique key is `NULLS NOT
DISTINCT`.

### L15 [MUST] — New evidence appends to an existing signal
**Given** an open identity signal with two evidence links
**When** a later attempt finds a third supporting span
**Then** a third `identity_review_signal_evidence` row is appended and the
signal parent row is byte-identical.

### L16 [MUST] — An ACTIONED signal cannot return to OPEN
**Given** a signal whose latest event is `ACTIONED`
**When** an `OPEN` event is appended
**Then** the transition trigger rejects it; revisiting requires a **new**
signal carrying new evidence.

### L17 [MUST] — Two search queries in one attempt both survive
**Given** one source found by query "acme hvac technicians" and by query
"acme mechanical careers" within one attempt
**When** discoveries are recorded
**Then** **two** `research_source_discoveries` rows exist with different
`discovery_context_hash` values.
*Protects:* revision 2's key omitted the context and discarded the second.

### L18 [MUST] — A machine-observed edge cannot exist without its fetch
**Given** a `REDIRECTS_TO` edge with `edge_origin = 'FETCH_OBSERVED'` and no
`observed_by_fetch_event_id`
**When** it is inserted
**Then** the CHECK rejects it.
**And** a `MIRROR_CANDIDATE` edge requires `edge_origin = 'DERIVED'` with
**both** supporting fetch events present.

### L19 [MUST] — Historical confidence stays explainable after a trust upgrade
**Given** a claim asserted under `trust_policy_version` v1 with a recorded
`source_class` and `trust_tier`
**When** trust policy v2 ships with different tiers
**Then** the existing claim's `confidence` is unchanged, its evidence links
still report the v1 policy version and the v1 tier, and the stored number can
be recomputed exactly from them.
*Protects:* an uncalibrated trust table is acceptable; an unexplainable
persisted number is not.

## M. Implementation-readiness lock (revision 4)

### M1 [MUST] — Two research plans for one company coexist
**Given** company X with plan A (HVAC context, 18 required attributes) and plan
B (another context, 12 required)
**When** both are executed and projections rebuilt
**Then** two `operational_research_plan_profiles` rows exist with different
coverage, **neither overwrites the other**, and one
`operational_research_profiles` row holds the merged company-global facts.
*Protects:* revision 3's `PK (company_id)` could store only one coverage.

### M2 [MUST] — Plan-specific gaps do not collide across plans
**Given** plan A and plan B both lacking ERP evidence
**When** gaps are raised
**Then** two gap rows exist, keyed by their runs — two questions going
unanswered, not one.

### M3 [MUST] — An identity conflict needs no fabricated claim
**Given** a page stating "ABC Service is a division of XYZ Holdings", and no
operational claim yet asserted for ABC
**When** the conflict is recorded
**Then** a `research_evidence_items` row exists with its locator and quote, an
identity signal occurrence references it, **zero `company_claims` rows were
created**, and a reviewer can read the supporting span.
*Protects:* revision 3 forced a claim into existence to hold the evidence.

### M4 [MUST] — Evidence names the exact derivation it came from
**Given** one body derived under `HTML_TEXT_V1` and `HTML_TEXT_V2`
**When** a claim is asserted from the V2 reading
**Then** its evidence item names that `artifact_derivation_id`, and the
artifact reached through it is V2's — not V1's.
*Protects:* `body_id` alone never determined the artifact.

### M5 [MUST] — Mirrors with different publication metadata stay one artifact
**Given** the same article on two sites, one stamped "Published March 2026" and
one undated
**When** both are captured
**Then** **one** `research_artifacts` row exists, two derivations exist
carrying `source_published_at = 2026-03-01` and `NULL` respectively, and
`corroborating_publisher_count` is **1**.
*Protects:* folding publication metadata into the canonical hash would have
split the mirrors and counted them as two independent witnesses.

### M6 [MUST] — A trust policy upgrade can re-assert
**Given** a claim asserted under `trust_policy_version` v1
**When** the same value and lineage are re-asserted under v2
**Then** `assertion_contract_hash` differs, so a **new** claim is created; the
v1 claim's `confidence` is unchanged and remains recomputable from its own
links.
*Protects:* revision 3's fingerprint omitted policy versions, so the partial
unique index rejected the v2 claim and the documented behaviour was
unreachable.

### M7 [MUST] — The same number in different units does not collide
**Given** `technician_count = 40` with unit `PEOPLE` and the same value with a
different unit
**When** both are asserted
**Then** their fingerprints differ and both claims exist.

### M8 [MUST] — Claim confidence reproduces exactly
**Given** a claim with four evidence links across two independent publishers
and mixed trust tiers
**When** confidence is recomputed from the links and the versions named in its
`assertion_contract_hash`
**Then** the result equals the persisted value, bit for bit.
**And** adding a weaker corroborating link never lowers confidence.

### M9 [MUST] — A publisher policy upgrade does not re-score history
**Given** a projection built under `publisher_policy_version` v1 reporting one
corroborating publisher
**When** v2 maps one of the sources to a different publisher
**Then** the v1 projection's recorded policy version still explains its count,
and a rebuild under v2 records v2 — the two are distinguishable, not silently
swapped.

### M10 [MUST] — A 304 validates without fabricating bytes
**Given** a source previously fetched, yielding body B
**When** a later request returns 304 Not Modified
**Then** a fetch event exists with `http_status = 304`, `body_id = B`, the
validator recorded, and **no new body row**; the event answers "at time T the
server confirmed B was still current" and freshness reads it.
*Protects:* revision 3 left 304's body semantics undefined.

### M11 [MUST] — A sampled extraction cannot masquerade as idempotent
**Given** a `SAMPLED` extraction contract run twice over one text derivation
**When** both complete
**Then** two extraction rows exist, distinguished by `sample_execution_id`, and
**neither asserts a `company_claim` directly**; a claim requires a confirming
`HUMAN` extraction.
*Protects:* a UNIQUE key keeping the first sample is a frozen race, not
idempotency.

### M12 [MUST] — A second attempt may carry different seeds
**Given** a completed attempt whose `attempt_seed_inputs` recorded two
projected identity domains
**When** the same logical run is retried after a third domain is projected
**Then** the **same run** is reused, attempt 2 records the new seed snapshot
and its hash, and attempt 1's seeds are byte-identical to before.

### M13 [MUST] — Attempt seeds freeze once the attempt starts
**Given** an attempt past `PENDING`
**When** its `attempt_seed_inputs` is updated
**Then** the trigger rejects it.

### M14 [MUST] — A company-level signal touches no M2 table
**Given** a `POSSIBLE_CEASED_TRADING` signal with no provider entity
**When** it is raised with evidence and a reviewer dismisses it
**Then** an M2 row-count fingerprint is identical throughout, no
`provider_entities` row is created, and no `entity_resolution_decisions` row is
written.
*Protects:* M2's review requires a `ProviderEntity`; routing this through it
would mean fabricating one.

### M15 [MUST] — New evidence leaves signal identity untouched
**Given** an open signal occurrence with two evidence items
**When** a later attempt finds a third supporting span
**Then** a third `identity_review_signal_evidence` row is appended, and both
the signal and the occurrence rows are byte-identical.
*Protects:* an evidence-set hash cannot be an immutable identity key.

### M16 [MUST] — The same concern after a terminal occurrence opens a new one
**Given** a signal whose occurrence 1 was `DISMISSED`
**When** the same concern is rediscovered
**Then** occurrence 2 is created referencing occurrence 1 as its predecessor,
occurrence 1 stays `DISMISSED`, and the signal row is unchanged.

## N. Registry fidelity (added during implementation)

### N1 [MUST] — The seeded registry matches the design table exactly
**Given** the attribute table in `M3_OPERATIONAL_RESEARCH_DESIGN.md` §7
**When** the M3 registry is seeded
**Then** the set of seeded `attribute_key` values equals the set of keys in
that table — no more, no fewer — and every one is `owner_milestone = 'M3'`
under registry version `M3-1.0`.
*Protects:* revision 4's prose claimed twenty-four attributes while its own
tables listed thirty-one. A hand-written count drifted; this test is the
mechanical one that cannot.

### N2 [MUST] — Required attributes drive coverage's denominator
**Given** the registry
**When** the required subset is computed
**Then** it is exactly the attributes marked required, and a non-applicable
attribute leaves coverage's numerator **and** denominator untouched.

## O. Canonical commercial alignment (added in revision 5)

Scenarios protecting the boundary between what GTM Core may observe and what
the canonical commercial ontology alone may decide. See
`GTM_ACCOUNT_FIELD_OWNERSHIP.md` and `M3_CANONICAL_COMMERCIAL_ALIGNMENT.md`.

### O1 [MUST] — A public fact never becomes a feature requirement
**Given** an evidence item stating a company uses a named ERP
**When** the claim is asserted
**Then** it is recorded as an operational observation with its own fact type,
and no capability id, module requirement or scope item is written anywhere as a
consequence. Requirements exist only downstream of an approved architecture.

### O2 [MUST] — Pre-outreach evidence never writes a qualification score
**Given** a company with the maximum possible M3 coverage
**When** every attribute is asserted at `FACT`
**Then** no qualification dimension, no composite qualification score and no
qualification route exists for that company. The canonical rubric is scored
after a diagnostic call, which M3 cannot observe.

### O3 [MUST] — Pre-architecture evidence never writes a commercial level
**Given** a company whose evidence strongly suggests complexity
**When** the research run completes
**Then** no classifier dimension and no commercial level — candidate or final —
is written. The classifier scores an approved architecture, not a website.

### O4 [MUST] — Contract value alone never classifies
**Given** any evidence bearing a monetary figure
**When** it is asserted
**Then** nothing in GTM Core derives a level, tier, price, floor or band from
it. The canonical anti-rule is carried in the contract and asserted verbatim.

### O5 [MUST] — Every canonical `EV-*` signal has a declared coverage verdict
**Given** the 18 canonical evidence signals
**When** the coverage matrix is evaluated
**Then** each signal maps to exactly one of `DIRECT` or `PARTIAL`, and **no**
signal maps to `NONE`. A signal with no primitive behind it is a gap, and a
gap must be visible rather than silently absent.

### O6 [MUST] — Each `EV-*` signal names at least one registry primitive
**Given** the coverage matrix
**When** each signal's supporting attribute keys are resolved
**Then** every key exists in the seeded registry at the current registry
version. A matrix that cites an attribute nobody seeds is drift, not coverage.

### O7 [MUST] — Process-observation primitives are observational, not judgemental
**Given** the eleven `PROCESS_OBSERVATION` attributes
**When** their definitions are validated
**Then** none is boolean-with-a-verdict, none carries an efficiency,
maturity, readiness or fit semantic, and each records something a person could
point at in a source document.

### O8 [MUST] — Inference-only primitives can never be asserted as FACT
**Given** a `PROCESS_OBSERVATION` attribute whose allowed fact types exclude
`FACT`
**When** an assertion attempts `FACT`
**Then** the registry rejects it. What a company's process *is* cannot be a
public fact merely because a job ad hints at it.

### O9 [MUST] — Evidence class ceilings bound fact type
**Given** a claim whose evidence class is `SCRIPT_FINGERPRINT`
**When** it is asserted above its ceiling
**Then** validation fails. A fingerprint is at most a hypothesis regardless of
how confident the extractor is.

### O10 [MUST] — Absence of a signal is `NOT_AVAILABLE`, never negative
**Given** a company for which no approval-step observation was found
**When** the Q2 projection is built
**Then** the signal is reported as unobserved, with a NULL value and NULL fact
type — never `false`, never `0`, never "no approvals required".

### O11 [MUST] — The Q2 projection carries observation dates, not fetch dates
**Given** an evidence item fetched today from a page published last year
**When** the Q2 seven-field projection is built
**Then** `date_observed` reflects the source's own observation date and is
**never** substituted with `retrieved_at`. A crawl date is not an event date.

### O12 [MUST] — The Q2 projection refuses to emit an unsupported claim
**Given** a claim with no surviving supporting evidence item
**When** the projection is built
**Then** the claim is excluded and the omission is recorded, rather than
emitted with an empty provenance list.

### O13 [MUST] — Outbound-safe classing is explicit, not inferred
**Given** a claim derived from an inference
**When** its outbound policy class is computed
**Then** it is not marked quotable in outreach. Only claims whose evidence
class and fact type both permit it may be referenced to a prospect.

### O14 [MUST] — M3 writes exactly two canonical Q1 fields
**Given** the 25 canonical Q1 account fields
**When** the set of fields M3 may write is computed from the ownership map
**Then** it is exactly `evidence[]` and `evidence_confidence`, and the ten
explicitly-forbidden fields appear in no M3 write path.

### O15 [MUST] — Milestone ownership covers every canonical field and stage
**Given** the canonical 25 fields and 14 sales stages
**When** the ownership documents are parsed
**Then** every field has exactly one owning milestone and every stage maps to a
milestone or is explicitly marked as out of GTM Core's scope.

### O16 [MUST] — The canonical contract leaks no economics
**Given** the committed contract file
**When** it is loaded
**Then** no price, floor, margin, cost, discount or rate value is present, and
the leak guard fails loudly if one is added later. The repository is public.

### O17 [MUST] — Contract and canonical YAML agree on counts and identifiers
**Given** the canonical price book and the committed contract
**When** drift validation runs
**Then** capability ids, evidence signal ids, qualification and classifier
dimension ids, product ids, intervention modes and FDR ids match exactly, and
any mismatch fails rather than warns.

### O18 [MUST] — The drift validator fails when sales motion order changes
**Given** the canonical 14-stage ordered sales motion
**When** two stages are transposed
**Then** validation fails. Stage **order** is semantic — it is what makes
"qualification comes after response" enforceable rather than aspirational.

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

**Scenario count: 135 (all MUST)** — A:14, B:5, C:14, D:6, E:6, F:6, G:15, H:7,
I:3, J:4, L:19, M:16, N:2, O:18.
