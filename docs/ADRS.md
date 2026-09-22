# Architecture Decision Records

ADR-001 to ADR-008 are inherited from the implementation pack
(`docs/07_ADRS.md`) and are treated as constraints. ADR-008 below **replaces**
the pack's ADR-008 at the client's instruction; the deferred-company-discovery
decision it used to hold is preserved as ADR-011 so nothing is lost.

ADR-009 to ADR-012 were raised during implementation.

---

## ADR-008 — Product naming is decoupled from domain architecture

**Status:** Accepted · 2026-09-22

### Context
The name "OpenGTM" is already in use by several active GTM products and
open-source projects, including `opengtm.com`. Continuing to bake it into
package names, environment variables and database identifiers would make the
eventual public naming decision an invasive refactor.

### Decision
Naming is a presentation concern and is held separately from the architecture.

* Public project codename: **BoRo GTM Core**
* Internal implementation: **BoRo GTM Engine**
* Python namespace: `boro_gtm` (neutral, one package to rename later)
* Repository layout: `apps/`, `packages/`, `services/`, `data/`, `docs/`
* Environment prefix: `GTM_`
* Databases: `gtm_core`, `gtm_core_test`
* Domain tables carry **no** brand at all: `market_*`, `strategy` entities,
  `scoring_*`, `research_gaps`

The final open-source name will be selected separately. Renaming must touch
only the package directory, the `GTM_` prefix and the database name — never a
table, column, metric key, component key or API path.

### Consequences
* No architecture, scope, schema semantics, scoring behaviour, API contract or
  acceptance criterion changed as a result of this decision.
* Brand names must not enter table names, metric keys or API paths.

---

## ADR-009 — The published percentile convention is mid-rank over N-1

**Status:** Accepted · 2026-09-22

### Context
04 specifies that percentile conventions be implemented as explicit candidates
and tested against the supplied component values, rather than tuned per
country. The source metadata says "percentile ranks within the 63-economy
candidate universe" without pinning down tie handling or the denominator.

### Decision
Five candidate conventions were implemented in
`boro_gtm.market_intelligence.scoring.percentile` and compared against the
published components. The dataset is reproduced by:

```
P(x) = ( count(v < x) + (count(v == x) - 1) / 2 ) / (N - 1),   N = 63
```

That is, the mean 0-indexed ordinal position among ties, scaled over `N - 1`.
Evidence, asserted in `tests/unit/test_percentile.py`:

* implied percentiles of every single-metric component land exactly on integer
  or half-integer ordinals out of 62 (half-integers are ties);
* ordering follows the raw values and equal raw values imply equal percentiles;
* the minimum is exactly 0.0 and the maximum exactly 1.0, which rules out the
  `/ N` conventions;
* the implied `P(log GDP)` inside `economic_strength_20` and the nested
  percentile inside `digitalization_opportunity_15` both fit the same rule.

### Consequence worth recording
The convention is purely **ordinal**, so the `log` transform named in the
published formula has no effect on any score — percentile rank is invariant
under strictly monotonic transforms. The transform is retained in the formula
strings and applied when recording raw values, for fidelity to the published
methodology, and is explicitly documented as score-neutral.

---

## ADR-010 — Native recalculation ranks over the observed sub-universe

**Status:** Accepted · 2026-09-22

### Context
ADR-007 requires honest native recalculation and forbids inventing raw inputs.
Two facts about the snapshot constrain what native mode can do:

1. `icp_density_proxy_20` needs implied population, which the dataset never
   supplies for any market.
2. The published percentiles were taken over **63** economies, but raw metrics
   exist for only **51**. The 12 normalization-universe-only markets carry a
   published total score and nothing else. Their ordinal slots are visibly
   absent from the implied percentiles (see ADR-009), which is direct evidence
   that they participated in the original ranking.

### Decision
* `icp_density_proxy_20` is marked `natively_computable: false`. In native mode
  it is reported with `coverage = 0`, excluded from the total, and emits a
  `population` research gap. It is never reverse-engineered.
* Native mode percentile-ranks over the markets that actually have an observed
  value for each metric, and records the observed counts and the reduced
  universe in `score_runs.universe_definition`.
* Native scores are therefore **expected to differ** from reference scores. The
  test suite asserts that they differ rather than asserting false parity.

### Consequences
* Native runs report `coverage = 0.80` for every market: 80 of 100 model weight
  is computable, and the missing 20 is named, not hidden.
* `gtm_ease_10` is the one component that needs no normalization universe, and
  it reproduces the published values exactly — a useful control showing the
  engine's arithmetic is correct and the divergence is purely a data gap.

---

## ADR-011 — Company discovery starts in M2

**Status:** Accepted (carried over from the pack's original ADR-008)

M0/M1 must first create a reproducible market-intelligence foundation.
Scraping and discovery before this layer is stable is explicitly deferred.
`tests/integration/test_end_to_end.py::test_no_m2_tables_exist` guards this.

---

## ADR-012 — Home-market benchmarks are scored but unranked

**Status:** Accepted · 2026-09-22

### Context
The source declares `international_top50_excludes_home_market: "Chile"`. Chile
is part of the normalization universe and has a published score (42.616) but no
rank. Ranking it alongside the international set shifts every market below it
by one and breaks rank parity with the published artifact.

### Decision
`MarketInputs.is_home_market` flags benchmark markets. They contribute to the
normalization universe and receive a score, but `_assign_ranks` skips them, so
the international ranking is 1..50 exactly as published. The ranking API
exposes `include_unranked=true` for callers who want the benchmark alongside
the ranked set.


---

## ADR-013 — Research-gap identity is scoped to the dimensions a metric depends on

**Status:** Accepted · 2026-09-22

### Context
06 acceptance criterion D requires "no duplicate open gap for the same context
+ metric". Taken literally — hashing the full
`(market, vertical, icp, offer, channel, snapshot, model)` tuple — a missing
*market-level* figure such as `market_size_estimate.sam_min` produces a new gap
for every offer/ICP/channel permutation a caller happens to request, even
though the underlying research task is identical in all of them.

### Decision
A gap's fingerprint hashes only the context dimensions its metric genuinely
depends on:

| Metric family | Scope |
| --- | --- |
| `population`, `market_size_estimate.*`, `base_market_score`, raw observation metrics | market + snapshot |
| `market_vertical_profile.*` | market + vertical + snapshot |
| anything else | full context |

The persisted row also nulls the out-of-scope foreign keys, so a market-level
gap never appears to belong to whichever vertical or offer first surfaced it.

### Consequences
* One open gap per real research question, and the de-duplication and
  no-resurrection guarantees are unchanged.
* The scope table is configuration in
  `boro_gtm.market_intelligence.research_gaps.detector` and is expected to grow
  as new metrics arrive; unrecognised metrics safely default to the full
  context.

---

## ADR-014 — `market_gtm_profiles` is removed; context belongs on the score run

**Status:** Accepted · 2026-09-22 · supersedes the table's place in the M1 architecture

### Context
The authoritative M1 architecture named a `market_gtm_profiles` table. The
implementation pack never defined it, and an audit confirmed it existed nowhere
in code, schema or specification. The full analysis is in
[DESIGN_NOTE_market_gtm_profiles.md](DESIGN_NOTE_market_gtm_profiles.md).

### Decision
The table is removed from the M1 architecture. Every responsibility its name
implies is already represented, and better:

* recommended channel, buyer titles, priority geographies → `market_deep_dives`
* channel legal posture, language, timezone → `market_observations`, with
  `fact_type`, `availability`, `period_granularity` and source provenance
* channel suitability weighting → `scoring_models.definition.channel_access_weights`
  (versioned configuration, not per-market evidence)

Re-homing any of these into a profile table would strip provenance, which
ADR-004 forbids.

The audit's instinct was still right about *something*: before this pass the
contextual dimensions of a run existed only inside `score_runs.context` JSONB.
The missing durable object was a first-class context on the run, not a profile.
`score_runs` therefore gained `vertical_id`, `icp_id`, `offer_id`, `channel_id`
and `ticket_usd`, indexed as `ix_score_runs_context`.

### Consequences
* `US x commercial_hvac x ICP x email x $20k` and
  `DE x industrial_maintenance x ICP x email x $20k` are distinct, durable,
  join-queryable contexts — asserted in `tests/integration/test_gtm_context.py`.
* No table was invented to satisfy a name.
* If per-market, per-channel evidence later arrives that is genuinely *not* a
  dated observation (a negotiated partner agreement, say), that is the moment
  to revisit — as new evidence, with its own provenance.

---

## ADR-015 — Absence of evidence is an availability fact, not a fact type

**Status:** Accepted · 2026-09-22

### Context
`N/D` was being stored in `market_observations.fact_type`, putting "we have no
data" inside the same vocabulary as FACT and PROXY. Any consumer reading fact
types had to know that one member of the enum meant the opposite of the others.

### Decision
`FactType` contains exactly five members: FACT, PROXY, ESTIMATE, INFERENCE,
HYPOTHESIS. Absence is modelled separately:

* `availability` — `OBSERVED` | `NOT_AVAILABLE`, NOT NULL
* `fact_type` — NULL exactly when `availability = NOT_AVAILABLE`
* `value_numeric` — NULL, never 0

A CHECK constraint enforces the pairing in both directions, and a second CHECK
restricts `fact_type` to the five-member vocabulary. `N/D` remains legal in a
*source document* and is translated at import; the original token is kept in
observation metadata for fidelity.

### Consequences
* "Is this known?" and "how good is this evidence?" are separate queries.
* The confidence algorithm scores a missing value as 0.0 evidence, while the
  scoring engines exclude the whole component from numerator and denominator —
  so unknown still never reads as zero.

---

## ADR-016 — Temporal provenance records what is known, and nothing more

**Status:** Accepted · 2026-09-22

### Context
The engine had no representation of *when* an observation was true. ADR/04
listed "age of observation" as a confidence input that did not exist. The risk
in adding one is inventing precision: turning "2025" into `2025-01-01`.

### Decision
Four distinct temporal facts, none derived from another:

| Field | Meaning |
|---|---|
| `market_observations.observed_at` | An exact date, **only** when the source states one |
| `market_observations.period_label` | What the value describes: "2025", "latest", "snapshot" |
| `market_observations.period_granularity` | DATE / YEAR / SNAPSHOT / UNDATED |
| `sources.published_at` | Source publication date, when the catalog states one |
| `market_observations.created_at` | Ingest time |

A CHECK constraint enforces that `observed_at` is present if and only if
granularity is `DATE`, which makes invented precision unrepresentable.

Recency participates in confidence through `RECENCY_FACTOR`: DATE 1.00,
YEAR 0.90, SNAPSHOT 0.85, UNDATED 0.70. An observation with no usable date is
therefore *less* confident, satisfying the requirement that missing dates
degrade confidence rather than being silently treated as current.

### Consequences
* In the 2026 snapshot no metric is dated to the day, so `observed_at` is NULL
  everywhere and `period_granularity = DATE` appears zero times. That is the
  correct, honest result, and a test asserts it.
* `sources.published_at` is likewise NULL for all 15 catalog entries: the
  catalog states publication dates only inside free-text titles
  ("IMF World Economic Outlook Database — April 2026"), and parsing those would
  be exactly the invented precision this ADR exists to prevent.

---

## ADR-017 — Snapshot identity is canonical, not byte-literal

**Status:** Accepted · 2026-09-22

### Context
`import_file` hashed raw file bytes while `import_payload` hashed a canonical
JSON serialization. The same document through different entry points produced
different identities, so an unchanged document could be rejected as a conflict.

### Decision
One identity function, `snapshot_identity(payload)`, hashes the canonical form
(`sort_keys`, tight separators, `ensure_ascii=False`, `allow_nan=False`). Both
entry points use it. The literal byte digest is retained separately as
`market_snapshots.source_file_sha256` for fidelity auditing — it is evidence,
not identity.

### Consequences
* Reindenting or reordering a source file is a no-op import.
* A semantic change under the same snapshot key remains an `IMPORT_CONFLICT`.
* **Migration note:** snapshots imported before this change carry a byte digest
  in `sha256`. Migration `0002` does not rewrite them, because recomputing an
  identity requires re-canonicalising the stored payload and would silently
  rewrite an immutable row. Pre-existing snapshots should be re-imported into a
  fresh database. Given the project is pre-release with a single snapshot, this
  is cheaper and more honest than a backfill.

---

## ADR-018 — Evidence tables are append-only at the database level

**Status:** Accepted · 2026-09-22 · resolves audit finding A-6

### Context
Snapshot immutability was enforced only in application code. Any direct SQL
could rewrite `raw_payload` or an observation without tripping anything. The
brief was to evaluate a *small* invariant and not build infrastructure.

### Decision
A single `plpgsql` function, `gtm_reject_update()`, is attached as a
`BEFORE UPDATE ... FOR EACH ROW` trigger to `market_snapshots` and
`market_observations`. Any UPDATE raises `restrict_violation`.

Deliberately narrow:

* **INSERT is untouched** — imports work unchanged.
* **DELETE is untouched** — `ON DELETE CASCADE` from a snapshot still works,
  and `alembic downgrade` still drops tables.
* **TRUNCATE does not fire row triggers** — test teardown is unaffected.

One ORM change was required: `MarketSnapshot.observations` and `.score_runs`
now use `passive_deletes=True`, so SQLAlchemy lets the database perform the
cascade instead of first nulling child foreign keys (which the trigger, quite
correctly, refuses).

### Consequences
* Rewriting stored evidence now requires deliberately dropping the trigger,
  which is auditable, rather than being a plain `UPDATE`.
* Mutable registry tables (`markets`, `scoring_models`, strategy registries)
  keep normal semantics; they are not evidence.
* Cost was two triggers and one function, so the "disproportionate cost"
  escape hatch was not needed.

---

## ADR-019 — The supplied JSON Schema is preserved verbatim

**Status:** Accepted · 2026-09-22 · resolves the second half of audit finding A-13

### Context
`data/market_intelligence_v1.schema.json` carries
`$id: https://opengtm.dev/schemas/market-intelligence-v1.schema.json` and the
title "OpenGTM Market Intelligence Import v1" — the pre-ADR-008 product name.

### Decision
Leave it **unchanged**. It is a supplied input artifact that travels with the
dataset it validates, and `data/` holds inputs, not project source. Editing a
received artifact to match our internal naming would break byte-fidelity with
the sender's copy for a purely cosmetic gain, and `$id` is an identifier rather
than a URL that is fetched — nothing resolves it at runtime.

The stale path inside *our own* code (`countries.py` naming a pre-rename
`app/...` path in a user-facing error) was a genuine defect and is fixed.

### Consequences
* The one remaining `opengtm` reference outside documentation is in a supplied
  artifact, deliberately and on the record.
* If the schema is ever re-authored rather than received, it should adopt the
  final public identifier at that point.
