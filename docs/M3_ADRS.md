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

**Status:** accepted; the **three-object split below was superseded in revision
2** by M3-ADR-016, which replaced `research_artifact_versions` with
`research_artifact_bodies`, `research_artifact_derivations` and
`research_text_derivations`. The separation of *where we looked* from *what we
got* — the actual decision — stands.

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

**Status:** accepted originally; **amended in revision 4** — the M2 integration
it assumed was verified not to exist. The core decision (M3 raises, M3 never
acts) stands; the consumer named below does not. See the amendment at the end.

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

### Amendment (revision 4) — the M2 consumer does not exist

The decision above was written from the *intent* of M2's review flow rather
than from its signature. Checked against live code, the integration it names is
not reachable:

```python
# packages/boro_gtm/discovery/services/resolution.py
def append_human_decision(session, entity: ProviderEntity, *, decision, ...)

# packages/boro_gtm/discovery/api/schemas.py
class HumanReviewRequest(BaseModel):
    provider_entity_id: uuid.UUID      # required, not optional
```

M2's review is **provider-entity based**. An M3 signal such as
`POSSIBLE_CEASED_TRADING` is raised against a *company* and has no provider
entity behind it, so there is nothing to pass. The only way to route it through
M2 would be to **fabricate a `ProviderEntity` row** — an identity write, by the
component this very ADR forbids from writing identity.

What changes:

* `identity_review_signals` is **M3-owned end to end**: M3 raises the concern,
  records the evidence, and a human works the queue in M3.
* A future integration may consume `ACTIONED` outcomes, but it must be designed
  explicitly. **None exists today**, and no M3 code may assume one does.
* **M3 must never create a `ProviderEntity` to make a signal routable.** That
  is the specific failure this amendment exists to name.

What does not change: M3 never creates, merges or splits a company, never
alters a resolution decision and never changes a domain role. The M2 row-count
fingerprint test still proves it, and now proves it across the whole pipeline.

Current behaviour is owned by **M3-ADR-034** (durable concern, review episodes
as occurrences) and described in
[M3_SCHEMA_GRAPH.md](M3_SCHEMA_GRAPH.md) §5a and
[M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md) §20.1.

This is recorded as an amendment rather than an edit because the original
reasoning is the useful part: it shows how a plausible integration was assumed
into existence from a description, and only fell over when someone read the
function signature.

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

**Status:** accepted; two details below were **superseded later**. A lineage is
the sorted set of `(source_id, artifact_id)` **evidence origins**, not artifact
ids (schema graph §4.2, M3-ADR-049), and `corroborating_lineage_count` was
replaced by `corroborating_publisher_count`, which applies an independence test
the original count lacked (schema graph §4.2a). The decision that a claim
belongs to a lineage, and that text versioning is separate from semantics,
stands. (revision 2)

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

**Status:** accepted (revision 2); the run key below was **superseded in
revision 3** by M3-ADR-025, which replaced
`UNIQUE (company_id, research_policy_version, target_set_hash)` with
`research_plan_hash` over every input that defines the question. Splitting the
question from its executions — the decision itself — stands.

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

---

## M3-ADR-047 — Confidence is computed once, because the row is append-only

**Status:** accepted (found during phase-2 implementation)

### Context

`company_claims` is append-only, enforced by a trigger. Confidence is a
function of a claim's evidence links, and links can be appended after the claim
exists — that is the whole point of a fingerprint that excludes the extractor.

The first implementation recomputed confidence after linking and wrote it with
an `UPDATE`. The trigger rejected it, correctly: design rule 1 says an
append-only row may not contain a value that changes, and this was exactly
that.

### Decision

Confidence is computed **before** the insert, from the evidence the assertion
already has, and never rewritten. When evidence is appended to an existing
claim, the derived value is recomputed and **compared**; a difference raises
`ConfidenceDivergenceError` rather than being silently kept or silently
written.

### Consequences

* The stored number is reproducible from the row's own links and policy
  versions, which is what the design promised it would be.
* The guard encodes a real claim about the fingerprint: a link may only join a
  claim whose lineage it already shares, and same lineage plus same publisher
  set means same confidence. If it ever fires, **the lineage key is too coarse**
  — the fix belongs to the fingerprint, not to the append-only rule.
* This is the third time a derived value stored on an immutable row has caused
  a defect in this project. The rule generalises: derive at read time, or
  freeze the inputs at write time — never both halfway.

---

## M3-ADR-048 — Independence is connected components, not distinct pairs

**Status:** accepted (found during phase-2 implementation)

### Context

Corroboration multiplies confidence, so what counts as a second voice is
load-bearing. The rule is that two evidence items are independent only when
they come from a **different publisher** *and* a **different semantic
document**.

The first implementation counted distinct `(publisher, document)` pairs. That
reads the rule as an *or*, and it is wrong in both directions:

* one publisher saying something on two of its own pages counted as two voices,
  so a company could corroborate itself by adding a page;
* two publishers serving one mirrored document would have counted as two, which
  is the inflation the rule exists to prevent.

The fixture corpus hit the first case immediately: "24/7 emergency service"
appears on both the services page and the emergency page, and the claim's
confidence came out at 0.9405 instead of 0.855.

### Decision

Two items are the same voice if they share a publisher **or** a document, so
the number of independent voices is the count of connected components over the
publisher/document graph.

### Consequences

* Repetition by one publisher, across any number of its own pages, contributes
  nothing. Adding pages does not raise confidence.
* A mirror still collapses, now for the structural reason rather than because
  the fixture maps both domains to one publisher key.
* The rule reads as written: *different publisher **and** different document*.

---

## M3-ADR-049 — The lineage key is evidence origins, not artifact ids

**Status:** accepted (regression found by adversarial review of phase 2)

### Context

The frozen schema graph (§4.2) defines an **evidence origin** as
`(source_id, artifact_id)` and a lineage as the sorted set of distinct origins
behind one assertion. It says explicitly why artifact ids alone are
insufficient: source A and source B both serving artifact X are two
observations that may carry different trust, publication context and dates.

The phase-2 implementation shipped `lineage_artifact_ids()` — sorted distinct
artifact ids — and fed it to the assertion fingerprint. That is revision 2's
model, which revision 3 replaced and the schema graph records as superseded.
`M3_OPERATIONAL_RESEARCH_DESIGN.md` §5.1.1 still carried the old line, which is
how the regression looked correct while being written.

### Decision

`lineage_evidence_origins()` returns sorted `(source_id, artifact_id)` pairs
and the fingerprint uses them. The stale design line is corrected and labelled.

### Consequences

* Two publishers serving the same document now produce two claims, each with
  its own trust tier and observation date, instead of one merged assertion.
* Grouping had to change with it (M3-ADR-051): keying only on the value
  merged independent origins *before* the fingerprint ever saw them, so the
  fingerprint fix alone would not have been enough.
* Where two documents state the same thing, there are now more claims and each
  is narrower. Corroboration is counted across them, by grouping, which is what
  the design said all along.
* **Lesson:** two documents stated the same invariant and one was stale. The
  fix is not "read more carefully" — it is that a contract stated twice needs a
  test that reads both.

---

## M3-ADR-050 — G8 is withdrawn; the invariant is one live attempt per run

**Status:** accepted (revision 5.1)

### Context

Acceptance G8 read *"two concurrent runs on one company are prevented"*. Under
the frozen run/attempt model a **run is a logical question**, not an execution,
and one company legitimately has many: G13 requires a different target set to
be a different run, and G4 requires a different policy version to be one.

G8 therefore forbids what two other MUST scenarios mandate. It is a survivor of
the pre-revision-3 model in which a run *was* an execution.

### Decision

G8 is **withdrawn**, visibly, with its replacement named: G15, *only one
attempt may be live per run*, enforced by the partial unique index
`uq_attempt_live`.

### Consequences

* The contract is 134 MUST scenarios plus one withdrawn, and the withdrawal is
  legible rather than a silent renumber.
* `start_attempt()` now raises `AttemptAlreadyLiveError` instead of letting an
  `IntegrityError` escape, which is what G15 actually asks for and what the
  phase-2 test was not checking.

---

## M3-ADR-051 — Grouping is by lineage, not by value

**Status:** accepted (found by adversarial review of phase 2)

### Context

`group_observations()` collapsed observations on
`(attribute, value, unit, fact_type)` before lineage was considered. Two
independent sources stating the same thing therefore became **one**
`PendingAssertion` holding both evidence sets — one claim, two lineages.

That is the merge the design forbids in as many words, and it made
"two independent witnesses" indistinguishable from "one witness, twice". The
fingerprint fix (M3-ADR-049) would not have caught it: by the time the
fingerprint was computed, the merge had already happened.

### Decision

The grouping key includes the lineage. A direct observation's lineage is its
own evidence origin, so independent sources stay separate. Several spans from
one origin still form one assertion.

An inference genuinely drawn across several origins keeps that ability through
an explicit `lineage_tag`: observations sharing a tag form one lineage wherever
they came from. That is the three-job-postings-and-a-services-page case, and it
is now deliberate rather than a side effect of grouping by value.

### Consequences

* Corroboration is counted **across** claims by grouping, never inside one.
  `corroborating_publisher_count()` does this and is what the C7 test asserts.
* The fixture corpus now demonstrates all three cases at once for
  `emergency_service = true`: the contractor states it on two of its own pages,
  a trade publication states it independently, and a directory republishes the
  contractor's page byte for byte. Four claims, **two** witnesses.

---

## M3-ADR-052 — Model raw output needs a retention class the schema forbade

**Status:** accepted (phase-3 implementation defect; **new migration**
`0005_m3_retention`)

### Context

Design §26 assigns model `raw_output` a retention class: *"Default 90 days; hash
permanent."* Implementing it showed the schema cannot represent that.
`0004_m3` gave `research_extractions` a blanket `gtm_reject_update()` trigger
and no retention columns, so the prune the design mandates is refused outright:

```
RestrictViolation: relation research_extractions is append-only:
UPDATE is not permitted
```

This is a **design/schema contradiction**, not an implementation bug: no
service-layer change can produce the behaviour, because the database forbids it.
Bodies and text derivations already have exactly the right mechanism — the
parameterised `gtm_m3_one_way_prune` — and extractions were simply not given it.

### Decision

A **new migration**, `0005_m3_retention`, adds `raw_output_retention` and
`raw_output_pruned_at`, two CHECK constraints keeping the pair consistent, and
swaps the blanket rejection for
`gtm_m3_one_way_prune('raw_output', 'raw_output_retention')`.

`0004_m3` is left **byte-identical**. It is published in this branch and
protected by the migration manifest; editing it to hide the defect would make
the manifest a formality and would silently diverge from every database already
migrated past it.

### Consequences

* Exactly one mutation is now permitted on an extraction, and it is the same
  one-way prune the other two payload tables use. Every other UPDATE is still
  refused by the database, not by convention.
* `raw_output_sha256` is unaffected, so a pruned extraction remains auditable:
  the audit trail survives the bulk.
* The retention service raises `PrunedPayloadError` rather than deriving from a
  missing payload. "Silently produced an empty extraction" and "the bytes are
  gone, refetch or record a gap" must not look the same.
* **Generalisable:** `gtm_reject_update()` is the right default and the wrong
  choice wherever a retention class exists. A table with a payload and a
  documented retention horizon needs the parameterised trigger from the start.

---

## M3-ADR-053 — Event status ties break on lifecycle position, not row id

**Status:** accepted (phase-3 implementation defect)

### Context

Gap status and identity-review status are both derived from an append-only
event log: the latest event wins. Both read `ORDER BY occurred_at DESC, id
DESC`, and the row id is a random UUID.

Two events on one entity at the same `occurred_at` are not hypothetical — the
pipeline raises a signal and records `OPEN` in the same call, and a gap can be
raised and resolved inside one transaction. When the timestamps tie, a random
UUID decides, so the same data reported either state run to run. It surfaced as
a test that passed and failed alternately, which is the worst way to find it.

### Decision

Both lifecycle graphs are DAGs — `RAISED → ATTEMPTED → RESOLVED | ABANDONED`
and `OPEN → ACKNOWLEDGED → ACTIONED | DISMISSED` — so "furthest along" is a
total order. Ties on `occurred_at` break on lifecycle position.

### Consequences

* "Current status" is now a function of the data, not of UUID luck.
* The `current_operational_research_gaps` view in `0004_m3` still carries the
  old `id DESC` tiebreak. It is not on any code path — the services do their own
  read — and the migration is protected, so it is left as-is and recorded here
  rather than silently patched. A reader using that view directly for ad-hoc SQL
  should know the tiebreak is arbitrary.
* **Generalisable:** a derived "latest" over an append-only log needs a
  deterministic tiebreak, and a surrogate key is not one. Where a lifecycle
  exists, the lifecycle is the tiebreak.

---

## M3-ADR-054 — A stale-read decision is refused, and told so

**Status:** accepted (phase-3 implementation defect)

### Context

`append_signal_status` checks the transition graph against the status it reads,
then inserts. Two reviewers can both read `OPEN`, both decide `ACKNOWLEDGED` is
legal, and the second insert then collides with `uq_signal_event` — surfacing a
raw `IntegrityError`, which the API contract forbids.

An ORM `add` inside a `try` does not help: the flush happens outside the
savepoint, so the session lands in `PendingRollbackError` and the error cannot
be translated where it was raised.

### Decision

The insert is a Core `ON CONFLICT DO NOTHING ... RETURNING`, the pattern M2's
resolution service already uses. No returned row means another reviewer got
there first, and the caller gets `IllegalSignalTransitionError` naming the
decision — not an index name.

### Consequences

* The service check is the fast path and the unique key is the real guarantee,
  which is the right division: a check over a value another transaction can
  change is advisory by nature.
* The caller is told to re-read rather than silently losing its decision.

---

## M3-ADR-055 — One invariant, one implementation, even across the boundary

**Status:** accepted (phase-3 implementation defect; **new migration**
`0006_m3_lifecycle_tiebreak`)

### Context

M3-ADR-053 fixed the nondeterministic "latest event" tiebreak in the gap and
review services. It did not fix the **triggers**, which resolve the same
question in SQL with the same `ORDER BY occurred_at DESC, id DESC`.

The result was worse than the original bug. The service read `ACKNOWLEDGED`,
allowed the transition, and the trigger — reading `OPEN` from the same two rows
because a different random UUID sorted higher — rejected it. Two statements of
one invariant, disagreeing, with the database winning and the caller getting an
exception for a move the service had just approved.

This is the third time in this project that one rule written twice has drifted:
the lineage key in two documents (M3-ADR-049), the coverage summary against its
own table, and now a lifecycle rule in Python and in PL/pgSQL.

### Decision

`0006_m3_lifecycle_tiebreak` gives both trigger functions and both `current_*`
views the same lifecycle-position tiebreak the services use. `0004_m3` is left
byte-identical.

### Consequences

* The service check and the database guard now agree by construction, not by
  coincidence.
* `current_operational_research_gaps` and `current_identity_review_occurrences`
  are recreated from `0004`'s definitions with only the `ORDER BY` changed, so
  nothing else about them can drift in the rewrite.
* **Generalisable, and the rule this project keeps relearning:** when an
  invariant is expressed in two places, fixing one is not fixing it. Either
  both change together or the pair needs a test that reads both.

> The migration manifest earned its keep here. Editing `0006` after recording
> its digest was refused — *"content changed after it was recorded, which is the
> condition the manifest exists to detect"* — which is exactly right. The entry
> had been recorded prematurely during this same session and never committed, so
> it was removed and re-recorded rather than forced.

---

## M3-ADR-056 — Review is keyed on evidence, and both decisions persist

**Status:** accepted (audit finding; **new migration** `0007_m3_review`)

### Context

`POST /research-claims/{claim_id}/review` could not express the workflow it
existed for. The frozen rule is that a `SAMPLED` reading does **not** assert a
claim and reaches assertion only after human confirmation — so at the moment it
becomes reviewable there is no claim, and a route keyed by claim cannot address
it.

Worse, `review_claim(decision="REJECT")` persisted nothing. It built a
`ReviewOutcome` and returned it. After the response there was no record that a
human had reviewed anything: not the actor, not the time, not the rationale,
not what was rejected. In an evidence system.

Acceptance D6 compounded it, reading *"a model claim a reviewer **rejects** …
a `HUMAN` extraction and a **new claim** are appended"* — two contradictions in
one sentence, since a rejection must not append a claim.

### Decision

`research_evidence_reviews` is append-only and keyed on something that exists
from the moment a sampled reading is recorded — exactly when it becomes
reviewable. Both decisions write a row carrying actor, time, decision and
rationale. A CHECK forbids a rejection from naming an extraction or a claim.

> *Amended in revision 5.3 (M3-ADR-064).* This ADR chose the **evidence item** as
> that key, and the routes below followed from it. An item is keyed on its
> locator, so two readings of one span share one; the key is now the **review
> candidate**, and the routes are
> `GET /research-review-candidates`,
> `POST /research-review-candidates/{id}/decision` and
> `GET /research-review-candidates/{id}/reviews`. The reasoning in this ADR —
> that the resource must predate the claim — is unchanged and is what M3-ADR-064
> builds on.

The API becomes `GET /research-reviews/pending`,
`POST /research-evidence-items/{id}/review` and
`GET /research-evidence-items/{id}/reviews` (superseded above). The claim-keyed
route is **superseded and removed** rather than kept as a misleading alias;
nothing is released, so there is no compatibility to preserve.

D6 is split into D6 (confirmation) and D7 (durable rejection), because they are
genuinely different behaviours and one scenario could not state both.

### Consequences

* A reviewer's queue is addressable before any claim exists, which is the state
  the milestone spent three phases insisting is real.
* A rejection is now auditable. It still asserts nothing: disbelief is not
  evidence of the opposite.
* **Generalisable:** if a workflow has a state, the API must have a resource
  for that state. A route keyed on an object that does not exist yet is a
  design error wearing a URL.

---

## M3-ADR-057 — Terminal siblings need uniqueness, not ordering

**Status:** accepted (audit finding; `0007_m3_review`)

### Context

M3-ADR-053 broke status ties by lifecycle position. `ACTIONED`/`DISMISSED` and
`RESOLVED`/`ABANDONED` share a rank, because they are **alternatives, not a
progression** — there is no "further along" between them.

So ordering cannot separate them, and two transactions reading the same
pre-terminal state could each insert a different terminal child. Reproduced on
this branch: a gap ended holding both `RESOLVED` and `ABANDONED`.

The signal side happened not to reproduce, because the transition trigger also
`UPDATE`s `is_open` on the occurrence and that row lock serialises the writers.
That is an accident of an unrelated statement, not an invariant.

### Decision

A partial unique index per lifecycle — one terminal event per gap, one per
occurrence. The second writer gets a unique violation, which the services
translate into a domain conflict.

### Consequences

* The guarantee no longer depends on a lock taken for another reason.
* Assigning the siblings different arbitrary ranks was the tempting fix and
  would have been wrong: it would have declared one of two alternatives
  "later", which is a claim about the domain that is not true.

---

## M3-ADR-058 — One job type, because one is what runs

**Status:** accepted (audit finding); **supersedes the four-job split in
M3-ADR-013 for the fixture-only milestone**

### Context

The design declared four job types on the stated grounds that each has a
different failure and retry profile: *"a robots denial must not be retried like
a timeout."* The reasoning is sound and it is about a **production** provider.

M3 ships fixture adapters only. Every outcome is deterministic and reproducible
from local files, so there is no transient failure for a per-stage retry to
retry. The implementation reflected that honestly and badly: `DISCOVER_SOURCES`
ran the entire pipeline and the other three returned `{"skipped": …}` and
marked themselves DONE. Three declared job types were no-ops.

### Decision

One job type, `M3_EXECUTE_RESEARCH`, which is what actually runs. The four-way
split returns with the provider whose failure modes justify it, and the ADR
that will reintroduce it should cite this one.

### Consequences

* The docs and the code describe the same system.
* Nothing is lost: the stages still exist as durable state on the attempt —
  `discovery_completed_at`, `fetch_completed_at`, `extraction_completed_at` —
  which is where stage progress belongs. What is gone is a queue topology with
  no failures to route.
* **Generalisable:** four names where three mean "skip and succeed" is not an
  implementation of a design. It is a way of appearing to have one.

---

## M3-ADR-059 — Corroboration is a maximum matching, not a component count

**Status:** accepted (audit finding)

### Context

The frozen contract is *the size of the largest set of pairwise-independent
lineages*, where two lineages are independent only when they differ in **both**
publisher and document.

M3-ADR-048 replaced an over-counting implementation with connected components.
That under-counts. On

```
A–doc1   A–doc2   B–doc2   B–doc3   C–doc3
```

every edge sits in one component, so it answered **1** — while `A/doc1`,
`B/doc2`, `C/doc3` are three genuinely independent witnesses.

A set of `(publisher, document)` pairs is pairwise independent exactly when no
publisher and no document repeats, which is the definition of a **matching**.
So the contract *is* maximum bipartite matching; components were never an
equivalent formulation of it.

### Decision

Kuhn's augmenting-path algorithm over the publisher↔document graph. No new
dependency; both sides iterated in sorted order, so the result is deterministic.

### Consequences

* The chain above now answers 3. Mirrors and self-corroboration are unchanged,
  because a publisher matches once and a document matches once — which is
  precisely the independence rule restated.
* This function feeds both confidence and the projections, so both were
  re-derived.
* **Generalisable, and the second time here:** when a contract states a
  property, implement the property. Both wrong answers came from substituting a
  cheaper graph computation that felt equivalent and was not.

---

## M3-ADR-060 — Precedence ranks a *more recent* earliest retrieval first

**Status:** accepted (audit finding)

### Context

The frozen precedence ends: fact type, trust, publication date, **more recent
earliest `retrieved_at` across the lineage**, lowest claim id.

`_claim_facts` computed `MIN(retrieved_at)` correctly. `precedence_key` then
placed that datetime into an ascending sort key consumed by `min(...)`, so an
**older** earliest retrieval won — the exact inverse of the rule, on a rung
every other element of the key negated.

### Decision

Negate the retrieval rung like every other rung. The meaning is unchanged: it
is still the *earliest* retrieval across the lineage, and a more recent one now
outranks an older one.

### Consequences

* The bug only surfaced when fact type, trust and publication date all tied,
  which is why the fixture corpus never showed it. Rungs below the first
  distinguishing one need their own test, not coverage by accident.

---

## M3-ADR-061 — A review targets one observation, about the company that captured it

**Status:** accepted (second audit; **new migration** `0008_m3_candidates`)

### Context

Three defects in one operation, all demonstrated on this branch.

**The caller chose the company.** `EvidenceReviewIn.company_id` flowed straight
into `assert_claim`. Evidence captured while researching company A could be
confirmed into company B — not a permissions problem, but evidence about one
organisation asserted about another.

**One review confirmed a whole document.** Confirmation ran the human extractor
across the entire text derivation. The sampled PDF reading holds four
observations, so reviewing *one* created four HUMAN evidence items and four
claims, and the stored HUMAN extraction recorded that the reviewer had
confirmed all four. They had seen one.

**"Reviewable" meant knowing a UUID.** Any evidence item was accepted, so an
ordinary deterministic observation could be pushed through confirmation and
acquire a HUMAN extraction nobody asked for. And a low-confidence observation
left no durable trace at all: `low_confidence_deferred` was transient state, so
once the process ended nothing connected the `INSUFFICIENT_EVIDENCE` gap to the
evidence behind it. D3 was passing without being implemented.

### Decision

* The subject company is **derived** — evidence → fetch event → attempt → run →
  company — and a chain that does not resolve uniquely is refused. There is no
  longer a field to supply.
* Confirmation is scoped to **one attribute at one span**. Both halves are
  needed: two rules can match the same text, so the locator alone is not one
  observation. The scope is inside the extraction contract hash, so each
  confirmed observation is its own extraction whose stored `observations` are
  exactly what was confirmed.
* `research_review_candidates` makes reviewability a durable state, carrying
  why the observation is waiting, which question raised it, and which attribute
  it concerns. Low-confidence observations now create evidence and a candidate
  rather than being dropped.

### Consequences

* `resulting_claim_id` stays singular and is now **true**: one attribute at one
  span is one value from one origin, so at most one claim. The service raises
  rather than silently recording the first of several.
* ~~The review must re-run the source extractor under the same context the
  pipeline used — an HTML locator carries a structural path computed from the
  raw document, and re-running without it produces a different locator hash. A
  pruned body therefore makes a confirmation impossible, and says so.~~
  **Superseded by M3-ADR-065.** Re-running was the wrong mechanism: it made a
  durable candidate depend on the current extractor registry, and the pruned-body
  consequence — stated here as an accepted cost — was a defect. The reviewed
  reading is read from the persisted extraction instead.
* The scoping decision above is unchanged in intent but no longer implemented by
  a scoped extractor: identity now comes from the observation fingerprint
  (M3-ADR-064), and the confirmed observation is copied from disk rather than
  recomputed.
* **Generalisable:** an operation that can affect several things must not
  record one of them. Either scope the operation or record the set — and
  scoping was right here, because a reviewer confirms what they read.

---

## M3-ADR-062 — Terminal siblings serialize on the parent, not on a unique index

**Status:** accepted (second audit)

### Context

`0007`'s partial unique indexes made two terminal siblings unrepresentable, and
the previous report claimed the loser received a domain conflict. It did not.
The services caught only `uq_signal_event`; the sibling race raises
`uq_signal_terminal_event`, and gaps had no translation at all.

The test that was supposed to prove otherwise was also not the race: both
sessions ran a preliminary `SELECT`, which does not freeze a READ COMMITTED
snapshot, then wrote and committed sequentially — so the second writer saw the
first commit and the race never occurred.

### Decision

Take the parent row `FOR UPDATE` before deciding a terminal transition, re-read
the state under that lock, and refuse with a domain error. The unique indexes
remain as the backstop for a writer that bypasses the service, and that path is
translated too.

The tests interleave with threads and an `Event` — no sleeps. The shape follows
from the fix being a lock: writer one holds the row and its insert open, writer
two blocks, writer one commits, writer two wakes and is refused.

### Consequences

* The loser is told which state was reached, rather than an index name.
* A serialized writer *blocks*, which is what a correct lock does — the test
  had to be rewritten to expect that rather than expecting both to proceed.

---

## M3-ADR-063 — C10: a stated negative is an observation, not a fact type

**Status:** accepted (second audit; acceptance corrected)

### Context

C10 read *"A stated negative is a fact … `emergency_service = false` is
asserted as `FACT`"*, and the test traced to it asserted only that a negative
claim existed and was `OBSERVED`. The scenario was not being tested as written
— and could not be. The fixture's negative comes from a third-party directory,
which the registry and the trust policy type as `PROXY`.

Promoting it to `FACT` to satisfy the sentence would have broken the rule the
whole milestone rests on: fact type follows the source and its evidence class,
not the shape of the claim.

### Decision

C10 becomes *"a stated negative is an observation, not absence"*: `false` is
representable, `availability = OBSERVED`, no `NO_EVIDENCE` gap is raised — and
the fact type follows the normal policy, which for a directory is `PROXY`. J3
already covers an explicit company statement reaching `FACT`.

The test is renamed so its name no longer claims something it does not assert,
and now checks the fact type and the source class it follows from.

### Consequences

* **Generalisable:** a scenario that requires an exception to a governing rule
  is usually a wrong scenario, not a needed exception. The fix was to the
  sentence.

---

## M3-ADR-064 — A review candidate is one observation, identified by fingerprint

**Status:** accepted (third audit; **new migration** `0009_m3_observation_identity`)

### Context

M3-ADR-061 said a review targets "one attribute at one span". The table did not.
`research_review_candidates` was keyed
`UNIQUE (evidence_item_id, run_id)`, and an evidence item is keyed on its
locator — so two extraction rules matching the same text share one.

The corpus contains two such pairs. "58 field technicians" yields
`technician_count` **and** `field_workforce_present` at one offset;
another sentence yields `fleet_size` and `fleet_presence`. Reproduced before
changing anything: the pipeline raised one candidate where two observations were
waiting, and `ON CONFLICT DO NOTHING` swallowed the second without an error.

Three things followed:

* **A question disappeared.** The second observation was never queued, so no
  reviewer would ever see it, and no gap explained its absence.
* **One decision answered two questions.** `pending_candidates` excluded by
  `evidence_item_id NOT IN (SELECT evidence_item_id FROM reviews)`, so deciding
  `technician_count` removed `field_workforce_present` from the queue.
* **A rejection was unreadable.** `research_evidence_reviews.evidence_item_id`
  could not distinguish "the figure 58 is wrong" from "there are no field
  technicians". A durable record that cannot say what was rejected records
  nothing usable.

### Decision

Candidate identity is the **observation**, not the evidence item:
`UNIQUE (run_id, observation_fingerprint)`.

The fingerprint is `sha256_json` over every field that can make two observations
different — `attribute_key`, the canonical `value` (which carries
`evidence_class` for a capped attribute), `unit`, `fact_type`, the locator hash,
and `support_kind`. The quote is excluded: it is a function of the span, so it
adds nothing and would make identity fragile to whitespace.

`attribute_key` alone is not sufficient and neither is the locator alone — the
two collisions above differ only in the attribute, and the same attribute could
in principle be read at one span with a different value, unit or strength. Both
are therefore in the hash, along with the rest.

A review names the **candidate**: `research_evidence_reviews.review_candidate_id`
replaces `evidence_item_id` outright, with `uq_review_identity
(review_candidate_id, actor, reviewed_at)`. The API follows the resource:
`GET /research-review-candidates`,
`POST /research-review-candidates/{id}/decision`,
`GET /research-review-candidates/{id}/reviews`. The old item-keyed routes are
**removed**, not aliased — keeping them would keep the ambiguity reachable, and
nothing is released.

Pending is per candidate, and means **undecided, not unresolved**: a candidate
leaves the queue on its first decision. Later reviewers may still record an
opinion, because the log is append-only and a disagreement is information, but
the queue does not reopen and nothing adjudicates. The first decision is
operative; the rest are recorded dissent. M3 has no consensus mechanism and
needs none — this is stated so that its absence is a decision rather than a gap.

### Consequences

* Migration 0009 backfills 0008's rows deterministically rather than deleting
  them: `'legacy:' || sha256(evidence_item_id || ':' || attribute_key)`. Those
  rows predate the fingerprint, so no honest fingerprint exists for them; the
  prefix says so explicitly instead of fabricating one that would collide with a
  real reading. Existing reviews are attached to the candidate with the lowest
  attribute key for their evidence item — the only deterministic choice
  available, since the old row did not record which observation it meant.
* The backfill has to disable the append-only trigger on both tables for the
  duration. A migration may; a service may not, and the migration says so.
* Evidence items keep their locator identity. Sharing one between observations
  was never the defect — treating that item as the unit of review was.
* **Generalisable:** a uniqueness constraint is a claim about what the row *is*.
  When the prose says "one observation" and the key says "one document", the key
  wins silently, and `ON CONFLICT DO NOTHING` turns the disagreement into
  missing data rather than an error.

---

## M3-ADR-065 — A confirmation reviews the historical reading, and closes no attempt

**Status:** accepted (third audit)

### Context

Confirmation looked up the currently registered extractor by id, wrapped it in a
scoped HUMAN extractor, and re-ran it over the derived text, hoping the locator
still reproduced. Two independent failures:

**It depended on the present.** A review candidate is durable by design — D11
exists precisely so a reviewer arriving tomorrow can act. But the extractor may
by then have been upgraded, renamed or withdrawn, in which case a legitimate
candidate became unanswerable. And retention prunes raw bodies and derived text
on a schedule; M3-ADR-061 recorded "a pruned body makes a confirmation
impossible" as an accepted consequence. It is not acceptable: retention must not
silently destroy a queued question.

**It rewrote a finished record.** `run_extraction(attempt_id=...)` records usage,
so a confirmation three days later added a `research_attempt_extractions` row to
a research attempt that had already reached `PARTIAL`. Reproduced before
changing anything. An attempt is the record of one execution; nothing executed,
and it happened later, by someone else.

### Decision

The reviewed object is the observation already persisted in
`research_extractions.observations`, located by the candidate's fingerprint.
Confirmation copies it into a HUMAN extraction. Nothing is executed, no
extractor is consulted, and no text or body is read — `observations` is not
prunable under the retention contract; only `raw_output` is.

If the fingerprint names no stored observation, the confirmation is refused with
`NotReviewableError`: the queue and the ledger disagree, and guessing which is
right would be worse than stopping.

The HUMAN extraction is inserted directly, with **no** `research_attempt_extractions`
row. It therefore belongs to no attempt at all, and its lineage is reachable
through `research_evidence_reviews.human_extraction_id` — the relationship that
actually describes how it came to exist.

### Consequences

* A confirmation now survives its extractor being deleted and its body and text
  being pruned. Both are tested directly (D16).
* The HUMAN extraction's contract hash is scoped by the fingerprint, so each
  confirmed observation is its own extraction and two confirmations of one
  document never collide.
* `scoped_human_extractor` is deleted rather than left unused: it existed only to
  re-run, and a helper that re-runs is an invitation to re-run.
* Attempt cost and usage totals no longer drift with review activity, which they
  silently did before.
* **Generalisable:** confirming a historical statement means reading what was
  said, not asking the current system what it would say now. When the two differ
  the second answers a different question — and an append-only record of a
  finished process is not a place to put something that happened afterwards.

---

## M3-ADR-066 — A review candidate is one observation *occurrence*

**Status:** accepted (Boro-first readiness; **new migration** `0010_m3_review_resolution`)

### Context

`0009` keyed a candidate `UNIQUE (run_id, observation_fingerprint)`. That
separated two readings of one span, which was the defect it was written for. It
also merged the *same* reading found on different sources, which nobody checked.

Reproduced on this branch by instrumenting `raise_candidate`: the pipeline made
**30** calls and landed **14**. Eight observations were each attempted from three
distinct evidence origins — `meridianmechanical.com/company`,
`www.meridianmechanical.com/`, and the unrelated domain
`meridian-mechanical.net/about` — and collapsed to one candidate apiece. Sixteen
questions never reached a reviewer, and no gap explained their absence.

Those are three publishers. For a GTM operator that distinction is the whole
point of the review queue: "the company's own site says 58 technicians" and "a
third-party directory says 58 technicians" are different things to believe, and
a reviewer who rejects the directory has said nothing about the company's site.
The corroboration machinery already treats them as independent origins; the
review queue did not.

### Decision

Identity is `UNIQUE (evidence_item_id, observation_fingerprint)` — one
observation **occurrence**.

The run is deliberately *not* in the key. A reviewer answers "is this reading,
from this source, trustworthy?", and that answer does not expire because a later
run surfaced the same occurrence again. Keying on the run would re-ask a question
already answered and duplicate the reviewer's work; the run stays reachable
through provenance, where it is a fact rather than a copy.

`lineage_tag` joins the fingerprint. It decides how an observation *groups* when
a claim is asserted — a tagged observation forms one lineage with everything
sharing its tag, an untagged one groups by evidence origin — so two readings
differing only in the tag assert different claims, which makes them different
review questions. `evidence_class` needs no entry: `__post_init__` folds it into
`value`.

The HUMAN extraction a confirmation writes is scoped by the fetch event as well
as the fingerprint. Two sources can mirror one document and therefore share a
text derivation; without the occurrence in the contract hash, one HUMAN
extraction would have carried the evidence of two separate confirmations, and the
first review's history would have grown when somebody else confirmed the second
source.

### Consequences

* The queue got larger, from 14 candidates to 30 on the same corpus. That is the
  fix, not a regression: the missing sixteen were real questions.
* A fingerprint now legitimately repeats across rows, so tests assert
  distinctness of the *pair*, not of the hash.
* `0010` recomputes every non-legacy fingerprint by matching each row against the
  observations persisted on its own extraction under the old hash, then writing
  the new one. Rows it cannot match are marked `unmatched:` rather than given an
  invented hash — the queue and the ledger already disagreed, and the service
  refuses to review such a row instead of guessing.
* **Generalisable:** "the same thing" is a question about the observer as much as
  the observation. Two witnesses saying the same sentence are two witnesses, and
  a uniqueness key that cannot tell them apart will quietly discard one.

---

## M3-ADR-067 — Candidate provenance is derived, not stored

**Status:** accepted (Boro-first readiness)

### Context

`research_review_candidates` stored `company_id`, `run_id` and `attempt_id`
alongside `evidence_item_id`. All three follow from the evidence item through
single-valued foreign keys: evidence → fetch event → attempt → run → company.

Three stored copies of a derivable fact are three ways for a row to disagree with
reality, and the failure mode is the one that matters most: an operator filtering
the queue by their account, and being shown another company's evidence because
one row was written wrong. The service already had a defensive check comparing
the derived company to the stored one — a guard that existed only because the
data model made the mismatch possible.

### Decision

Drop all three columns. `provenance_of_evidence()` walks the chain once and
returns the whole tuple; the pending queue's company filter uses a subquery over
the same walk; the API derives `company_id` and `run_id` for its response.

Preferred over a composite constraint or a constraint trigger because it makes
the inconsistent state **unrepresentable** rather than **rejectable**, and it
removes code instead of adding it. The same reasoning retired the caller-supplied
`company_id` on the review request in M3-ADR-061: the fix was that there was no
longer a field to supply.

### Consequences

* The company filter is four joins on indexed keys over a reviewer's queue. That
  is not a hot path, and correctness is now structural.
* `raise_candidate` takes the evidence item and nothing else about the chain, so
  a caller cannot get it wrong.
* **Generalisable:** denormalising for a filter buys a join and sells an
  invariant. Only worth it when the join is actually the problem.

---

## M3-ADR-068 — The first decision is operative; the rest are recorded dissent

**Status:** accepted (Boro-first readiness)

### Context

The documented rule since M3-ADR-064 was that a candidate leaves the queue on its
first decision and later reviewers record an opinion. The implementation did not
follow it: `review_candidate` ran the full confirmation every time. A second
reviewer confirming an observation a first had *rejected* appended another HUMAN
extraction, another evidence item and another claim — silently reversing a
colleague, with nothing in the record saying which decision was in force.

Reading before writing would not have fixed it either. Two operators arriving
together both read "nobody has decided" and both act.

### Decision

`research_evidence_reviews.is_operative`, with
`UNIQUE (review_candidate_id) WHERE is_operative`. The service takes the
candidate row `FOR UPDATE` before deciding which kind of review this is, so the
loser waits and then sees the decision that already happened.

Two CHECKs make the semantics the schema's rather than the service's intention: a
non-operative row may carry no HUMAN extraction and no claim, and the
"a confirmation has provenance" rule applies only to the operative one.

M3 has **no consensus mechanism**, and this ADR is where that is a decision
rather than an omission. A disagreement is information and is kept; adjudicating
it is a human's job, not a scoring function's.

### Consequences

* Dissent is durable and inert. An operator can see that a colleague disagreed
  without the account state having changed underneath them.
* Migration `0010` backfills `is_operative` as the earliest decision per
  candidate — the only defensible reading of rows written when every decision
  acted.
* **Generalisable:** "the first one wins" is a claim about serialization. Without
  a lock and a partial unique index it is a claim about luck.

---

## M3-ADR-069 — A gap event names exactly one actor

**Status:** accepted (Boro-first readiness)

### Context

`operational_research_gap_events.attempt_id` was NOT NULL, because every event
used to be something an execution did. Once a human review can close a gap, that
no longer holds: the reviewer's decision happened days later, nothing ran, and
borrowing the attempt that raised the gap would record a machine run performing
work a person performed.

### Decision

`attempt_id` becomes nullable, `resolved_by_review_id` is added, and a CHECK
requires exactly one of the two on every event — not "at least one", because two
would be two stories about who closed the gap. A second CHECK confines the human
actor to `RESOLVED`: nothing else in the gap lifecycle is something a reviewer
does.

### Consequences

* `resolve_gap` takes `attempt_id` **or** `review_id` and refuses both or
  neither, so the caller cannot leave the question open.
* An operator reading a closed gap can see *why* it closed, and by whom.
* **Generalisable:** when a second kind of actor appears, a NOT NULL column
  naming the first kind becomes a lie rather than a constraint.

---

## M3-ADR-070 — A confirmation reconciles the account synchronously

**Status:** accepted (Boro-first readiness)

### Context

An operative confirmation created the claim and stopped. The
`INSUFFICIENT_EVIDENCE` gap stayed open, the company profile still had nothing
for the attribute, and plan coverage still counted it as uncovered. Every one of
those was correct at the time of the research attempt and wrong the moment a
human confirmed.

For the system's first and primary user that is not a cosmetic issue. The account
view is what a person looks at before deciding whether they know enough to sell
to this company, and it was contradicting itself.

### Decision

After an operative confirmation that produced a claim, the service resolves the
matching open `INSUFFICIENT_EVIDENCE` gap for that attribute and run, and rebuilds
the company profile and that run's plan profile — in the same transaction as the
review.

Synchronous on purpose. This is one attribute on one company; making it a queued
job would mean the operator's screen is briefly lying, and there is nothing to
gain by that. `REBUILD_RESEARCH_PROFILE` remains a direct call rather than a job,
as it already was.

The projections are **rebuilt from the ledger**, not patched. They are functions
of the claims, and the claim now exists; recomputing is how they stay
reproducible.

### Consequences

* A rejection reconciles nothing, deliberately: the gap is still open, because
  disbelieving evidence is not knowing the answer.
* Dissent reconciles nothing either, because it asserts nothing.
* `ReviewOutcome` reports `resolved_gap_id` and `profiles_rebuilt`, so the caller
  can see what the decision actually changed rather than inferring it.
* **Generalisable:** a write that leaves a derived view contradicting the ledger
  has not finished. If the view is what a person acts on, "eventually" is a
  decision to mislead them for a while.

---

## M3-ADR-071 — Two providers, chosen explicitly, and no framework between them

**Status:** accepted (BoRo-first live research)

### Context

`run_pipeline` named `FixtureDiscoveryProvider` in its body, and `_discover`
named the fixture corpus directly: `corpus.HOME` for the sitemap and crawl
roots, `PLAN_SEARCH_QUERIES` for search, and two corpus constants seeded by
hand. There was no way to research a real website without editing the pipeline.

The obvious response — a provider registry, an entry-point plugin system, a
configuration schema — would be building a platform for users who do not exist.
BoRo is the only customer, and BoRo needs one production adapter.

### Decision

Two protocols, `Transport` and `DiscoveryProvider`, with two implementations
each chosen at the call site:

* `FixtureTransport` + `FixtureDiscoveryProvider` — deterministic QA, no network.
* `ProductionWebTransport` + `BoRoFirstPartyDiscoveryProvider` — real research.

*What to look for* moves onto the provider as `plan()` and `seed_inputs()`;
*how it is recorded* stays in the pipeline. The fixture corpus's home page and
query list are the fixture provider's business and now live there.

No registry, no entry points, no configuration file naming a class. Selection is
an argument.

`run_pipeline` assembles the fixture pair when neither is passed, so every
existing caller is unchanged and nothing reaches the internet by omission. A
live transport with **no** provider is refused outright: the fixture provider
names addresses that do not exist, and a live transport would go and ask for
them.

### Consequences

* The production transport returns the same `FetchResult` the fixture returns,
  so `acquisition.record_fetch` remains the only way a retrieval becomes a row.
  There is no second acquisition model and no path around `ResearchFetchEvent`.
* Adding a third provider later means writing a class and passing it. If BoRo
  ever has a second customer, that is when a registry earns its keep.
* **Generalisable:** an abstraction with one implementation is a guess. Two
  implementations is the smallest number that proves the seam is in the right
  place.

---

## M3-ADR-072 — The crawl policy is a versioned judgement, and a budget is recorded

**Status:** accepted (BoRo-first live research)

### Context

A contractor's website has a few pages describing how the company operates and a
great many that do not: blog archives, tag pages, privacy policies, paginated
news. Fetching all of them is impolite, slow, and yields nothing.

But any rule for telling them apart is a judgement about one market at one time,
not a fact. Written as a constant with no version, it becomes invisible: a run
from six months ago cannot be read, because the policy it ran under is gone.

### Decision

`FIRST_PARTY_POLICY_VERSION`, recorded in every discovery context and in the
attempt's seed inputs. The relevance rule is a deterministic substring policy
over the path — exclusions checked *first*, so `/blog/commercial-hvac-tips`
matches three relevant terms and is still a blog post.

`CrawlBudget` is conservative by default: 25 pages, depth 2, 5 MB per document,
40 retrievals, one second between requests to a host, 15-second timeout, 5
redirects. Chosen after fetching real contractor sites, and asserted in a test so
that loosening them is a visible decision rather than a drifting constant.

When a budget stops exploration, the run records **where**:
`PipelineResult.budget_stopped_at` and the sources discovered but not retrieved.

### Consequences

* "We stopped looking" and "there was nothing there" are different states, and
  an operator can tell them apart. Without this they both render as an empty
  attribute, and the second is a lie.
* The relevance rule is allowed to be wrong in both directions. A missed page is
  a truthful gap; an irrelevant page that gets fetched simply yields no
  observations. Neither failure mode invents evidence.
* **Generalisable:** a heuristic that decides what gets *looked at* must be
  versioned and recorded, because its output is indistinguishable from the world
  being empty.

---

## M3-ADR-073 — Safety is enforced at the resolved address, on every hop

**Status:** accepted (BoRo-first live research; release blocker)

### Context

A research worker takes addresses from third parties — sitemaps, page links, an
operator's paste buffer — and issues requests from inside our network. That is
the shape of a server-side request forgery, and the usual defences do not hold:

* A URL allowlist cannot see where a hostname points. Whoever controls the DNS
  for `bigmechanical.com` decides whether it resolves to `93.184.216.34` or
  `10.0.0.5`.
* `follow_redirects=True` makes the request *before* this process sees the
  `Location` header, so a public page redirecting to `169.254.169.254` has
  already succeeded by the time anything could refuse it.

### Decision

Every hop is resolved and checked before it is requested. Redirects are followed
manually, and each target goes through the same check as the original.

An address is allowed only when it is globally routable. The specific reasons —
loopback, link-local, private, reserved, multicast, unspecified, cloud metadata —
are checked first because they make better error messages, and `is_global` is
the backstop. That backstop is not redundant: carrier-grade NAT space
(`100.64.0.0/10`) is flagged by *no* Python property — `is_private` is False and
`is_loopback` is False — yet it reaches other customers of the same carrier. It
was found by a test written before the code was trusted.

IPv4-mapped and 6to4 addresses are unwrapped before checking, so `::ffff:10.0.0.5`
cannot smuggle a private address through a v6 literal. Non-HTTP schemes and URLs
carrying credentials are refused. A host that resolves to several addresses is
refused if *any* of them is not routable — picking the good one would be racing
the resolver.

### Consequences

* A refusal is recorded as `DENIED` with `error_class = "UnsafeTarget"`, so it is
  visible in the fetch ledger rather than silently dropped.
* `allow_private` exists for a local test server and is a parameter rather than
  an environment variable, so enabling it is visible at the call site. The
  pipeline and the CLI never pass it.
* **Generalisable:** validate the thing you are about to act on, not the string
  that produced it. Between the check and the request, a name can change meaning.

---

## M3-ADR-074 — Research scope comes from M2, never from the company's name

**Status:** accepted (BoRo-first live research)

### Context

Live research needs to know which website *is* this company. The tempting answer
is to search for the name — and it is the one answer that must never be used.
Two contractors called "Allied Mechanical" are two companies, and a name lookup
researches whichever one ranks better. The resulting evidence is attached to the
wrong account with a complete, internally consistent provenance chain, and there
is no repair for that: the claim looks exactly like a correct one.

### Decision

Scope is resolved from `company_domains`, using M2's own rules, before anything
is fetched. `IDENTITY`, `ALTERNATE`, `REDIRECT` and `COUNTRY_TLD` are in scope —
M2 has already decided those are the same organisation. `GROUP` is never in
scope: M2 records that role precisely because the host does *not* identify a
company, and crawling a franchise portal's root as one of its franchisees is the
error the role exists to prevent. `DEFUNCT` is out of scope, because whatever is
served there now belongs to someone else.

M2's shared-host blocklist is re-applied here rather than trusting the role
alone, so a row claiming `IDENTITY` for `wixsite.com` is still refused.

Membership is by registrable domain, so `www.` and a `service.` subdomain are in
scope and `notthem.com` is not. Every candidate — including a URL an operator
typed by hand — is checked against the scope.

A company with no researchable domain produces a report saying so. It does not
produce a guess.

### Consequences

* Out-of-scope addresses are counted and reported, so an operator asking "why did
  this find nothing?" is not left guessing whether a domain was missing or
  refused.
* v1 implements only `HUMAN_SEED`, `SITEMAP` and `CRAWL_LINK`. `SEARCH`,
  `JOB_BOARD`, `REGISTRY` and `API` return nothing — a decision recorded in the
  methods themselves, not an unimplemented stub. Third-party search is where
  wrong-company evidence comes from, and first-party research has not yet proven
  insufficient.
* **Generalisable:** when an identity decision has already been made by a system
  that owns it, re-deriving it downstream is not redundancy. It is a second,
  worse answer that will eventually disagree.

---

## M3-ADR-075 — The first-party web evidence surface, measured

**Status:** accepted (live transport validation pilot; **finding, not a code change**)

### Context

M3's extraction rules were written against a fixture corpus whose prose was
authored alongside them. This is the first measurement of what real commercial
HVAC and mechanical contractor websites actually publish.

Measured over **146 pages** with extracted text, from the reachable sites in the
**live transport validation cohort** — seventeen real contractor domains selected
to exercise production research. They are **not** BoRo's sales cohort and no
conclusion here is about BoRo's outbound population.

### Present, and useful

| Signal | Pages | Share |
| --- | ---: | ---: |
| Careers / hiring language | 146 | 100% |
| Locations named | 143 | 98% |
| Service categories (chiller, boiler, RTU, controls, …) | 142 | 97% |
| Markets / industries served | 120 | 82% |
| Commercial orientation | 114 | 78% |
| Industrial orientation | 90 | 62% |
| Retrofit / replacement | 74 | 51% |
| Energy / efficiency | 71 | 49% |
| Preventive maintenance | 65 | 45% |
| Design-build | 50 | 34% |
| Service agreements | 47 | 32% |
| Emergency / 24-7 | 46 | 32% |
| Years in business | 24 | 16% |

### Expected, and effectively not public

| Signal | Pages | Share |
| --- | ---: | ---: |
| Branch count | 7 | 5% |
| Named FSM / ERP / dispatch system | 5 | 3% |
| Certifications | 5 | 3% |
| Technician count | 3 | 2% |
| Employee count | 2 | 1% |
| Fleet size | 0 | 0% |
| Internal approval / handoff process | 0 | 0% |
| Billing readiness | 0 | 0% |

*(Corrects an earlier note in this project that read "zero" for technician and
branch counts. They are rare, not absent: 2% and 5% of pages. At roughly half the
cohort reachable, that is about one account in a hundred — which is the number
that matters when deciding whether an extractor is worth writing.)*

### The finding

**A contractor's website exposes a narrower and differently-shaped evidence
surface than the fixture corpus predicted.** It describes *what the company
does and for whom* — services, markets, orientation, agreements, hiring. It does
not describe *how the company runs* — headcount, fleet, systems, internal
process. That second set is what the registry was largely built around.

`NO_EVIDENCE` for those attributes is the correct and truthful answer, and it
must stay that way. Inferring a technician count from "our large team of
experienced professionals" would convert marketing language into an operational
fact, which is the one thing this milestone exists to prevent.

### Consequences

* The useful work is on what is actually there, not on what is missing.
* The operational attributes will come from another source class — job postings,
  registry filings, a licensed provider — or they will not come at all. That is a
  commercial decision, and it should be made against this table.
* **Generalisable:** rules written against a corpus you also wrote are a
  hypothesis. First contact with real data is a measurement, and it is cheaper to
  take it than to build on the hypothesis.

---

## M3-ADR-076 — Normalization is identity, not addressing

**Status:** accepted (BoRo-first live pilot; **bug found against real sites**)

### Context

`normalize_locator` collapses the spellings of one address into a single
identity: case, default ports, tracking parameters, parameter order, a trailing
slash and the fragment are presentation, and what remains is the document. That
is correct, and it is what makes two discoveries of one page one source.

The live transport used it for something else. On a redirect it normalized the
`Location` and requested *that*.

`total-mechanical.com` answers `/hvac` with `301 → /hvac/`. The transport
normalized the target back to `/hvac`, asked again, was redirected again, and
spent its entire redirect budget on a site that was politely telling it the
correct spelling. Measured in the first live cohort run: **32 wasted retrievals**
with `TooManyRedirects`, and one site of seventeen never researched at all.

### Decision

The raw resolved URL is what the request is made against; the normalized form is
what the fetch event records and what scope and identity are checked with. They
are different jobs and they now use different values.

Loop detection tracks **raw** addresses. Keying it on the normalized form would
reintroduce the same bug in a new shape — `/hvac/` and `/hvac` are one identity
and two different requests, and the second one is the one the server asked for.

A genuine cycle (`/a → /b → /a`) now ends at the first repeated request with
`RedirectLoop` rather than consuming the redirect budget.

### Consequences

* The affected site went from zero pages to a 238 KB document in one request.
* **Generalisable:** a canonicalizing function has a purpose, and using it
  outside that purpose looks like consistency while being a bug. "Which thing is
  this" and "what do I send on the wire" are not the same question, and a server
  that disagrees with your normalizer is not wrong.

---

## M3-ADR-077 — Most of BoRo's cohort is behind bot mitigation, and that stays a gap

**Status:** accepted (BoRo-first live pilot; **finding, not a code change**)

### Context

The first live run met a wall that no amount of implementation quality gets
past. Of the seventeen real U.S. commercial HVAC and mechanical contractor sites
in the **live transport validation cohort** — selected to exercise production
research, and **not** BoRo's sales cohort — a majority answered an honest,
identified, robots-respecting fetcher with `403`. Nine of seventeen were
reachable.

Diagnosed at the response, without attempting a bypass:

| Site | Response |
| --- | --- |
| `metromech.com` | Cloudflare `cf-mitigated: challenge`, "Just a moment" interstitial |
| `comfortsystemsusa.com` | plain `nginx` 403 |
| `campbellinc.com` | Cloudflare, denial after redirect |

These are interactive bot challenges and server-side blocks. Passing them means
executing a JavaScript challenge, solving a CAPTCHA, or presenting a browser's
`User-Agent` while not being one.

### Decision

Nothing. The denial is recorded as `DENIED`, the attributes become
`NO_EVIDENCE` gaps, and the account reads as unresearched — which is true.

M3 does not evade bot mitigation. Not because it would be difficult, but because
a site returning `403` to an identified research bot has stated a preference, and
the value of this system rests on its evidence being defensible. Evidence
obtained by pretending to be a browser is not.

### Consequences

* **First-party-only research cannot cover a cohort like this one.** Nine of
  seventeen sites were reachable. That is a property of the market, not a defect, and no
  change to the transport improves it.
* The decision this forces belongs to BoRo, not to this milestone: accept
  partial coverage, or add a lawful second source — a licensed data provider,
  job boards, registry filings — in a later version. It is recorded here so the
  choice is made with the number in hand.
* The sites that *are* reachable are researched properly, and the ones that are
  not are visibly and honestly empty. An operator can tell which is which, which
  is the minimum the system owed them.
* **Generalisable:** when the constraint is someone else's stated preference, the
  engineering question is closed. What remains is a commercial question, and
  pretending otherwise just moves the cost somewhere less visible.

---

## M3-ADR-078 — The extraction rules are fixture-shaped sentence templates

**Status:** accepted (live transport validation pilot; **diagnosis, not a code change**)

### Context

Across the validation cohort the extractors produced three attributes:
`preventive_maintenance`, `emergency_service` and `installation`. The obvious
reading — "the rules only cover three of forty-two attributes" — is wrong.
Twenty-seven attributes *have* rules. They simply did not fire.

Reading the patterns explains why:

| Attribute | Pattern | Signal present on |
| --- | --- | ---: |
| `service_area` | `We serve (...), Ohio\.` | 98% of pages |
| `service_categories` | `Categories: (...)\.` | 97% of pages |
| `recurring_service_contracts` | `maintenance agreements with [^.]+\.` | 32% of pages |
| `preventive_maintenance` | `preventive maintenance` | 45% — **fires** |
| `emergency_service` | `24/7 emergency\|24 hours a day, 7 days a week emergency` | 32% — **fires** |

`service_area` is hardcoded to Ohio. `service_categories` expects the fixture's
literal `Categories: X, Y.` sentence, which no real site writes. The two rules
that work are the two whose patterns happen to be ordinary English phrases rather
than transcriptions of a fixture sentence.

### The finding

The registry primitives are largely sound; the **rules** encode the corpus they
were written against. A rule that matches a whole sentence template fires only
when a real page coincidentally uses that sentence.

This is a different and much more tractable problem than "M3 cannot read the
web". The signals are present at 82–98% frequency, a primitive exists for most of
them, and what is missing is phrasing breadth.

### Decision

Nothing yet, deliberately. Rewriting rules against one seventeen-site sample
would repeat the original mistake with a different corpus. The next change should
be small, aimed at the signals measured in M3-ADR-075, and validated against
pages the rules were not written from.

### The frozen classification

Design input for the next small change. Frozen here so the calibration target is
fixed before any rule is touched.

**Working now** — the two rules written as ordinary English phrases.

| Attribute | Pages |
| --- | ---: |
| `preventive_maintenance` | 45% |
| `emergency_service` | 32% |

**Existing primitive, extractor needs work** — where most of the value is.

| Attribute | Pages | Why it does not fire |
| --- | ---: | --- |
| `service_area` | 98% | rule hardcoded to `We serve (...), Ohio\.` |
| `service_categories` | 97% | rule expects the fixture's `Categories: X, Y.` |
| `operating_markets` | 82% | primitive exists, **no rule at all** |
| `hiring_signal`, `hiring_field_roles` | 100% | primitive exists, **no rule at all** |
| `recurring_service_contracts` | 32% | rule needs `maintenance agreements with …` |

**Possible new primitive — not added.** Each needs a registry decision first:
commercial orientation (78%), industrial orientation (62%),
retrofit/replacement (51%), energy/efficiency (49%), design-build (34%),
years in business (16%).

**Rare, and not a first-party web target.** Truthful `NO_EVIDENCE` is and stays
the correct answer: branch count (5%), FSM/ERP/dispatch (3%), certifications
(3%), technician count (2%), employee count (1%), fleet size (0%), internal
approvals and handoffs (0%), billing readiness (0%).

`certification` belongs in that last group, not among the fixable rules: it has
**no** rule, and at 3% of pages it does not earn one. An earlier draft of this
ADR listed it as a too-narrow rule, which was wrong on both counts.

### Consequences

* **Generalisable:** an extractor tested only against text written for it is
  untested. The pattern that survives contact with real prose is the one that
  matches how people write, not how the fixture author wrote.
