# Scoring Model Reference

## `market-attractiveness:1.0`

Seven components, weights summing to 100. `P(...)` is the percentile function
defined in [ADR-009](ADRS.md): mid-rank among ties over `N - 1`, with `N = 63`.

| Component | Weight | Formula | Natively computable |
| --- | --- | --- | --- |
| `economic_strength_20` | 20 | `12*P(log GDP nominal) + 8*P(log GDP per capita)` | yes |
| `technology_investment_readiness_20` | 20 | `20*P(WIPO GII 2025 or documented proxy)` | yes |
| `digitalization_opportunity_15` | 15 | `15*P(P(log GDP per capita) * (1-P(digital maturity)))` | yes |
| `icp_density_proxy_20` | 20 | `9*P(log population) + 7*P(log manufacturing VA) + 4*P(log GDP nominal)` | **no — population absent** |
| `ability_to_pay_10` | 10 | `10*P(log GDP per capita)` | yes |
| `gtm_ease_10` | 10 | `4*language + 3*timezone overlap + 3*B2B email access` | yes (exactly) |
| `outlook_5` | 5 | `5*P(IMF real GDP growth 2026)` | yes |

### The percentile function

```
P(x) = ( count(v < x) + (count(v == x) - 1) / 2 ) / (N - 1)
```

The lowest value scores 0.0, the highest 1.0, and ties share the mean of the
ordinal positions they occupy.

Because this is purely ordinal, the `log` transform in the formulas **does not
change any score** — percentile rank is invariant under strictly monotonic
transforms. The transform is retained for fidelity to the published method and
is applied when recording raw values.

### Competition is not scored

Every market carries a competition assessment (`VERY_HIGH` … `LOW_TO_MEDIUM`,
fact type `INFERENCE`) with `included_in_score = false`, following the source's
reasoning that high competition may be evidence of strong demand. It is stored
and queryable but never affects a score.

## Metric mapping

| Source field | Normalized key | Period | Default fact type |
| --- | --- | --- | --- |
| `gdp_nominal_2025_usd_bn` | `gdp_nominal_usd_bn` | 2025 | FACT |
| `gdp_per_capita_2026_usd` | `gdp_per_capita_usd` | 2026 | FACT |
| `real_gdp_growth_2026_pct` | `real_gdp_growth_pct` | 2026 | FACT |
| `innovation_digital_proxy_2025` | `innovation_digital_proxy` | 2025 | PROXY |
| `manufacturing_value_added_proxy_usd_bn` | `manufacturing_value_added_usd_bn` | latest | PROXY |
| `software_spending_2024_pct_gdp` | `software_spending_pct_gdp` | 2024 | FACT |
| `language_access_0_1` | `language_access` | snapshot | INFERENCE |
| `timezone_overlap_0_1` | `timezone_overlap` | snapshot | INFERENCE |
| `b2b_email_legal_access_0_1` | `b2b_email_legal_access` | snapshot | INFERENCE |

A source `null` produces a row with `value_numeric = NULL`,
`availability = NOT_AVAILABLE` and `fact_type = NULL`. The row still exists so
provenance — and the fact that we *know* it is unknown — both survive.
`N/D` is never stored as a fact type (ADR-015).

### Temporal provenance

| Column | Meaning |
| --- | --- |
| `observed_at` | An exact date, only when the source states one |
| `period_label` | What the value describes: "2025", "latest", "snapshot" |
| `period_granularity` | DATE / YEAR / SNAPSHOT / UNDATED |
| `sources.published_at` | Source publication date, when stated |
| `created_at` | Ingest time |

A CHECK constraint makes `observed_at` present if and only if granularity is
`DATE`, so a year can never be widened into an invented exact date (ADR-016).
In the 2026 snapshot no metric is day-precise, so `observed_at` is NULL
throughout.

## `contextual-market-fit:1.0`

| Component | Weight | Evidence it needs |
| --- | --- | --- |
| `base_market_prior` | 35 | base score from a selected score run |
| `vertical_density_fit` | 25 | market x vertical profile fit score or SAM |
| `icp_availability` | 15 | market SAM ICP firm-count range |
| `ticket_compatibility` | 10 | observed market ticket range vs requested ticket |
| `channel_accessibility` | 10 | channel-specific legal/language/timezone inputs |
| `strategic_reuse_localization` | 5 | language access, recommended motion |

These weights are an implementation seed, not a research claim. They live in
`scoring_models.definition` and are versioned with the model.

### Missing-data policy

`renormalize_to_covered_weight` — identical in the contextual engine and in
base **native** scoring:

```
covered_weight = sum(weight of scoreable components)
score          = earned_points / covered_weight * 100
coverage       = covered_weight / total_model_weight
confidence     = weighted mean component confidence over covered * coverage
```

Base **reference reproduction** is the one exception and is deliberately *not*
renormalized: the published total is the sum of the published components, and
reproducing it exactly is the purpose of that mode. Coverage is still recorded,
and ranking is still coverage-gated.

An unknown component leaves both sums. It never contributes 0 to the numerator
while still occupying the denominator, which is what would make "unknown" read
as "bad".

A component can also be **partially** covered — a channel whose legal input is
observed but whose timezone input is not. It is renormalized over its observed
inputs, scored, and its unobserved inputs are still reported as research gaps.

### Confidence — one algorithm

Defined once in `boro_gtm.market_intelligence.scoring.confidence` and used by
both engines. Every factor materially participates; there are no decorative
parameters (ADR-010 remediation).

```
evidence_confidence = fact_type_factor x recency_factor x label_factor
market_confidence   = weighted mean over covered components x coverage
```

| Fact type | factor | | Granularity | factor | | Label | factor |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FACT | 1.00 | | DATE | 1.00 | | HIGH | 1.00 |
| PROXY | 0.75 | | YEAR | 0.90 | | HIGH_MEDIUM | 0.92 |
| ESTIMATE | 0.65 | | SNAPSHOT | 0.85 | | MEDIUM | 0.85 |
| INFERENCE | 0.50 | | UNDATED | 0.70 | | LOW_MEDIUM | 0.75 |
| HYPOTHESIS | 0.30 | | (absent) | 0.70 | | LOW | 0.60 |

`fact_type = NULL` (no value) scores 0.00. An absent or unrecognised label is
**neutral** (1.00), so a source that declines to self-assess is not punished.

An observation with no usable date is therefore less confident than a dated
one — the mechanism by which missing temporal provenance degrades confidence
rather than being silently treated as current (ADR-016).

### Ranking eligibility

Every model carries an explicit `minimum_rank_coverage`. A result below it
keeps its score and coverage but is returned **unranked**, with
`metadata.unranked_reason = "coverage_below_minimum"`. Both seeded models use
`0.5`. Three things leave a result unranked, and a low score is not one of
them: it is a home-market benchmark, it has no comparable score, or its
coverage is below the threshold.

## Score-run kinds

| Kind | Meaning |
| --- | --- |
| `imported_reference` | Scores and ranks copied verbatim from the snapshot |
| `reference_reproduction` | Recomputed by summing imported components — must match |
| `native_recalculation` | Recomputed from raw observations where possible |
| `contextual` | An M1 market x vertical x ICP x offer x channel run |

Runs are append-only. A new run never mutates an older one, and the canonical
`markets` table has no score column at all (ADR-002).
