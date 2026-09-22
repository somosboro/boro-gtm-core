# 03 — Importer and Data Contract

## Source file

Primary source artifact:
`boro_market_intelligence_top50.json`

Treat the source as immutable.

## Import command

Provide a CLI command similar to:

```bash
python -m app.cli market-intelligence import ./data/boro_market_intelligence_top50.json
```

or an equivalent project-native command.

Also provide a recalculation command:

```bash
python -m app.cli market-intelligence recalculate --snapshot MI-2026-09-21-V1 --model market-attractiveness:1.0
```

## Import lifecycle

1. Read file.
2. Validate against `market_intelligence_v1.schema.json` plus semantic validators.
3. Compute SHA-256.
4. If identical snapshot hash already exists, importer must be idempotent and return the existing snapshot rather than duplicating rows.
5. Create immutable `market_snapshots` record.
6. Import `source_catalog`.
7. Resolve/create market registry entries for:
   - 50 ranked international markets
   - Chile home-market benchmark
   - 12 normalization-universe-only markets
8. Import observations from every `raw` object.
9. Import supplied subscores as reference/imported score-component observations, not as the only calculation source.
10. Import categories, competition assessments, technology growth, TAM/SAM/SOM and deep-dive records where present.
11. Create scoring-model definition from metadata.
12. Create an `imported_reference` score run containing the supplied scores/ranks.
13. Run the native scoring engine into a separate `recalculated` score run.
14. Compare both score runs and fail acceptance if tolerance is exceeded.

## Country mapping

Use a deterministic country-name → ISO mapping.

Required examples:
- United States → US / USA
- United Kingdom → GB / GBR
- United Arab Emirates → AE / ARE
- South Korea → KR / KOR
- Czechia → CZ / CZE
- Taiwan → TW / TWN (application identifier even if source-taxonomy differences exist)

Keep mapping in code/config with tests.

## Metric mapping

Input field → normalized metric key:

- `gdp_nominal_2025_usd_bn` → `gdp_nominal_usd_bn`, period `2025`
- `gdp_per_capita_2026_usd` → `gdp_per_capita_usd`, period `2026`
- `real_gdp_growth_2026_pct` → `real_gdp_growth_pct`, period `2026`
- `innovation_digital_proxy_2025` → `innovation_digital_proxy`, period `2025`
- `manufacturing_value_added_proxy_usd_bn` → `manufacturing_value_added_usd_bn`, period `latest/source-dependent`
- `software_spending_2024_pct_gdp` → `software_spending_pct_gdp`, period `2024`
- `language_access_0_1` → `language_access`, period `snapshot`
- `timezone_overlap_0_1` → `timezone_overlap`, period `snapshot`
- `b2b_email_legal_access_0_1` → `b2b_email_legal_access`, period `snapshot`

## Provenance rules

The source JSON provides sources at market level and a source catalog.

M0 should attach the market's listed sources to relevant observations where deterministic attribution is available.

If a metric-to-source mapping cannot be determined exactly, preserve:
- market-level source references,
- metric fact type,
- and import metadata,
without pretending exact provenance.

Do not fabricate a per-metric source relationship that the source file does not support.

## Fact type rules

Preserve the supplied taxonomy:
- FACT
- ESTIMATE
- PROXY
- INFERENCE
- HYPOTHESIS
- N/D

If a source object omits an explicit fact type, derive it only when the dataset metadata explicitly defines the field semantics. Otherwise store `UNKNOWN` internally or leave nullable; do not silently upgrade to FACT.

## Missing values

Input `null` remains SQL NULL.

Examples:
- missing `software_spending_2024_pct_gdp`
- missing TAM/SAM/SOM for markets outside the deep-dive set
- missing country technology-spending growth

Do not coerce NULL to 0.

## Deep-dive parsing

The input currently stores several deep-dive fields as semicolon-separated strings. M0 may persist the raw string. If parsed into arrays, retain the original raw value in metadata.

## Validation rules

Fail import on:
- duplicate market names inside the same source section
- `rank` duplicates within ranked top50
- top50 count not equal to 50
- metadata `universe_size` inconsistent with 50 + benchmark + excluded universe count unless explicitly justified
- score values outside 0–100
- component scores below 0 or above their component weight (with small float tolerance)
- invalid fact-type vocabulary where explicit
- invalid source keys referenced by a market

Warn, do not fail, on:
- missing TAM/SAM/SOM
- missing technology-spending growth
- missing software-spending percentage
- low-confidence estimates

## Import result

Return a structured summary:

```json
{
  "snapshot_key": "MI-2026-09-21-V1",
  "created": true,
  "markets": 63,
  "ranked_markets": 50,
  "home_market_benchmarks": 1,
  "sources": 14,
  "observations": 0,
  "warnings": [],
  "reference_score_run_id": "...",
  "recalculated_score_run_id": "...",
  "max_score_delta": 0.0
}
```

`observations` is illustrative; implementation must return the actual count.
