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

A source `null` produces a row with `value_numeric = NULL` and fact type `N/D`.
The row still exists so provenance and the fact that we *know* it is unknown
both survive.

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

`renormalize_to_covered_weight`:

```
score      = (sum of earned weight) / (sum of covered weight) * 100
coverage   = (sum of covered weight) / (total weight)
confidence = weighted mean component confidence * coverage
```

An unknown component leaves both sums. It never contributes 0 to the numerator
while still occupying the denominator, which is what would make "unknown" read
as "bad".

A component can also be **partially** covered — a channel whose legal input is
observed but whose timezone input is not. It is renormalized over its observed
inputs, scored, and its unobserved inputs are still reported as research gaps.

### Confidence inputs

Fact-type defaults, stored in the model definition:

| Fact type | Confidence |
| --- | --- |
| FACT | 1.00 |
| PROXY | 0.75 |
| ESTIMATE | 0.65 |
| INFERENCE | 0.50 |
| HYPOTHESIS | 0.30 |
| N/D, UNKNOWN | 0.00 |

The source's HIGH/MEDIUM/LOW labels map through a separate documented scale
(`confidence_label_scale`) rather than being converted invisibly.

## Score-run kinds

| Kind | Meaning |
| --- | --- |
| `imported_reference` | Scores and ranks copied verbatim from the snapshot |
| `reference_reproduction` | Recomputed by summing imported components — must match |
| `native_recalculation` | Recomputed from raw observations where possible |
| `contextual` | An M1 market x vertical x ICP x offer x channel run |

Runs are append-only. A new run never mutates an older one, and the canonical
`markets` table has no score column at all (ADR-002).
