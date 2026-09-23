# M3 — Operational Research

**Status:** design, revision 1. **Not implemented.** No M3 runtime code,
migrations or tables exist in this repository.

**Milestone position.** M1 answers *which market × vertical × ICP × channel
contexts are worth pursuing*. M2 answers *which real commercial organizations
exist in a chosen context, and are we sure they are distinct*. M3 answers the
next question, and only that one:

> **What do we actually know about how this canonical company operates, and
> what remains unknown?**

M3 does **not** decide whether BoRo should sell to the company. That is M4.

---

## 0. One sentence

**M3 is an evidence-acquisition and operational-fact system for canonical
companies: it turns a company id into captured, immutable, traceable evidence
about how that company operates, and into an explicit account of what is still
unknown.**

The pipeline:

```
canonical company (M2)
   ↓ research plan: which operational attributes to pursue
source discovery            ← candidates, never facts
   ↓
artifact capture            ← immutable, byte-faithful, versioned
   ↓
extraction                  ← stamped with extractor and locator
   ↓
observations                ← what the source says, quoted
   ↓
typed company claims        ← what we assert, with evidence links
   ↓
research gaps + coverage    ← what is still unknown, explicitly
```

Every arrow is append-only. Nothing downstream may rewrite anything upstream.

---

## 1. What M3 is not

M3 is not a scraper, not a qualification engine and not a people database.

| Not M3 | Why | Owner |
| --- | --- | --- |
| "Is this a good lead?" | Requires commercial judgement over evidence | M4 |
| `lead_score`, `icp_fit`, `pain_score`, `recommend_contact` | Same | M4 |
| Buyer/contact discovery, person profiles | Different subject entity, different privacy regime | M5 |
| Creating or merging canonical companies | Identity is M2's, and M2 alone's | M2 |
| Outbound, campaigns, CRM sync | Downstream of qualification | M6+ |

**The load-bearing rule.** M3 may record that a company advertises 24/7
emergency service. It may record that this *implies* an on-call dispatch
requirement, as an `INFERENCE`. It may not record that this makes the company
a good prospect, because that sentence is not about the company — it is about
BoRo.

---

## 2. Evidence semantics, inherited unchanged

M3 reuses M0's five fact types verbatim. They are not extended.

| Type | In M3 this means |
| --- | --- |
| `FACT` | The source states it directly about itself, and the source is the company or an authority. "Our 42 technicians" on the company's own site. |
| `ESTIMATE` | A quantity the source itself presents as approximate: "50+ employees", "around 30 vans". |
| `PROXY` | The source evidences something adjacent and correlated. A job ad for a dispatch coordinator proxies for dispatch operations existing. |
| `INFERENCE` | We derived it by a stated, reproducible rule over other evidence. |
| `HYPOTHESIS` | We are recording a possibility to test. Never projected as current truth. |

**Absence.** `availability = 'NOT_AVAILABLE'` with a NULL value and a NULL
fact type, exactly as M0 and M2 already enforce. This is the rule M3 is most
likely to be pressured to break, because a research system is constantly
asked "do they use an FSM?" and the honest answer is usually "we do not know".

> **No evidence found is never `false`, never `0`, never "does not use", and
> never "does not have".** It is a research gap. §13.

A negative fact requires positive evidence of the negative — a page that says
"we do not offer emergency service" is a `FACT` that emergency service is
absent. Silence is not that page.

---

## 3. Source model: location identity is not content identity

M3 must never rely on a live URL. A URL is a *place we looked*, not a thing we
know. The model therefore separates three concepts that a naïve design
collapses into one:

| Concept | Table | Identity | Mutable? |
| --- | --- | --- | --- |
| **Where we looked** | `research_sources` | normalized locator | append-only; last-seen state in a projection |
| **What we got, semantically** | `research_artifacts` | canonical content hash + canonicalization contract | append-only |
| **What we got, literally** | `research_artifact_versions` | raw body hash under an artifact | append-only |

This is the same three-tier shape M2 uses for providers
(`provider_entities` / `provider_record_versions` / `provider_record_bodies`),
and it is reused deliberately: the problems are the same problems, and a second
vocabulary for them would be a cost with no benefit.

### 3.1 `research_sources` — where we looked

A source is a *normalized locator*, not a URL string. Normalization is
versioned (`locator_policy_version`) and strips what does not carry identity:

* scheme and case, trailing slash, default ports
* tracking parameters (`utm_*`, `gclid`, `fbclid`, `mc_cid`, session ids)
* fragment, unless the fragment is the document (SPA routes — see below)
* `www.` host prefix

It preserves what does carry identity: path, meaningful query parameters
(`?id=`, `?job=`), and the host.

Redirects are recorded, not followed silently: a source has an optional
`resolves_to_source_id`, so `/about-us` → `/company/about` is a fact about the
site, queryable later. The canonical URL a page declares (`<link rel=canonical>`)
is recorded as a separate edge; a page may lie about its canonical URL, so the
edge is evidence, not a merge instruction.

**Sources are never merged.** Two URLs serving the same bytes converge at the
*artifact* level, not the source level, because "these two places served the
same document" is a finding worth keeping.

### 3.2 `research_artifacts` — what we got, semantically

Identity is `(canonicalization_strategy, canonicalization_version,
canonical_content_hash)`. Carrying the strategy and its version *inside* the
identity key is not optional: a hash means nothing without the algorithm that
produced it, and M2 already learned this the expensive way (M2-ADR-028).

Canonicalization is **content-type specific and versioned**:

| Strategy | Applies to | Normalizes away | Preserves |
| --- | --- | --- | --- |
| `HTML_TEXT_V1` | `text/html` | scripts, styles, comments, attribute order, whitespace runs, nav/footer boilerplate, session tokens in markup | visible text, heading structure, link targets, structured data blocks |
| `PDF_TEXT_V1` | `application/pdf` | producer metadata, creation timestamps, object ordering | page-segmented text, page count |
| `JSON_CANONICAL_V1` | `application/json` | key order, whitespace | values, structure — reused verbatim from M0 |
| `PLAINTEXT_V1` | `text/plain` | line-ending style, trailing whitespace | text |

A cosmetic HTML change — a rotating testimonial, a build hash in a script tag,
a copyright year in the footer — must **not** create a new semantic artifact.
A changed sentence about emergency service must. That is exactly what the
canonicalization policy decides, which is why it is versioned and why the
version is part of identity: when the policy improves, old artifacts remain
interpretable under the policy that produced them, and the new policy produces
new artifacts rather than silently reinterpreting old ones.

### 3.3 `research_artifact_versions` — what we got, literally

One semantic artifact may have many byte-different versions: the same page
fetched on two days with a different build hash is one artifact, two versions.
Each version records:

`source_id`, `retrieved_at`, `http_status`, `final_url` (after redirects),
`content_type`, `content_length`, `raw_body_sha256`, `raw_body` (nullable, see
§27), `extracted_text`, `extraction_policy_version`, `language`,
`title`, `source_published_at`, `source_published_granularity`, `etag`,
`last_modified`.

**`source_published_at` is only ever populated when the source states it.** A
page with no date has `NULL` and granularity `UNDATED`. A job ad saying
"Posted March 2026" gets `2026-03-01` with granularity `MONTH`. A retrieval
timestamp is *never* copied into it. This is the single most common way an
evidence system starts lying, and §12 returns to it.

`source_published_granularity` extends M0's vocabulary with `MONTH`, which M0
did not need: `DATE | MONTH | YEAR | UNDATED`.

### 3.4 What this answers

The prompt's provenance questions, mapped:

| Question | Answered by |
| --- | --- |
| Where did this claim come from? | `claim_evidence_links → extraction → artifact_version → source` |
| What exact content was observed? | `raw_body` / `extracted_text` + `canonical_content_hash` |
| When did we retrieve it? | `artifact_version.retrieved_at` |
| What did the source itself date? | `artifact_version.source_published_at` + granularity, NULL when unstated |
| What content type? | `artifact_version.content_type`, validated against the strategy |
| What extraction read it? | `research_extractions` (§10) |
| Has the live page changed? | A later version under the same source with a different `raw_body_sha256`; a different artifact if the change was semantic |
| Which exact span supported the claim? | `claim_evidence_links.locator` (§9) |

---

## 4. The research run

A run is a reproducible unit of work against **one company** under **one
policy version**.

```
PENDING → DISCOVERING → FETCHING → EXTRACTING → ASSERTING → COMPLETED
                                                          ↘ PARTIAL
   any stage ────────────────────────────────────────────→ FAILED
```

Six states, not eight. `DISCOVERING` and `FETCHING` stay distinct because
discovery may succeed while every fetch fails, and the difference matters for
retry. `EXTRACTING` and `ASSERTING` stay distinct because re-extraction under
a new extractor version is a first-class operation (§10) that must not re-fetch.

`PARTIAL` is a terminal state meaning: some evidence was captured and asserted,
some planned work did not complete. It is not a failure — partial evidence is
still evidence — but it *is* visible in coverage.

### 4.1 The gate on canonical writes

M2 learned that a mutable `status` column cannot be the gate on canonical
writes, because a later stage overwrites it (M2-ADR-031). M3 inherits the
lesson rather than the defect:

* `discovery_completed_at`, `fetch_completed_at` and `extraction_completed_at`
  are durable timestamps, written once when that stage genuinely finishes.
* Claim assertion requires `extraction_completed_at IS NOT NULL`, unless the
  run was created with `allow_partial_assertion = true`.
* `status` is descriptive. No guard reads it.

### 4.2 Retry and idempotency

A retried run does not create a second run. It advances the same run, and
because every write below it is insert-or-ignore on a natural key (§23),
re-running any stage converges rather than duplicating. A *new* run is created
when the policy version or the target attribute set changes — because then it
is genuinely a different question.

---

## 5. The claim model — and an audit of whether M2's will do

The M2 design states that M3 "may attach findings as `company_claims` against
registered attributes". **This design does not accept that automatically.**
The live schema was audited against M3's needs:

| M3 requirement | `company_claims` today | Verdict |
| --- | --- | --- |
| Attribution to a research artifact | FK exists only to `provider_record_versions`; the CHECK `exactly_one_attribution_path` allows `provider_record_version_id` XOR `subject_company_id` | **Usable via `subject_company_id`** — no constraint change needed |
| One claim supported by **several** artifacts | Single nullable FK. Impossible. | **Gap** |
| One artifact supporting several claims | Possible (many claims → one version) | OK |
| Exact evidence span / page / selector | No column, no table | **Gap** |
| Conflicting sources coexisting without mutation | Append-only, no uniqueness on (company, attribute) | OK |
| Source disappears without erasing claim history | Claim does not FK the source; artifact retention is separate | OK |
| Model-assisted extraction distinguishable from human assertion | No extractor provenance anywhere | **Gap** |
| Re-extraction under a newer extractor without rewriting the old claim | Append-only, so a new claim is a new row | OK, given a way to record *which* extractor |

Three gaps, all of them about **provenance richness**, none about the claim's
value semantics. The value side — typed shadows, availability, fact type,
confidence, temporal granularity, append-only enforcement — is already exactly
what M3 needs, and rebuilding it would be duplication.

### 5.1 Decision: one ledger, richer provenance (option A′)

**`company_claims` remains the single source of truth for what we assert about
a company.** M3 does not create a parallel claim table. The three gaps are
closed by *adding provenance beside the claim*, not by forking the claim:

* **`claim_evidence_links`** — an N:M table between a claim and the evidence
  that supports it, carrying the locator and the extraction. One claim, many
  artifacts. One artifact, many claims. One claim, many *independent* sources —
  which is the strongest evidential state M3 can reach and deserves to be
  representable.
* **`research_extractions`** — what read the artifact, with what extractor,
  model and prompt version (§10). The link points at it, so "human said so"
  and "GPT-class model said so" are different rows, not a convention.

M3 claims use the `subject_company_id` attribution path, which already exists
and already satisfies `exactly_one_attribution_path`. **No released CHECK
constraint is altered.**

Rejected: **option B** (separate M3 observation tables promoted into
`company_claims` on validation). It sounds safer and is not. It creates two
places that answer "what do we believe about this company", guarantees they
drift, and forces every consumer — including M4 — to know which one to read
and when. The prompt's own warning applies: *do not create two competing
sources of truth accidentally*. This design declines to create them
deliberately either.

### 5.2 The one thing M3 must add to an M2 table

An unsourced M3 claim must be impossible, not merely discouraged. M2 already
demonstrated the failure mode where an invariant held only because every reader
remembered to re-apply it (M2-ADR-032).

`attribute_definitions` gains one additive nullable column,
`owner_milestone` (`M2 | M3`), set when M3's registry version is seeded. A
deferred constraint trigger on `company_claims` then rejects, at COMMIT, any
claim whose attribute is owned by M3 and which has no row in
`claim_evidence_links`. Additive, backfillable, and it leaves every existing
M2 claim untouched.

This is the **only** change M3 requires to an M2-owned table, and it changes no
M2 behaviour.

---

## 6. Observation vs claim vs inference

These are three different things and the design keeps them apart, because
collapsing them is how a research system starts asserting its own guesses.

Worked example. Source text on a company's services page:

> "24-hour emergency HVAC service across Greater Boston."

**Observations** — what the source says, quoted, not interpreted. Stored on the
extraction, not as claims:

| Observation | Quote | Locator |
| --- | --- | --- |
| phrase | `24-hour emergency HVAC service` | `css=main > section.services > p:nth-of-type(2)` + quote hash |
| geography | `Greater Boston` | same span |

**Claims** — what we assert about the company, each linked to that evidence:

| Attribute | Value | Fact type | Why |
| --- | --- | --- | --- |
| `emergency_service` | `true` | `FACT` | The company states it about itself |
| `service_area` | `Greater Boston` | `FACT` | Stated directly |

**Inference** — derived by a stated rule, stored as a claim with
`fact_type = 'INFERENCE'` and a rule id recorded on the extraction:

| Attribute | Value | Fact type | Rule |
| --- | --- | --- | --- |
| `dispatch_centralization` | `LIKELY_REQUIRED` | `INFERENCE` | `emergency_service = true ⇒ on-call dispatch requirement` (rule `R-DISPATCH-001`, version 1) |

The inference is **never** `FACT`. It is reproducible: same evidence, same rule
version, same output. And it is separable: a consumer that wants only
observed facts filters `fact_type = 'FACT'` and gets a defensible set.

**The walk is always possible:**

```
claim → claim_evidence_links → research_extractions → research_artifact_versions
      → research_artifacts → research_sources → (raw body or its hash)
```

---

## 7. The operational attribute taxonomy, v1

Registered in `attribute_definitions` under registry version `M3-1.0`, owned by
milestone M3. **Twenty-four attributes**, not hundreds. Each is here because a
concrete research question for the target profile (§17) needs it and because it
can be evidenced rather than guessed.

Every attribute declares: value kind, value type, cardinality, allowed units,
allowed fact types, temporal semantics, projection strategy, conflict strategy
and a staleness horizon.

### WORKFORCE

| Attribute | Kind / type | Card. | Units | Allowed fact types | Temporal | Staleness |
| --- | --- | --- | --- | --- | --- | --- |
| `technician_count` | RANGE / numeric | ONE | `PEOPLE` | ESTIMATE, PROXY, FACT | point-in-time | 365d |
| `field_workforce_present` | SCALAR / boolean | ONE | — | FACT, PROXY, INFERENCE | durable | 730d |
| `field_roles` | SET / text | MANY | — | FACT, PROXY | point-in-time | 365d |
| `hiring_field_roles` | SET / text | MANY | — | FACT, PROXY | interval | 180d |

`technician_count` is a **RANGE**, not a scalar, and this is deliberate: the
sources disagree by construction (§11), and a range projects an honest envelope
where a scalar would force a false winner.

### FOOTPRINT

| Attribute | Kind / type | Card. | Units | Allowed fact types | Temporal | Staleness |
| --- | --- | --- | --- | --- | --- | --- |
| `branch_count` | RANGE / numeric | ONE | `LOCATIONS` | FACT, ESTIMATE, PROXY | point-in-time | 365d |
| `service_area` | SET / text | MANY | — | FACT, PROXY, INFERENCE | interval | 540d |
| `operating_markets` | SET / reference → `markets` | MANY | — | FACT, PROXY, INFERENCE | interval | 540d |
| `fleet_presence` | SCALAR / boolean | ONE | — | FACT, PROXY, INFERENCE | durable | 730d |
| `fleet_size` | RANGE / numeric | ONE | `VEHICLES` | ESTIMATE, PROXY | point-in-time | 365d |

`operating_markets` references M1's `markets` — a read-only FK. M3 never writes
a market.

### SERVICE_MODEL

| Attribute | Kind / type | Card. | Allowed fact types | Temporal | Staleness |
| --- | --- | --- | --- | --- | --- |
| `installation` | SCALAR / boolean | ONE | FACT, PROXY | durable | 730d |
| `preventive_maintenance` | SCALAR / boolean | ONE | FACT, PROXY | durable | 730d |
| `corrective_maintenance` | SCALAR / boolean | ONE | FACT, PROXY | durable | 730d |
| `emergency_service` | SCALAR / boolean | ONE | FACT, PROXY | durable | 730d |
| `recurring_service_contracts` | SCALAR / boolean | ONE | FACT, PROXY, INFERENCE | durable | 730d |
| `service_categories` | SET / text | MANY | FACT, PROXY | durable | 730d |

### OPERATIONAL_SYSTEMS

Every one of these is a SET of **named system observations**, never a boolean
"has a system". The value shape is
`{vendor, product, evidence_class, first_seen_source}`.

| Attribute | Card. | Allowed fact types | Temporal | Staleness |
| --- | --- | --- | --- | --- |
| `erp` | MANY | FACT, PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `field_service_management` | MANY | FACT, PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `dispatch_system` | MANY | FACT, PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `crm` | MANY | FACT, PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `customer_portal` | MANY | FACT, PROXY, INFERENCE | interval | 365d |
| `technician_mobile_app` | MANY | FACT, PROXY, INFERENCE | interval | 365d |

`evidence_class` is part of the *value*, because a technology claim's strength
is inseparable from how it was detected (§19).

### OPERATING_MODEL

| Attribute | Kind / type | Card. | Allowed fact types | Temporal | Staleness |
| --- | --- | --- | --- | --- | --- |
| `dispatch_centralization` | SCALAR / enum | ONE | PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `work_order_process` | SCALAR / enum | ONE | PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `evidence_collection_method` | SET / enum | MANY | FACT, PROXY, INFERENCE | interval | 365d |
| `parts_inventory_process` | SCALAR / enum | ONE | PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `asset_tracking` | SCALAR / enum | ONE | PROXY, INFERENCE, HYPOTHESIS | interval | 365d |

**These five may never be `FACT` except `evidence_collection_method`**, and the
registry enforces it. A company almost never states "our dispatch is
decentralised"; we infer it. The registry making `FACT` unrepresentable here is
the same mechanism that stopped M2 recording a directory category as a fact
(M2 revision 6, item 23).

Enum vocabularies are closed and versioned, e.g. `dispatch_centralization ∈
{CENTRAL, PER_BRANCH, HYBRID, LIKELY_REQUIRED, UNKNOWN_STRUCTURE}`. There is no
`HIGH`/`LOW`. There is no `digital_maturity`.

### CHANGE_SIGNALS

Interval-valued by nature: a signal was true over a window.

| Attribute | Kind / type | Card. | Allowed fact types | Temporal | Staleness |
| --- | --- | --- | --- | --- | --- |
| `hiring_signal` | SET / json | MANY | FACT, PROXY | interval | 180d |
| `branch_expansion` | SET / json | MANY | FACT, PROXY, INFERENCE | interval | 540d |
| `acquisition` | SET / json | MANY | FACT, PROXY | interval | permanent |
| `system_migration` | SET / json | MANY | FACT, PROXY, INFERENCE, HYPOTHESIS | interval | 365d |
| `certification` | SET / text | MANY | FACT, PROXY | interval | 730d |

`acquisition` never goes stale: an acquisition that happened stays happened.
Staleness is about *current-state* attributes, and the registry says which is
which rather than applying one rule to all.

### 7.1 What is deliberately absent

No `digital_maturity`. No `operational_pain_score`. No `tech_stack_quality`.
Each would be a judgement wearing an attribute's clothes, and each is M4's to
make from M3's evidence. §20 covers how pain evidence is recorded without
being scored.

---

## 8. Evidence locators

A locator answers *which exact span supported this claim* and must survive
reasonable reprocessing. Line numbers over arbitrarily-parsed text do not, so
they are not used alone.

Locators are content-type specific, stored as JSONB on `claim_evidence_links`,
and **always carry a quote hash** so a locator that has rotted can still be
validated against retained text:

| Content type | Locator shape |
| --- | --- |
| HTML | `{kind: "html", css: "...", heading_path: ["Services","Emergency"], quote: "...", quote_sha256: "..."}` |
| PDF | `{kind: "pdf", page: 4, section: "3.2 Maintenance", char_start: 1180, char_end: 1274, quote: "...", quote_sha256: "..."}` |
| JSON | `{kind: "json", pointer: "/employment/description", quote: "...", quote_sha256: "..."}` |
| Job posting | `{kind: "job", field: "description", char_start: 402, char_end: 486, quote: "...", quote_sha256: "..."}` |
| Plain text | `{kind: "text", char_start: ..., char_end: ..., quote: "...", quote_sha256: "..."}` |

**Resolution order when replaying a locator:** quote hash first (exact match
anywhere in the retained text), then structural path, then character offsets.
A locator whose quote hash no longer matches is marked `locator_status =
'UNRESOLVED'` on read — it is **not** deleted, and the claim it supports is
**not** invalidated. Evidence that we can no longer point at precisely is
weaker than evidence we can, and that is a fact about the evidence, not grounds
for erasing it.

`heading_path` exists because it survives CSS restructuring far better than a
selector does; the selector survives copy edits better. Keeping both is cheap.

---

## 9. Extraction, including model-assisted

An extraction is a row in `research_extractions`: *this extractor read that
artifact version and produced these observations*.

Every extraction stamps:

`artifact_version_id`, `extractor_kind`
(`RULE | PARSER | MODEL | HUMAN`), `extractor_id`, `extractor_version`,
`model_provider`, `model_name`, `model_version`, `prompt_template_version`,
`output_schema_version`, `determinism` (`DETERMINISTIC | SAMPLED`),
`temperature`, `created_at`, `raw_output_sha256`, `raw_output` (nullable, per
retention policy), `status`, `error`.

### 9.1 A model's confidence is not evidential strength

This is the rule the design most needs to defend, because it will be under
constant pressure.

> **The source determines the fact type. The extractor determines only whether
> we read the source correctly.**

A model that is 0.99 confident it read "24/7 emergency service" on the page has
told us something about *extraction reliability*, not about *the company*. If
the page is the company's own site, the claim is `FACT` because of whose page
it is — not because the model was sure. If the same sentence appeared in an
unaffiliated blog post, the same 0.99-confident extraction yields `PROXY`.

Concretely, `company_claims.confidence` is computed from the **evidence type
and source trust**, exactly as M0 computes it. Extractor confidence is stored
on the extraction, is visible in the API, and is **not** an input to the
claim's confidence. An extraction below an extractor-confidence threshold
produces no claim at all — it produces a research gap and a review candidate.

### 9.2 Re-extraction

Re-extraction under a new `extractor_version` is a first-class operation. It
creates a **new** extraction row against the **same** artifact version, and new
claims. It never rewrites the old extraction or the old claims. The
`operational_research_profiles` projection (§13) prefers the newest extractor
version by precedence rule, so the current view updates while history survives
intact — the same shape M2 uses for superseded resolution decisions.

---

## 10. Contradictions

M3 will find disagreement constantly. The design's position: **disagreement is
a finding, not a problem to be flattened.**

Worked example, all three observed for one company:

| Source | Says | Claim written |
| --- | --- | --- |
| Company site, `/about` | "120 technicians" | `technician_count` = `{min:120,max:120}`, FACT, trust 0.9 |
| Job posting, dated 2026-02 | "team of more than 80 technicians" | `technician_count` = `{min:80,max:null}`, ESTIMATE, trust 0.6 |
| Third-party profile | "51–100 employees" | `technician_count` = `{min:51,max:100}`, PROXY, trust 0.4 |

All three claims are stored. None overwrites another. The projection then:

1. **Keeps an envelope, not a winner.** `technician_count_min = 51`,
   `technician_count_max = 120`, with a `contradiction = true` flag and the
   contributing claim ids.
2. **Records a best estimate separately** (`technician_count_best = 120`),
   chosen by the precedence rule below, clearly labelled as a selection rather
   than a measurement.
3. **Never silently collapses.** A consumer reading only `_best` and ignoring
   `contradiction` has chosen to; the data did not hide it.

Precedence, applied in order and recorded on the projected row:

1. Higher fact type: `FACT > ESTIMATE > PROXY > INFERENCE > HYPOTHESIS`
2. Higher source trust tier
3. More recent `source_published_at` (NULLs last — an undated source never
   outranks a dated one on recency)
4. More recent `retrieved_at`
5. Lowest claim id — a deterministic tiebreak, so rebuilds are identical
   (M2-ADR-024's lesson: "keep the current value" is not a rule)

**Confidence never erases disagreement.** A high-confidence claim and a
low-confidence contradicting claim both persist; confidence orders them, it
does not delete the loser.

---

## 11. Temporal semantics and staleness

Four distinct times, and conflating any two of them is a defect:

| Field | Lives on | Means |
| --- | --- | --- |
| `retrieved_at` | artifact version | when *we* fetched it |
| `source_published_at` | artifact version | when the *source* says it was published — NULL when unstated |
| `observed_at` | claim | when the asserted fact was true, per the source |
| `valid_from` / `valid_to` | claim value (interval attributes) | the window the fact covers |

> **A retrieval date is never a fact date.** Fetching a page today does not
> make its unsourced claim about technician headcount true today. It makes it
> *observed by us* today, from a source of unknown vintage.

An undated page yields claims with `period_granularity = 'SNAPSHOT'` and
`observed_at = NULL`: "true as of when we looked, no finer". M0's existing
CHECK — `observed_at` present **iff** granularity is `DATE` — already makes
invented precision unrepresentable, and M3 inherits it.

### 11.1 Staleness is metadata, never mutation

> **An old fact is never rewritten to `false` because it is old.**

Staleness is computed at read time from the registry's per-attribute horizon
(§7) against the claim's best-known date, and surfaces as:

* `staleness = FRESH | AGING | STALE | UNKNOWN_AGE` on the projected row
* a research gap of kind `STALE_EVIDENCE` when a required attribute's best
  evidence is past its horizon (§13)

`UNKNOWN_AGE` matters: a claim from an undated page has no computable
staleness, and saying so is more honest than assuming it is fresh *or* stale.

Because staleness is derived, the projection stays deterministic: the stored
row holds the dates, not a clock-dependent verdict. A `current_*` **view**
applies `now()` at query time — the same split M2 uses so that
`company_market_presences` is deterministic while
`current_company_market_presences` is current.

---

## 12. Research gaps

A gap means **"we do not currently have sufficient evidence"** and never "the
company does not have this."

Kinds:

| Kind | Raised when |
| --- | --- |
| `NO_EVIDENCE` | A required attribute has no claim at all |
| `INSUFFICIENT_EVIDENCE` | Only claims weaker than the attribute's required fact type (e.g. only HYPOTHESIS where PROXY is the floor) |
| `STALE_EVIDENCE` | Best evidence is past the staleness horizon |
| `CONTRADICTED` | Contradiction the precedence rule could not resolve to an acceptable confidence |
| `UNRESOLVABLE_SOURCE` | Every candidate source failed permanently (404/410/denied) |

**Deterministic identity**, mirroring M1's research-gap discipline:

```
UNIQUE (company_id, attribute_key, research_policy_version, gap_kind)
```

Re-running research over unchanged evidence produces the same gap rows, not
duplicates. A gap closes by being **resolved** — an append-only status change
with the claim that closed it — not by deletion, so "we once did not know this"
stays answerable.

Gaps carry `attempted_source_count` and `last_attempt_at`, so "unknown because
we never looked" is distinguishable from "unknown after eleven sources", which
are very different states that a single boolean would merge.

---

## 13. Coverage — and what it must not become

```
operational_research_coverage = covered_required_weight / total_required_weight
```

Computed exactly like M0's, over the attributes the policy marks **required**,
with unknown leaving both numerator *and* denominator untouched for attributes
marked not-applicable to the company's vertical.

Coverage answers **"how much of the required operational profile has
evidence?"** It does not answer "is this a good lead", and the design keeps
three numbers separate, as M0 does:

| Number | Means |
| --- | --- |
| `coverage` | how much of the required profile has evidence |
| `confidence` | how strong the evidence we have is |
| `contradiction_rate` | how much of it disagrees |

Collapsing these into one figure would produce something that looks like a
score, and something that looks like a score gets used as one. That is the
M3/M4 boundary breaking from the inside, so it is prevented in the data model
rather than in a guideline.

---

## 14. Source discovery

An interface, not an implementation. **No provider is selected and none is
implemented.**

```
ResearchSourceProvider
  capabilities()      → declared kinds, rate limits, auth requirement, cost model
  discover(company, plan) → Iterable[SourceCandidate]     # the only I/O method
```

`SourceCandidate` carries `url`, `discovery_method`, `expected_kind`,
`relevance_hint`, `discovered_from_source_id`. It is **a place to look**, never
a fact. Nothing in the candidate is ever written to a claim.

Discovery methods the interface anticipates: company website crawl, sitemap,
search engine, job boards, company registries, document search, structured
APIs. Each is a future adapter. Fixture adapters reading local files will be
the only implementations at M3's first release, exactly as M2 shipped.

---

## 15. Fetch policy

Responsible by default, and the defaults are conservative:

| Control | Default |
| --- | --- |
| `robots.txt` | Respected, cached per host with TTL. A disallowed path is **not fetched**, and the attempt is recorded as `ROBOTS_DENIED` — a recorded non-fetch, not a silent skip |
| Rate limit | ≤ 1 request/second/host, jittered; a per-host token bucket |
| Concurrency | ≤ 2 in flight per host |
| Retries | 3 attempts, exponential backoff, only on 429/5xx/timeout. Never on 4xx |
| Redirects | Max 5, all recorded; cross-host redirect ends same-domain crawling |
| Timeout | 20s connect+read |
| Max size | 10 MB; larger is recorded as `TOO_LARGE` with headers kept |
| MIME validation | Declared type must match sniffed type, or `MIME_MISMATCH` |
| Crawl depth | ≤ 3 from seed |
| Domain boundary | Same registrable domain only (§16) |
| Browser automation | **Not used.** If a future provider genuinely needs it, that is an ADR, not a default |
| User agent | Identifies the crawler and a contact URL |

Failure states are data: `404`, `410`, `403`, `401`, `LOGIN_WALL`, `TIMEOUT`,
`ROBOTS_DENIED`, `TOO_LARGE`, `MIME_MISMATCH`. A 410 marks the source
permanently gone; a 404 is retried once on a later run before being treated the
same way. Neither deletes prior artifacts captured from that source — the page
existed, we saw it, that remains true.

---

## 16. The company-website boundary

M2 owns company domains, and M3 must not decide identity for itself. A domain
may be crawled as "this company's website" **only** when:

1. `company_domains.domain_role = 'IDENTITY'` for that company, **or**
2. `domain_role = 'ALTERNATE'` **and** a human override exists, **or**
3. an explicit human-supplied seed exists for this research run.

**A `GROUP` domain is never a crawl seed.** M2 demotes shared hosting
(`wixsite.com`, `business.site`, …) to `GROUP` precisely because thousands of
unrelated firms share it (M2-ADR-032). Crawling the host would attribute other
companies' pages to this one — a false-evidence defect far worse than missing
evidence.

For a company whose only domain is `krause.wixsite.com`, M3 may crawl the
**path prefix** `krause.wixsite.com/krause/*` when an explicit seed says so,
never the host root. When no crawlable seed exists, M3 emits a
`NO_CRAWLABLE_SEED` gap and proceeds with other source kinds. Unknown stays
unknown.

---

## 17. Job postings

High-value, easy to over-read.

Recorded fields: `job_title`, `location`, `posted_at`
(+ granularity), `removed_observed_at`, `department`, `employment_type`,
`requirements_text`, `tools_mentioned`, `is_field_role`.

Evidence typing, stated as rules:

| Observation | Yields | Fact type | Why |
| --- | --- | --- | --- |
| Posting exists on the company's own careers page | `hiring_signal` | `FACT` | The company published it |
| Title is a field role ("HVAC Service Technician") | `field_workforce_present` | `PROXY` | Hiring one strongly implies having them |
| "Join our team of more than 80 technicians" | `technician_count` `{min:80}` | `ESTIMATE` | The company says it, approximately |
| Posting mentions a tool in *requirements* | that system attribute | `PROXY` | Used by *some* role, not proven company-wide |
| Posting for "ServiceNow administrator" | `erp`/`FSM` | **`PROXY` at most, never `FACT`** | Hiring an admin does not prove operational adoption across the field workforce |
| Three dispatch-coordinator postings in six months | `dispatch_centralization` | `INFERENCE` | Derived by a stated rule over multiple postings |

**Temporal decay.** A posting's evidence is anchored to `posted_at`, not to
`retrieved_at`. `hiring_field_roles` has a 180-day horizon: a two-year-old
posting is not current hiring. But it is **still true that they posted it** —
so the claim persists with its original `observed_at`, and only its *staleness*
changes. A removed posting sets `removed_observed_at`; it never deletes the
claim.

---

## 18. Technology detection

Evidence class is part of the value, and it determines the ceiling on fact
type:

| Evidence class | Example | Max fact type |
| --- | --- | --- |
| `EXPLICIT_COMPANY_STATEMENT` | "We run ServiceTitan" on their site | `FACT` |
| `CUSTOMER_PORTAL_BRANDING` | Portal at `company.servicetitan.com` | `FACT` |
| `INTEGRATION_DOC` | Vendor case study naming the company | `FACT` |
| `JOB_DESCRIPTION_MENTION` | Tool in a job requirement | `PROXY` |
| `THIRD_PARTY_TECH_DATABASE` | A tech-lookup vendor says so | `PROXY` |
| `SCRIPT_FINGERPRINT` | A vendor script tag on the site | `HYPOTHESIS` |
| `EMPLOYEE_PROFILE_MENTION` | Someone lists it on a profile | `HYPOTHESIS` |

> A script tag proves a script loaded on a web page. It does not prove the
> field workforce uses that platform operationally.

`SCRIPT_FINGERPRINT` therefore tops out at `HYPOTHESIS`, and the registry
enforces the ceiling rather than trusting the extractor to be modest. A
marketing pixel and an FSM deployment are not the same kind of knowledge.

---

## 19. Operational pain: collected, never scored

Pain evidence is recorded as ordinary claims with ordinary fact types:

| Evidence | Attribute | Fact type |
| --- | --- | --- |
| Repeated dispatch-coordinator postings | `hiring_signal` + `dispatch_centralization` | FACT + INFERENCE |
| Downloadable PDF service forms | `evidence_collection_method` = `PAPER_FORM` | FACT |
| Different phone numbers and intake forms per branch | `work_order_process` = `PER_BRANCH_INTAKE` | PROXY |
| Announced "digital transformation project" | `system_migration` | FACT |
| Scheduling complaints in public reviews | `hiring_signal` / `work_order_process` | PROXY |

There is **no** `pain_score`, no severity ranking and no weighting. M3 records
that the company publishes downloadable PDF work-order forms. Whether that
constitutes pain worth selling into is M4's judgement, and M3 does not
pre-empt it by attaching a number that M4 would then merely rescale.

---

## 20. The M2 ↔ M3 firewall

M3 may **extend** company evidence. It may not touch identity.

| M3 must never | Enforcement |
| --- | --- |
| Create a canonical company | No M3 code path constructs `Company`; a fingerprint test asserts M2 table row counts are unchanged across a research run, exactly as M2 does for M1 |
| Merge or split companies | Same |
| Alter an entity-resolution decision | `entity_resolution_decisions` is append-only at the database level |
| Reinterpret provider identities | M3 reads `provider_entities`; never writes |
| Change an identity domain | `company_domains` is an M2 projection, rebuilt only by M2 |
| Mutate historical M2 claims | `company_claims` append-only trigger |

### 20.1 The identity-review signal

Research *will* discover identity conflicts: the site says "a division of X",
or two companies share a phone number and address. M3 records this and stops.

`identity_review_signals` — append-only, M3-written, **M2-consumed**:

`company_id`, `signal_kind` (`POSSIBLE_DUPLICATE | POSSIBLE_PARENT |
POSSIBLE_ACQUISITION | DOMAIN_MISMATCH | NAME_MISMATCH |
POSSIBLE_CEASED_TRADING`), `related_company_id` (nullable), `evidence_summary`,
`claim_evidence_link_ids`, `raised_at`, `raised_by_run_id`, plus an
append-only `status` chain (`OPEN → ACKNOWLEDGED → ACTIONED | DISMISSED`).

It is a **queue for M2's existing human-review path**, not an instruction. M2's
`AMBIGUOUS` review flow already exists and already appends decisions rather
than mutating them; this feeds it. M3 autonomously rewriting identity is
exactly the failure this table prevents.

---

## 21. The M3 ↔ M4 firewall

M3 produces evidence, claims, gaps and coverage. Nothing else.

**Forbidden columns anywhere in M3:** `lead_score`, `qualification_score`,
`icp_fit_score`, `pain_score`, `priority_score`, `recommend_contact`,
`sales_ready`, `tier`, `grade`.

Enforced the way M2 enforces its M3 boundary today: a test asserting that no
table in the M3 schema declares a column matching that vocabulary, so the
boundary fails a build rather than degrading over a quarter of well-intentioned
commits.

Coverage and confidence are exported; their *interpretation* is not.

---

## 22. Idempotency

| Case | Result |
| --- | --- |
| Same URL, same bytes | New `research_artifact_versions` row? **No.** Unique on `(source_id, raw_body_sha256)`; a repeat fetch records a *sighting* (`retrieved_at` appended) and nothing else |
| Same URL, different bytes, same canonical content | New **version**, same **artifact**. Cosmetic change, no new semantics |
| Same URL, different canonical content | New artifact **and** new version. The page genuinely changed |
| Different URL, same content | Same artifact, new source, new version. Both places recorded; the finding "mirrored at two URLs" is preserved |
| Re-fetched next week | Sighting if unchanged; new version if changed. Either way, one row per genuine state |
| Same extraction version rerun | Unique on `(artifact_version_id, extractor_id, extractor_version, prompt_template_version)` → no-op |
| New extractor version | New extraction, new claims, old ones untouched (§9.2) |
| Same claim value extracted twice from one source | Unique on `(subject_company_id, attribute_key, value_hash, extraction_id)` → no-op |
| Same claim from two *different* sources | **Two claims, both kept** — independent corroboration is the strongest state M3 can reach and must be representable, not deduplicated away |
| Research gap re-raised | Unique on `(company_id, attribute_key, policy_version, gap_kind)` → no-op |

The last two rows are the subtle pair: deduplicating identical claims from
different sources would destroy exactly the signal worth having, while *not*
deduplicating a re-extraction would inflate the evidence base without new
evidence. M2 shipped that second defect and had to fix it; M3 starts with the
constraint.

---

## 23. Concurrency

Every write is insert-or-ignore on a natural key, enforced by the database —
never SELECT-then-INSERT, which M2 proved is not safe under two real
connections.

| Race | Protection |
| --- | --- |
| Two workers fetch the same URL | `UNIQUE (source_id, raw_body_sha256)` on versions; both converge, one row |
| Same content discovered via two URLs | `UNIQUE (canonicalization_strategy, canonicalization_version, canonical_content_hash)` on artifacts |
| Same artifact extracted concurrently | `UNIQUE (artifact_version_id, extractor_id, extractor_version, prompt_template_version)` |
| Same claim asserted concurrently | `UNIQUE (subject_company_id, attribute_key, value_hash, extraction_id)` |
| Same gap created concurrently | `UNIQUE (company_id, attribute_key, policy_version, gap_kind)` |
| Two runs on one company | Partial unique index: at most one non-terminal run per `(company_id, policy_version)` |
| Profile rebuilt concurrently | Advisory lock per company for the rebuild; the projection is derived, so the loser simply re-derives |

Every `IntegrityError` on these keys is caught and translated into a re-read,
never surfaced raw — the pattern M2's resolution service already uses.

---

## 24. Jobs

Reuses M2's PostgreSQL `FOR UPDATE SKIP LOCKED` queue unchanged. **No Redis.**
No new queue machinery.

Four job types, because each has a genuinely different failure and retry
profile:

| Job | Retries | Why distinct |
| --- | --- | --- |
| `DISCOVER_SOURCES` | 3 | Provider-bound; failure means no candidates |
| `FETCH_ARTIFACT` | 3, host-rate-limited | Network-bound; must respect per-host limits |
| `EXTRACT_ARTIFACT` | 2 | CPU/model-bound; no network; re-runnable from stored bytes |
| `ASSERT_CLAIMS` | 2 | Database-bound; pure over extractions |

`REBUILD_RESEARCH_PROFILE` is **not** a job. It is a derived projection
rebuilt synchronously at the end of `ASSERT_CLAIMS` and on demand via the API,
the way M2 rebuilds projections. Making it a queued job would add a state where
claims exist and the profile silently lags.

---

## 25. API

Read surfaces:

```
GET /companies/{id}/research                 profile + coverage + staleness
GET /companies/{id}/research/artifacts       sources and versions seen
GET /companies/{id}/research/claims          claims + evidence links
GET /companies/{id}/research-gaps            what is unknown, and why
GET /companies/{id}/research-coverage        coverage, confidence, contradictions
GET /operational-research/runs               filterable by status and company
GET /operational-research/runs/{id}          one run, with per-stage timestamps
GET /research-artifacts/{id}/versions        version history for one artifact
GET /research-extractions/{id}               extractor provenance + raw output policy
GET /identity-review-signals                 the queue handed back to M2
```

Actions:

```
POST /companies/{id}/research-runs           start a run under a policy version
POST /operational-research/runs/{id}/retry   advance the same run, never a second
POST /research-claims/{id}/review            human confirm/reject — appends, never mutates
POST /identity-review-signals/{id}/status    acknowledge / action / dismiss
```

Human evidence review is justified and included: model-assisted extraction below
threshold produces review candidates rather than claims (§9.1), and that queue
needs a way to be worked. It appends a `HUMAN` extraction and a new claim; it
never edits the model's.

Not exposed: the job queue, fetch internals, raw model output by default (§26),
locator internals beyond what the claim needs.

All responses use the existing error envelope. `404` for an unknown company or
run; `409` for a run that has not reached a stage that permits assertion.

---

## 26. Retention

Raw HTML and PDF bodies dominate storage and are the least reusable part.

| Class | Retention | Rationale |
| --- | --- | --- |
| Metadata (source, URL, status, times, hashes) | **Permanent** | Provenance must outlive bytes |
| `canonical_content_hash`, `raw_body_sha256` | **Permanent** | Identity and later verification |
| Claims, extractions, evidence links, locators | **Permanent** | The findings themselves |
| `extracted_text` | Default 24 months, configurable | Enough to re-resolve a locator and re-extract |
| `raw_body` | Default 90 days, configurable per content type | Expensive; recoverable by re-fetch when the page still exists |
| Model `raw_output` | Default 90 days; hash permanent | Audit trail without indefinite bulk |

**Deleting a raw body must never make provenance unintelligible.** After
pruning, an artifact version still reports its source, retrieval time, status,
both hashes, its `source_published_at`, and `body_retention = 'PRUNED'` with
the pruning date. A claim's evidence link still resolves to that version and
still carries its own quote and quote hash — so the exact supporting text
remains readable from the claim even when the full body is gone. The claim
degrades from "we can show you the whole page" to "we can show you the quoted
span and prove it hashed to this", which is a real but bounded loss, and it is
stated rather than discovered.

---

## 27. Privacy and the people boundary

**M3 researches companies. It does not profile people.** M5 owns people.

A job posting may name a recruiter; a case study may quote an operations
manager. M3:

* **Never** creates a person record, a contact or an email address
* **Never** keys anything by a person
* Redacts detected personal data (names in contact blocks, direct emails, direct
  phone numbers, photos) from `extracted_text` at extraction time, replacing it
  with a typed placeholder, and records `redaction_policy_version`
* Retains the raw body under §26 unredacted **only** while retention allows,
  because re-extraction needs the original; the redacted text is what
  extractions and locators use
* Keeps a company's main switchboard number and general info@ address, which
  are company attributes, not personal data

A quote attributed to a named employee is stored as company evidence with the
name redacted: *"[PERSON] , Operations Manager, said the company runs 42
vans"* supports `fleet_size` without starting a person database.

---

## 28. Pressure test: commercial HVAC contractor, USA, 20–150 employees

Walking the target profile through the design, looking for places it breaks.

**Seed.** M2 gives `company_id`, identity domain `acme-mechanical.com`, market
`US`, vertical `hvac_mechanical_services`. Domain role is `IDENTITY`, so §16
permits a crawl.

**Discovery.** Sitemap → `/services`, `/about`, `/careers`, `/locations`,
`/commercial-maintenance`. Job board → 4 postings. Registry → a state
contractor licence record (PDF).

**Capture.** 9 artifacts. `/locations` is served at both `/locations` and
`/our-locations`; both sources, one artifact (§22). The licence PDF is 2.1 MB,
under the limit.

**Extraction and claims.**

| Evidence | Claim | Type |
| --- | --- | --- |
| "24/7 emergency service" on `/services` | `emergency_service = true` | FACT |
| "Planned maintenance agreements" | `preventive_maintenance = true`, `recurring_service_contracts = true` | FACT |
| `/locations` lists 3 addresses | `branch_count = {3,3}` | FACT |
| Service-area map naming 6 counties | `service_area` = 6 values | FACT |
| "Our team of 40+ technicians" | `technician_count = {40,null}` | ESTIMATE |
| 2 postings "HVAC Service Technician" | `hiring_field_roles`, `field_workforce_present` | FACT / PROXY |
| 1 posting "Service Dispatcher", requirements mention ServiceTitan | `dispatch_system` `{ServiceTitan, JOB_DESCRIPTION_MENTION}` | **PROXY** |
| Careers page photo of branded vans | `fleet_presence = true` | PROXY |
| Downloadable PDF maintenance checklist | `evidence_collection_method = PAPER_FORM` | FACT |
| Licence PDF, page 2, issued 2019 | `certification` | FACT, `observed_at` 2019 |

**Inference.** `emergency_service = true` + 3 branches + a dispatcher posting →
`dispatch_centralization = LIKELY_REQUIRED`, `INFERENCE`, rule `R-DISPATCH-001`.
Not FACT. The registry would reject FACT for this attribute.

**Gaps.** `erp` → `NO_EVIDENCE`. `customer_portal` → `NO_EVIDENCE`.
`technician_mobile_app` → `NO_EVIDENCE`. `field_service_management` →
`INSUFFICIENT_EVIDENCE`, because the only signal is a PROXY job mention of
ServiceTitan, which the policy marks below the floor for asserting an FSM in
operational use.

**Coverage.** 13 of 18 required attributes evidenced → 0.72. Confidence 0.68.
Contradictions 0.

**What M3 does not do.** It does not say this is a good prospect. It does not
score the PDF checklist as pain. It hands M4 thirteen evidenced attributes,
five gaps and a walkable provenance chain for every one.

**Where it strained.** The ServiceTitan mention is the sharpest edge: it is the
single most commercially interesting signal in the whole run, and the design
deliberately caps it at PROXY with an `INSUFFICIENT_EVIDENCE` gap. That will
feel wrong to a salesperson and is correct: one job ad does not establish that
a company's field workforce runs on a platform. M4 may still weigh a PROXY
heavily — that is its right. M3's job is to hand over the PROXY honestly
labelled, not to promote it.

---

## 29. Adversarial self-review

Before this document was considered complete, it was read against the failure
modes an evidence system of this shape is prone to. Each row is either shown to
be prevented, or was a real contradiction found and fixed here.

| Failure mode | Status |
| --- | --- |
| URL treated as identity | **Prevented.** Three tiers; content identity is a canonical hash, never a URL (§3, M3-ADR-005) |
| Current webpage treated as eternal truth | **Prevented.** Artifacts are versioned and append-only; a changed page adds, never replaces (§3.3, A3) |
| Retrieval date mistaken for observed date | **Prevented.** Four distinct times; `source_published_at` is NULL when unstated (§11, E1) |
| LLM confidence mistaken for evidential strength | **Prevented.** The source sets fact type; extractor confidence is stored and is not an input (§9.1, M3-ADR-004) |
| No-evidence mistaken for false | **Prevented.** `NOT_AVAILABLE` + gap; a stated negative is a separate, representable case (§2, C9, C10) |
| A claim having only one possible source | **Prevented.** `claim_evidence_links` is N:M (§5.1, C5, C7) |
| Source conflicts overwritten | **Prevented.** Envelope projection with a contradiction flag (§10, M3-ADR-012) |
| Append-only tables requiring a later UPDATE | **Found and fixed.** Gap and signal status changes had nowhere to live on an append-only parent. Two event-log tables now carry them (schema graph §3.9) |
| Mutable current state masquerading as evidence | **Prevented.** `operational_research_profiles` is a truncatable projection; evidence is elsewhere |
| M3 modifying M2 identity | **Prevented.** Identity-review signal only; row-count fingerprint test (§20, H1–H4) |
| M4 judgement smuggled into an attribute | **Prevented.** No score columns; operating-model attributes cannot be FACT; enforced by a test (§21, M3-ADR-001) |
| Duplicated evidence under retries | **Prevented.** Natural keys on every write (§22) |
| Wall-clock-dependent persisted projections | **Prevented.** Staleness is a view, never a stored verdict (§11.1, E5) |
| Evidence unreproducible after the page changes | **Bounded, not eliminated.** Quote and quote hash live on the evidence link, so the supporting span survives body pruning; a rotted locator weakens the claim rather than invalidating it (§8, B5, §26) |
| One giant EAV table with no schema contract | **Prevented.** The versioned attribute registry contracts every attribute, as it already does for M2 (§7) |

Two further contradictions were found and fixed during this review: a
projection referred to in the singular where the table is plural, and a table
count in the schema graph that disagreed with its own ownership summary. Both
are corrected; the count is now derived from the ownership table rather than
written by hand.

---

## 30. Open questions

Listed rather than silently decided:

1. **Source trust tiers.** §10 relies on per-source-kind trust. The tiers are
   not yet calibrated and, like M2's match thresholds, would be configuration
   asserted as fact if set now.
2. **Boilerplate stripping.** `HTML_TEXT_V1` must remove nav/footer to avoid
   every page differing by a rotating banner, but over-stripping removes real
   content. Needs validation against real sites before the version is frozen.
3. **Inference rule catalogue.** §6 shows one rule. A versioned rule registry
   is implied and not yet designed; it may deserve its own table.
4. **Review threshold.** The extractor-confidence level below which a claim
   becomes a review candidate is a policy number nobody has calibrated.
5. **Locator rot rate.** Quote-hash-first resolution is expected to be robust;
   no measurement exists yet.
6. **Redaction detection quality.** §27 assumes reliable PII detection in
   `extracted_text`. False negatives are a privacy risk, false positives
   destroy evidence.
7. **`markets` reference granularity.** `service_area` as free text vs
   references to M1 markets — a US county has no M1 market row, so free text is
   the interim answer and may not be the right one.
