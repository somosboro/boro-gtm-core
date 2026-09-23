# M3 — Proposed Schema Graph

**Status:** design, revision 2. **Not implemented.** No table below exists.

Companion to [M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md).

Revision 2 resolves six structural contradictions in revision 1. Each is
recorded in [M3_ADRS.md](M3_ADRS.md) (M3-ADR-014 … 019). The governing rule,
applied uniformly: **an append-only row may not contain a value that changes.**
Revision 1 broke it in four places.

---

## 1. The graph

```
  operational_research_runs            the logical research question
  (company, policy_version, target_set_hash)   immutable identity
            │
            │ 1:N
            ▼
  operational_research_attempts        one execution; terminal states final
            │
            │ N:1 ────────────────────────────┐
            ▼                                 │
  research_source_discoveries ──▶ research_sources ◀── research_source_edges
  (how we found it, per attempt)   (locator identity)   (REDIRECTS_TO, …)
            │                            │
            │                            ▼
            │                  research_fetch_events     every retrieval,
            │                  (attempt, outcome, time)  success or failure
            │                            │ 0..1 on success
            │                            ▼
            │                  research_artifact_bodies  byte identity
            │                  UNIQUE (raw_body_sha256)  one row per byte string
            │                       │              │
            │      ┌────────────────┘              └──────────────┐
            │      ▼                                              ▼
            │  research_artifact_derivations            research_text_derivations
            │  (body × canonicalization contract)       (body × text policy)
            │      │                                              │
            │      ▼                                              │
            │  research_artifacts                                 │
            │  UNIQUE (strategy, version, canonical_hash)         │
            │  semantic identity                                  │
            │                                                     ▼
            │                                          research_extractions
            │                                          (text derivation × extractor)
            │                                                     │
            │                                                     ▼
            │                                          claim_evidence_links
            │                                          N:M + locator
            │                                                     │
            │                                                     ▼
            │                                    company_claims  (M2-owned ledger)
            │                                    + assertion_fingerprint
            ▼
  operational_research_gaps ──▶ operational_research_gap_events
  (identity only)                (RAISED / ATTEMPTED / RESOLVED / ABANDONED)

  identity_review_signals ──▶ identity_review_signal_events
  (identity only)              (OPEN / ACKNOWLEDGED / ACTIONED / DISMISSED)

  operational_research_profiles       PROJECTION, truncatable, no clock

  discovery_jobs (M2-owned queue) ── reused unchanged, no new queue
```

**Seventeen new tables.** Three M2 objects are touched, all additively (§5).

---

## 2. The five layers, and why each exists

Revision 1 collapsed retrieval, bytes, semantics and text into one
`research_artifact_versions` row keyed `(source_id, raw_body_sha256)`. That row
could not record a second retrieval of identical bytes, could not let one byte
string belong to two canonicalization contracts, and could not accept a text
extractor upgrade without an UPDATE.

| Layer | Table | Identity | Answers |
| --- | --- | --- | --- |
| **A. Retrieval** | `research_fetch_events` | surrogate; append-only log | *When did we look, and what happened?* |
| **B. Bytes** | `research_artifact_bodies` | `UNIQUE (raw_body_sha256)` | *What exact octets exist?* |
| **C. Semantics** | `research_artifacts` via `research_artifact_derivations` | `UNIQUE (strategy, version, canonical_hash)` | *What does this mean, under which contract?* |
| **D. Text** | `research_text_derivations` | `UNIQUE (body_id, text_policy_version)` | *What readable text do extractions and locators use?* |
| **E. Claims** | `research_extractions` → `claim_evidence_links` → `company_claims` | assertion fingerprint | *What do we assert, and on what evidence?* |

Each layer is independently versioned, so an upgrade at one layer never forces
a refetch or a rewrite at another:

* a **new canonicalization version** re-derives C from existing B — no refetch
* a **new text policy** re-derives D from existing B — no refetch
* a **new extractor** re-derives E from existing D — no refetch, no re-parse

---

## 3. Table specifications

Legend — **Mutability:** `identity` (insert-only, no column ever changes),
`append-only` (BEFORE UPDATE trigger rejects; one-way body prune where noted),
`stateful` (stage timestamps written once, terminal states final),
`projection` (truncatable, rebuilt from evidence).

### 3.1 `operational_research_runs` — the logical question

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **identity** |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (company_id, research_policy_version, target_set_hash)` |
| Key FKs | `company_id → companies` (RESTRICT), `vertical_id → verticals` (RESTRICT, nullable) |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `target_attribute_keys` (sorted text[]), `target_set_hash`
(sha256 of the sorted keys plus the policy version), `seed_inputs` (JSONB),
`created_by`.

A run is a **question**, not an execution: *"what do we know about company X's
operations, under policy v2, across these 18 attributes?"* Asking the same
question again reuses the run. Changing the policy version or the target set
changes `target_set_hash`, so it is a **different question and a different
run** — which revision 1 asserted in prose while leaving the target set out of
the key entirely.

No status, no stage timestamps, no error. Those belong to an execution.

### 3.2 `operational_research_attempts` — one execution

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **stateful**; terminal states are final |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (run_id, attempt_number)`; partial unique on `run_id WHERE status NOT IN ('COMPLETED','PARTIAL','FAILED')` — at most one live attempt per run |
| Key FKs | `run_id → operational_research_runs` (RESTRICT) |
| Truncatable | No — it is the provenance root of every fetch |
| Temporal | `created_at`, `started_at`, `discovery_completed_at`, `fetch_completed_at`, `extraction_completed_at`, `completed_at` |

Columns: `attempt_number`, `status`, `error`, `failure_stage`,
`allow_partial_assertion`, `cost_units`.

**Legal transitions**, and no others:

```
PENDING → DISCOVERING → FETCHING → EXTRACTING → ASSERTING → COMPLETED
                                                          ↘
   any non-terminal ──────────────────────────────────────→ PARTIAL
   any non-terminal ──────────────────────────────────────→ FAILED

terminal: COMPLETED, PARTIAL, FAILED — never re-entered
```

A trigger rejects any transition not in that graph, and any transition **out
of** a terminal state. Retrying a `PARTIAL` attempt creates attempt *n+1* on
the same run; the old attempt stays `PARTIAL` forever.

Revision 1 called `PARTIAL` terminal and simultaneously had retry reopen it.
Both cannot be true, and the honest split is that the *question* persists while
the *execution* is finished (M3-ADR-019).

### 3.3 `research_sources` — locator identity, and nothing else

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **identity** |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (normalized_locator, locator_policy_version)` |
| Key FKs | none |
| Truncatable | No |
| Temporal | `first_seen_at` |

Columns: `normalized_locator`, `locator_policy_version`, `host`,
`registrable_domain`, `first_seen_at`.

**No `discovered_by_run_id`. No `discovered_from_source_id`. No
`resolves_to_source_id`.** A source is a place on the internet. It can be
discovered by many attempts, through many parent pages, by several methods, for
several companies, and it can acquire redirect and canonical relationships long
after it was first created. Revision 1 stored each of those as a single field
on an append-only row, which made all but the first unrecordable
(M3-ADR-015).

A source row is global and reusable: two companies researched a year apart
that both link to the same trade-association page share one source row, and
each retains its own provenance through §3.4 and §3.6.

### 3.4 `research_source_discoveries` — how we found it, each time

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (source_id, attempt_id, discovery_method, discovered_from_source_id)` — NULLs distinct via a generated sentinel |
| Key FKs | `source_id → research_sources` (RESTRICT), `attempt_id → operational_research_attempts` (RESTRICT), `discovered_from_source_id → research_sources` (RESTRICT, nullable) |
| Truncatable | No |
| Temporal | `discovered_at` |

Columns: `discovery_method` (`SITEMAP | CRAWL_LINK | SEARCH | JOB_BOARD |
REGISTRY | HUMAN_SEED | API`), `discovery_context` (JSONB — the query, the
anchor text, the sitemap URL), `relevance_hint`.

One source found by both a sitemap and a search engine yields **two
discoveries**, which is a real finding about how discoverable the page is, and
which revision 1 could not express.

### 3.5 `research_source_edges` — relationships between locators

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (from_source_id, to_source_id, relation_type, observed_by_fetch_event_id)` |
| Key FKs | `from_source_id`, `to_source_id → research_sources` (RESTRICT), `observed_by_fetch_event_id → research_fetch_events` (RESTRICT, nullable) |
| Truncatable | No |
| Temporal | `observed_at` |

`relation_type ∈ {REDIRECTS_TO, DECLARES_CANONICAL, LANGUAGE_VARIANT_OF,
MIRROR_CANDIDATE}`.

An edge is an **observation**, carrying the fetch event that observed it.
Discovering a redirect six months after a source was created appends an edge
and mutates nothing. A site that later changes its redirect target appends a
second edge; both remain, and the current one is the most recent — a history
that revision 1's single nullable FK destroyed on every change.

`MIRROR_CANDIDATE` is emitted when two sources yield the same body (§3.7); it
is a *candidate*, never an automatic merge, because sources are never merged.

### 3.6 `research_fetch_events` — every retrieval, successful or not

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | none — every retrieval is a distinct event, by design |
| Key FKs | `source_id → research_sources` (RESTRICT), `attempt_id → operational_research_attempts` (RESTRICT), `body_id → research_artifact_bodies` (RESTRICT, **nullable** — NULL when the fetch failed) |
| Truncatable | No |
| Temporal | `retrieved_at` |

Columns: `fetch_outcome` (`OK | NOT_MODIFIED | NOT_FOUND | GONE | DENIED |
LOGIN_WALL | TIMEOUT | ROBOTS_DENIED | TOO_LARGE | MIME_MISMATCH |
TRANSPORT_ERROR`), `http_status`, `final_url`, `content_type`,
`content_length`, `etag`, `last_modified`, `request_headers_digest`,
`error_class`, `error_detail`, `duration_ms`.

**This is the table revision 1 promised and never declared.** Acceptance A1
required that identical bytes produce "one new retrieval event" and no new
version, but the only place a retrieval time could live was a row unique on
`(source_id, raw_body_sha256)` — so the second retrieval was unrecordable
(M3-ADR-014).

Deliberately **not** unique on anything: *"we saw these exact bytes on Sep 1,
Sep 8 and Sep 20"* is three rows, all pointing at one body. A failed fetch is a
row with `body_id IS NULL`, so "we tried eleven times and it 403'd" is equally
answerable — and neither requires touching evidence.

### 3.7 `research_artifact_bodies` — byte identity

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only + one-way prune |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (raw_body_sha256)` — **global**, not per source |
| Key FKs | none — bytes are independent of where they came from |
| Truncatable | No |
| Temporal | `first_seen_at` |

Columns: `raw_body_sha256`, `raw_body` (nullable), `byte_length`,
`declared_content_type`, `sniffed_content_type`, `body_retention`
(`RETAINED | PRUNED`), `pruned_at`.

Global uniqueness on the content hash is what makes "the same PDF mirrored at
four URLs" one body with four sources and four fetch events, rather than four
copies of a 2 MB file.

The one-way prune (M3-ADR-008) is unchanged: the trigger permits exactly
`raw_body` non-NULL → NULL together with `body_retention → 'PRUNED'` and
`pruned_at → now`, and rejects every other UPDATE.

### 3.8 `research_artifact_derivations` — bytes interpreted under a contract

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (body_id, canonicalization_strategy, canonicalization_version)` |
| Key FKs | `body_id → research_artifact_bodies` (RESTRICT), `artifact_id → research_artifacts` (RESTRICT) |
| Truncatable | No — it is cheap to recompute, but it is also the audit record of *what the contract produced*, which is not recomputable once the body is pruned |
| Temporal | `derived_at` |

Columns: `canonicalization_strategy`, `canonicalization_version`,
`canonical_content_hash`, `derivation_status`, `error`.

**The join that revision 1 lacked.** One body under `HTML_TEXT_V1` and the same
body under `HTML_TEXT_V2` are two derivations pointing at two artifacts — which
is exactly what acceptance A9 demands and what a single
`version → artifact` FK made unrepresentable (M3-ADR-016).

Reading the other way: two different byte strings that canonicalize to the same
content are two derivations pointing at **one** artifact, which is how a
cosmetic edit produces a new body without producing new semantics.

### 3.9 `research_artifacts` — semantic identity

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **identity** |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (canonicalization_strategy, canonicalization_version, canonical_content_hash)` |
| Key FKs | none |
| Truncatable | No |
| Temporal | `first_seen_at` |

Columns: `canonical_content_hash`, `canonicalization_strategy`,
`canonicalization_version`, `language`, `title`, `source_published_at`,
`source_published_granularity`.

`CHECK ((source_published_at IS NOT NULL) = (source_published_granularity <> 'UNDATED'))`

`source_published_at` lives here, not on the fetch event, because it is a
property of *what the document says about itself*, not of when we collected it.
Every canonicalization strategy is therefore required to preserve declared
publication metadata in its canonical form — otherwise two documents differing
only in publication date would collapse to one artifact. That requirement is
part of the strategy contract, stated in the design (§3.3).

### 3.10 `research_text_derivations` — readable text, versioned separately

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only + one-way prune |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (body_id, text_extraction_policy_version)` |
| Key FKs | `body_id → research_artifact_bodies` (RESTRICT) |
| Truncatable | No |
| Temporal | `derived_at` |

Columns: `text_extraction_policy_version`, `extracted_text` (nullable),
`redaction_policy_version`, `page_offsets` (JSONB, for PDFs),
`text_retention`, `pruned_at`, `status`, `error`.

**Separate from canonicalization on purpose.** The two derivations answer
different questions and move at different speeds:

* canonicalization asks *are these the same document?* and is aggressive —
  stripping nav, footers and rotating banners so cosmetic churn does not look
  like a change of fact;
* text extraction asks *what can a reader and an extractor see?* and is
  conservative — keeping content a locator may need to point at.

Upgrading the PDF text extractor re-derives D from stored bytes with **no
refetch and no new artifact**, which revision 1 could not do because
`extracted_text` and its policy version sat on the same append-only row as the
bytes (M3-ADR-017).

Extractions point at a **text derivation**, never at a body, so an extraction
is fully reproducible: same body + same text policy + same extractor version =
same input.

### 3.11 `research_extractions`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only + one-way prune of `raw_output` |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (text_derivation_id, extractor_id, extractor_version, prompt_template_version)` |
| Key FKs | `text_derivation_id → research_text_derivations` (RESTRICT), `attempt_id → operational_research_attempts` (RESTRICT) |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `extractor_kind` (`RULE | PARSER | MODEL | HUMAN`), `extractor_id`,
`extractor_version`, `model_provider`, `model_name`, `model_version`,
`prompt_template_version`, `output_schema_version`, `determinism`,
`temperature`, `extractor_confidence`, `observations` (JSONB), `status`,
`error`, `raw_output`, `raw_output_sha256`.

### 3.12 `claim_evidence_links`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (claim_id, extraction_id, locator_hash)` |
| Key FKs | `claim_id → company_claims` (RESTRICT), `extraction_id → research_extractions` (RESTRICT), `artifact_id → research_artifacts` (RESTRICT) |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `locator` (JSONB), `locator_hash`, `quote`, `quote_sha256`,
`support_kind` (`DIRECT_STATEMENT | DERIVED | CORROBORATING`).

The unique key is on `extraction_id`, not `artifact_version_id` as in revision
1: a **re-extraction of the same lineage adds a link to the existing claim**
rather than creating a second claim (§4). Carrying `artifact_id` denormalised
alongside is what makes lineage grouping (§4.2) a single indexed read.

### 3.13 `operational_research_gaps` — identity only

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **identity** |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (company_id, attribute_key, research_policy_version, gap_kind)` |
| Key FKs | `company_id → companies` (RESTRICT) |
| Truncatable | No |
| Temporal | `first_raised_at` |

Columns: exactly the key, plus `first_raised_at`.

**No `attempted_source_count`, no `last_attempt_at`, no
`resolved_by_claim_id`.** All three change, and revision 1 put them on a row it
declared append-only (M3-ADR-018). They are now derived from §3.14.

### 3.14 `operational_research_gap_events`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (gap_id, event_kind, attempt_id, source_id)` — NULLs via sentinel |
| Key FKs | `gap_id → operational_research_gaps` (RESTRICT), `attempt_id → operational_research_attempts` (RESTRICT), `source_id → research_sources` (RESTRICT, nullable), `resolved_by_claim_id → company_claims` (RESTRICT, nullable) |
| Truncatable | No |
| Temporal | `occurred_at` |

`event_kind ∈ {RAISED, ATTEMPTED, RESOLVED, ABANDONED}`.

Derived by view, never stored:

| Derived | From |
| --- | --- |
| `attempted_source_count` | `count(DISTINCT source_id) WHERE event_kind='ATTEMPTED'` |
| `last_attempt_at` | `max(occurred_at) WHERE event_kind='ATTEMPTED'` |
| `current_status` | the latest event's kind |
| `resolved_by_claim_id` | the latest `RESOLVED` event's claim |

Eleven attempts against a gap append eleven rows and leave the parent
byte-identical.

### 3.15 `identity_review_signals` — identity only

| Property | Value |
| --- | --- |
| Owner | **M3** — written by M3, consumed by M2's review queue |
| Mutability | **identity** |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (company_id, signal_kind, related_company_id, evidence_digest)` |
| Key FKs | `company_id`, `related_company_id → companies` (RESTRICT) |
| Truncatable | No |
| Temporal | `raised_at` |

### 3.16 `identity_review_signal_events`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (signal_id, status, occurred_at)` |
| Key FKs | `signal_id → identity_review_signals` (RESTRICT) |
| Truncatable | No |
| Temporal | `occurred_at` |

`status ∈ {OPEN, ACKNOWLEDGED, ACTIONED, DISMISSED}`, plus `actor`, `note`.

### 3.17 `operational_research_profiles`

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | **projection** |
| PK | `company_id` |
| Natural uniqueness | the PK |
| Key FKs | `company_id → companies` |
| Truncatable | **Yes** — truncate and rebuild must reproduce it byte-identically |
| Temporal | none stored; no clock read during rebuild |

Columns: projected values and envelopes, `contradiction` flags,
`derived_from_claim_ids` (sorted), `corroborating_lineage_count` per attribute
(§4.2), `coverage`, `confidence`, `contradiction_rate`.

---

## 4. The unit of a claim

Revision 1 said both *"one claim, many artifacts"* and, in acceptance C7,
*"two independent sources yield two claims"*. Those are different models.

### 4.1 The decision

> **A `company_claim` is one atomic assertion produced from one evidence
> lineage.**

A lineage is the set of artifacts that justify the assertion. Two independent
sources are two lineages, therefore two claims — they may legitimately differ
in fact type, confidence, source trust and source date, and collapsing them
would destroy all four.

`claim_evidence_links` stays N:M because **one** assertion may need several
spans to justify it — most obviously an inference drawn from three job postings
and a services page. That is one lineage with four links, not four claims.

### 4.2 The assertion fingerprint

```
assertion_fingerprint = sha256(
    subject_company_id,
    attribute_key,
    attribute_registry_version,
    canonical_json(value),
    fact_type,
    period_granularity,
    observed_at,
    lineage_key            -- sorted, distinct artifact ids
)
```

`company_claims` gains a nullable `assertion_fingerprint` column with a
**partial unique index WHERE NOT NULL**, so M2 claims are wholly unaffected.

The fingerprint deliberately **excludes the extractor**. That single choice
gives all four required behaviours:

| Situation | Result |
| --- | --- |
| Same extraction rerun | Same fingerprint → no new claim |
| **Newer extractor, same lineage, same value** | Same fingerprint → **no new claim**; a new `claim_evidence_link` is appended to the existing claim |
| Newer extractor, same lineage, **different** value | Different fingerprint → new claim; the disagreement is between two claims over one lineage, which is real and visible |
| Two independent sources, same value | Different `lineage_key` → **two claims** |
| Contradictory values | Different fingerprint → separate claims, never merged |

The second row is the one revision 1 got wrong. Its D4 created new claims on
every extractor upgrade, so re-extracting one page three times looked like
three-fold corroboration. Corroboration is now **counted over distinct
lineages**, and a lineage has exactly one claim per asserted value by
construction, so double-weighting is not merely avoided by convention — it is
unrepresentable (M3-ADR-017).

### 4.3 Projection precedence

Unchanged from revision 1 except that grouping is by
`(attribute_key, canonical value)` **over distinct lineages**:

1. Higher fact type: `FACT > ESTIMATE > PROXY > INFERENCE > HYPOTHESIS`
2. Higher source trust tier
3. More recent `source_published_at` (NULLs last)
4. More recent earliest `retrieved_at` across the lineage
5. Lowest claim id — deterministic tiebreak

`corroborating_lineage_count` is the number of distinct lineages asserting the
winning value.

---

## 5. Changes to M2-owned objects

Three, all additive. None alters an existing column, CHECK or index.

| Object | Change | Why |
| --- | --- | --- |
| `attribute_definitions` | `owner_milestone VARCHAR(8) NULL`, backfilled `'M2'` | Separates the M3 taxonomy and lets the trigger below tell them apart |
| `company_claims` | `assertion_fingerprint CHAR(64) NULL` + partial unique index `WHERE assertion_fingerprint IS NOT NULL` | Makes duplicate assertions unrepresentable (§4.2) |
| `company_claims` | Deferred constraint trigger | Rejects, at COMMIT, an M3-owned claim with no `claim_evidence_links` row |

M3 claims use the existing `subject_company_id` attribution path, which already
satisfies `exactly_one_attribution_path`.

---

## 6. Tables deliberately not created

| Candidate | Verdict |
| --- | --- |
| `research_queries` | **Not created.** Discovery context lives on `research_source_discoveries.discovery_context`; a separate table would duplicate the attempt's parameters |
| A separate M3 claims table | **Not created.** One ledger (M3-ADR-002) |
| `research_artifact_versions` | **Removed in revision 2.** It conflated retrieval, bytes, semantics and text; those are now §3.6–§3.10 |
| `REBUILD_RESEARCH_PROFILE` job | **Not created.** Synchronous derived rebuild |

---

## 7. Index plan

```
operational_research_runs      UNIQUE (company_id, research_policy_version,
                                       target_set_hash)

operational_research_attempts  UNIQUE (run_id, attempt_number)
                               UNIQUE (run_id) WHERE status NOT IN
                                      ('COMPLETED','PARTIAL','FAILED')
                               INDEX  (status) WHERE status NOT IN
                                      ('COMPLETED','FAILED','PARTIAL')

research_sources               UNIQUE (normalized_locator, locator_policy_version)
                               INDEX  (registrable_domain)

research_source_discoveries    UNIQUE (source_id, attempt_id, discovery_method,
                                       discovered_from_source_id)
                               INDEX  (attempt_id)

research_source_edges          UNIQUE (from_source_id, to_source_id,
                                       relation_type, observed_by_fetch_event_id)
                               INDEX  (to_source_id)

research_fetch_events          INDEX  (source_id, retrieved_at DESC)
                               INDEX  (attempt_id)
                               INDEX  (body_id) WHERE body_id IS NOT NULL
                               INDEX  (fetch_outcome) WHERE fetch_outcome <> 'OK'

research_artifact_bodies       UNIQUE (raw_body_sha256)
                               INDEX  (body_retention) WHERE body_retention='RETAINED'

research_artifact_derivations  UNIQUE (body_id, canonicalization_strategy,
                                       canonicalization_version)
                               INDEX  (artifact_id)

research_artifacts             UNIQUE (canonicalization_strategy,
                                       canonicalization_version,
                                       canonical_content_hash)

research_text_derivations      UNIQUE (body_id, text_extraction_policy_version)

research_extractions           UNIQUE (text_derivation_id, extractor_id,
                                       extractor_version, prompt_template_version)
                               INDEX  (attempt_id)

claim_evidence_links           UNIQUE (claim_id, extraction_id, locator_hash)
                               INDEX  (artifact_id)
                               INDEX  (extraction_id)

operational_research_gaps      UNIQUE (company_id, attribute_key,
                                       research_policy_version, gap_kind)

operational_research_gap_events   UNIQUE (gap_id, event_kind, attempt_id, source_id)
                                  INDEX  (gap_id, occurred_at DESC)

identity_review_signals        UNIQUE (company_id, signal_kind,
                                       related_company_id, evidence_digest)

identity_review_signal_events  UNIQUE (signal_id, status, occurred_at)
                               INDEX  (signal_id, occurred_at DESC)

operational_research_profiles  PK (company_id)

company_claims (M2)            UNIQUE (assertion_fingerprint)
                                 WHERE assertion_fingerprint IS NOT NULL
```

---

## 8. Views

Everything time-varying or derived-from-events lives in a view.

| View | Purpose |
| --- | --- |
| `current_operational_research_profiles` | Profile + staleness against `now()` |
| `current_operational_research_gaps` | Gap + derived status, attempt count, last attempt, resolving claim |
| `current_research_source_edges` | Latest edge per `(from, to, relation_type)` |
| `research_source_retrieval_history` | Fetch events per source, newest first |
| `current_operational_research_attempts` | Latest attempt per logical run |
| `unresolved_identity_review_signals` | Signals whose latest event is not `ACTIONED`/`DISMISSED` |

---

## 9. Ownership summary

| Table | Owner | Mutability | Truncatable |
| --- | --- | --- | --- |
| `operational_research_runs` | M3 | identity | No |
| `operational_research_attempts` | M3 | stateful | No |
| `research_sources` | M3 | identity | No |
| `research_source_discoveries` | M3 | append-only | No |
| `research_source_edges` | M3 | append-only | No |
| `research_fetch_events` | M3 | append-only | No |
| `research_artifact_bodies` | M3 | append-only + one-way prune | No |
| `research_artifact_derivations` | M3 | append-only | No |
| `research_artifacts` | M3 | identity | No |
| `research_text_derivations` | M3 | append-only + one-way prune | No |
| `research_extractions` | M3 | append-only + one-way prune | No |
| `claim_evidence_links` | M3 | append-only | No |
| `operational_research_gaps` | M3 | identity | No |
| `operational_research_gap_events` | M3 | append-only | No |
| `identity_review_signals` | M3 | identity | No |
| `identity_review_signal_events` | M3 | append-only | No |
| `operational_research_profiles` | M3 | projection | **Yes** |
| `companies`, `company_claims`, `company_domains` | M2 | unchanged | per M2 |
| `attribute_definitions`, `discovery_jobs` | M2 | unchanged | per M2 |
