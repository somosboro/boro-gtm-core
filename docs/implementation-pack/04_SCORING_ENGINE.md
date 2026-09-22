# 04 — Scoring Engine Specification

## Objective

Build a deterministic, versioned scoring engine that reproduces the supplied base market ranking and can later apply contextual market/vertical/ICP/channel/offer modifiers without mutating canonical market data.

## Base scoring model — v1

The source metadata defines:

```text
economic_strength_20
  = 12*P(log GDP nominal) + 8*P(log GDP per capita)

technology_investment_readiness_20
  = 20*P(WIPO GII 2025 score or documented equivalent proxy)

digitalization_opportunity_15
  = 15*P(P(log GDP per capita) * (1-P(digital/innovation maturity)))

icp_density_proxy_20
  = 9*P(log implied population)
    + 7*P(log manufacturing value added)
    + 4*P(log GDP nominal)

ability_to_pay_10
  = 10*P(log GDP per capita)

gtm_ease_10
  = 4*language access
    + 3*time-zone overlap
    + 3*B2B-email legal access

outlook_5
  = 5*P(IMF real GDP growth 2026)
```

Total = 100.

## Important issue: implied population

The supplied normalized JSON contains the resulting `icp_density_proxy_20` but does not expose a standalone population field for every market.

Therefore M0 must distinguish two score modes:

### `reference_reproduction`
Use imported source component/subscore values to reproduce the exact published market score.

This guarantees deterministic parity with the supplied research artifact.

### `native_recalculation`
Recalculate components only when all required raw metrics exist.

For any component whose raw prerequisites are absent from the source dataset, the engine must:
- mark the component as not fully reproducible from raw inputs,
- use the supplied component value only in reference-reproduction mode,
- lower coverage for a truly native run,
- emit a research/data gap.

Do not reverse-engineer hidden population values from other numbers unless a documented formula makes that exact and intentional.

## Percentile implementation

Percentile methodology must be centralized and deterministic.

Because the source artifact says percentile ranks were used but may not define every tie/interpolation implementation detail, first test standard approaches against supplied components.

Implementation workflow:
1. implement explicit candidate percentile functions,
2. compare against source component values,
3. choose the method that reproduces the dataset within tolerance,
4. document the chosen method in scoring-model definition and ADR/test fixture.

Do not silently tweak formulas per country.

## Score-run context

Base score context example:

```json
{
  "kind": "base_market",
  "snapshot_key": "MI-2026-09-21-V1"
}
```

M1 contextual score request:

```json
{
  "vertical": "commercial_hvac",
  "icp": "boro_field_service_midmarket_v1",
  "offer": "operations_architecture_sprint",
  "channel": "multichannel",
  "ticket_usd": 3000,
  "markets": ["US", "GB", "DE", "AU", "CA", "FR", "ES", "IT", "AE", "PL"]
}
```

## M1 contextual score design

Do NOT define a fake magical formula that claims to know exact conversion probability.

Use an explainable composition with three layers:

### Layer A — Base Market Prior
The base 0–100 market attractiveness score from a selected score run.

### Layer B — Context Evidence
Stored market × vertical / ICP / channel / ticket evidence.

Examples:
- exact/estimated vertical firm counts
- market × vertical SAM
- ticket range compatibility
- channel legal/access suitability
- language/localization fit
- existing commercial-market evidence when later available

### Layer C — Contextual result
Return:
- `score 0–100`
- `confidence 0–1`
- `coverage 0–1`
- component explanations
- missing metrics / research gaps

For M1, contextual score weights must be configuration-driven and versioned.

Suggested initial M1 model structure (not hard-coded truth):

```text
Base market prior                   35
Vertical density/fit                25
ICP availability                    15
Ticket compatibility                10
Channel accessibility/compliance    10
Strategic reuse/localization         5
TOTAL                              100
```

This is an implementation seed, not a research claim. It must be editable/versioned.

## Coverage

Coverage represents how much required evidence is available.

Example component coverage:

```text
Base market prior                 1.00
Vertical density                  0.60
ICP availability                  0.45
Ticket compatibility              0.80
Channel accessibility             0.90
Strategic reuse                   1.00
```

Aggregate using a weighted average based on component weight.

## Confidence

Confidence should combine:
- source fact types
- explicit confidence labels
- coverage
- age of observation if implemented

Initial fact-type confidence defaults can be configuration values, for example:
- FACT 1.00
- PROXY 0.75
- ESTIMATE 0.65
- INFERENCE 0.50
- HYPOTHESIS 0.30
- N/D 0.00

These are engine defaults, not scientific truths. Store/version them in the model definition.

Do not map a source's HIGH/MEDIUM/LOW label to numeric confidence invisibly. Document conversion.

## Unknown data behavior

Unknown must not equal negative.

For a missing contextual component:
- do not add 0 as if the market is bad,
- compute score from available evidence according to the configured missing-data policy,
- reduce coverage/confidence,
- emit research gaps.

Recommended policy for M1:
- normalize available component contribution back to the covered weight only for the displayed score,
- return coverage prominently,
- reject direct rank comparison when coverage falls below configurable minimum, unless caller explicitly opts in.

## Required score outputs

Every score endpoint/run returns:
- total score
- rank where comparable
- confidence
- coverage
- model version
- snapshot ID
- full component list
- explanations
- research gaps

## Reproducibility tolerance

For the reference score run:
- score delta target: <= 0.005 where summing imported components
- rank must match exactly for top50 unless source ties justify otherwise

For native recalculation of components with all inputs present:
- establish a tolerance after implementing the precise percentile convention
- record differences in tests; no country-specific hacks
