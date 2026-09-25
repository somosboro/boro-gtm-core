# M3 — Operational Research

**Status:** design, **revision 4 — implementation ready**. Not implemented. No M3 runtime code,
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

## 3. Source, bytes, semantics, text — four separable layers

M3 must never rely on a live URL. A URL is a *place we looked*, not a thing we
know. Revision 1 separated three concepts; revision 2 separates five, because
building the acceptance scenarios against three showed that retrieval history,
raw bytes, semantic identity and readable text each change on their own
schedule and cannot share a row.

| Layer | Table | Identity | Answers |
| --- | --- | --- | --- |
| **A. Retrieval** | `research_fetch_events` | none — every retrieval is an event | *When did we look, from where, and what happened?* |
| **B. Bytes** | `research_artifact_bodies` | `UNIQUE (raw_body_sha256)`, global | *What exact octets exist?* |
| **C. Semantics** | `research_artifacts`, joined by `research_artifact_derivations` | `(strategy, strategy_version, canonical_hash)` | *What does this mean, under which contract?* |
| **D. Text** | `research_text_derivations` | `(body, text_policy_version)` | *What readable text do extractions and locators use?* |
| **E. Claims** | extraction → evidence link → claim | assertion fingerprint | *What do we assert, on what evidence?* |

Each layer is versioned independently, so an upgrade at one never forces work
at another: a new canonicalization version re-derives C from stored bytes, a
new text policy re-derives D from stored bytes, and a new extractor re-derives
E from stored text. **None requires a refetch.**

### 3.1 `research_sources` — where we looked, and nothing more

A source is a *normalized locator* under a versioned policy. Normalization
strips what does not carry identity — scheme and case, trailing slash, default
ports, tracking parameters (`utm_*`, `gclid`, `fbclid`, session ids), the
fragment, a `www.` prefix — and preserves what does: host, path, and meaningful
query parameters such as `?id=` or `?job=`.

The row holds the locator, its policy version, the host, the registrable domain
and `first_seen_at`. **Nothing else.** In particular it does not hold who
discovered it, what page linked to it, or where it redirects, because a source
can be discovered by many attempts, through many parents, by several methods,
for several companies, and can acquire redirect and canonical relationships
long after it was created. Revision 1 stored each of those as one field on an
append-only row, which made all but the first unrecordable (M3-ADR-015).

A source row is therefore **global and reusable**: two companies researched a
year apart that both cite the same trade-association page share one source,
and each keeps its own provenance.

### 3.2 Discovery and relationships, as append-only observations

`research_source_discoveries` records *how we found it, each time*: the source,
the attempt, the method (`SITEMAP | CRAWL_LINK | SEARCH | JOB_BOARD |
REGISTRY | HUMAN_SEED | API`), the parent source when there was one, and the
query or anchor text. One page found by both a sitemap and a search engine is
two discoveries — a real finding about how discoverable it is.

`research_source_edges` records relationships between locators —
`REDIRECTS_TO`, `DECLARES_CANONICAL`, `LANGUAGE_VARIANT_OF`,
`MIRROR_CANDIDATE` — each carrying the fetch event that observed it. Learning
about a redirect six months later **appends an edge and mutates nothing**. A
site that changes its redirect target appends a second edge and both survive.

The canonical URL a page declares is an edge, never a merge instruction: a page
may misdeclare it. **Sources are never merged.** Two URLs serving one document
converge at the *body*, because "the same document is mirrored at two URLs" is
worth keeping.

### 3.3 Bytes, and the contracts applied to them

`research_artifact_bodies` is unique on `raw_body_sha256` **globally**. The same
PDF at four URLs is one body, four sources and four fetch events — not four
copies of a 2 MB file.

`research_artifact_derivations` joins a body to an artifact under a named,
versioned contract. This is the join revision 1 lacked: a single
`version → artifact` pointer made it impossible for one byte string to belong
to two canonicalization contracts at once, which acceptance A9 requires
(M3-ADR-016).

Canonicalization is content-type specific and versioned:

| Strategy | Applies to | Normalizes away | Preserves |
| --- | --- | --- | --- |
| `HTML_TEXT_V1` | `text/html` | scripts, styles, comments, attribute order, whitespace runs, nav/footer boilerplate, session tokens | visible text, heading structure, link targets, structured data, **declared publication metadata** |
| `PDF_TEXT_V1` | `application/pdf` | producer metadata, creation timestamps, object ordering | page-segmented text, page count, **declared publication date** |
| `JSON_CANONICAL_V1` | `application/json` | key order, whitespace | values, structure — reused verbatim from M0 |
| `PLAINTEXT_V1` | `text/plain` | line-ending style, trailing whitespace | text |

Every strategy is **required to preserve declared publication metadata**.
Without that requirement two documents differing only in publication date would
canonicalize to one artifact, and the artifact is where
`source_published_at` lives.

A cosmetic change — a rotating testimonial, a build hash, a footer year —
produces a **new body** and **no new artifact**. A changed sentence about
emergency service produces both.

### 3.4 Text, versioned separately from semantics

`research_text_derivations` holds the readable text extractions and locators
work against, keyed `(body, text_extraction_policy_version)`.

Canonicalization and text extraction are separate because they answer different
questions at different speeds. Canonicalization asks *are these the same
document?* and is aggressive, stripping boilerplate so churn does not look like
change. Text extraction asks *what can a reader see?* and is conservative,
keeping content a locator may need. Upgrading a PDF text extractor re-derives
text from stored bytes with no refetch and **no new artifact** — impossible in
revision 1, where `extracted_text` and its policy version sat on the same
append-only row as the bytes (M3-ADR-017).

Extractions point at a text derivation, never at a body, so an extraction is
reproducible: same body + same text policy + same extractor = same input.

### 3.5 Retrieval history

`research_fetch_events` records **every** retrieval, successful or not: source,
attempt, `retrieved_at`, outcome, HTTP status, final URL, content type, ETag,
Last-Modified, and the body when one was obtained (NULL when the fetch failed).

It is unique on nothing, deliberately. *"We saw these exact bytes on Sep 1, Sep
8 and Sep 20"* is three events pointing at one body, and no evidence row is
touched to record the second and third. Revision 1 promised exactly this
behaviour in acceptance A1 and had nowhere to put it: the only row that could
hold a retrieval time was unique on `(source_id, raw_body_sha256)`, so the
second retrieval of identical bytes was unrecordable (M3-ADR-014).

Eleven failed attempts are eleven events with `body_id IS NULL`, which is how
"we tried and were refused" stays distinguishable from "we never looked".

### 3.6 What this answers

| Question | Answered by |
| --- | --- |
| Where did this claim come from? | The link names one extraction **and** one fetch event; composite FKs prove both name the same body, so the walk is single-valued |
| What exact content was observed? | `research_artifact_bodies.raw_body` (or its hash after pruning) |
| When did we retrieve it — every time? | `research_fetch_events`, one row per retrieval |
| What did the source itself date? | `research_artifacts.source_published_at` + granularity, NULL when unstated |
| What content type? | `research_fetch_events.declared_content_type`; sniffed type in `research_body_classifications` under a versioned classifier |
| What extraction read it? | `research_extractions`, with model and prompt version |
| Has the live page changed? | A later fetch event yielding a different body; a different artifact only if the change was semantic |
| Which exact span supported the claim? | `claim_evidence_links.locator` (§8) |
| How did we find this page? | `research_source_discoveries`, one row per method |
| Where does this URL redirect? | `research_source_edges`, with the observing fetch event |

## 4. The logical run, and the attempts that execute it

Revision 1 had one `operational_research_runs` table that was simultaneously
the research question and its execution. That produced a contradiction it could
not resolve: `PARTIAL` was called terminal, and retry was said to advance the
same run. A terminal execution cannot become active again without rewriting
history.

Revision 2 splits them (M3-ADR-019).

### 4.1 The run is a question

```
operational_research_runs
  UNIQUE (research_plan_hash)
```

*"What do we know about company X's operations, under policy v2, across these
18 attributes?"* The run carries the company, the policy version, the vertical, the sorted
target attribute keys and the canonicalized immutable plan inputs — all of
which are folded into `research_plan_hash`. It has **no status**, no stage
timestamps, no error and no execution seeds.

Every input that changes the question is inside the hash. Asking the same
question again reuses the run; changing the policy, the vertical or the
attribute set makes it a different question and a different run.

### 4.2 The attempt is an execution

```
operational_research_attempts
  UNIQUE (run_id, attempt_number)
  partial UNIQUE (run_id) WHERE status NOT IN ('COMPLETED','PARTIAL','FAILED')
```

```
PENDING → DISCOVERING → FETCHING → EXTRACTING → ASSERTING → COMPLETED
   any non-terminal ─────────────────────────────────────→ PARTIAL
   any non-terminal ─────────────────────────────────────→ FAILED

terminal: COMPLETED, PARTIAL, FAILED — never re-entered
```

A trigger rejects any transition outside that graph and any transition **out
of** a terminal state. `PARTIAL` means: some evidence was captured and
asserted, some planned work did not complete. It is genuinely terminal.

**Retry creates attempt *n+1* on the same run.** The `PARTIAL` attempt stays
`PARTIAL` forever, with its own stage timestamps and its own error, and the
evidence it captured keeps pointing at it. The partial unique index allows at
most one live attempt per run, so two workers cannot execute the same question
concurrently.

The attempt also freezes `attempt_seed_inputs` — the company's projected
identity domains as of this execution, plus any human seed. Those are
*execution* inputs: re-running the same question next month with a different
projected domain set is the same run and a new attempt.

Fetch events, discoveries and gap events carry `attempt_id` directly, because
each **is** an execution event. Extractions do not: they are reusable results,
and which attempts used one is recorded in `research_attempt_extractions`
(M3-ADR-021). "Which execution captured this?" stays answerable either way,
including for evidence captured by an attempt that later failed.

### 4.3 The gate on canonical writes

Durable per-stage timestamps on the **attempt** —
`discovery_completed_at`, `fetch_completed_at`, `extraction_completed_at` —
gate claim assertion. `status` is descriptive and no guard reads it, inheriting
M2-ADR-031 rather than relearning it: a later stage overwriting `status` must
not be able to launder an incomplete earlier stage into permission to write.

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
  that supports it, carrying the locator and the extraction. One artifact may
  support many claims, and **one claim may need several spans to justify it** —
  most obviously an inference drawn from three job postings and a services
  page. That is one claim with four links.
* **`research_extractions`** — what read the artifact, with what extractor,
  model and prompt version (§10). The link points at it, so "human said so"
  and "GPT-class model said so" are different rows, not a convention.

M3 claims use the `subject_company_id` attribution path, which already exists
and already satisfies `exactly_one_attribution_path`. **No released CHECK
constraint is altered.**

### 5.1.1 What one claim is

Revision 1 said both *"one claim, many independent sources"* and, in acceptance
C7, *"two independent sources yield two claims"*. Those are different models
and revision 2 picks one:

> **A `company_claim` is one atomic assertion produced from one evidence
> lineage.**

A lineage is the set of artifacts justifying the assertion. Two independent
sources are two lineages, therefore **two claims** — they may legitimately
differ in fact type, confidence, source trust and source date, and merging them
would destroy all four. Corroboration is computed by grouping, not by
collapsing.

The dedupe key is an **assertion fingerprint**:

```
sha256(subject_company_id, attribute_key, attribute_registry_version,
       canonical_json(value), fact_type, period_granularity, observed_at,
       lineage_key)           -- lineage_key = sorted distinct artifact ids
```

stored on `company_claims.assertion_fingerprint` (nullable, partial unique
index `WHERE NOT NULL`, so M2 claims are untouched).

It **excludes the extractor**, and that one choice produces every behaviour
required:

| Situation | Result |
| --- | --- |
| Same extraction rerun | Same fingerprint → no new claim |
| **Newer extractor, same lineage, same value** | Same fingerprint → **no new claim**; a new evidence link is appended to the existing one |
| Newer extractor, same lineage, different value | New fingerprint → new claim; a real disagreement within one lineage, now visible |
| Two independent sources, same value | Different lineage → **two claims** |
| Contradictory values | Different fingerprint → separate claims |

The second row is what revision 1 got wrong. Its rule — a newer extractor
creates new claims — meant re-extracting one page three times looked like
threefold corroboration. Corroboration is now counted over **distinct
lineages**, and a lineage has exactly one claim per asserted value by
construction, so double-weighting is unrepresentable rather than merely
discouraged (M3-ADR-017).

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
claim → claim_evidence_links → research_evidence_items
                                   ├─▶ research_extractions
                                   │      → research_text_derivations → body
                                   ├─▶ research_fetch_events → research_sources
                                   └─▶ research_artifact_derivations
                                          → research_artifacts
                                          + source_published_at, language, title

every hop single-valued; three composite FKs force the extraction, the fetch
event and the derivation to name the same body — no trigger
```

---

## 7. The operational attribute taxonomy, v1

Registered in `attribute_definitions` under registry version `M3-1.0`, owned by
milestone M3. **Thirty-one attributes**, not hundreds — sixteen of them
required, the rest optional.

> *Corrected during implementation (revision 4.1).* Revision 4's prose said
> "twenty-four" while the tables below listed thirty-one. The tables are the
> contract and the implementation follows them; the prose was a hand-written
> count that was never mechanically checked. Acceptance N1 now asserts the
> seeded registry matches this section exactly, so the two cannot drift again. Each is here because a
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
text derivation and produced these observations*.

Every extraction stamps:

`text_derivation_id`, `extractor_kind`
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

### 9.3 Deterministic contracts assert; sampled ones do not

A `SAMPLED` extraction contract may produce different output on each run, so a
UNIQUE key that keeps whichever sample landed first is **not** idempotency — it
is one arbitrary draw frozen by a race.

* Only a `DETERMINISTIC` extraction may assert a `company_claim` directly.
* A `SAMPLED` extraction carries a `sample_execution_id` in its identity, so
  repeated sampling appends rather than colliding, and it reaches a claim only
  through a `HUMAN` extraction confirming it.

This is the same principle as §9.1 seen from the other side: there, a model's
confidence could not raise evidential strength; here, a model's *variability*
cannot be laundered into reproducibility by a constraint.

### 9.2 Re-extraction

Re-extraction under a new `extractor_version` is a first-class operation. It
creates a **new** extraction row against the **same** text derivation, and new
claims where the value differs. It never rewrites the old extraction or the old claims. The
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

### 10.1 Independence, and why it is deliberately strict

Corroboration is not "we found it twice". Two lineages corroborate
independently only when **both** the publisher and the document differ:

| Situation | Independent? |
| --- | --- |
| The same page mirrored at two URLs | No — one document, one publisher |
| `/about` and `/services` on the company site both saying it | No — one publisher saying it twice |
| Company website **and** a government registry | **Yes** |
| Company website **and** a third-party scrape of the same exact text | No — identical canonical content is the same document copied, not a second witness |

The last row is why the document test exists. A scraper republishing a
sentence verbatim yields the *same artifact*, because artifact identity is the
canonical content hash, so "it is all over the internet" cannot masquerade as
corroboration.

The policy under-counts rather than over-counts: it will occasionally refuse
two genuine witnesses who happened to publish identical text. Under-counting is
recoverable; over-counting inflates confidence in a claim resting on a single
source, which is the failure this whole system exists to prevent.

### 10.2 Trust must stay explainable

`confidence` is computed from evidence type and source trust. The trust tiers
are **not calibrated yet**, and that is acceptable. What is not acceptable is a
persisted confidence nobody can later reconstruct.

Every evidence link therefore freezes the inputs used at assertion time:
`source_class`, `trust_policy_version` and the `trust_tier` that policy
assigned. A recalibration ships a new policy version; historical claims keep
the tier that produced their stored number, and re-asserting under the new
policy creates new claims. Nothing silently reinterprets a number already
written.

## 11. Temporal semantics and staleness

Four distinct times, and conflating any two of them is a defect:

| Field | Lives on | Means |
| --- | --- | --- |
| `retrieved_at` | fetch event | when *we* fetched it, once per retrieval |
| `source_published_at` | artifact | when the *source* says it was published — NULL when unstated |
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
duplicates.

The gap row holds **that key and `first_raised_at`, and nothing else.**
Everything that changes lives in `operational_research_gap_events` —
`RAISED`, `ATTEMPTED`, `RESOLVED`, `ABANDONED` — each carrying the attempt,
the source where applicable, and the resolving claim when one exists.

Revision 1 declared the gap row append-only while putting
`attempted_source_count`, `last_attempt_at` and `resolved_by_claim_id` on it.
All three change. They are now derived (M3-ADR-018):

| Derived | From |
| --- | --- |
| `attempted_source_count` | `count(DISTINCT source_id) WHERE event_kind = 'ATTEMPTED'` |
| `last_attempt_at` | `max(occurred_at) WHERE event_kind = 'ATTEMPTED'` |
| current status | the latest event's kind |
| `resolved_by_claim_id` | the latest `RESOLVED` event's claim |

Eleven attempts append eleven rows and leave the parent byte-identical. A gap
closes by a `RESOLVED` event, never by deletion, so "we once did not know this"
stays answerable — and "unknown because we never looked" stays distinguishable
from "unknown after eleven sources", which a single counter could record but
not evidence.

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
| Same URL, same bytes, fetched again | **One new `research_fetch_events` row.** No new body, no new artifact, no new derivation. "Seen on Sep 1, Sep 8 and Sep 20" is three events, one body |
| Same URL, different bytes, same canonical content | New **body**, new fetch event, new derivation pointing at the **existing artifact**. Cosmetic change, no new semantics |
| Same URL, different canonical content | New body **and** new artifact. The page genuinely changed |
| Different URL, same bytes | **Same body** (globally unique on the content hash), new source, new fetch event, and a `MIRROR_CANDIDATE` edge |
| Same body under a new canonicalization version | New **derivation** and new **artifact**; the old derivation and artifact are untouched. No refetch |
| Same body under a new text extraction policy | New **text derivation**; no new body, no new artifact, no refetch |
| Same extractor rerun over the same text derivation | Unique on `(text_derivation_id, extractor_id, extractor_version, prompt_template_version)` → no-op |
| Newer extractor, same lineage, same value | **No new claim.** A new evidence link is appended to the existing claim (§5.1.1) |
| Newer extractor, same lineage, different value | New claim — a genuine disagreement within one lineage |
| Same claim value from two *different* lineages | **Two claims, both kept.** Independent corroboration is the strongest state M3 reaches and must be representable |
| Research gap re-raised | Unique on `(company_id, attribute_key, policy_version, gap_kind)` → no new parent; an `ATTEMPTED` event is appended |
| Attempt retried | New attempt on the same run; the terminal attempt is untouched |
| Same question asked again | Same run reused; a new attempt executes it |
| Different target set, vertical or policy | Different `research_plan_hash` → **different run** |

Three rows carry the weight. Deduplicating identical claims from *different*
lineages would destroy exactly the signal worth having. *Not* deduplicating a
re-extraction of the *same* lineage would inflate corroboration without new
evidence — revision 1's rule did precisely that. And recording a repeat fetch
anywhere other than an event log would require mutating evidence, which is why
`research_fetch_events` exists at all. M2 shipped the re-extraction defect once
and had to fix it; M3 starts with the constraint.

---

## 23. Concurrency

Every write is insert-or-ignore on a natural key, enforced by the database —
never SELECT-then-INSERT, which M2 proved is not safe under two real
connections.

| Race | Protection |
| --- | --- |
| Two workers fetch the same URL | `UNIQUE (raw_body_sha256)` on bodies; both converge on one body, and each records its own fetch event |
| Same content discovered via two URLs | `UNIQUE (raw_body_sha256)` on bodies, then `UNIQUE (body_id, strategy, version)` on derivations |
| Same text derivation extracted concurrently | `UNIQUE (text_derivation_id, extractor_id, extractor_version, prompt_template_version)` |
| Same claim asserted concurrently | partial `UNIQUE (assertion_fingerprint)` on `company_claims` |
| Same gap created concurrently | `UNIQUE (company_id, attribute_key, policy_version, gap_kind)` on the identity parent |
| Two attempts on one question | Partial unique index: at most one non-terminal attempt per run |
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

The surface follows the run/attempt split: a **logical run is a question** and
has no status, no stage timestamps and no error, so nothing execution-shaped is
exposed on it. Revision 3's surface filtered runs by status and offered
per-stage timestamps on a run, both of which became false when the split
landed; it also offered `/research-artifacts/{id}/versions`, and versions no
longer exist (M3-ADR-036).

**Research questions and executions**

```
GET  /operational-research/runs                  filter: company, policy, vertical
GET  /operational-research/runs/{id}             the question + its attempts
POST /operational-research/runs                  create or reuse by research_plan_hash
POST /operational-research/runs/{id}/attempts    start an execution
GET  /operational-research/attempts              filter: status, company, run
GET  /operational-research/attempts/{id}         stages, timings, error, seed inputs
POST /operational-research/attempts/{id}/retry   creates attempt n+1 on the same run
```

**Evidence, addressable in its own right**

```
GET  /research-sources/{id}                      locator identity
GET  /research-sources/{id}/fetch-events         full retrieval history
GET  /research-sources/{id}/discoveries          how it was found, per attempt
GET  /research-sources/{id}/edges                redirects, canonicals, mirrors
GET  /research-artifacts/{id}                    semantic identity
GET  /research-artifacts/{id}/derivations        contracts applied to bodies
GET  /research-bodies/{id}                       hash, length, retention state
GET  /research-extractions/{id}                  extractor + contract provenance
GET  /research-evidence-items/{id}               one span, with its full walk
```

**Company knowledge (global)**

```
GET  /companies/{id}/research                    projected operational facts
GET  /companies/{id}/research/claims             claims + their evidence items
GET  /companies/{id}/research/evidence           evidence items about this company
```

**Per-question results (plan-scoped)**

```
GET  /operational-research/runs/{id}/coverage    coverage, confidence, contradictions
GET  /operational-research/runs/{id}/gaps        what this question could not answer
```

Coverage and gaps hang off the **run**, never off the company, because both
depend on the plan's target set, applicability and required/optional split
(§13, M3-ADR-029). A company-scoped coverage endpoint would have to pick one
plan arbitrarily, which is the same defect as revision 3's `PK (company_id)`.

**Identity review — an M3-owned queue**

```
GET  /identity-review-signals                    filter: company, kind, open
GET  /identity-review-signals/{id}               concern + occurrence history
GET  /identity-review-signals/{id}/evidence      the spans that justify it
POST /identity-review-signals/{id}/occurrences/{n}/status
                                                 acknowledge / action / dismiss
```

This queue is **not** wired into M2 (§21.1). M3 owns it end to end.

**Human evidence review**

```
POST /research-claims/{id}/review                confirm or reject — appends
```

Appends a `HUMAN` extraction and, on confirmation, a new claim. It never edits
the model's extraction or claim. A `SAMPLED` extraction reaches a canonical
claim only through this path (§9.3).

Not exposed: the job queue, fetch internals, raw model output by default, and
locator internals beyond what an evidence item needs.

All responses use the existing error envelope. `404` for an unknown company,
run, attempt or signal; `409` for an attempt that has not reached a stage
permitting assertion, and for a second live attempt on one run.

## 26. Retention

Raw HTML and PDF bodies dominate storage and are the least reusable part.

| Class | Retention | Rationale |
| --- | --- | --- |
| Metadata: sources, discoveries, edges, fetch events, classifications, derivations, artifacts | **Permanent** | Provenance must outlive bytes |
| `canonical_content_hash`, `raw_body_sha256` | **Permanent** | Identity and later verification |
| Claims, extractions, evidence links, locators | **Permanent** | The findings themselves |
| `research_text_derivations.extracted_text` | Default 24 months, configurable | Enough to re-resolve a locator and re-extract |
| `research_artifact_bodies.raw_body` | Default 90 days, configurable per content type | Expensive; recoverable by re-fetch when the page still exists |
| Model `raw_output` | Default 90 days; hash permanent | Audit trail without indefinite bulk |

**Deleting a raw body must never make provenance unintelligible.** After
pruning, a body still reports its hash, its byte length and
`body_retention = 'PRUNED'` with the pruning date, while its fetch events keep
the source, every retrieval time and every status, and its artifact keeps
`source_published_at`. A claim's evidence link still resolves to that version and
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

Two further contradictions were found and fixed during revision 1's review: a
projection referred to in the singular where the table is plural, and a table
count that disagreed with its own ownership summary. Both are corrected; the
count is now derived from the ownership table rather than written by hand.

### Revision 2 findings

Revision 2 resolved six structural contradictions, all instances of one rule
being broken: **an append-only row may not contain a value that changes.**

| # | Contradiction | Resolution |
| --- | --- | --- |
| 1 | A "sighting" was promised in the design and in acceptance A1, and no such table existed. The only row that could hold a retrieval time was unique on `(source_id, raw_body_sha256)`, so a second retrieval of identical bytes was unrecordable | `research_fetch_events`, append-only, unique on nothing (M3-ADR-014) |
| 2 | `research_sources` was an append-only identity row carrying three one-to-many facts as three single fields: who discovered it, what linked to it, where it redirects | Locator identity only; discovery and relationships become append-only observations (M3-ADR-015) |
| 3 | One byte string could point at only one artifact, while acceptance A9 required the same bytes under two canonicalization versions to produce two artifacts | `research_artifact_bodies` → `research_artifact_derivations` → `research_artifacts` (M3-ADR-016) |
| 4 | `extracted_text` and its policy version sat on the append-only bytes row, so a parser upgrade required an UPDATE | `research_text_derivations`, keyed `(body, text policy version)` (M3-ADR-017) |
| 5 | The gap row was declared append-only while storing an attempt counter, a last-attempt timestamp and a resolving claim pointer | Identity parent plus `operational_research_gap_events`; all three derived (M3-ADR-018) |
| 6 | `PARTIAL` was called terminal *and* retry was said to advance the same run; the target attribute set determined run identity in prose but not in the key | Logical run (the question) split from attempts (executions), with `target_set_hash` in the run key (M3-ADR-019; superseded in revision 4 by `research_plan_hash`, M3-ADR-025) |

A seventh was found while rechecking, not listed in the brief: **M3-ADR-008
argued against a separate bodies table, which revision 2 then adopted.** The
ADR is amended rather than rewritten — the retention decision it records still
stands, and the table now exists for a different reason. An accepted ADR
silently contradicting the live design is the kind of rot that makes a decision
log worthless.

Two claims in revision 1's own review table needed correcting in light of the
above: "append-only tables requiring a later UPDATE — found and fixed" was true
only for gap and signal status, and missed four further instances; and
"duplicated evidence under retries — prevented" was false for re-extraction,
which double-counted corroboration until M3-ADR-017.

### Revision 3 findings

Revision 3 resolved fourteen further structural issues, under a second rule
added to the first:

> **A globally deduplicated identity row may not carry a fact belonging to one
> of the many contexts that produced it**, and **every provenance walk must be
> single-valued.**

| # | Issue | Resolution |
| --- | --- | --- |
| 1 | `claim → … → body → fetch event` was one-to-many, so a claim could not name the source that supplied its evidence — breaking trust and independence, both of which are per-source | The link names one extraction **and** one fetch event; two composite FKs prove both name the same body (M3-ADR-020) |
| 2 | A lineage was artifact ids alone, so two sources serving one artifact collapsed to one claim | Evidence origin is `(source_id, artifact_id)` (M3-ADR-020) |
| 3 | Independence was undefined, so two mirrors could read as corroboration | Both publisher **and** document must differ (§10.1, M3-ADR-023) |
| 4 | A reusable extraction carried one `attempt_id`, so a second attempt's reuse was unrecordable | `research_attempt_extractions`, plus an audit of all 20 tables for the pattern (M3-ADR-021) |
| 5 | Extraction uniqueness omitted model provider, name, version, schema, determinism and temperature while storing them — a model upgrade collided | `extraction_contract_hash` in the key (M3-ADR-022) |
| 6 | Text-derivation uniqueness omitted `redaction_policy_version` while storing it | `text_derivation_contract_hash` in the key (M3-ADR-022) |
| 7 | Globally deduplicated bodies carried `declared_content_type` — a per-retrieval fact | Moved to the fetch event (M3-ADR-023) |
| 8 | …and `sniffed_content_type` — an algorithm's answer with no named algorithm | `research_body_classifications`, keyed by classifier version (M3-ADR-023) |
| 9 | The immutable run carried `vertical_id` and `seed_inputs` outside its key, so two different questions resolved to one row | `research_plan_hash` over every question-defining input; execution seeds move to the attempt (M3-ADR-025) |
| 10 | Gap event uniqueness permitted one `ATTEMPTED` per source per attempt, so `last_attempt_at` reported the *first* retrieval while the prose claimed eleven rows | `fetch_event_id` joins the key (M3-ADR-024) |
| 11 | Identity signals promised evidence and carried none | `identity_review_signal_evidence` (M3-ADR-028) |
| 12 | Signal statuses had no transition rules, so `ACTIONED → OPEN` was legal | Trigger-enforced graph with terminal states (M3-ADR-027) |
| 13 | Discovery uniqueness omitted the context, discarding a second search query's provenance | `discovery_context_hash` in the key (M3-ADR-026) |
| 14 | "Every edge carries its fetch event" was prose while the column was nullable | `edge_origin` + CHECK; `MIRROR_CANDIDATE` is `DERIVED` and carries both observations (M3-ADR-027) |

A fifteenth was found while auditing rather than listed in the brief: **nullable
columns participating in unique keys had no stated NULL semantics.**
PostgreSQL treats two NULLs as distinct, which silently disables a constraint
exactly where duplicates are most likely — a root-level discovery with no
parent, a signal with no related company, a gap event with no source. Eleven
keys are now `NULLS NOT DISTINCT`.

Revision 1's review table also needs one further correction: "source conflicts
overwritten — prevented" was true, but "one claim having only one possible
source — prevented" was *over*-stated, since until M3-ADR-020 a claim had many
possible sources and no way to say which.

---

### Revision 4 findings

Revision 4 is the implementation-readiness lock. It resolved fourteen further
issues under a third rule added to the first two:

> **A derived value may not be keyed more narrowly than the context that
> determines it**, and **evidence exists independently of whatever consumes
> it.**

| # | Issue | Resolution |
| --- | --- | --- |
| 1 | `operational_research_profiles` had `PK (company_id)` yet stored coverage, whose denominator comes from the plan — two plans for one company could not coexist | Company-global facts stay on the company projection; coverage moves to `operational_research_plan_profiles`, keyed by run (M3-ADR-029) |
| 2 | Gaps mixed two meanings: global evidence state and plan-specific incompleteness | Gaps are **plan-specific**, keyed by run (M3-ADR-029) |
| 3 | Evidence hung off `claim_evidence_links`, so an identity conflict found before any claim had nowhere to live — preserving it meant fabricating a claim | `research_evidence_items` is first-class; claims and signals both reference it (M3-ADR-030) |
| 4 | Evidence named `body_id` and `artifact_id`, but one body has many derivations, so `body_id` never determined the artifact | Evidence names the exact `artifact_derivation_id`; three composite FKs bind all paths to one body, declaratively (M3-ADR-031) |
| 5 | `source_published_at` sat on the globally deduplicated artifact, forcing publication metadata into the canonical hash — which would have split mirrors into two artifacts and broken independence detection | Publication metadata moves to the derivation that observed it; the hash stays purely semantic (M3-ADR-031) |
| 6 | The assertion fingerprint omitted every policy version, so re-asserting under trust v2 collided with the v1 claim and the documented behaviour was unreachable | `assertion_contract_hash` joins the fingerprint, and so does `unit` (M3-ADR-032) |
| 7 | Claim confidence was prose: "computed from evidence type and source trust" | A versioned formula with a stated contract for one link, many links, repeated publishers, independent publishers, mixed tiers and inference (M3-ADR-035) |
| 8 | `publisher_policy_version` was named and persisted nowhere, so a mapping change would silently re-score historical corroboration | Frozen on the evidence item; recorded on both projections (M3-ADR-033) |
| 9 | `NOT_MODIFIED` had no defined `body_id` semantics, so a 304 either fabricated bytes or dropped out of provenance | A 304 references the validated body and records the validator; CHECK per outcome (M3-ADR-037) |
| 10 | A `SAMPLED` contract was treated as idempotent because a UNIQUE key kept the first sample | Deterministic contracts assert; sampled ones carry a sample slot and reach a claim only via human confirmation (M3-ADR-038) |
| 11 | `attempt_seed_inputs` was promised in prose and in acceptance L11 and never declared | Declared, canonicalized, hashed, frozen at start (M3-ADR-036) |
| 12 | Docs claimed signals are "consumed by M2's existing human-review path" — **verified false against live code**: `append_human_decision` takes a `ProviderEntity` and `HumanReviewRequest.provider_entity_id` is required, while a company-level signal has none | The queue is M3-owned end to end; the M2 workflow that might consume it is explicitly future work (§21.1) |
| 13 | `evidence_digest` was an identity key *and* the docs said evidence is appended to an open signal — a hash over a growing set cannot be both | Identity is the semantic concern; review episodes are occurrences (M3-ADR-034) |
| 14 | Stale revision-2 names survived in live design text and in the API surface | Mechanically swept; historical mentions retained only where labelled |

## 30. Revision 4 freeze criteria

The brief's seven conditions, each checked mechanically rather than asserted:

| # | Criterion | Status |
| --- | --- | --- |
| 1 | Every acceptance scenario is representable by the schema | **Met** — every table named in acceptance exists in the graph; 115 scenarios |
| 2 | Every provenance walk is single-valued | **Met** — evidence names one extraction, one fetch event, one derivation; three composite FKs bind them to one body |
| 3 | No immutable identity row contains contextual mutable state | **Met** — §5b classifies every object; bodies, artifacts, sources and signals hold only identity |
| 4 | Plan-specific state cannot overwrite another plan | **Met** — coverage keyed by `run_id`, gaps keyed by `(run_id, attribute, kind)` |
| 5 | Evidence exists independently of claims and signals | **Met** — `research_evidence_items` is referenced by both and owned by neither |
| 6 | Persisted confidence and corroboration are reproducible | **Met** — trust inputs frozen on the link, policy versions inside `assertion_contract_hash`, publisher policy frozen on the evidence item and recorded on both projections |
| 7 | Cross-document mechanical audit returns zero stale live-design references | **Met** — zero; the remaining mentions are labelled prior-revision history |

**M3 DESIGN REVISION 4 — IMPLEMENTATION READY.**

Counts, computed from the documents: **23 tables · 115 acceptance scenarios ·
38 ADRs.** Three M2 objects are touched, all additively:
`attribute_definitions.owner_milestone`,
`company_claims.assertion_fingerprint` with a partial unique index, and a
deferred constraint trigger on `company_claims`.

## 31. Open questions

These do **not** block implementation. Each is a calibration or a policy table
to be populated, not a structural unknown — the contracts that consume them are
versioned, so populating them later is a new policy version rather than a
schema change.

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

8. **Confidence weights.** The formula and its version are fixed (§4.5 of the
   schema graph); `base(fact_type)`, the trust tiers, the corroboration
   saturation curve and the inference penalties are not calibrated. They ship
   as fixture configuration under `confidence_formula_version` 1.
9. **Publisher override table.** Which job boards, registries and directories
   count as publishers in their own right is unpopulated under
   `publisher_policy_version` 1.
10. **Signal concern normalization.** `normalized_concern` in the signal
    fingerprint needs a stated normalization for names like "XYZ Holdings" vs
    "XYZ Holdings Inc." — currently the same `normalize_name` M2 uses, which
    may be too aggressive for identity concerns.
