# M2 — Company Discovery and Entity Resolution

**Status:** design only, revision 4. No M2 code, migrations or tables exist in
this repository, and none should be created from this document without a
separate implementation authorization.

**Revision 4** makes the rebuild contract genuinely deterministic, closes a
cross-entity supersession hole, corrects body/version cardinality and adds a
per-provider canonicalization contract — see [§0](#0-what-changed).

**Milestone position:** M2 sits between M1 (contextual market intelligence) and
M3 (operational research). M1 answers *"which market × vertical × ICP × channel
× ticket contexts are worth pursuing?"* M2 answers *"which real commercial
organizations exist in a chosen context, and are we sure they are distinct?"*

M2 is **not** "scrape companies". Scraping is one possible provider
implementation. M2 is the canonical-identity problem: given noisy, overlapping,
partially wrong records from several providers, maintain a defensible registry
of commercial organizations with traceable evidence for every claim.

---

## 0. What changed

| # | Invariant violated in revision 2 | Resolution |
| --- | --- | --- |
| 1 | `companies` was a truncatable projection, yet claims and decisions held FKs to it — rebuild would have had to regenerate UUIDs | `companies` becomes a minimal **durable identity anchor**, never truncated. Business attributes move to `company_profiles` and the other derived tables (§3, §5.6) |
| 2 | `UNIQUE (supersedes_decision_id)` prevents forked children but **not** two concurrent *root* decisions for one provider entity | A second partial unique index on the chain root. Two indexes together force exactly one linear chain, hence exactly one head, with no lock and no mutable state (§6.3, M2-ADR-018) |
| 3 | "Verbatim raw payload" stored as JSONB — JSONB preserves neither bytes, whitespace, key order nor duplicate keys | Byte-faithful policy: `provider_record_bodies` holds the literal bytes; `provider_record_versions` holds the parsed payload and both digests. Identity stays on the canonical semantic digest (§4.3, M2-ADR-019) |
| 4 | Every provider was assumed to issue a durable external id | Explicit `identity_capability` per provider: `NATIVE_EXTERNAL_ID`, `DERIVED_STABLE_KEY`, `CONTENT_ONLY`, with derived keys never presented as provider-issued (§4.2, M2-ADR-020) |
| 5 | `company_relationships` was append-only but relationships start, end and get corrected | Relationships become claims with validity intervals and additive supersession; the relationship table becomes a derived projection. Inverse types removed as stored data (§12, M2-ADR-021) |
| 6 | `company_claims` was an untyped EAV blob with no schema contract | A versioned **attribute registry** defining value kind, type, units, fact types, projection and conflict strategy, cardinality and indexing per attribute (§5.2, M2-ADR-022) |
| 7 | Claims carried `company_id`, so a corrected resolution would have required rewriting immutable claims | `company_id` is **removed** from provider-sourced claims. Attribution is derived through the effective decision (§5.5, M2-ADR-023) |

### Revision 4

| # | Invariant violated in revision 3 | Resolution |
| --- | --- | --- |
| 8 | `company_profiles.last_projected_at` and a "currently-valid" relationship projection both made projection content depend on wall-clock time, contradicting byte-identical rebuild | Operational metadata moves to `projection_runs`; the relationship projection stores **deterministic effective intervals** and the date filter moves into a view. The equality contract is defined precisely (§3.4, M2-ADR-024) |
| 9 | Precedence rule 5 said "keep the current projected value" — meaningless when rebuilding from empty, and therefore itself non-deterministic | Replaced by a deterministic total order on stored claim ids (§5.3, M2-ADR-024) |
| 10 | The partial indexes forced one root and one child, but nothing stopped a decision of entity **B** superseding a decision of entity **A** | Composite self-foreign-key on `(supersedes_decision_id, provider_entity_id)` makes cross-entity supersession unrepresentable (§6.3, M2-ADR-025) |
| 11 | `raw_body_sha256` sat on `provider_record_versions`, but one semantic version may legitimately have many byte-different bodies | Byte fidelity moves to `provider_record_bodies`, which becomes 1:N under a version (§4.3, M2-ADR-026) |
| 12 | M0's canonical-JSON algorithm was assumed universal, though providers return CSV, XML and HTML | Each provider declares a versioned `canonicalization_strategy`, stamped on every version (§4.4, M2-ADR-027) |

---

## 1. The canonical identity unit

### 1.1 Definition

> A **company** is a *canonical commercial operating organization* — the unit
> BoRo would sell to, contract with and deliver to as **one account**.

It is explicitly **not** necessarily one legal entity, not one provider record,
and not one domain.

The operational test, in order:

1. **Commercial autonomy.** Does it make its own buying decision for an
   operations project?
2. **Operational coherence.** Does it run one operation — its own work orders,
   dispatch, technicians, service contracts?
3. **Account identity.** Would two BoRo reps working these separately be
   duplicating each other, or covering genuinely different accounts?

If all three say "one", it is one company, regardless of how many legal
entities, domains or provider records sit underneath.

### 1.2 Why not the legal entity

A legal entity is a *jurisdictional artifact*. One German operating business may
hold a GmbH plus a property-holding entity plus a dormant predecessor.
Targeting the legal entity produces three "companies" where one account exists.

Legal entities are recorded — as claims with `attribute_key = 'legal_entity'`
carrying registry identifiers — but they are *evidence about* a company, never
the identity of one.

### 1.3 Implications by structure

| Structure | Modelling | Reasoning |
| --- | --- | --- |
| **Branch / depot** | **Same company.** A location row plus a presence row | No independent buying authority |
| **Subsidiary, operationally autonomous** | **Separate company**, `SUBSIDIARY_OF` relationship claim | Buys, contracts and delivers on its own |
| **Subsidiary, administratively controlled** | **Same company**, recorded as a `legal_entity` claim | Signing happens at the parent |
| **Holding group** | **Relationship node**, usually not itself a target | No field operations to re-architect |
| **Franchisee** | **Separate company**, `FRANCHISE_OF` | Buys independently; the most common false-merge trap |
| **Franchisor** | **Separate company** | A different account and a different motion |
| **Multiple legal entities on one domain** | Apply the test above; domain equality is a *candidate* signal only (§8) | A group domain covers many autonomous businesses |
| **Regional operating entity** | **Separate company** if it holds its own P&L and buying authority | "Acme UK" vs "Acme DACH" are usually separate accounts |

### 1.4 Identity policy is explicit and versioned

These rules are **configuration**, stored as a versioned
`identity_policy_version` stamped on every resolution decision and on every
identity anchor at creation. Changing the policy is a dated decision, not a
silent behaviour change — the same discipline as `scoring_models.version` in M0.

## 2. Pipeline

```
Discovery Request          M1 context: market × vertical × ICP × channel × ticket
   ↓
Provider Adapter           provider-specific I/O, isolated
   ↓
Provider Entity            provider-side identity, per capability model (§4.2)
   ↓
Provider Record Version    immutable observation: parsed payload + digests
   ↓
Normalization              pure function: provider schema → candidate shape
   ↓
Candidate Matching         retrieve plausible existing companies
   ↓
Resolution Decision        append-only, one linear chain per entity (§6.3)
   ↓
Company Claims             typed assertions against the attribute registry (§5)
   ↓
Derived Projections        profiles, names, domains, locations, presences,
                           verticals, relationships — all rebuildable
```

Every arrow attaches provenance. No arrow discards it.

## 3. Identity anchors, evidence and projections

Revision 2 called `companies` a projection while claims and decisions held
foreign keys to it. Truncating and rebuilding would have had to regenerate
UUIDs, breaking every reference. Three distinct roles are now separated:

### 3.1 The three tiers

| Tier | Tables | Truncatable? | Why |
| --- | --- | --- | --- |
| **Identity anchors** | `companies` | **Never** | UUIDs are referenced by evidence. They are minted by a decision and are not derivable |
| **Evidence** | `provider_entities`, `provider_record_versions`, `provider_record_bodies`, `provider_record_normalizations`, `provider_record_sightings`, `discovery_runs`, `discovery_queries`, `entity_resolution_candidates`, `entity_resolution_decisions`, `company_claims`, `company_relationship_claims` | **Never** | Append-only. This *is* the record |
| **Derived projections** | `company_profiles`, `company_names`, `company_domains`, `company_locations`, `company_market_presences`, `company_verticals`, `company_relationships`, `entity_resolution_heads` | **Yes — fully** | Pure functions of evidence + registry + policy version |
| **Configuration** | `discovery_providers`, `attribute_definitions` | Rebuildable from the code seed | Versioned contracts, seeded exactly as M0 seeds `scoring_models`. Claims and decisions reference the *version*, not the row |
| **Operational** | `projection_runs` | Yes, independently | Wall-clock timing, row counts and content digests of each rebuild. Deliberately **outside** every deterministic projection payload (§3.4) |

### 3.2 `companies` — the durable anchor

Only what is required to preserve identity and lifecycle:

| Column | Notes |
| --- | --- |
| `id uuid pk` | The anchor. **Never regenerated.** |
| `created_at timestamptz` | When the identity was minted |
| `identity_policy_version text` | Which policy was in force at creation |
| `lifecycle_status text` | `ACTIVE` / `MERGED` / `DISSOLVED` |
| `merged_into_company_id uuid null` | Self-FK, set when this identity retires in favour of another |

Deliberately **absent**: `canonical_name`, `primary_domain`, `employee_count_*`,
`legal_form`, `founded_year`, `market_id`, `vertical_id`, `score`. Every one is
a claim-derived projection.

**No `created_by_decision_id` column.** It would create a circular foreign key
(`companies → decisions → companies`). The creating decision is *derived*:

```sql
SELECT d.* FROM entity_resolution_decisions d
WHERE d.company_id = $1 AND d.decision = 'CREATED_NEW'
ORDER BY d.decided_at ASC LIMIT 1;
```

**Why `lifecycle_status` is identity-level, not derived.** It governs whether an
id may be referenced as a live target, so it must be readable without running a
projection. It is maintained transactionally by the merge/split writer and is
**reconcilable** against decisions — a consistency check, not a rebuild input:

```sql
-- must return zero rows
SELECT c.id FROM companies c
LEFT JOIN LATERAL (
  SELECT d.decision, d.company_id AS survivor
  FROM entity_resolution_decisions d
  WHERE d.decision IN ('MERGED','SPLIT') AND d.merged_company_id = c.id
    AND NOT EXISTS (SELECT 1 FROM entity_resolution_decisions s
                    WHERE s.supersedes_decision_id = d.id)
  ORDER BY d.decided_at DESC LIMIT 1
) eff ON TRUE
WHERE (eff.decision = 'MERGED') <> (c.lifecycle_status = 'MERGED');
```

### 3.3 `company_profiles` — the business projection

One row per company, entirely derived: `canonical_name`, `primary_domain`,
`legal_form`, `founded_year`, `employee_count_min/max`,
`derived_from_claim_ids uuid[]`, `projection_conflict boolean`,
`attribute_registry_version`.

**No `last_projected_at`.** A wall-clock timestamp inside a projection payload
would make byte-identical rebuild impossible by construction. Rebuild timing
lives in `projection_runs` (§3.4).

Two rules apply to every derived projection, not just this one:

* **Natural keys only — no surrogate primary keys.** A generated UUID would
  differ on every rebuild, so `company_profiles` is keyed on `company_id`,
  `company_names` on `(company_id, name_normalized, name_type)`,
  `company_relationships` on `(from_company_id, to_company_id,
  relationship_type)`, and so on. A projection with a surrogate key cannot
  satisfy the rebuild contract.
* **Array columns are sorted.** `derived_from_claim_ids` is stored ascending,
  so two rebuilds cannot differ by ordering alone.

### 3.4 The rebuild contract

> **All derived projections may be truncated and rebuilt byte-identically,
> while identity anchors, claims, decisions and relationship claims are never
> truncated.**

"Byte-identical" needs a precise definition, or the claim is untestable.

**The projection function.** Let `P` be the projection function. Its inputs are
exactly: the evidence tables, `attribute_registry_version` and
`identity_policy_version`. `P` is **pure** — it reads no clock, no sequence, no
random source, and no prior projection state.

**What the equality contract compares.** For every deterministic projection
table, the full set of rows over **all of its columns**. There are no excluded
columns, because nothing non-deterministic is stored in a projection at all:

| Hazard | How it is eliminated |
| --- | --- |
| Rebuild timestamps | Live in `projection_runs`, never in a projection |
| Surrogate keys | Forbidden — projections use natural keys (§3.3) |
| Array ordering | Arrays are stored sorted (§3.3) |
| Wall-clock filtering | Projections store intervals; date filters live in views (§12.4) |
| Tie-breaks on prior state | Replaced by a total order on stored claim ids (§5.3) |
| Row insertion order | Compared as a set, via a sorted digest |

Mechanically, the comparison is a canonical digest per table:

```sql
SELECT md5(string_agg(t::text, '|' ORDER BY t::text))
FROM company_profiles t;          -- and likewise for each projection table
```

`projection_runs` records that digest per table per rebuild, so drift is
detectable without keeping a second copy of the data.

**What is deliberately outside the contract.** `projection_runs` itself —
`started_at`, `completed_at`, `row_counts`, `content_digests`,
`triggered_by`. It is operational telemetry about the rebuild, not a
projection of evidence, and it is truncatable independently without affecting
any equality claim.

**What is exempt, and why.** The anchor tier. Company UUIDs are *minted*, not
computed; making them deterministic would require hashing an identity that
§1.1 defines as a judgement, reintroducing exactly the false-merge problem the
identity policy exists to prevent.

**What legitimately changes over time.** Views over the projections — such as
`current_company_relationships` — depend on `CURRENT_DATE` by design. A view
returning different rows tomorrow is correct behaviour and says nothing about
the projection beneath it, which is unchanged. Acceptance scenarios B3d–B3f
assert exactly this separation.

## 4. Provider layer

### 4.1 Identity and observations, separated

* **`provider_entities`** — stable provider-side identity.
  `UNIQUE (provider_id, provider_external_id)`.
* **`provider_record_versions`** — immutable observations.
  `UNIQUE (provider_entity_id, canonical_payload_hash)`.

Identical semantic payload → no new version; a sighting is recorded. Changed
payload → new version under the same entity. Nothing is overwritten, and **no
resolution state lives here** (§5.5).

### 4.2 Provider identity capability

Not every provider issues a durable external id. `discovery_providers`
declares which case applies, and the entity records how its key was obtained —
so a derived key is never mistaken for a provider-issued one.

| `identity_capability` | Meaning | `provider_entities.external_id_kind` |
| --- | --- | --- |
| `NATIVE_EXTERNAL_ID` | The provider issues a durable id it promises to keep stable | `NATIVE` |
| `DERIVED_STABLE_KEY` | No provider id; the adapter derives one from normalized fields | `DERIVED` |
| `CONTENT_ONLY` | No stable object identity is possible at all | `CONTENT` |

**`DERIVED_STABLE_KEY` requires, in provider configuration:**

* `key_fields` — the ordered list of normalized fields that participate, e.g.
  `[registrable_domain]`, or `[normalized_legal_name, country, postal_code]`.
  Declared per provider, never improvised per record.
* `key_algorithm_version` — stamped on every entity, so a change of derivation
  is a versioned event rather than silent churn.
* The derived key is stored in `provider_external_id` with
  `external_id_kind = 'DERIVED'` and the algorithm version alongside it.

**Collision handling.** Two genuinely different organizations can produce one
derived key (two "Schmidt GmbH" in the same postcode). Detection: more than one
version under a single derived entity whose *non-key* normalized identity fields
disagree materially. Consequence: the entity is flagged
`identity_collision = true`, and **resolution must route it to `AMBIGUOUS`** —
it may never auto-match. A colliding derived key is weaker evidence than no key.

**When key fields change.** The derived key changes, so a **new**
`provider_entity` is created. The old one persists untouched. The two are linked
in the only honest way available: the new entity's resolution decision matches
it to the **same company**, which is queryable. No mutable pointer is invented,
and no claim is made that the provider said they were the same object — because
it did not.

**`CONTENT_ONLY`.** `provider_external_id` is the canonical payload hash and
`external_id_kind = 'CONTENT'`. Consequence, stated plainly: any payload change
creates a new entity, so such providers **cannot express "the same object
changed"**. Their evidence is correspondingly weaker and their trust tier should
reflect that. They are usable for discovering candidates, not for tracking
change over time.

### 4.3 Raw payload fidelity

Revision 2 called the stored JSONB "verbatim". JSONB preserves neither original
bytes, whitespace, key ordering, nor duplicate keys — so the claim was false.

**Policy A — byte-faithful — is adopted**, with byte fidelity living on the
body, not the version:

| Artifact | Where | Purpose |
| --- | --- | --- |
| `canonical_payload_hash` | `provider_record_versions` | **Semantic identity and idempotency** |
| `parsed_payload jsonb` | `provider_record_versions` | Parsed, queryable representation |
| `canonicalization_strategy` + `_version` | `provider_record_versions` | Which algorithm produced the hash (§4.4) |
| `raw_body bytea` | `provider_record_bodies` | The literal bytes, exactly as received |
| `raw_body_sha256` | `provider_record_bodies` | Digest of *those* bytes |
| `content_type`, `retrieved_at`, `discovery_query_id` | `provider_record_bodies` | Which fetch produced these bytes |

**One semantic version may have many byte-faithful bodies.** Revision 3 put
`raw_body_sha256` on the version, which silently asserted one body per version —
directly contradicting the decision that identity is *semantic*. If a provider
reindents its JSON between two calls, that is one version and two bodies.

```
provider_record_versions  1 ──── N  provider_record_bodies
     canonical_payload_hash              raw_body_sha256
     UNIQUE (entity, canonical_hash)     UNIQUE (version_id, raw_body_sha256)
```

| Event | Version | Body | Sighting |
| --- | --- | --- | --- |
| Identical bytes re-fetched | unchanged | **deduped** by the unique constraint | recorded |
| Reindented / reordered JSON, same semantics | **unchanged** | **new body** | recorded |
| Semantic payload change | **new version** | new body under it | recorded |
| Bodies pruned by retention | **untouched** | removed | untouched |

`provider_record_bodies` carries a surrogate `id` because it is *evidence*, not
a projection — the no-surrogate-key rule in §3.3 applies only to the derived
tier.

Bodies live in their own table so a retention or redaction policy can prune
them without touching the append-only version row. Pruning a prunable table is
not an `UPDATE` on an immutable one — and the design says so explicitly rather
than quietly allowing an `UPDATE ... SET raw_body = NULL`.

`provider_record_normalizations` follows the same pattern, keyed by
`(version_id, normalizer_version)`, so a normalizer upgrade re-derives without
destroying the previous output and the raw table stays strictly immutable.

### 4.4 Canonicalization is per provider, and versioned

**M0's canonical-JSON algorithm is not automatically universal.** It is defined
for JSON documents. Providers return CSV exports, XML feeds, HTML pages and
binary formats, none of which have a meaningful "sorted keys, tight
separators" form.

Each provider therefore declares, in `discovery_providers`:

| Field | Purpose |
| --- | --- |
| `canonicalization_strategy` | Named algorithm, e.g. `JSON_CANONICAL_V1` |
| `canonicalization_version` | Version of that algorithm |

and every `provider_record_versions` row stamps both, so the hash is always
interpretable and a strategy change is a visible, dated event rather than
silent churn.

`JSON_CANONICAL_V1` is defined as M0's algorithm — sorted keys, tight
separators, `ensure_ascii=False`, `allow_nan=False` — and JSON providers may
reuse it directly.

For non-JSON sources the strategy must be **explicit and versioned** before that
adapter is written. None is specified here: inventing an HTML or CSV
canonicalization in the abstract, with no provider to validate it against,
would be guessing. What the design fixes now is that no adapter may quietly
borrow the JSON algorithm for a payload it does not fit.

**Consequence worth stating.** Changing a provider's canonicalization strategy
changes its canonical hashes, so previously-seen payloads will produce **new
versions**. That is a migration event, recorded as open question 9.

**Identity remains the canonical semantic digest.** A provider that reformats
its JSON, or reorders keys between calls, must not manufacture a spurious
version — the same reasoning that makes a reindented source file a no-op import
in M0 (ADR-017).

### 4.5 Runs and queries

**`discovery_runs`** — one execution: `provider_id`, M1 context as real FKs
(`market_id`, `vertical_id`, `icp_id`, `channel_id` — the ADR-014 pattern),
`status` (§7), `adapter_version`, `allow_partial_resolution`, timing, cost,
error.

**`discovery_queries`** — the exact query: parameters, pagination cursor, page
number, result count. Reproducibility lives here.

### 4.6 Provider abstraction

```
ProviderAdapter
  .capabilities()   -> filters, geography granularity, rate limits,
                       identity_capability, key_fields, key_algorithm_version,
                       canonicalization_strategy + version
  .canonicalize(raw)-> bytes                  # pure; per-provider (§4.4)
  .search(query)    -> Iterator[RawRecord]    # I/O, may fail or partially fail
  .derive_key(rec)  -> str | None             # pure; only for DERIVED_STABLE_KEY
  .normalize(raw)   -> CandidateCompany       # pure, deterministic, testable
```

Adapters never write canonical tables. `derive_key` and `normalize` are pure
functions — no I/O, no database — so both are testable from fixtures with zero
network, preserving the M0/M1 discipline.

## 5. Claims — the evidence model

### 5.1 What a claim is

One row per atomic assertion, append-only, typed by the attribute registry. The
source of truth for everything in the projection tier.

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `attribute_key text` | Must exist in the registry version in force |
| `attribute_registry_version text` | Which contract this claim was written against |
| `value_jsonb jsonb` | Canonical value, shaped by the registry |
| `value_numeric numeric null` | Typed shadow — registry-derived |
| `value_text text null` | Typed shadow — registry-derived |
| `value_ref_id uuid null` | Typed shadow for `REFERENCE` attributes (vertical, market) |
| `unit text null` | Must be in the registry's `allowed_units` |
| `fact_type text null` | M0 vocabulary; must be in the registry's `allowed_fact_types` |
| `availability text` | `OBSERVED` / `NOT_AVAILABLE` — M0 semantics |
| `confidence numeric null` | From M0's single confidence algorithm |
| `provider_record_version_id uuid null` | Provider-sourced attribution path |
| `subject_company_id uuid null` | Direct attribution path (human/derived claims) |
| `resolution_decision_id uuid null` | The decision in force **when written** — history, not attribution |
| `observed_at date null` | Only when the provider states a date |
| `period_granularity text` | `DATE` / `YEAR` / `SNAPSHOT` / `UNDATED` |
| `created_at timestamptz` | |

M0's constraints carry over: `NOT_AVAILABLE` implies NULL value and NULL fact
type; `observed_at` present **iff** granularity is `DATE`. Unknown is never
zero; a year is never widened into a date.

Exactly one attribution path is permitted:

```sql
CHECK ( (provider_record_version_id IS NOT NULL) <> (subject_company_id IS NOT NULL) )
```

### 5.2 The attribute registry

A generic `attribute_key` with no contract is an untyped EAV store. The registry
supplies the missing contract: a versioned definition per attribute, held in
code as configuration and persisted at seed time, exactly as M0 persists
`scoring_models`.

Per attribute:

| Field | Purpose |
| --- | --- |
| `attribute_key` | Stable key |
| `value_kind` | `SCALAR` / `RANGE` / `SET` |
| `value_type` | `TEXT` / `INTEGER` / `NUMERIC` / `DATE` / `BOOLEAN` / `REFERENCE` / `DOMAIN` / `STRUCTURED` |
| `allowed_units` | Or empty for unitless |
| `allowed_fact_types` | Subset of M0's five |
| `cardinality` | `ONE` / `MANY` |
| `projection_strategy` | `HIGHEST_PRECEDENCE` / `ENVELOPE` / `UNION` / `LATEST` |
| `conflict_strategy` | `PRECEDENCE` / `ENVELOPE` / `FLAG_AMBIGUOUS` |
| `shadow_column` | Which typed shadow is authoritative |
| `value_schema` | JSON Schema for `value_jsonb` |
| `target_projection` | Which projection table/column it feeds |
| `index_strategy` | What the projection needs indexed |

Seed registry:

| `attribute_key` | kind / type | card. | projection | conflict | shadow |
| --- | --- | --- | --- | --- | --- |
| `employee_count` | RANGE / INTEGER | ONE | ENVELOPE | ENVELOPE | `value_numeric` |
| `revenue_band` | RANGE / NUMERIC | ONE | ENVELOPE | ENVELOPE | `value_numeric` |
| `founded_year` | SCALAR / INTEGER | ONE | HIGHEST_PRECEDENCE | PRECEDENCE | `value_numeric` |
| `legal_name` | SCALAR / TEXT | ONE | HIGHEST_PRECEDENCE | PRECEDENCE | `value_text` |
| `trading_name` | SET / TEXT | MANY | UNION | — | `value_text` |
| `legal_form` | SCALAR / TEXT | ONE | HIGHEST_PRECEDENCE | PRECEDENCE | `value_text` |
| `legal_entity` | SET / STRUCTURED | MANY | UNION | FLAG_AMBIGUOUS | — |
| `domain` | SET / DOMAIN | MANY | UNION | PRECEDENCE | `value_text` |
| `vertical` | SET / REFERENCE | MANY | UNION | — | `value_ref_id` |
| `market_presence` | SET / REFERENCE | MANY | UNION | — | `value_ref_id` |
| `location` | SET / STRUCTURED | MANY | UNION | — | — |
| `relationship` | SET / STRUCTURED | MANY | UNION | — | — |

**`value_jsonb` and the typed shadows must never disagree.** Preferred
enforcement is a PostgreSQL **generated column** where a single extraction
expression suffices — for example
`value_numeric numeric GENERATED ALWAYS AS ((value_jsonb->>'value')::numeric) STORED`
for `SCALAR`/`RANGE` numerics — which makes disagreement unrepresentable rather
than merely forbidden. Where the shape varies, the writer populates the shadow
from the registry's declared extractor and acceptance scenario B8 asserts
agreement across every claim in the database.

A claim whose `attribute_key` is absent from its `attribute_registry_version`,
or whose value fails that version's `value_schema`, is rejected at write time.

### 5.3 Projection precedence

For `HIGHEST_PRECEDENCE` attributes, in order:

1. **Human review wins** — a claim whose decision method is `HUMAN_REVIEW`.
2. **Fact type** — FACT > PROXY > ESTIMATE > INFERENCE > HYPOTHESIS.
3. **Provider trust tier** — versioned `discovery_providers` configuration.
4. **Recency** — `observed_at`, then `retrieved_at`.
5. **Deterministic tie-break** — the claim with the lowest `claim.id` wins, and
   `projection_conflict = true` is set.

   Revision 3 said "keep the current projected value", which has no meaning
   when rebuilding from an empty table and therefore made the projection
   depend on its own prior state. Claim ids are stored and immutable, so
   ordering on them is a total order that survives truncation.

For `ENVELOPE` attributes, do **not** pick a winner among equally-ranked claims:
project min-of-mins and max-of-maxes, recording the contributing claim ids.
Providers disagreeing is information, not noise — the same reasoning that keeps
M0 from collapsing score and coverage into one number.

For `UNION` attributes, every non-superseded claim contributes a row to the
target projection. Where several claims collapse to one projection row, the
contributing ids are recorded sorted, so the row is identical regardless of the
order claims were read in.

### 5.4 Corrections without mutation

* A projection is never fixed by editing a claim. A wrong projection means the
  precedence rules, the trust tiers or the registry are wrong — all versioned
  configuration.
* A retracted claim is a **new** claim with `availability = NOT_AVAILABLE` and a
  superseding decision — never a delete.

### 5.5 Attribution is derived, never stored

Revision 2 put `company_id` on every claim. When a resolution was corrected
from company A to company B, that column was either wrong or had to be
rewritten — and rewriting an append-only table is exactly what the design
forbids.

**`company_id` is removed from provider-sourced claims.** Attribution runs
through the effective decision:

```
claim → provider_record_version → provider_entity → effective decision → company_id
```

```sql
CREATE VIEW company_claims_effective AS
SELECT c.*, h.company_id
FROM company_claims c
JOIN provider_record_versions v ON v.id = c.provider_record_version_id
JOIN entity_resolution_heads  h ON h.provider_entity_id = v.provider_entity_id
WHERE h.company_id IS NOT NULL
UNION ALL
SELECT c.*, c.subject_company_id AS company_id
FROM company_claims c
WHERE c.subject_company_id IS NOT NULL;
```

Consequences, all of them intended:

* Correcting a resolution from A to B **moves the whole claim history** to B,
  with zero writes to `company_claims`. Everything that provider said was, in
  fact, about B.
* A's projection loses those claims on the next rebuild — no contamination.
* `resolution_decision_id` remains on the claim as **history**: "which decision
  was in force when this was written". It is never used for attribution.

**Projection input rule, stated once:**

> A projection rebuild consumes **only** claims whose provider entity resolves,
> under the selected `identity_policy_version`, to an **effective** (non-superseded)
> decision naming that company — plus directly-attributed claims. Claims under
> superseded decisions remain fully queryable as history and contribute to no
> current projection.

### 5.6 The derived tables

* **`company_profiles`** — one row per company (§3.3).
* **`company_names`** — from `legal_name` / `trading_name` claims;
  `name_type ∈ {LEGAL, TRADING, FORMER, LOCALIZED, PROVIDER_DISPLAY}`.
  Normalization is stored *alongside* the raw name, never instead of it.
* **`company_domains`** — from `domain` claims; `domain_role ∈ {IDENTITY,
  ALTERNATE, REDIRECT, COUNTRY_TLD, DEFUNCT, GROUP}` (§8).
* **`company_locations`** — physical or registered places only:
  `location_type ∈ {HEADQUARTERS, BRANCH, DEPOT, REGISTERED_OFFICE}`.
  `SERVICE_AREA` remains excluded: a service area is not a place, and treating
  it as one fabricates buildings out of coverage statements.
* **`company_market_presences`** — operating geography:
  `presence_type ∈ {HEADQUARTERED, BRANCH, OPERATES, SERVES_REMOTELY}`. A
  location implies a presence; a presence does not imply a location.
* **`company_verticals`** — with `classification_method`, `fact_type`,
  `confidence`. A keyword-inferred vertical is `INFERENCE`, never `FACT`.
* **`company_relationships`** — effective relationships only (§12).

## 6. Entity resolution

### 6.1 Candidates

`entity_resolution_candidates`: `provider_record_version_id`,
`candidate_company_id`, `match_signals jsonb`, `score numeric`, `tier`,
`created_at`. Append-only. Rejected candidates are retained — a rejected
candidate is evidence about *why* a decision was made.

### 6.2 Decisions

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `provider_entity_id` → `provider_entities` | What is being resolved |
| `provider_record_version_id` → versions | Which observation triggered this |
| `company_id uuid null` | Resolved company; NULL for `AMBIGUOUS` / `REJECTED` |
| `merged_company_id uuid null` | The retiring identity, for `MERGED` / `SPLIT` |
| `decision text` | `MATCHED` / `CREATED_NEW` / `AMBIGUOUS` / `REJECTED` / `MERGED` / `SPLIT` |
| `method text` | `DETERMINISTIC` / `CANDIDATE_AUTO` / `HUMAN_REVIEW` |
| `supersedes_decision_id uuid null` | **Points backwards** to the decision replaced |
| `identity_policy_version text` | Policy in force |
| `signals jsonb` | Full signal vector |
| `rationale text`, `decided_by text`, `decided_at timestamptz` | |

Append-only trigger, as with M0's evidence tables. **No row is ever updated.**

### 6.3 Exactly one effective head per provider entity

`UNIQUE (supersedes_decision_id)` stops a decision being superseded twice, but
it does **not** stop two concurrent workers each writing a *root* decision for
the same provider entity — in PostgreSQL, multiple NULLs coexist happily in a
unique index. Revision 2 could therefore fork at the root.

Two partial unique indexes bound the *shape* of the chain:

```sql
-- at most one root per provider entity
CREATE UNIQUE INDEX uq_resolution_root
    ON entity_resolution_decisions (provider_entity_id)
    WHERE supersedes_decision_id IS NULL;

-- at most one child per decision
CREATE UNIQUE INDEX uq_resolution_supersedes
    ON entity_resolution_decisions (supersedes_decision_id)
    WHERE supersedes_decision_id IS NOT NULL;
```

But shape is not enough. Neither index prevents a decision belonging to entity
**B** from superseding a decision belonging to entity **A** — which would splice
two entities' histories into one chain, leaving A headless and B forked.

**A composite self-foreign-key makes that unrepresentable:**

```sql
ALTER TABLE entity_resolution_decisions
  ADD CONSTRAINT uq_decision_entity UNIQUE (id, provider_entity_id);

ALTER TABLE entity_resolution_decisions
  ADD CONSTRAINT fk_supersedes_same_entity
  FOREIGN KEY  (supersedes_decision_id, provider_entity_id)
  REFERENCES entity_resolution_decisions (id, provider_entity_id);
```

The referenced row must match on **both** columns, so a superseding decision can
only point at a decision of the *same* provider entity. `UNIQUE (id,
provider_entity_id)` is redundant against the primary key on `id` alone, but
PostgreSQL requires a unique constraint covering the referenced columns for the
composite foreign key to be legal — it exists to serve the FK, not to add a new
rule.

Together, the four constraints give the full invariant:

| Constraint | Prevents |
| --- | --- |
| `PRIMARY KEY (id)` | Duplicate decisions |
| `uq_resolution_root` | Two chains for one entity |
| `uq_resolution_supersedes` | A forked chain |
| `fk_supersedes_same_entity` | **Cross-entity supersession** |
| `CHECK (id <> supersedes_decision_id)` | Self-reference |

One root, one child each, all within one entity, means the decision graph per
entity is a **linear chain** — therefore exactly one head, by construction.

Cycles are unconstructible rather than merely forbidden: every insert must
reference an already-committed row, and rows are never updated, so no sequence
of writes can close a loop.

This was chosen over the two options considered (M2-ADR-018):

| Option | Verdict |
| --- | --- |
| Transactional advisory lock on `provider_entity_id` | Works, but correctness depends on every writer remembering to take it. A forgotten lock is silent |
| Mutable `entity_resolution_heads` table as the invariant | Works, but makes correctness depend on mutable state the rebuild invariant then has to except |
| **Two partial unique indexes** | **Chosen.** Declarative, enforced by the database for every writer including ad-hoc SQL, no lock, no mutable state |

**Concurrency behaviour.** Both races resolve to one head with no lost update:

* *Two concurrent initial decisions* — one commits, the other violates
  `uq_resolution_root`, re-reads the now-existing head, and either concludes
  the same thing (no write) or supersedes it.
* *Two concurrent superseding decisions* against the same head — one commits,
  the other violates `uq_resolution_supersedes`, re-reads the new head, and
  re-evaluates against it.

The retry loop is the same `ON CONFLICT` / re-read pattern already proven under
a real two-connection race by M1's research-gap detector.

### 6.4 Querying the effective head

The **authoritative definition** is a view — the decision nothing supersedes:

```sql
CREATE VIEW current_entity_resolutions AS
SELECT d.*
FROM entity_resolution_decisions d
WHERE NOT EXISTS (SELECT 1 FROM entity_resolution_decisions s
                  WHERE s.supersedes_decision_id = d.id);
```

Because §6.3 forces one linear chain per entity, this yields exactly one row per
provider entity — `ORDER BY … LIMIT 1` is not needed for correctness.

`entity_resolution_heads (provider_entity_id PK, current_decision_id,
company_id)` is a **materialized cache of that view**, maintained in the same
transaction as the decision and fully rebuildable. It is a performance
projection, not the invariant — the difference from revision 2's proposal,
where the table *was* the invariant.

The two must agree, and acceptance scenario C12 asserts it:

```sql
-- must return zero rows
SELECT * FROM entity_resolution_heads h
FULL OUTER JOIN current_entity_resolutions v
  ON v.provider_entity_id = h.provider_entity_id
 AND v.id                 = h.current_decision_id
WHERE h.provider_entity_id IS NULL OR v.id IS NULL;
```

### 6.5 Three tiers of authority

**Deterministic — may auto-match.** Verified national registry identifier; an
existing effective decision for the same provider entity; an `IDENTITY`-role
domain match that passes the domain policy (§8). Never for an entity flagged
`identity_collision` (§4.2).

**Candidate — scored, then gated.**

| Signal | Indicative weight |
| --- | --- |
| Normalized legal name equality within same market | high |
| Normalized name + same city/postal code | high |
| Shared phone number | medium |
| Shared normalized address | medium |
| Shared `GROUP`-role domain | low — presence of a group, not identity |
| Fuzzy name similarity (trigram) | low |
| Same vertical + same market | very low, tie-break only |

Above `auto_match_threshold` → `CANDIDATE_AUTO` with the full signal vector
persisted. Between the thresholds → `AMBIGUOUS`, which creates nothing and
merges nothing. Below → `CREATED_NEW`.

**Fuzzy similarity alone never merges.** Trigram similarity is a *retrieval*
mechanism, never a *decision* mechanism — the same discipline that keeps M1
coverage honest instead of inventing vertical density.

### 6.6 Merges and splits

A merge writes a `MERGED` decision and updates the **anchor's lifecycle plus
projections only**: `companies.lifecycle_status = 'MERGED'`,
`merged_into_company_id` set. Claims, versions and relationship claims are
untouched; claims re-project onto the survivor via §5.5.

Reversal writes a `SPLIT` decision whose `supersedes_decision_id` points at the
merge. Anchors return to `ACTIVE`, projections rebuild. **No evidence row is
rewritten in either direction**, which is what makes reversal safe.

## 7. Run lifecycle and transaction semantics

```
PENDING → FETCHING → ┬→ FETCHED ──→ NORMALIZING → RESOLVING → COMPLETED
                     ├→ PARTIAL_FETCH ─┘ (only if allow_partial_resolution)
                     └→ FAILED
```

| State | Meaning |
| --- | --- |
| `PENDING` | Requested, not started |
| `FETCHING` | Provider calls in progress |
| `PARTIAL_FETCH` | Provider failed mid-run; some raw versions persisted |
| `FETCHED` | All expected pages retrieved |
| `NORMALIZING` | Pure normalization over persisted versions |
| `RESOLVING` | Decisions and projections being written |
| `COMPLETED` | Terminal success |
| `FAILED` | Terminal failure; raw evidence retained |

| Stage | Transaction | On failure |
| --- | --- | --- |
| Fetch, per page | One per page: versions + bodies + sightings + query row | Committed pages survive; run → `PARTIAL_FETCH` |
| Normalize, per version | One per version | Others unaffected; unnormalizable version flagged |
| Resolve, per provider entity | One short transaction: candidates + decision + head + claims + projection | That entity is skipped; others proceed |

> **A run that has not reached `FETCHED` does not write to the canonical
> registry**, unless created with `allow_partial_resolution = true`.

Raw evidence already paid for is never discarded; the canonical registry is
never touched by an incomplete fetch, because a truncated result set is not
evidence of absence — and treating it as such would let a provider outage
silently shrink a market.

**Retry is resumable, not restarting.** A `PARTIAL_FETCH` run resumes from the
last `discovery_queries` cursor. Already-persisted payloads produce sightings,
not duplicate versions, because the canonical hash is unchanged. Normalize and
resolve re-run independently over stored versions without re-paying the
provider.

## 8. Domain identity policy

Normalize to the **registrable domain** (eTLD+1 via the Public Suffix List):
lowercase, strip trailing dot, IDN → punycode, strip `www.`. Subdomains are
never identity on their own.

Universal one-domain-one-company is false: group domains, subsidiaries on a
parent domain, hosting and marketplace domains, rebrands and post-acquisition
redirects all break it.

| Strength | Conditions |
| --- | --- |
| **Deterministic** | `IDENTITY`-role registrable domain, not blocklisted, not `GROUP`, and no conflicting strong signal (different verified registry ids, or different countries with separately-registered entities) |
| **Strong candidate** | Domain shared but marked `GROUP`, names/locations materially differ, or the match is on a subdomain |
| **Insufficient** | Blocklisted generic/hosting domain, parked domain, or a marketplace profile URL |

The constraint lives on the projection, not on the anchor:

```sql
CREATE UNIQUE INDEX uq_identity_domain
    ON company_domains (domain_normalized) WHERE domain_role = 'IDENTITY';
```

A company may hold many domains; at most one company may claim a given domain as
its *identity*. Group and alternate domains are freely shared. An acquisition is
a relationship with a date (§12), never a silent merge.

## 9. Concurrency and idempotency

| Event | Mechanism | Result |
| --- | --- | --- |
| Same external id, identical semantic payload | `UNIQUE (provider_entity_id, canonical_payload_hash)` + `ON CONFLICT DO NOTHING` | No new version; sighting recorded |
| Same external id, changed payload | Same constraint, different hash | New immutable version, same entity |
| Two concurrent root decisions | `uq_resolution_root` | One head; loser re-reads (§6.3) |
| Two concurrent superseding decisions | `uq_resolution_supersedes` | One head; loser re-reads (§6.3) |
| Concurrent identity-domain creation | `uq_identity_domain` | One company; loser re-reads |
| Re-run of a completed run | Runs are immutable | New run, diffable against the old |

No distributed lock service. PostgreSQL constraints plus short transactions,
the established house pattern (ADR-001).

## 10. Worker architecture

A PostgreSQL job table consumed with `SELECT ... FOR UPDATE SKIP LOCKED`.
**Redis is not part of the M2 design** (M2-ADR-010).

M2's workload is tens to low thousands of provider calls per run, bounded by
provider rate limits — orders of magnitude below where a dedicated queue earns
its operational cost. The decisive factor is transactionality: a job that
enqueues follow-up work in the same transaction as its raw writes cannot drift
out of sync, which with Redis would require an outbox pattern to match.

Revisit only if sustained throughput exceeds a few thousand jobs/second, or
fan-out/pub-sub delivery becomes a requirement, or workers must run where
PostgreSQL is unreachable.

## 11. The M1 ↔ M2 boundary — discovery counts are not density

If provider A returns 2,400 US HVAC companies and 800 German ones, that does
**not** establish that the US has three times the density. It establishes that
*this provider, with this query, on this date, indexed those counts*. Coverage,
indexing depth, language handling and query phrasing all bias the result —
typically toward large English-language markets, precisely the direction that
would flatter a US-first conclusion.

1. A discovery count **never** writes to `market_observations` automatically.
2. Promotion enters as `fact_type = PROXY` — never `FACT` — carrying provider,
   adapter version, exact query definition, `retrieved_at`, coverage assumption,
   calibration (if established against an independent register, itself
   evidenced) and a confidence from M0's algorithm.
3. Promotion is explicit, human-authorised, logged and reversible.
4. Uncalibrated counts support within-provider, within-market trends only.
5. Until calibrated, the honest M1 representation of vertical density remains
   what it is today: **a research gap**.

## 12. Company relationships over time

### 12.1 Relationships are claims

Revision 2 made `company_relationships` append-only with a uniqueness
constraint — which cannot express a relationship that ends, or one asserted in
error. `company_relationship_claims` replaces it as the evidence table:

| Column | Notes |
| --- | --- |
| `id uuid pk` | |
| `from_company_id`, `to_company_id` | Anchors |
| `relationship_type text` | Canonical types only (§12.3) |
| `valid_from date null`, `valid_to date null` | NULL `valid_to` = still in force |
| `assertion text` | `ASSERTED` / `RETRACTED` |
| `supersedes_claim_id uuid null` | `UNIQUE` — corrections are additive |
| `provider_record_version_id`, `resolution_decision_id` | Provenance |
| `fact_type`, `confidence`, `observed_at`, `period_granularity` | M0 vocabulary |
| `created_at` | |

`company_relationships` becomes a **derived projection** of effective,
non-retracted relationships — storing their **validity intervals**, not a
snapshot of what is valid today (§12.4).

### 12.2 The three required statements

| Statement | Representation |
| --- | --- |
| *"A was a franchisee until 2027"* | One `ASSERTED` `FRANCHISE_OF` claim with `valid_to = 2027-xx-xx` |
| *"B was a subsidiary of X, then acquired by Y"* | Two claims: `(B, X, SUBSIDIARY_OF, valid_to = D)` and `(B, Y, ACQUIRED_BY, valid_from = D)` |
| *"That relationship claim was wrong"* | A new `RETRACTED` claim with `supersedes_claim_id` pointing at the mistaken one |

No `UPDATE` in any case. A relationship ending is an interval bound, not a
deletion; a relationship being wrong is a retraction, not an erasure.

### 12.3 Canonical types and derived inverses

Storing both directions would mean two rows that can disagree. Only the
canonical direction is stored:

| Canonical (stored) | Derived inverse (query projection only) |
| --- | --- |
| `SUBSIDIARY_OF` | `PARENT_OF` |
| `FRANCHISE_OF` | `FRANCHISOR_OF` |
| `ACQUIRED_BY` | `ACQUIRER_OF` |
| `SISTER_OF` *(symmetric)* | — |

`SISTER_OF` is symmetric, so it is stored once with
`from_company_id < to_company_id` enforced by a CHECK, preventing a duplicate
pair in the other order.

`FORMERLY` is **removed** from relationship types: "formerly known as" is a name
claim, and belongs in `company_names` with `name_type = FORMER`. It was a
relationship only by accident of vocabulary.

### 12.4 The projection stores intervals; views apply the date

Revision 3 described the projection as holding "currently-valid" relationships.
That made its content depend on `CURRENT_DATE`, so the same evidence would
rebuild into different rows tomorrow — breaking the determinism contract for no
benefit.

The stored projection is therefore **time-independent**: one row per effective
`(from, to, type)` with `effective_from` and `effective_to` derived from the
non-retracted, non-superseded claims. Nothing in it reads the clock.

The date filter moves into views:

```sql
CREATE VIEW current_company_relationships AS
SELECT * FROM company_relationships
WHERE (effective_from IS NULL OR effective_from <= CURRENT_DATE)
  AND (effective_to   IS NULL OR effective_to   >  CURRENT_DATE);
```

and `?as_of=DATE` applies the same predicate against a supplied date. A view
returning different rows tomorrow is correct; the projection beneath it is
unchanged, and its digest proves it.

This is the general rule, not a relationship special case: **no persisted
projection may depend on `now()`**. Any temporal narrowing belongs in a view.

### 12.5 Derived inverses

Inverses are exposed through a view:

```sql
CREATE VIEW company_relationships_bidirectional AS
SELECT from_company_id AS company_id, to_company_id AS related_company_id,
       relationship_type, 'CANONICAL' AS direction, valid_from, valid_to
FROM company_relationships
UNION ALL
SELECT to_company_id, from_company_id,
       CASE relationship_type
            WHEN 'SUBSIDIARY_OF' THEN 'PARENT_OF'
            WHEN 'FRANCHISE_OF'  THEN 'FRANCHISOR_OF'
            WHEN 'ACQUIRED_BY'   THEN 'ACQUIRER_OF'
            WHEN 'SISTER_OF'     THEN 'SISTER_OF'
       END,
       'DERIVED', valid_from, valid_to
FROM company_relationships;
```

## 13. API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/discovery-providers` | Registry, capabilities, `identity_capability` |
| `POST` | `/discovery-runs` | Start a run for an M1 context |
| `GET` | `/discovery-runs[/{id}]` | Lifecycle state, counts, cost |
| `POST` | `/discovery-runs/{id}/retry` | Resume from the last cursor |
| `GET` | `/discovery-runs/{id}/queries` | Exact queries and cursors |
| `GET` | `/provider-entities/{id}/versions` | Full observation history |
| `GET` | `/provider-entities/{id}/resolution-chain` | Root → head, in order |
| `GET` | `/companies` | Filter by market, vertical, presence, status |
| `GET` | `/companies/{id}` | Anchor plus current profile |
| `GET` | `/companies/{id}/claims` | Effective claims, with provider and date |
| `GET` | `/companies/{id}/claims?include_superseded=true` | Full history |
| `GET` | `/companies/{id}/locations` · `/market-presences` · `/verticals` | Projections |
| `GET` | `/companies/{id}/relationships[?as_of=DATE]` | Effective or historical |
| `GET` | `/attribute-registry[/{version}]` | The contract itself |
| `GET` | `/entity-resolution/decisions` | Filter by decision, method, run |
| `GET` | `/entity-resolution/ambiguous` | The human-review queue |
| `POST` | `/entity-resolution/decisions` | Record a human decision |

New error codes: `PROVIDER_UNAVAILABLE`, `RESOLUTION_AMBIGUOUS`,
`IDENTITY_POLICY_CONFLICT`, `MERGE_CONFLICT`, `PARTIAL_FETCH_NOT_RESOLVABLE`,
`ATTRIBUTE_NOT_IN_REGISTRY`, `RESOLUTION_HEAD_CONFLICT`.

## 14. Open design questions

1. **Registry migration.** When `attribute_registry_version` changes, existing
   claims were written against the old contract. Re-validating every claim is
   expensive; ignoring it risks a projection reading a value shape that no
   longer exists. A migration strategy is undesigned.
2. **Identity policy migration.** Same problem for `identity_policy_version`:
   past decisions were made under the old policy. Re-resolving everything is
   expensive; leaving it is inconsistent.
3. **Incremental projection.** Full rebuild is the integrity guarantee, but at
   scale it needs an incremental path. Trigger-based or job-based incremental
   projection is undesigned.
4. **Sub-national coverage.** "Serves Bavaria" has no home, since M1 markets are
   national. Options: a `company_service_areas` table keyed to a geography
   vocabulary M1 does not yet have, or deferring to M3.
5. **Raw body retention.** `provider_record_bodies` is prunable, but the
   retention period, and whether pruning is permitted before a dispute window
   closes, is a policy question rather than a schema one.
6. **Provider trust calibration.** Trust tiers are currently a judgement.
   Making them evidence-based requires a labelled corpus of human decisions,
   which M2 will produce but does not yet have.
7. **Equal-rank scalar conflicts.** The envelope rule handles ranges; two
   contradictory registry identifiers fall to the stability tie-break and a
   conflict flag. Whether that should instead force `AMBIGUOUS` is unresolved.
8. **Derived-key rotation across providers.** When a `DERIVED_STABLE_KEY`
   provider rotates keys en masse (a normalization change on their side), many
   new entities appear at once. Detecting that as rotation rather than growth
   is undesigned.
9. **Canonicalization strategy migration.** Changing a provider's strategy
   changes its canonical hashes, so previously-seen payloads produce new
   versions. Whether to re-canonicalize historical bodies under the new
   strategy, or to treat the change as a clean cut-over, is undesigned. Bodies
   are retained precisely so that re-canonicalization stays *possible*.
10. **Temporal market presence.** Relationships now carry validity intervals,
    but `company_market_presences` does not — a company that exits a market has
    no way to say so. The same interval treatment probably applies; it was out
    of scope here and would need its own claim shape.
11. **Projection digest granularity.** `projection_runs` records a digest per
    table. Whether per-company digests are needed to localise drift, and what
    that costs on a full rebuild, is undesigned.

## 15. How M3 consumes M2

M3 (Operational Research) takes a canonical company and gathers operational
evidence — fleet size, service contracts, dispatch tooling, hiring signals. It
depends on M2 for a **stable anchor id**, **market presence**, **vertical
association with confidence**, **provider versions** as seed URLs, and an
explicit **ambiguity flag** so effort is not spent on possible duplicates.

M3 attaches findings as `company_claims` against registered attributes — which
is why the registry is versioned and extensible rather than M2-specific.

## 16. Explicit non-goals for M2

* No people, buyers, contacts or email discovery — M5.
* No outbound, sequences or campaigns — M6.
* No CRM synchronisation.
* No scoring of companies — qualification is M4, following the same
  score/confidence/coverage discipline.
* No automatic promotion of discovery counts into M1 (§11).
* No web scraping of company websites — that is M3; M2 consumes structured
  provider output.
* No machine-learned entity resolution in the first implementation.
  Deterministic rules plus scored candidates first; a model only once there is a
  labelled corpus of human decisions to train and evaluate against.

---

See [M2_SCHEMA_GRAPH.md](M2_SCHEMA_GRAPH.md) for the table graph and
cardinalities, [M2_ACCEPTANCE_CRITERIA.md](M2_ACCEPTANCE_CRITERIA.md) for the
scenarios an implementation must satisfy, and [M2_ADRS.md](M2_ADRS.md) for the
decisions behind this revision.
