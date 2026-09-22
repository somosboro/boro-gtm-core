# 02 — Domain Model and Database

## Principles
- UUID primary keys unless a lookup table clearly benefits from a stable textual key.
- ISO country codes are canonical identifiers for markets.
- Timestamps stored in UTC.
- Use JSONB only for genuinely variable metadata; do not turn the entire domain into JSON blobs.
- Preserve fact type and confidence wherever the source distinguishes them.

## M0 entities

### `market_snapshots`
Represents an immutable imported intelligence snapshot.

Fields:
- `id uuid pk`
- `key text unique` — e.g. `MI-2026-09-21-V1`
- `title text`
- `generated_date date`
- `score_version text nullable`
- `universe_size int`
- `source_filename text`
- `sha256 text unique`
- `raw_payload jsonb` OR object-storage path; for local M0, JSONB is acceptable given source size
- `metadata jsonb`
- `created_at timestamptz`

### `markets`
Canonical market registry.

Fields:
- `id uuid pk`
- `iso2 char(2) unique nullable until resolved`
- `iso3 char(3) unique nullable`
- `name text unique`
- `region text`
- `is_home_market boolean default false`
- `created_at timestamptz`

Importer must map country names in the dataset to ISO codes deterministically. Do not silently invent codes when unresolved; fail validation or require explicit mapping.

### `sources`
Source catalog.

Fields:
- `id uuid pk`
- `source_key text unique`
- `title text`
- `url text nullable`
- `publisher text nullable`
- `used_for jsonb nullable`
- `note text nullable`
- `created_at timestamptz`

### `market_observations`
Normalized metric values.

Fields:
- `id uuid pk`
- `market_id fk markets`
- `snapshot_id fk market_snapshots`
- `metric_key text`
- `value_numeric numeric nullable`
- `value_text text nullable`
- `unit text nullable`
- `period_label text nullable`
- `fact_type text` enum-like: FACT, ESTIMATE, PROXY, INFERENCE, HYPOTHESIS, N/D
- `confidence text nullable`
- `methodology text nullable`
- `metadata jsonb nullable`
- `created_at timestamptz`

Unique recommendation:
`(market_id, snapshot_id, metric_key, period_label)` when semantic uniqueness applies.

Initial `metric_key` vocabulary should include at least:
- `gdp_nominal_usd_bn`
- `gdp_per_capita_usd`
- `real_gdp_growth_pct`
- `innovation_digital_proxy`
- `manufacturing_value_added_usd_bn`
- `software_spending_pct_gdp`
- `language_access`
- `timezone_overlap`
- `b2b_email_legal_access`
- `technology_spending_growth_pct`

### `observation_sources`
Many-to-many between observations and sources.

Fields:
- `observation_id`
- `source_id`
- composite PK

### `market_categories`
Stable lookup or text relation for:
- GIANT_MARKET
- HIGH_VALUE_NICHE
- DIGITALIZATION_GAP
- EMERGING_SCALE
- BALANCED_REGIONAL

### `market_snapshot_categories`
- `market_id`
- `snapshot_id`
- `category_key`

### `market_competition_assessments`
Fields:
- `market_id`
- `snapshot_id`
- `level`
- `fact_type`
- `included_in_score boolean`

### `market_size_estimates`
TAM/SAM/SOM values when provided.

Fields:
- `id`
- `market_id`
- `snapshot_id`
- `fact_type`
- `confidence_level`
- `tam_min nullable`
- `tam_max nullable`
- `sam_min nullable`
- `sam_max nullable`
- `som_accounts_min nullable`
- `som_accounts_max nullable`
- `ticket_min_usd nullable`
- `ticket_max_usd nullable`
- `sam_value_min_usd nullable`
- `sam_value_max_usd nullable`
- `som_pool_value_min_usd nullable`
- `som_pool_value_max_usd nullable`
- `warning text nullable`

One record per market/snapshot for M0. M1 may extend it by vertical/ICP context.

### `market_deep_dives`
Keep structured deep-dive metadata available without flattening everything into observations.

Fields:
- `market_id`
- `snapshot_id`
- `priority_verticals jsonb`
- `priority_geographies jsonb/text`
- `recommended_channel text`
- `common_buyers jsonb`
- `common_problem_pattern text`

## Scoring entities

### `scoring_models`
Fields:
- `id`
- `key` e.g. `market-attractiveness`
- `version` e.g. `1.0`
- `name`
- `description`
- `normalization_method`
- `definition jsonb`
- `active boolean`
- timestamps

Unique `(key, version)`.

### `scoring_model_components`
Fields:
- `id`
- `scoring_model_id`
- `component_key`
- `weight numeric`
- `formula text`
- `required_metrics jsonb`
- `ordinal int`

### `score_runs`
Fields:
- `id`
- `scoring_model_id`
- `snapshot_id`
- `context jsonb nullable`
- `universe_definition jsonb`
- `status`
- `started_at`
- `completed_at`
- `engine_version text`

### `market_scores`
Fields:
- `id`
- `score_run_id`
- `market_id`
- `rank nullable`
- `score numeric`
- `confidence numeric`
- `coverage numeric`
- `metadata jsonb`

Unique `(score_run_id, market_id)`.

### `market_score_components`
Fields:
- `market_score_id`
- `component_key`
- `raw_value numeric nullable`
- `normalized_value numeric nullable`
- `weighted_score numeric`
- `confidence numeric`
- `coverage numeric`
- `explanation text nullable`
- `metadata jsonb nullable`

## M1 strategy entities

### `verticals`
Fields:
- `id`
- `key unique`
- `name`
- `description`
- `taxonomy_codes jsonb nullable` — e.g. NAICS/NACE/SIC mappings

Seed:
- commercial_hvac
- mechanical_contractors
- industrial_maintenance
- facilities_management
- refrigeration
- electrical_contractors
- elevator_service
- industrial_equipment_service
- energy_services

### `icps`
Fields:
- `id`
- `key unique`
- `name`
- `description`
- `definition jsonb`
- `version text`
- timestamps

Seed BoRo ICP definition includes employee range, field-worker range, B2B, recurring service, positive/negative signals.

### `offers`
Fields:
- `id`
- `key unique`
- `name`
- `description`
- `currency`
- `ticket_min`
- `ticket_max`
- `definition jsonb`

Seed:
- `operations_architecture_sprint`
- `operations_os_core`
- `operations_os_scale`
- `operations_transformation`

### `channels`
Fields:
- `id`
- `key unique`
- `name`
- `definition jsonb`

Seed examples:
- email
- phone
- linkedin
- partner
- event
- multichannel

### `market_vertical_profiles`
Context-specific stored profile/evidence.

Fields:
- `id`
- `market_id`
- `vertical_id`
- `snapshot_id`
- `fit_score nullable`
- `confidence numeric`
- `coverage numeric`
- `tam_min nullable`
- `tam_max nullable`
- `sam_min nullable`
- `sam_max nullable`
- `ticket_min_usd nullable`
- `ticket_max_usd nullable`
- `priority_geographies jsonb nullable`
- `recommended_motion jsonb nullable`
- `problem_patterns jsonb nullable`
- `evidence jsonb nullable`

Do not invent profile values simply to populate seeds.

### `research_gaps`
Fields:
- `id`
- `market_id`
- `vertical_id nullable`
- `icp_id nullable`
- `offer_id nullable`
- `channel_id nullable`
- `metric_key`
- `priority`
- `reason`
- `status` OPEN/IN_PROGRESS/RESOLVED/DISMISSED
- timestamps

## Future compatibility
The schema should make it possible later to add:
- companies
- people
- evidence/claims
- experiments
- commercial metrics
without altering the semantics of M0/M1 tables.
