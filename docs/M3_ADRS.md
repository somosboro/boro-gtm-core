# M3 — Architecture Decision Records

**Status:** design, revision 5 — schema implemented, services pending.

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

**Status:** accepted, **amended in revision 2** — see the note below. The
decision stands; the table it applies to changed.

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

### Revision 2 amendment

`research_artifact_versions` no longer exists; M3-ADR-016 split it into
`research_artifact_bodies`, `research_artifact_derivations` and
`research_artifacts`.

Option (b) above — a separate bodies table — therefore now exists. It was
**not** adopted for the reason this ADR rejected it: the split happened because
one byte string must be interpretable under several canonicalization contracts,
not to keep the append-only rule pure. The retention decision itself is
unchanged: pruning is a one-way in-place transition, never a DELETE.

It now applies to three columns, each with the same narrow trigger:

| Table | Pruned column |
| --- | --- |
| `research_artifact_bodies` | `raw_body` |
| `research_text_derivations` | `extracted_text` |
| `research_extractions` | `raw_output` |

The consequence listed above is strengthened by the split rather than weakened:
after a body is pruned, its fetch events still carry every retrieval time and
status, and its artifact still carries `source_published_at` — none of which
lived on the body itself.

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


---

## M3-ADR-014 — Retrieval history is an event log, not a property of evidence

**Status:** accepted (revision 2)

### Context

Revision 1 required that re-fetching identical bytes create no new evidence and
record "a sighting". No sighting table existed. The only row that could hold a
retrieval time was `research_artifact_versions`, unique on
`(source_id, raw_body_sha256)` and append-only — so the second retrieval of
identical bytes was literally unrecordable, and the acceptance scenario
demanding it could not have passed.

The general defect: an append-only row cannot carry a value that changes, and
"when did we last see this" changes every time we look.

### Decision

`research_fetch_events` — append-only, unique on nothing, one row per
retrieval attempt. It carries the source, the attempt, `retrieved_at`, the
outcome, HTTP status, final URL, content type, ETag, Last-Modified, and the
body when one was obtained (NULL on failure).

### Consequences

* *"We saw these exact bytes on Sep 1, Sep 8 and Sep 20"* is three rows
  pointing at one body, and no evidence is mutated to record it.
* A failed fetch is a first-class row, so "we tried eleven times and were
  refused" is distinguishable from "we never looked" — which the gap model
  (M3-ADR-018) then derives rather than storing.
* Conditional requests become possible: the last event's ETag is a read away.

---

## M3-ADR-015 — A source is a locator, and carries no run-specific state

**Status:** accepted (revision 2)

### Context

Revision 1's `research_sources` was globally unique by normalized locator and
append-only, yet carried `discovered_by_run_id`, `discovered_from_source_id`
and `resolves_to_source_id`. Every one of those is one-to-many in reality: a
source is discovered by many runs, through many parent pages, by several
methods, for several companies, and acquires redirect and canonical
relationships long after creation.

Three one-to-many facts stored as three single fields on an immutable row. The
first discovery would be recorded and every later one lost.

### Decision

`research_sources` holds the locator, its policy version, host, registrable
domain and `first_seen_at`. Nothing else.

* `research_source_discoveries` — append-only, one row per
  `(source, attempt, method, parent)`.
* `research_source_edges` — append-only, one row per observed relationship
  (`REDIRECTS_TO`, `DECLARES_CANONICAL`, `LANGUAGE_VARIANT_OF`,
  `MIRROR_CANDIDATE`), each carrying the fetch event that observed it.

### Consequences

* A source row is global and reusable across companies and runs with no loss of
  provenance.
* Learning about a redirect later appends an edge and mutates nothing; a
  changed redirect target appends a second edge and both survive.
* A page found by both a sitemap and a search engine yields two discoveries,
  which is a real finding about discoverability.
* Sources are still never merged. Convergence happens at the body.

---

## M3-ADR-016 — Bytes are one layer; semantic interpretation is another

**Status:** accepted (revision 2)

### Context

Revision 1's `research_artifact_versions` was unique on
`(source_id, raw_body_sha256)` and pointed at exactly one artifact. Acceptance
A9 simultaneously required that one byte string canonicalized under `V1` and
under `V2` produce two distinct artifacts. A single FK cannot point at two
rows, so the model could not satisfy its own acceptance criterion.

Keying bytes by source was also wasteful: the same PDF at four URLs meant four
copies.

### Decision

Three tables where there was one:

* `research_artifact_bodies` — `UNIQUE (raw_body_sha256)`, **global**. One row
  per byte string, whatever served it.
* `research_artifact_derivations` — `UNIQUE (body_id, strategy, version)`,
  joining a body to the artifact that contract produces.
* `research_artifacts` — unchanged semantic identity.

### Consequences

* A canonicalization upgrade re-derives from stored bytes: new derivation, new
  artifact, no refetch, old rows untouched.
* Two byte strings canonicalizing to the same content are two derivations
  pointing at one artifact — a cosmetic edit, correctly modelled.
* The same document mirrored at four URLs is one body, four sources, four fetch
  events.
* `source_published_at` lives on the artifact, so every canonicalization
  strategy is now required to preserve declared publication metadata; otherwise
  two documents differing only in date would collapse into one artifact.

---

## M3-ADR-017 — Text extraction is versioned separately, and a claim belongs to a lineage

**Status:** accepted (revision 2)

### Context

Two defects with one root: revision 1 attached derived products to the row that
held the raw bytes.

`extracted_text` and `extraction_policy_version` sat on the append-only
version row, so a parser upgrade over unchanged bytes required an UPDATE.

And the claim model said both "one claim, many independent sources" and, in
acceptance C7, "two independent sources yield two claims". Its rule that a new
extractor version creates new claims meant re-extracting one page three times
presented as threefold corroboration.

### Decision

**Text:** `research_text_derivations`, keyed `(body_id,
text_extraction_policy_version)`. Extractions point at a text derivation, never
at a body. Canonicalization and text extraction are separate because they
answer different questions — *are these the same document?* versus *what can a
reader see?* — and move at different speeds.

**Claims:** a `company_claim` is **one atomic assertion produced from one
evidence lineage**. The dedupe key is an assertion fingerprint over
`(company, attribute, registry version, canonical value, fact type,
granularity, observed_at, lineage_key)`, stored in a nullable column on
`company_claims` with a partial unique index. It **excludes the extractor**.

### Consequences

* A text extractor upgrade produces a new derivation, no new artifact, no
  refetch, and re-extraction from stored bytes.
* A newer extractor agreeing with an older one over the same lineage adds an
  *evidence link* to the existing claim, not a twin — so corroboration cannot
  be inflated by re-reading one page.
* A newer extractor disagreeing produces a second claim, which is a real
  disagreement within one lineage and should be visible.
* Two independent lineages remain two claims, because they may legitimately
  differ in fact type, confidence, trust and date.
* `corroborating_lineage_count` counts distinct lineages, and a lineage has
  exactly one claim per asserted value by construction — double-weighting is
  unrepresentable rather than merely discouraged.

---

## M3-ADR-018 — A gap is an identity; its history is events

**Status:** accepted (revision 2)

### Context

Revision 1 declared `operational_research_gaps` append-only while storing
`attempted_source_count`, `last_attempt_at` and `resolved_by_claim_id` on it.
All three change as research proceeds. Acceptance F3 and F4 both required
changes to those values, so the table's stated mutability and its acceptance
criteria contradicted each other.

### Decision

The gap parent holds the deterministic identity
`(company_id, attribute_key, research_policy_version, gap_kind)` and
`first_raised_at`. Everything else moves to `operational_research_gap_events`
(`RAISED | ATTEMPTED | RESOLVED | ABANDONED`), each carrying the attempt, the
source where applicable, and the resolving claim where one exists.

`attempted_source_count`, `last_attempt_at`, current status and
`resolved_by_claim_id` are derived by view.

### Consequences

* Eleven attempts append eleven rows and leave the parent byte-identical.
* "Unknown because we never looked" versus "unknown after eleven sources"
  becomes evidenced rather than merely counted — the attempts name their
  sources.
* A gap closes by a `RESOLVED` event, never by deletion, so "we once did not
  know this" stays answerable.

---

## M3-ADR-019 — The research question and its execution are different objects

**Status:** accepted (revision 2)

### Context

Revision 1 had one table that was both. It then asserted that `PARTIAL` is
terminal *and* that retrying a `PARTIAL` run advances the same run. A terminal
execution cannot become active again without rewriting history, so one of the
two had to go.

Separately, the design said a different target attribute set creates a
different run while the run's identity contained only company and policy
version — so two different questions collided on one row.

### Decision

* `operational_research_runs` — the **question**:
  `UNIQUE (company_id, research_policy_version, target_set_hash)`, carrying the
  sorted target attribute keys and the seed inputs. No status, no timestamps,
  no error.
* `operational_research_attempts` — one **execution**: status, stage
  timestamps, error, with a trigger enforcing the legal transition graph and
  rejecting any transition out of `COMPLETED`, `PARTIAL` or `FAILED`.

Retry creates attempt *n+1* on the same run. A partial unique index permits at
most one live attempt per run.

### Consequences

* `PARTIAL` is genuinely terminal, and retry is genuinely possible, because
  they now apply to different objects.
* Evidence carries `attempt_id`, so "which execution captured this?" is
  answerable even for an attempt that later failed.
* Changing the policy version or the attribute set produces a different run,
  as the design always claimed and the schema now enforces.


---

## M3-ADR-020 — Provenance is single-valued, and the database proves it

**Status:** accepted (revision 3)

### Context

Revision 2's walk ran `claim → link → extraction → text derivation → body →
fetch event → source`. The last hop is one-to-many: a body is reachable from
every retrieval of those bytes — several URLs, several attempts, several
companies, several dates. So a claim could not answer *which* source supplied
its evidence, which also broke source trust and independence counting, both of
which are per-source.

Revision 2 also defined a lineage as *artifact ids alone*, so source A and
source B serving artifact X collapsed into one lineage and one claim — merging
two observations the design elsewhere insists must stay separate.

### Decision

`claim_evidence_links` names **one extraction and one fetch event**, and
carries `body_id`, `artifact_id` and `source_id`.

Agreement is enforced declaratively. `body_id` appears on the link, on the
extraction and on the fetch event; two composite foreign keys —
`(extraction_id, body_id)` and `(fetch_event_id, body_id)` — force all three to
name the same body. A link citing an extraction over body X and a retrieval of
body Y is unrepresentable. This is the pattern M2 already uses in
`fk_supersedes_same_entity`.

An **evidence origin** becomes `(source_id, artifact_id)`, and a lineage is the
sorted set of distinct origins.

### Consequences

* Every hop of the walk is single-valued.
* Source trust and independence are computable, because both are properties of
  a source and the claim now names one.
* Source A and source B serving the same artifact are two lineages and
  therefore two claims — which does **not** by itself make them corroborating
  (M3-ADR-023).

---

## M3-ADR-021 — An extraction result is separate from the executions that use it

**Status:** accepted (revision 3)

### Context

`research_extractions` was keyed on the derivation and the extractor, correctly
making the result reusable — and simultaneously carried a single `attempt_id`.
When attempt 2 legitimately reused what attempt 1 produced, the row still
pointed at attempt 1 and "which attempts used this?" was unanswerable.

### Decision

Drop `attempt_id` from the result. Add `research_attempt_extractions`
(`attempt_id`, `extraction_id`, `usage_role ∈ {CREATED, REUSED}`) with
`UNIQUE (attempt_id, extraction_id)`.

Every other table was audited for the same shape. Bodies, artifacts, sources,
derivations, text derivations and classifications never carried an attempt and
remain clean; their usage is recoverable through the extractions that read
them, so a second association table would add rows without adding answers.
Fetch events, discoveries and gap events keep their `attempt_id`, because each
**is** an execution event belonging to exactly one attempt.

### Consequences

* One extraction output, many usage records. No duplicated output merely to
  record that a second attempt reused it.
* The general rule is now stated once: **an immutable reusable object does not
  own a single-execution field.**

---

## M3-ADR-022 — Every behaviour-affecting input is inside the contract hash

**Status:** accepted (revision 3)

### Context

`research_extractions` was unique on
`(text_derivation_id, extractor_id, extractor_version,
prompt_template_version)` while separately storing `model_provider`,
`model_name`, `model_version`, `output_schema_version`, `determinism` and
`temperature` — all of which change the output. A model upgrade under one
extractor version therefore **collided** with the existing row and was
representable only by an UPDATE to an append-only table.

`research_text_derivations` had the identical defect: keyed on
`text_extraction_policy_version` while storing `redaction_policy_version`
beside it.

### Decision

Explicit contract hashes, and the hash is the key:

```
extraction_contract_hash      = sha256(extractor kind/id/version,
                                       model provider/name/version,
                                       prompt template version,
                                       output schema version,
                                       determinism, temperature)
text_derivation_contract_hash = sha256(text extraction policy version,
                                       redaction policy version)
```

The alternative — asserting that `extractor_version` transitively covers the
model and schema — was rejected. It is a promise no constraint enforces, and
the fields were stored separately precisely because they vary independently.

### Consequences

* A model upgrade, a schema change or a redaction-policy change each produce a
  new row and leave the old one untouched. No collisions, no UPDATEs.
* The stored fields become the *explanation* of the hash rather than
  decoration.

---

## M3-ADR-023 — Global identity rows hold only global facts; independence is strict

**Status:** accepted (revision 3)

### Context

Two defects with one root — putting context-specific facts on
context-independent rows.

`research_artifact_bodies` is globally unique on the content hash, yet stored
`declared_content_type` and `sniffed_content_type`. The identical byte string
can be served as `text/plain` by one host and `text/html` by another, so the
row had to pick one and discard the other. And a sniffed type is an
*algorithm's answer*, which changes as the algorithm improves.

Separately, independence was undefined, so two URLs serving the same document
could have counted as two corroborating sources.

### Decision

A body holds hash, bytes, byte length and retention state. Nothing contextual.

* **Declared** media type → `research_fetch_events`, one per retrieval.
* **Sniffed** media type → `research_body_classifications`, keyed
  `(body_id, classifier_policy_version)`.

Two lineages corroborate independently only when **both** the publisher and the
document differ. `publisher_key` is derived under a versioned policy — the
registrable domain unless an override maps it, since a job board or registry is
a publisher in its own right.

### Consequences

* A page mirrored at two URLs, and a third-party scrape of the same exact text,
  both fail the document test: identical canonical content is the same document
  copied, not a second witness.
* Two pages on one site fail the publisher test.
* A company site plus a government registry passes both.
* The policy under-counts rather than over-counts. Under-counting corroboration
  is recoverable; over-counting inflates confidence in a claim resting on one
  source, which is the failure this system exists to prevent.

---

## M3-ADR-024 — An attempt event names the retrieval that performed it

**Status:** accepted (revision 3)

### Context

Gap events were unique on `(gap_id, event_kind, attempt_id, source_id)`, which
permitted exactly one `ATTEMPTED` row per source per execution. Fetching a
source three times inside one attempt recorded one event, so `last_attempt_at`
reported the **first** retrieval — while the design claimed eleven attempts
append eleven rows. The constraint and the prose contradicted each other.

### Decision

`ATTEMPTED` means one concrete evidence-acquisition attempt and names its
`fetch_event_id`, which joins the key (`NULLS NOT DISTINCT`). `attempt_count`,
`attempted_source_count` and `last_attempt_at` are then all derived correctly.

Gap events also gain a legal transition graph — `RAISED → ATTEMPTED* →
RESOLVED | ABANDONED`, with the last two terminal.

### Consequences

* Eleven retrievals genuinely append eleven rows.
* A gap that later goes stale is a different `gap_kind` and therefore a
  different gap row, so reopening a terminal gap is never required.

---

## M3-ADR-025 — The research plan hash covers every input that defines the question

**Status:** accepted (revision 3)

### Context

The run was unique on `(company_id, research_policy_version, target_set_hash)`
while the immutable row also carried `vertical_id` and `seed_inputs`. Two runs
differing only in vertical resolved to one already-existing immutable row, and
the second set of values was silently discarded — an immutable column that
could legitimately differ while its natural key stayed identical.

### Decision

`research_plan_hash` covers company, policy version, sorted target attributes,
vertical and canonicalized immutable plan inputs, and **is** the natural key.

Discovered or late-arriving seeds — the company's currently projected identity
domains, a human seed added mid-campaign — are execution inputs and move to the
attempt as `attempt_seed_inputs`.

### Consequences

* Re-running the same question next month with a different projected domain set
  is the same run and a new attempt.
* Changing the vertical, the policy or the attribute set is a different
  question and a different run.
* The invariant is now general: **no immutable column may legitimately differ
  while its natural key stays identical.**

---

## M3-ADR-026 — Discovery context is part of discovery identity

**Status:** accepted (revision 3)

### Context

Discoveries were unique on
`(source_id, attempt_id, discovery_method, discovered_from_source_id)`. One
source found by two different search queries in one attempt collapsed to one
row, discarding the second query — and "which query surfaced this page" is
exactly what makes a discovery method evaluable.

### Decision

Add `discovery_context_hash` to the key, with `NULLS NOT DISTINCT` throughout.

### Consequences

* Two queries finding one page are two discoveries.
* The cost is one hash column and some duplicate-looking rows, which is the
  correct trade for provenance that can answer how a source was reached.

---

## M3-ADR-027 — Edges carry evidence; terminal states are terminal

**Status:** accepted (revision 3)

### Context

Two invariants asserted in prose and unenforced in schema.

Every source edge was said to carry the fetch event that observed it, while
`observed_by_fetch_event_id` was nullable for every relation type. And
`MIRROR_CANDIDATE` is an inference over *two* observations that no single fetch
can witness, so one nullable field could not represent it honestly either.

Identity signal statuses listed four values and no transition rules, so
`ACTIONED → OPEN` and `DISMISSED → ACKNOWLEDGED` were both legal.

### Decision

`edge_origin ∈ {FETCH_OBSERVED, DERIVED, HUMAN_ASSERTED}` with a CHECK:
`FETCH_OBSERVED` requires one fetch event, `DERIVED` requires two (and
`MIRROR_CANDIDATE` must be `DERIVED`), `HUMAN_ASSERTED` requires an actor and a
rationale.

Signal transitions: `OPEN → ACKNOWLEDGED → ACTIONED | DISMISSED`, `OPEN →
DISMISSED`, with `ACTIONED` and `DISMISSED` terminal, enforced by trigger.

### Consequences

* "Every edge has evidence" becomes true in the schema, not only in the prose.
* The two observations behind a mirror are recoverable.
* Revisiting a closed signal means a **new** signal with new evidence — whose
  `evidence_digest` differs, so the identity key admits it — rather than
  resurrecting a closed review and losing the record that it was closed.

---

## M3-ADR-028 — Trust inputs are frozen on the assertion, not looked up later

**Status:** accepted (revision 3)

### Context

`confidence` is computed from evidence type and source trust, and no versioned
trust input was recorded anywhere. Once the trust table changed, a persisted
confidence could not be explained or reproduced.

The tiers being uncalibrated is fine and is listed as an open question. A
persisted number nobody can reconstruct is not.

Separately, identity review signals promised evidence and carried none.

### Decision

Every `claim_evidence_links` row freezes `source_class`,
`trust_policy_version` and the `trust_tier` in force at assertion time. These
are facts about an assertion event, so they belong on the append-only link.

`identity_review_signal_evidence` links a signal to the evidence links that
justify it — a table, not a JSON array on the signal, because a mutable list on
an identity row is revision 1's gap-counter defect wearing a different hat.

### Consequences

* A recalibration ships a new `trust_policy_version`; historical claims keep
  the tier that produced their stored confidence, and re-asserting creates new
  claims. Nothing silently reinterprets a number already written.
* A reviewer receives an assertion **and** the spans supporting it.
* New evidence for an existing signal appends a link and leaves the parent
  untouched.


---

## M3-ADR-029 — A derived value is keyed by the context that determines it

**Status:** accepted (revision 4)

### Context

`operational_research_profiles` had `PK (company_id)` and stored `coverage`,
`confidence` and `contradiction_rate`. Revision 3 had already made multiple
logical research questions per company legal. Coverage's denominator comes from
the target attribute set, from applicability for the vertical and from the
policy's required/optional split — all of which are inside
`research_plan_hash`.

So company X with plan A (HVAC, 18 required, coverage 0.78) and plan B
(another context, 12 required, coverage 0.92) had one row for two answers. The
second rebuild would silently overwrite the first.

The same question applies to gaps: is "`erp` is unknown" a fact about the
company or about a question?

### Decision

Split by context:

* `operational_research_profiles` — **company-global**: projected facts,
  contradictions, contributing claim ids. No coverage.
* `operational_research_plan_profiles` — **plan-specific**, keyed by `run_id`:
  coverage, confidence summary, contradiction rate, attribute counts.

Gaps are **plan-specific**, keyed `(run_id, attribute_key, gap_kind)`. Whether
an attribute is required, applicable, or sufficiently evidenced all come from
the plan. The company-global reading — *is there any ERP evidence at all?* — is
answerable from claims and needs no gap row.

### Consequences

* Two plans coexist without overwriting each other.
* Projected facts still merge across plans, because a fact is a fact whoever
  went looking for it.
* The rule generalises: **no object may be keyed only by company if its value
  changes when the vertical, target set or policy changes.** §5b of the schema
  graph classifies every derived object against it.

---

## M3-ADR-030 — Evidence is first-class and owned by nobody

**Status:** accepted (revision 4)

### Context

Locators, quotes and the provenance chain lived on `claim_evidence_links` —
that is, evidence was a property of a claim. But the M2/M3 firewall requires
that an identity conflict be **raised and stopped on**, not acted upon. A page
stating *"ABC Service is a division of XYZ Holdings"* must produce a signal
before any operational claim exists.

Revision 3 had nowhere to put that observation. Preserving it meant fabricating
a company claim first — creating a canonical assertion in order to file a doubt
about identity. And `identity_review_signal_evidence` pointed at
`claim_evidence_links`, so a reviewer could only be shown evidence some claim
already owned.

### Decision

`research_evidence_items` becomes the unit: *this extraction, over this
retrieval, of this body under this contract, contains this span.* It carries
the locator, quote, hashes and publisher classification.

`claim_evidence_links` becomes thin — claim, evidence item, `support_kind`, and
the trust metadata frozen for *that* assertion.
`identity_review_signal_evidence` points at evidence items directly.

### Consequences

* An observation can support a claim, a signal, both, or nothing yet.
* Locator and quote structures exist once, not in two unrelated tables.
* Retention never prunes evidence items, so the supporting span survives body
  pruning — which is what made the retention story coherent in the first place.

---

## M3-ADR-031 — Provenance names the exact derivation; artifacts hold no observation

**Status:** accepted (revision 4)

### Context

Two defects with one root: confusing *the content* with *an observation of the
content*.

Evidence carried `body_id` and `artifact_id`. One body has many derivations
under different canonicalization contracts, so `body_id` never determined
`artifact_id`, and a trigger was keeping them consistent by hand.

Worse, `research_artifacts` — globally deduplicated semantic content — stored
`source_published_at`, `language` and `title`. To make those fit, revision 3
required every canonicalization strategy to fold declared publication metadata
into the canonical form. That "fix" would have broken independence detection:
an article mirrored on two sites, one stamped "Published March 2026" and one
undated, would canonicalize to **two artifacts**, and the independence rule
rests on *same artifact ⇒ same document*. Two mirrors would have counted as two
independent witnesses — precisely the failure the rule exists to prevent.

### Decision

Evidence items name `artifact_derivation_id` and reach the artifact through it.
Three composite foreign keys — to `research_extractions (id, body_id)`,
`research_fetch_events (id, body_id)` and
`research_artifact_derivations (id, body_id)` — force every path to name the
same body, declaratively. The trigger is gone.

`source_published_at`, `source_published_granularity`, `language` and `title`
move to the **derivation**. Publication metadata is **not** in the canonical
hash.

### Consequences

* Two bodies canonicalizing to one artifact may carry different publication
  observations, and both survive.
* The artifact stays one document, so independence detection keeps working.
* A claim reaches publication metadata through the exact derivation its
  evidence names — the honest route, since that is where it was observed.

---

## M3-ADR-032 — Assertion identity includes the policy that produced it

**Status:** accepted (revision 4)

### Context

Revision 3 stated that a trust recalibration re-asserts under v2, creating a
new claim while the v1 claim keeps its confidence. The schema made that
impossible: `assertion_fingerprint` covered company, attribute, registry
version, value, fact type, granularity, `observed_at` and lineage — and **no
policy version**. The same value over the same lineage under trust v2 produced
an identical fingerprint, and the partial unique index rejected the new claim.
A documented behaviour was unreachable.

The fingerprint also omitted `unit`.

### Decision

```
assertion_contract_hash = sha256(assertion_policy_version, trust_policy_version,
                                 confidence_formula_version,
                                 fact_type_mapping_version,
                                 publisher_policy_version, inference_rule_version)
```

Both `assertion_contract_hash` and `unit` join the fingerprint.

### Consequences

* Re-assertion under a new policy is representable, which is what makes
  M3-ADR-028's promise real rather than aspirational.
* `40 PEOPLE` and `40 FTE` no longer collide.
* The fingerprint still excludes the extractor, so re-reading one page with a
  better model appends evidence rather than inflating corroboration.

---

## M3-ADR-033 — A projected number names the policy that produced it

**Status:** accepted (revision 4)

### Context

`publisher_key` was derived under `publisher_policy_version`, and that version
was persisted nowhere. A later mapping change — deciding a directory is its own
publisher, say — would silently turn one corroborating publisher into two for
output already written, with no way to tell which answer you were reading.

### Decision

`publisher_key` and `publisher_policy_version` are frozen on the evidence item
when the observation is recorded. Both projections record the
`publisher_policy_version` and `assertion_policy_version` they were rebuilt
under.

### Consequences

* Historical corroboration stays explainable.
* A rebuild under a new policy is a different projection contract, visible in
  the row, rather than a silent re-scoring.

---

## M3-ADR-034 — Signal identity is the concern; review is an occurrence

**Status:** accepted (revision 4)

### Context

`identity_review_signals` used `evidence_digest` as part of its identity key
while the design also said new evidence appends to an open signal. A hash over
a growing set cannot be an immutable key. The docs simultaneously said a closed
signal rediscovered later becomes a *new* signal "whose evidence_digest
differs" — which only worked because the key was already unstable.

### Decision

Identity is the **semantic concern**: company, signal kind, related company,
normalized concern, policy version. Review episodes are
`identity_review_signal_occurrences`, at most one open per signal, each with
its own status chain and an optional link to its predecessor.

### Consequences

* Appending evidence leaves both the signal and the occurrence untouched.
* A dismissed episode stays dismissed; the same concern resurfacing opens
  occurrence *n+1* with its history intact.
* Terminal states stay terminal without needing a mutable key to express it.

---

## M3-ADR-035 — Claim confidence is a versioned formula, not prose

**Status:** accepted (revision 4)

### Context

A claim has one persisted `confidence` and may have many evidence items.
"Computed from evidence type and source trust" does not say what happens with
four links in one lineage, or the same publisher repeated, or mixed trust
tiers.

### Decision

A declared formula — `base(fact_type) × trust_factor × corroboration_factor ×
inference_penalty` — with every input naming its version, all of which sit
inside `assertion_contract_hash`. Weights are fixture configuration and are
**not** calibrated.

`trust_factor` uses the **maximum** tier across the claim's links, not the
mean: adding a weak corroborating source must never lower confidence.
`corroboration_factor` is monotone non-decreasing in the count of *independent
publishers* and saturates.

### Consequences

* A stored confidence is reproducible from its own claim: read the links, read
  the versions, recompute.
* The numbers may be wrong until calibrated; they can never be unexplainable.

---

## M3-ADR-036 — Execution inputs are declared, frozen, and API-visible

**Status:** accepted (revision 4)

### Context

Revision 3 moved late-arriving seeds to `attempt_seed_inputs` in prose, and
acceptance L11 required it — but the attempts table never declared the column.

Separately, the API surface still filtered logical runs by status and offered
per-stage timestamps on a run, both of which became false when runs and
attempts split, plus a route to artifact *versions*, which no longer exist.

### Decision

`attempt_seed_inputs` (canonicalized JSONB) and `attempt_seed_inputs_hash` are
declared on the attempt, written once at `PENDING → DISCOVERING` and frozen by
the transition trigger.

The API is reorganised around the split: runs are filtered by company, policy
and vertical; attempts are filtered by status and carry the stages; coverage
and gaps hang off the run; evidence, sources, derivations and evidence items
are addressable in their own right.

### Consequences

* An attempt is reproducible, which is the only reason to record its seeds.
* No execution property is exposed on a logical run.
* A company-scoped coverage endpoint is deliberately absent: it would have to
  pick one plan arbitrarily.

---

## M3-ADR-037 — A 304 validates a known body and says which validator proved it

**Status:** accepted (revision 4)

### Context

`fetch_outcome = NOT_MODIFIED` existed with no defined `body_id` semantics. A
304 returns no bytes, so the options were to fabricate a body reference, or to
leave it NULL and drop the event out of provenance. The second is worse: a page
confirmed unchanged is exactly the evidence that a claim is still fresh.

### Decision

Per-outcome CHECK. `OK` requires a body. `NOT_MODIFIED` requires
`http_status = 304`, the **previously known body it validated**, and the
validator that was sent (`ETAG` or `LAST_MODIFIED`) with its value. Genuine
failures require `body_id IS NULL`.

### Consequences

* The event answers "at time T the server confirmed body B was still current",
  and staleness reads it.
* No bytes are invented, and the assertion is checkable rather than assumed.

---

## M3-ADR-038 — Deterministic contracts assert; sampled ones are confirmed first

**Status:** accepted (revision 4)

### Context

`extraction_contract_hash` included `determinism` and `temperature`, and
uniqueness was `(text_derivation_id, extraction_contract_hash)`. If
`determinism = SAMPLED`, two executions may legitimately differ — so the unique
key kept whichever sample landed first. That is not idempotency; it is one
arbitrary draw frozen by a race, presented as a reproducible result.

### Decision

Only a `DETERMINISTIC` extraction may assert a `company_claim` directly. A
`SAMPLED` extraction must carry a `sample_execution_id`, which joins its
uniqueness so repeated sampling appends rather than colliding, and it reaches a
claim only through a confirming `HUMAN` extraction.

### Consequences

* Sampled extraction stays available for experimentation and review without
  contaminating the canonical ledger.
* This is M3-ADR-004 from the other side: there, a model's *confidence* could
  not raise evidential strength; here, its *variability* cannot be laundered
  into reproducibility by a constraint.


---

## M3-ADR-039 — Counts stated in prose must be mechanically derived

**Status:** accepted (revision 4.1, found during implementation)

### Context

Implementing the attribute registry produced thirty-one attributes. Revision
4's §7 prose said "twenty-four"; its own tables listed thirty-one. The tables
were right — every attribute in them is referenced elsewhere in the design and
in the HVAC pressure test — so the prose was simply a hand-written count that
was never checked.

This is the same class of error revision 4 already fixed twice for table and
scenario counts, where the fix was to derive the number from the document
rather than write it. The attribute count was missed because it lives in prose
rather than in a summary table.

### Decision

The prose count is corrected to thirty-one, and acceptance N1 asserts that the
**seeded registry equals the design table**, key for key. The check is
mechanical, so the two cannot drift again.

### Consequences

* The registry has one source of truth — §7's tables — and a test enforcing it.
* No attribute was added or removed: the implementation always followed the
  tables. Only the prose was wrong.
* Generalisable rule: any number a document states about its own contents
  should be derived, and where the contents are a contract, asserted by a test.

---

## M3-ADR-040 — The canonical Price Book outranks GTM Core

**Status:** accepted (revision 5)

### Context

GTM Core was designed before the Operations OS Price Book v2.0 became the
canonical commercial ontology. Where the two disagree — on what qualification
means, on when a commercial level may exist, on what a capability is — there
must be a single answer, or each will quietly teach the other its mistakes.

### Decision

The canonical commercial ontology is authoritative. When GTM Core conflicts
with it, **GTM Core changes**. Commercial policy is never altered to preserve
an implementation.

### Consequences

* One conflict was found and resolved in this direction: a test asserted the
  classifier anti-rule mentioned "price" and "budget"; the canonical wording is
  narrower. The test changed, not the spec.
* The reverse move — editing the canonical YAML to match a passing test — is
  prohibited, and the drift validator makes it visible if attempted.

---

## M3-ADR-041 — The canonical files stay out of this public repository

**Status:** accepted (revision 5)

### Context

The canonical price book carries internal economics: delivery costs, margin
targets, floors and discount policy. This repository is public. The price book
itself states that its margin target is an internal management target and not a
customer-facing claim, which settles the question of whether it was meant to be
published.

### Decision

Neither canonical file is committed. A **redacted contract**
(`commercial/CANONICAL_CONTRACT.json`) is committed instead: identifiers,
counts, ordered stages, rubric dimensions, field names, and a SHA-256 of each
canonical file. No value that prices anything.

### Consequences

* GTM Core can be validated against the ontology in CI without publishing it.
* A leak guard rejects any key resembling price, cost, margin, floor, discount
  or rate, with a reviewed allowlist of two exceptions. It fired on its own
  contract during development — on `price_book_sha256`, a file hash — which is
  the desired sensitivity.
* Anyone holding the canonical files can verify the contract describes them.

---

## M3-ADR-042 — The pre-outreach layer is Interpretation, not Qualification

**Status:** accepted (revision 5); supersedes the informal M4 naming

### Context

The old roadmap named the milestone after M3 "M4 Qualification". Canonical
qualification is scored **after a diagnostic call** and needs complexity,
impact, sponsor, trigger and budget. None is publicly observable.

A name is not cosmetic here. Had the layer kept the name, it would eventually
have acquired a score, and a score computed before any contact with the
prospect is indistinguishable downstream from one earned in a conversation.

### Decision

M4 becomes **Account Evidence Interpretation**: it derives canonical `EV-*`
signals and bounded hypotheses from M3 evidence. Qualification moves to **M7**,
after response. M3 is renamed from "Operational Research" to "Operational
Evidence" for the same reason.

### Consequences

* `qualification_score` and `qualification_route` have exactly one writer, M7.
* The rename is documentation only; no table is renamed and no migration is
  created for a noun.

---

## M3-ADR-043 — Eleven process-observation primitives, no judgement columns

**Status:** accepted (revision 5)

### Context

Nine of the eighteen canonical `EV-*` signals had no supporting primitive in
the M3 registry. The tempting fix is a column per signal — `has_manual_process`,
`coordination_maturity` — which is judgement wearing an evidence costume.

### Decision

Add eleven attributes in a new `PROCESS_OBSERVATION` group, each recording
something a person could point at in a source document: a named responsibility,
an observed handoff, an approval step, a system touchpoint, re-entry of the
same data, a source-of-truth statement. None may be asserted as `FACT` where
the underlying evidence class cannot support one. Signals are **derived** in
M4 from these observations; they are not stored in M3.

### Consequences

* Coverage of the 18 canonical signals goes from 4 DIRECT / 5 PARTIAL /
  9 NONE to 7 DIRECT / 11 PARTIAL / **0 NONE**.
* Every process observation is **evidence-class capped**: these are the
  attributes most often read out of job ads and page fingerprints, so only an
  explicit company statement can carry one to `FACT`.
* The registry grows from 31 to 42 attributes. All eleven are optional, so the
  required set stays at 16 and coverage denominators are unchanged.
* No migration: attributes are rows, not columns. This is the payoff of the
  registry design and the reason ontology growth is cheap.

---

## M3-ADR-044 — Canonical Q1 fields are a projection, not a table

**Status:** accepted (revision 5)

### Context

The canonical account model lists 25 minimum fields spanning discovery through
go-live. Materialising them as one account row invites defaults, and a default
in `qualification_score` is indistinguishable from a real score once written.

### Decision

Each field is owned by exactly one milestone
(`GTM_ACCOUNT_FIELD_OWNERSHIP.md`) and the canonical view is assembled as a
projection over owners. A field nobody has legitimately written has nothing to
read, rather than a zero a later reader mistakes for a judgement.

### Consequences

* M3 writes exactly two of the 25: `evidence[]` and `evidence_confidence`.
* Ten fields are explicitly forbidden to M3, each a plausible-looking mistake.
* This is the same rule as "absence is `NOT_AVAILABLE`, never false", applied
  one layer up.

---

## M3-ADR-045 — Q2 evidence projection uses observation dates

**Status:** accepted (revision 5)

### Context

The canonical Q2 evidence record has seven fields including a date. M3 has two
candidate dates per item: when the source described something, and when we
fetched it. Substituting the fetch date is the easy implementation and is
silently wrong — it makes a decade-old page look like today's news.

### Decision

`date_observed` carries the source's own observation date. `retrieved_at` is
**never** substituted for it. Where the source states no date, the field is
unavailable rather than backfilled.

### Consequences

* Some evidence will project with no date. That is the honest outcome; a
  reader can tell "undated" from "recent", which a fetch date destroys.

---

## M3-ADR-046 — Ontology conformance is a CI gate, not a review habit

**Status:** accepted (revision 5)

### Context

The contract can drift from the canonical files it describes, in either
direction, without anything failing — which is how every count in this design
drifted before being made mechanical.

### Decision

`validate_yaml_against_contract()` compares capability ids, signal ids, rubric
dimensions, product ids, modes, FDRs **and the order of the 14 sales stages**
against the canonical files when they are present, and fails rather than warns.
Stage order is semantic: it is what makes "qualification comes after response"
enforceable.

### Consequences

* The gate is skipped, loudly, where the canonical files are absent — they are
  not in this repository — so it protects the machines that hold them.
* Consistent with the rule established for counts: any number or ordering a
  document asserts about a contract should be checked by a test.
