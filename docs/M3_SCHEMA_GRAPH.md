# M3 — Proposed Schema Graph

**Status:** design, revision 3. **Not implemented.** No table below exists.

Companion to [M3_OPERATIONAL_RESEARCH_DESIGN.md](M3_OPERATIONAL_RESEARCH_DESIGN.md).

Revision 2 resolved six structural contradictions in revision 1, under the
rule **an append-only row may not contain a value that changes.**

Revision 3 adds a second rule and applies both uniformly
(M3-ADR-020 … 028):

> **A globally deduplicated identity row may not carry a fact that belongs to
> one of the many contexts that produced it** — and every provenance walk must
> be single-valued.

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
  (per attempt, per context)       (locator identity)   (evidence required)
            │                            │
            │                            ▼
            │                  research_fetch_events     every retrieval;
            │                  declared_content_type     success or failure
            │                            │ 0..1 on success
            │                            ▼
            │                  research_artifact_bodies  PURE byte identity
            │                  UNIQUE (raw_body_sha256)  hash · bytes · retention
            │                    │        │         └──▶ research_body_classifications
            │      ┌─────────────┘        └──────────────┐   (sniffed type, versioned)
            │      ▼                                     ▼
            │  research_artifact_derivations    research_text_derivations
            │  (body × canonicalization)        (body × text+redaction contract)
            │      │                                     │
            │      ▼                                     ▼
            │  research_artifacts                research_extractions
            │  semantic identity                 (derivation × contract hash)
            │      │                                     │      ▲
            │      │                                     │      │ CREATED/REUSED
            │      │                                     │  research_attempt_extractions
            │      └──────────┐        ┌─────────────────┘
            │                 ▼        ▼
            │            claim_evidence_links
            │            binds ONE extraction to ONE fetch event,
            │            composite FKs prove both name the same body
            │                          │
            │                          ▼
            │            company_claims  (M2-owned ledger)
            │            + assertion_fingerprint
            ▼
  operational_research_gaps ──▶ operational_research_gap_events
  (identity only)                (ATTEMPTED names its fetch_event)

  identity_review_signals ──▶ identity_review_signal_events   (legal transitions)
  (identity only)          └─▶ identity_review_signal_evidence (the promised links)

  operational_research_profiles       PROJECTION, truncatable, no clock

  discovery_jobs (M2-owned queue) ── reused unchanged, no new queue
```

**Twenty new tables.** Three M2 objects are touched, all additively (§5).

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
| Natural uniqueness | `UNIQUE (research_plan_hash)` |
| Key FKs | `company_id → companies` (RESTRICT), `vertical_id → verticals` (RESTRICT, nullable — but inside the hash, see below) |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `company_id`, `research_policy_version`, `vertical_id`,
`target_attribute_keys` (sorted text[]), `plan_inputs` (JSONB — canonicalized),
`research_plan_hash`, `created_by`.

```
research_plan_hash = sha256(canonical_json({
    company_id,
    research_policy_version,
    target_attribute_keys,      -- sorted
    vertical_id,                -- affects attribute applicability
    plan_inputs                 -- immutable, canonicalized
}))
```

**Every input that changes the question is inside the hash.** Revision 2 keyed
the run on `(company_id, research_policy_version, target_set_hash)` while the
immutable row also carried `vertical_id` and `seed_inputs` — so two runs
differing only in vertical resolved to one already-existing immutable row, and
the second set of values was silently discarded (M3-ADR-025).

**Execution inputs moved out.** Seeds that are discovered rather than declared —
the company's currently projected identity domains, a human seed added later —
are properties of an *execution*, not of the question, and now live on the
attempt as `attempt_seed_inputs`. Re-running the same question next month with
a different projected domain set is the same question and a new attempt.

The rule this enforces: **no immutable column may legitimately differ while its
natural key stays identical.**

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
| Natural uniqueness | `UNIQUE NULLS NOT DISTINCT (source_id, attempt_id, discovery_method, discovered_from_source_id, discovery_context_hash)` |
| Key FKs | `source_id → research_sources` (RESTRICT), `attempt_id → operational_research_attempts` (RESTRICT), `discovered_from_source_id → research_sources` (RESTRICT, nullable) |
| Truncatable | No |
| Temporal | `discovered_at` |

Columns: `discovery_method` (`SITEMAP | CRAWL_LINK | SEARCH | JOB_BOARD |
REGISTRY | HUMAN_SEED | API`), `discovery_context` (JSONB — the query, the
anchor text, the sitemap URL), `discovery_context_hash` (sha256 of the
canonicalized context), `relevance_hint`.

One source found by both a sitemap and a search engine yields two discoveries.
**So does one source found by two different search queries in one attempt**:
revision 2's key omitted the context, so the second query's provenance was
discarded — and "which query surfaced this page" is exactly what makes a
discovery method evaluable (M3-ADR-026).

`NULLS NOT DISTINCT` throughout: PostgreSQL's default treats two NULL parents
as different values, which would let the same root-level discovery be recorded
twice.

### 3.5 `research_source_edges` — relationships between locators

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE NULLS NOT DISTINCT (from_source_id, to_source_id, relation_type, observed_by_fetch_event_id)` |
| Key FKs | `from_source_id`, `to_source_id → research_sources` (RESTRICT), `observed_by_fetch_event_id`, `corroborating_fetch_event_id → research_fetch_events` (RESTRICT, both nullable per `edge_origin`) |
| Truncatable | No |
| Temporal | `observed_at` |

`relation_type ∈ {REDIRECTS_TO, DECLARES_CANONICAL, LANGUAGE_VARIANT_OF,
MIRROR_CANDIDATE}`.

`edge_origin ∈ {FETCH_OBSERVED, DERIVED, HUMAN_ASSERTED}`, with a CHECK making
"every edge has evidence" true in the schema rather than only in prose
(M3-ADR-027):

```sql
CHECK (
  (edge_origin = 'FETCH_OBSERVED'
     AND observed_by_fetch_event_id IS NOT NULL
     AND corroborating_fetch_event_id IS NULL)
  OR
  (edge_origin = 'DERIVED'
     AND observed_by_fetch_event_id IS NOT NULL
     AND corroborating_fetch_event_id IS NOT NULL)
  OR
  (edge_origin = 'HUMAN_ASSERTED'
     AND asserted_by IS NOT NULL AND rationale IS NOT NULL)
)
CHECK (relation_type <> 'MIRROR_CANDIDATE' OR edge_origin <> 'FETCH_OBSERVED')
```

`REDIRECTS_TO` and `DECLARES_CANONICAL` are `FETCH_OBSERVED`: one retrieval saw
them. `MIRROR_CANDIDATE` is **`DERIVED` by construction** — it is the inference
"these two locators served the same body", which no single fetch can witness —
so it carries **both** fetch events, and the two observations supporting it are
recoverable by reading them. Revision 2 left the fetch event nullable for every
type while claiming in prose that every edge carries one.

An edge is an observation, not a mutation. Learning about a redirect six months
after a source was created appends an edge; a changed redirect target appends a
second, and both survive. **Sources are never merged.**

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
TRANSPORT_ERROR`), `http_status`, `final_url`, **`declared_content_type`**,
`content_length`, `etag`, `last_modified`, `request_headers_digest`,
`error_class`, `error_detail`, `duration_ms`.

Additional uniqueness for composite-FK targets: `UNIQUE (id, body_id)`, used
by `claim_evidence_links` to bind a claim to one exact retrieval (§3.12).

`declared_content_type` lives **here** and not on the body: what a server
claims the bytes are is a property of the HTTP exchange, and the identical byte
string can be served as `text/plain` by one host and `text/html` by another
(M3-ADR-023).

**This is the table revision 1 promised and never declared.** Acceptance A1
required that identical bytes produce "one new retrieval event" and no new
version, but the only place a retrieval time could live was a row unique on
`(source_id, raw_body_sha256)` — so the second retrieval was unrecordable
(M3-ADR-014).

Deliberately **not** unique on anything: *"we saw these exact bytes on Sep 1,
Sep 8 and Sep 20"* is three rows, all pointing at one body. A failed fetch is a
row with `body_id IS NULL`, so "we tried eleven times and it 403'd" is equally
answerable — and neither requires touching evidence.

### 3.7 `research_artifact_bodies` — byte identity, and nothing contextual

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only + one-way prune |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (raw_body_sha256)` — **global** |
| Key FKs | none |
| Truncatable | No |
| Temporal | `first_seen_at` |

Columns: `raw_body_sha256`, `raw_body` (nullable), `byte_length`,
`body_retention` (`RETAINED | PRUNED`), `pruned_at`, `first_seen_at`.

**No `declared_content_type`, no `sniffed_content_type`.** A globally
deduplicated identity row may not carry a fact belonging to one of the many
retrievals that produced it. Revision 2 stored both on the body, so the same
bytes served as `text/plain` by one host and `text/html` by another had to
pick one and discard the other (M3-ADR-023).

* **Declared** media type → `research_fetch_events`, one per retrieval.
* **Sniffed** media type → `research_body_classifications` (§3.7a), because
  content sniffing is an algorithm whose answer changes as the algorithm
  improves, and a derived value must name the versioned contract that produced
  it.

What remains is exactly what a byte string *is*: its hash, its bytes, its
length and whether we still hold it.

The one-way prune (M3-ADR-008) is unchanged: the trigger permits only
`raw_body` non-NULL → NULL with `body_retention → 'PRUNED'` and `pruned_at →
now`, and rejects every other UPDATE.

### 3.7a `research_body_classifications` — sniffed type, under a named contract

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (body_id, classifier_policy_version)` |
| Key FKs | `body_id → research_artifact_bodies` (RESTRICT) |
| Truncatable | No — it is recomputable only while the bytes are retained |
| Temporal | `classified_at` |

Columns: `classifier_policy_version`, `sniffed_media_type`, `confidence`,
`encoding`.

A classifier upgrade appends a row; the old classification stays readable, and
an artifact derived under the old one remains interpretable.

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
| Natural uniqueness | `UNIQUE (body_id, text_derivation_contract_hash)`; plus `UNIQUE (id, body_id)` as a composite-FK target |
| Key FKs | `body_id → research_artifact_bodies` (RESTRICT) |
| Truncatable | No |
| Temporal | `derived_at` |

Columns: `text_extraction_policy_version`, `redaction_policy_version`,
`text_derivation_contract_hash`, `extracted_text` (nullable), `page_offsets`
(JSONB, for PDFs), `text_retention`, `pruned_at`, `status`, `error`.

```
text_derivation_contract_hash = sha256(canonical_json({
    text_extraction_policy_version,
    redaction_policy_version
}))
```

Revision 2 keyed this table on the text policy alone while storing the
redaction policy beside it — so a redaction upgrade over unchanged bytes
collided with the existing row and was representable only by an UPDATE. Both
versions now sit inside the key, so a redaction change produces a second
derivation and leaves the first untouched (M3-ADR-022).

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

### 3.11 `research_extractions` — an immutable extraction *result*

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only + one-way prune of `raw_output` |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (text_derivation_id, extraction_contract_hash)`; plus `UNIQUE (id, body_id)` as a composite-FK target |
| Key FKs | `text_derivation_id → research_text_derivations` (RESTRICT), composite `(text_derivation_id, body_id) → research_text_derivations (id, body_id)` |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `extractor_kind` (`RULE | PARSER | MODEL | HUMAN`), `extractor_id`,
`extractor_version`, `model_provider`, `model_name`, `model_version`,
`prompt_template_version`, `output_schema_version`, `determinism`,
`temperature`, `extraction_contract_hash`, `body_id` (denormalised, held
consistent by the composite FK), `extractor_confidence`, `observations`
(JSONB), `status`, `error`, `raw_output`, `raw_output_sha256`.

```
extraction_contract_hash = sha256(canonical_json({
    extractor_kind, extractor_id, extractor_version,
    model_provider, model_name, model_version,
    prompt_template_version, output_schema_version,
    determinism, temperature
}))
```

**No `attempt_id`.** Revision 2 keyed this table on four fields while storing
one attempt id, so when attempt 2 legitimately reused the extraction attempt 1
had produced, "which attempts used this?" was unanswerable and the row still
pointed only at attempt 1 (M3-ADR-021). Usage is now §3.11a.

The contract hash closes a second hole: revision 2's key omitted the model
provider, name, version, output schema, determinism and temperature while
storing all six. A model upgrade under one `extractor_version` therefore
**collided** with the existing row and was representable only by an UPDATE
(M3-ADR-022).

### 3.11a `research_attempt_extractions` — which execution used which result

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (attempt_id, extraction_id)` |
| Key FKs | `attempt_id → operational_research_attempts` (RESTRICT), `extraction_id → research_extractions` (RESTRICT) |
| Truncatable | No |
| Temporal | `used_at` |

Columns: `usage_role` (`CREATED | REUSED`), `used_at`.

Attempt 1 creates extraction E and records `CREATED`. Attempt 2 reuses it and
records `REUSED`. **One extraction output, two usage rows** — no duplicated
output merely to capture usage, and "which attempts used this?" is a single
indexed read.

**Audit of the same pattern across every table.** Each immutable reusable
object was checked for a single-attempt field it should not own:

| Table | Verdict |
| --- | --- |
| `research_extractions` | **Defect — fixed here.** Reusable across attempts |
| `research_artifact_bodies`, `research_artifacts`, `research_sources` | Clean; never carried an attempt |
| `research_artifact_derivations`, `research_text_derivations`, `research_body_classifications` | Clean; pure functions of `(body, contract)`. Their usage is recoverable through the extractions that read them, so a second association table would add rows without adding answers |
| `research_fetch_events`, `research_source_discoveries`, `operational_research_gap_events` | Correct as-is: each **is** an execution event and belongs to exactly one attempt |

### 3.12 `claim_evidence_links` — one link, one exact observation

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (claim_id, extraction_id, fetch_event_id, locator_hash)` |
| Key FKs | `claim_id → company_claims` (RESTRICT); composite `(extraction_id, body_id) → research_extractions (id, body_id)`; composite `(fetch_event_id, body_id) → research_fetch_events (id, body_id)`; `artifact_id → research_artifacts` (RESTRICT); `source_id → research_sources` (RESTRICT) |
| Truncatable | No |
| Temporal | `created_at` |

Columns: `extraction_id`, `fetch_event_id`, `body_id`, `artifact_id`,
`source_id`, `locator` (JSONB), `locator_hash`, `quote`, `quote_sha256`,
`support_kind` (`DIRECT_STATEMENT | DERIVED | CORROBORATING`),
`source_class`, `trust_policy_version`, `trust_tier` (§4.4).

**The two composite foreign keys are the fix for revision 2's ambiguous
provenance.** A body is reachable from many fetch events — several URLs,
several attempts, several companies, several times — so a walk that went
`extraction → text derivation → body → fetch events` fanned out to *all*
retrievals of those bytes and could not say which one supplied the evidence
(M3-ADR-020).

The link now names the exact fetch event, and the database proves the two
halves agree: `body_id` is carried on the link, on the extraction and on the
fetch event, and both composite FKs force all three to be the same body. A link
citing an extraction over body X and a retrieval of body Y is unrepresentable
rather than merely unlikely — the same declarative pattern M2 uses in
`fk_supersedes_same_entity`.

The provenance walk is therefore **single-valued at every hop**:

```
claim → link → extraction → text derivation → body
             ↘ fetch event → source
             ↘ artifact  (semantic identity)
```

`artifact_id` and `source_id` are denormalised for indexed lineage grouping
(§4); both are derivable from the composite-FK chain, and a CHECK-equivalent
trigger verifies they agree with it.

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
| Natural uniqueness | `UNIQUE NULLS NOT DISTINCT (gap_id, event_kind, attempt_id, source_id, fetch_event_id)` |
| Key FKs | `gap_id → operational_research_gaps` (RESTRICT), `attempt_id → operational_research_attempts` (RESTRICT), `source_id → research_sources` (RESTRICT, nullable), `fetch_event_id → research_fetch_events` (RESTRICT, nullable), `resolved_by_claim_id → company_claims` (RESTRICT, nullable) |
| Truncatable | No |
| Temporal | `occurred_at` |

`event_kind ∈ {RAISED, ATTEMPTED, RESOLVED, ABANDONED}`.

`ATTEMPTED` means **one concrete evidence-acquisition attempt**, so it names
the `fetch_event_id` that performed it and that id is part of the key. Revision
2 keyed on `(gap_id, event_kind, attempt_id, source_id)`, which permitted
exactly one `ATTEMPTED` row per source per execution — so fetching a source
three times inside one attempt recorded one event, and `last_attempt_at`
reported the *first* retrieval while the design claimed eleven attempts append
eleven rows (M3-ADR-024).

Legal transitions, trigger-enforced:

```
RAISED → ATTEMPTED* → RESOLVED | ABANDONED
terminal: RESOLVED, ABANDONED — never re-entered
```

A gap that goes stale later is a **different gap row**, because `gap_kind` is
part of the parent's identity, so reopening is never needed.

Derived by view, never stored:

| Derived | From |
| --- | --- |
| `attempt_count` | `count(*) WHERE event_kind='ATTEMPTED'` — one per retrieval |
| `attempted_source_count` | `count(DISTINCT source_id) WHERE event_kind='ATTEMPTED'` |
| `last_attempt_at` | `max(occurred_at) WHERE event_kind='ATTEMPTED'` |
| `current_status` | the latest event's kind |
| `resolved_by_claim_id` | the latest `RESOLVED` event's claim |

Eleven retrievals now genuinely append eleven rows, and the parent stays
byte-identical.

### 3.15 `identity_review_signals` — identity only

| Property | Value |
| --- | --- |
| Owner | **M3** — written by M3, consumed by M2's review queue |
| Mutability | **identity** |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE NULLS NOT DISTINCT (company_id, signal_kind, related_company_id, evidence_digest)` |
| Key FKs | `company_id`, `related_company_id → companies` (RESTRICT) |
| Truncatable | No |
| Temporal | `raised_at` |

### 3.15a `identity_review_signal_evidence` — the evidence the signal promised

| Property | Value |
| --- | --- |
| Owner | M3 |
| Mutability | append-only |
| PK | `id` (uuid) |
| Natural uniqueness | `UNIQUE (signal_id, claim_evidence_link_id)` |
| Key FKs | `signal_id → identity_review_signals` (RESTRICT), `claim_evidence_link_id → claim_evidence_links` (RESTRICT) |
| Truncatable | No |
| Temporal | `created_at` |

The design and acceptance H4 both say an identity conflict raises a signal
**with evidence**; revision 2's schema gave the signal only identity and state
fields, so a reviewer received an assertion with nothing to check
(M3-ADR-028).

New evidence for an existing signal **appends a link**. Deliberately a table
rather than a JSON array of ids on the signal: a mutable list on an identity
row is the same defect as revision 1's gap counters, and a real FK means
evidence cannot be deleted out from under a signal.

`related_company_id` is nullable — a `POSSIBLE_CEASED_TRADING` signal names no
second company — so the parent's uniqueness is `NULLS NOT DISTINCT`. Plain
PostgreSQL uniqueness treats two NULLs as different values, which would have
let the identical signal be raised on every research run forever.

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

Legal transitions, trigger-enforced at the database boundary rather than left
to whatever writes the row:

```
OPEN → ACKNOWLEDGED → ACTIONED | DISMISSED
OPEN → DISMISSED
terminal: ACTIONED, DISMISSED — never re-entered
```

`ACTIONED → OPEN` and `DISMISSED → ACKNOWLEDGED` are rejected. Revision 2
listed the four statuses and no invariant, so both were legal. If a signal
genuinely needs reopening, the answer is a **new signal** carrying the new
evidence — its `evidence_digest` differs, so the identity key admits it —
rather than resurrecting a closed review and losing the record that it was
closed (M3-ADR-027).

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

### 4.2 Evidence origin, lineage and the assertion fingerprint

An **evidence origin** is the pair that identifies a concrete observation:

```
evidence_origin = (source_id, artifact_id)
```

A **lineage** is the sorted set of distinct evidence origins justifying one
assertion.

Revision 2 defined the lineage as *artifact ids alone*, which collapsed two
genuinely different observations: source A serving artifact X and source B
serving artifact X produced one lineage and therefore one claim — even though
the two may carry different trust, publication context and dates, all of which
the design elsewhere insists must not be merged (M3-ADR-020).

```
assertion_fingerprint = sha256(canonical_json({
    subject_company_id, attribute_key, attribute_registry_version,
    value, fact_type, period_granularity, observed_at,
    lineage_key                 -- sorted [(source_id, artifact_id), …]
}))
```

Still excludes the extractor, so a newer extractor agreeing with an older one
over the same origins appends an evidence link rather than a twin.

### 4.2a Independence — deliberately conservative

Two lineages **corroborate independently** only when both hold:

1. **Different publisher.** `publisher_key(source)` differs.
2. **Different document.** `artifact_id` differs.

`publisher_key` is derived under a versioned policy
(`publisher_policy_version`): the source's registrable domain, unless an
override maps it — a job board, a registry and a directory are publishers in
their own right, not the company.

The two rules produce the behaviours the brief requires:

| Situation | Same artifact? | Same publisher? | Independent? |
| --- | --- | --- | --- |
| Company page mirrored at two URLs | Yes | Yes | **No** — one lineage's worth of weight |
| Company `/about` and `/services` both stating it | No | Yes | **No** — one publisher saying it twice |
| Company website + government registry | No | No | **Yes** |
| Company website + third-party scrape of the same exact text | **Yes** | No | **No** — identical canonical content is the same document copied, not a second witness |

The third row is the one that matters and the reason rule 2 exists. A scraper
republishing a company's sentence verbatim produces the *same artifact*,
because artifact identity is the canonical content hash. Requiring a different
document as well as a different publisher makes "we found it twice on the
internet" incapable of masquerading as corroboration.

The policy is conservative by design: it will occasionally refuse to count two
genuinely independent witnesses that happen to have published identical text.
Under-counting corroboration is a recoverable error; over-counting it inflates
confidence in a claim that rests on one source, which is the failure this
system exists to prevent.

`corroborating_publisher_count` is the size of the largest set of pairwise
independent lineages asserting the winning value. It replaces revision 2's
`corroborating_lineage_count`, which counted lineages without an independence
test.

### 4.3 Projection precedence

Unchanged from revision 1 except that grouping is by
`(attribute_key, canonical value)` **over distinct lineages**:

1. Higher fact type: `FACT > ESTIMATE > PROXY > INFERENCE > HYPOTHESIS`
2. Higher `trust_tier` — the value **recorded on the claim's evidence links at
   assertion time**, never recomputed (§4.4)
3. More recent `source_published_at` (NULLs last)
4. More recent earliest `retrieved_at` across the lineage
5. Lowest claim id — deterministic tiebreak

`corroborating_publisher_count` (§4.2a) is projected beside the winning value,
as is `contradiction`.

### 4.4 Source trust provenance

`company_claims.confidence` is computed from evidence type and source trust.
Revision 2 named no versioned trust input anywhere, so a persisted confidence
could not be explained or reproduced once the trust policy changed — the
calibration being uncalibrated is acceptable, a persisted number nobody can
reconstruct is not (M3-ADR-028).

Every `claim_evidence_links` row therefore records the inputs used **at
assertion time**:

| Column | Meaning |
| --- | --- |
| `source_class` | the evidence class, e.g. `COMPANY_OWN_SITE`, `GOVERNMENT_REGISTRY`, `JOB_BOARD`, `THIRD_PARTY_DIRECTORY` |
| `trust_policy_version` | the trust table in force when the claim was asserted |
| `trust_tier` | the numeric tier that policy assigned to that class |

These are **facts about an assertion event**, not configuration lookups, so
they are frozen on the append-only link. A trust recalibration ships a new
`trust_policy_version`; historical claims keep the tier that produced their
stored confidence, and re-asserting under the new policy creates *new* claims
whose links carry the new version. Nothing silently reinterprets a number
already written.

Extractor confidence remains entirely separate and is still not an input
(M3-ADR-004).

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
operational_research_runs      UNIQUE (research_plan_hash)
                               INDEX  (company_id)

operational_research_attempts  UNIQUE (run_id, attempt_number)
                               UNIQUE (run_id) WHERE status NOT IN
                                      ('COMPLETED','PARTIAL','FAILED')

research_sources               UNIQUE (normalized_locator, locator_policy_version)
                               INDEX  (registrable_domain)

research_source_discoveries    UNIQUE NULLS NOT DISTINCT
                                      (source_id, attempt_id, discovery_method,
                                       discovered_from_source_id,
                                       discovery_context_hash)
                               INDEX  (attempt_id)

research_source_edges          UNIQUE NULLS NOT DISTINCT
                                      (from_source_id, to_source_id,
                                       relation_type, observed_by_fetch_event_id)
                               INDEX  (to_source_id)

research_fetch_events          UNIQUE (id, body_id)          -- composite FK target
                               INDEX  (source_id, retrieved_at DESC)
                               INDEX  (attempt_id)
                               INDEX  (body_id) WHERE body_id IS NOT NULL
                               INDEX  (fetch_outcome) WHERE fetch_outcome <> 'OK'

research_artifact_bodies       UNIQUE (raw_body_sha256)
                               INDEX  (body_retention) WHERE body_retention='RETAINED'

research_body_classifications  UNIQUE (body_id, classifier_policy_version)

research_artifact_derivations  UNIQUE (body_id, canonicalization_strategy,
                                       canonicalization_version)
                               INDEX  (artifact_id)

research_artifacts             UNIQUE (canonicalization_strategy,
                                       canonicalization_version,
                                       canonical_content_hash)

research_text_derivations      UNIQUE (body_id, text_derivation_contract_hash)
                               UNIQUE (id, body_id)          -- composite FK target

research_extractions           UNIQUE (text_derivation_id, extraction_contract_hash)
                               UNIQUE (id, body_id)          -- composite FK target

research_attempt_extractions   UNIQUE (attempt_id, extraction_id)
                               INDEX  (extraction_id)

claim_evidence_links           UNIQUE (claim_id, extraction_id, fetch_event_id,
                                       locator_hash)
                               INDEX  (source_id, artifact_id)   -- lineage grouping
                               INDEX  (extraction_id)
                               INDEX  (fetch_event_id)

operational_research_gaps      UNIQUE (company_id, attribute_key,
                                       research_policy_version, gap_kind)

operational_research_gap_events   UNIQUE NULLS NOT DISTINCT
                                         (gap_id, event_kind, attempt_id,
                                          source_id, fetch_event_id)
                                  INDEX  (gap_id, occurred_at DESC)

identity_review_signals        UNIQUE NULLS NOT DISTINCT
                                      (company_id, signal_kind,
                                       related_company_id, evidence_digest)

identity_review_signal_evidence   UNIQUE (signal_id, claim_evidence_link_id)

identity_review_signal_events  UNIQUE (signal_id, status, occurred_at)
                               INDEX  (signal_id, occurred_at DESC)

operational_research_profiles  PK (company_id)

company_claims (M2)            UNIQUE (assertion_fingerprint)
                                 WHERE assertion_fingerprint IS NOT NULL
```

**NULL semantics are explicit everywhere a nullable column participates in a
unique key.** PostgreSQL's default treats two NULLs as distinct, which silently
disables the constraint exactly where duplicates are most likely: a root-level
discovery with no parent, a signal with no related company, a gap event with no
source. Every such key is declared `NULLS NOT DISTINCT` (PostgreSQL 15+, and
this project runs 16).

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
| `research_body_classifications` | M3 | append-only | No |
| `research_artifact_derivations` | M3 | append-only | No |
| `research_artifacts` | M3 | identity | No |
| `research_text_derivations` | M3 | append-only + one-way prune | No |
| `research_extractions` | M3 | append-only + one-way prune | No |
| `research_attempt_extractions` | M3 | append-only | No |
| `claim_evidence_links` | M3 | append-only | No |
| `operational_research_gaps` | M3 | identity | No |
| `operational_research_gap_events` | M3 | append-only | No |
| `identity_review_signals` | M3 | identity | No |
| `identity_review_signal_evidence` | M3 | append-only | No |
| `identity_review_signal_events` | M3 | append-only | No |
| `operational_research_profiles` | M3 | projection | **Yes** |
| `companies`, `company_claims`, `company_domains` | M2 | unchanged | per M2 |
| `attribute_definitions`, `discovery_jobs` | M2 | unchanged | per M2 |

## 10. Terminal states and legal transitions

Every state machine in M3 declares its legal transitions and enforces them in
the database, so "terminal" is a property rather than a convention.

| Object | Transitions | Terminal |
| --- | --- | --- |
| `operational_research_attempts` | `PENDING → DISCOVERING → FETCHING → EXTRACTING → ASSERTING → COMPLETED`; any non-terminal → `PARTIAL` / `FAILED` | `COMPLETED`, `PARTIAL`, `FAILED` |
| `operational_research_gap_events` | `RAISED → ATTEMPTED* → RESOLVED \| ABANDONED` | `RESOLVED`, `ABANDONED` |
| `identity_review_signal_events` | `OPEN → ACKNOWLEDGED → ACTIONED \| DISMISSED`; `OPEN → DISMISSED` | `ACTIONED`, `DISMISSED` |

Reopening is never the answer. A gap that later goes stale is a different
`gap_kind` and therefore a different gap row; a signal that needs revisiting
carries new evidence and therefore a different `evidence_digest`, so the
identity key admits it as a new signal. In both cases the closed record stays
closed and the history stays readable.
