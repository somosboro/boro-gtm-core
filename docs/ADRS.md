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
