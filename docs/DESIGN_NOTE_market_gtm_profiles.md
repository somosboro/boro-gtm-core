# Design note — where GTM context lives, and whether `market_gtm_profiles` earns a table

**Status:** analysis complete · decision recorded as [ADR-014](ADRS.md)
**Question:** the authoritative M1 architecture names `market_gtm_profiles`.
The implementation pack never defined it. Does it have a distinct, durable
responsibility, or is that responsibility already represented?

## 1. Where each contextual dimension lives today

| Dimension | Representation | Durable? | Scope |
|---|---|---|---|
| market | `markets` (ISO identity) | yes | registry |
| vertical | `verticals` | yes | registry |
| ICP | `icps` (versioned `definition`) | yes | registry |
| offer | `offers` (ticket band, currency) | yes | registry |
| channel | `channels` (`definition.requires`) | yes | registry |
| ticket | `score_runs.ticket_usd` | yes | per run |
| market × vertical **evidence** | `market_vertical_profiles` | yes | per snapshot |
| market **GTM-motion** evidence | `market_deep_dives.recommended_channel`, `.common_buyers`, `.priority_geographies` | yes | per snapshot |
| market **channel-access** evidence | `market_observations` — `language_access`, `timezone_overlap`, `b2b_email_legal_access` | yes | per snapshot, with provenance |
| market × vertical × ICP × offer × channel × ticket **result** | `score_runs` + `market_scores` + `market_score_components` | yes | per run |

## 2. What a `market_gtm_profiles` table would hold

Reading the name at face value, it would carry per-market GTM-motion facts:
recommended channel, buyer titles, channel-by-channel legal posture,
language/localisation requirements.

Every one of those already has a home:

* **recommended channel, common buyers, priority geographies** →
  `market_deep_dives`, imported from the snapshot's `deep_dive` block with
  the original strings retained in `raw_values`.
* **channel legal posture / language / timezone** → `market_observations`,
  where they are first-class evidence with `fact_type`, `availability`,
  `period_granularity` and source provenance. Moving them into a profile table
  would *lose* provenance, which ADR-004 forbids.
* **channel suitability weighting** → `scoring_models.definition
  .channel_access_weights`, which is versioned configuration rather than
  per-market evidence.

A `market_gtm_profiles` table would therefore be a denormalised copy of
`market_deep_dives` plus a provenance-stripped copy of three observations.
That is duplication, not a distinct responsibility.

## 3. The one real gap the name pointed at

The audit was right that something was weak — but it was not a missing table.
Before this pass, the contextual dimensions of a run lived **only** inside
`score_runs.context` JSONB. Nothing tied a result to `verticals.id` or
`channels.id`, so "show me every result for US × commercial_hvac × email"
meant querying JSON and hoping the keys were spelled consistently.

The durable object that was missing was not a *profile*; it was a **first-class
context on the run**.

## 4. What changed instead

`score_runs` gained real foreign keys — `vertical_id`, `icp_id`, `offer_id`,
`channel_id` — plus `ticket_usd`, indexed together as `ix_score_runs_context`.
The JSONB `context` is retained for values with no FK (the market list,
coverage policy, the base run used as prior).

This delivers what the requirement actually asks for: distinct, durable,
queryable contexts, with referential integrity and no duplicated evidence.

## 5. Proof the required contexts are distinct

`tests/integration/test_gtm_context.py` asserts, against the database, that

* `US × commercial_hvac × boro_field_service_midmarket_v1 × email × $20k` and
* `DE × industrial_maintenance × boro_field_service_midmarket_v1 × email × $20k`

are two different persisted runs, joinable by foreign key, each with their own
per-market scores, components, coverage and research gaps — and that changing
any single dimension (vertical, ICP, offer, channel or ticket) yields a
distinguishable context rather than colliding with an existing one.

## 6. Decision

**Option B** — `market_gtm_profiles` is removed from the M1 architecture, and
the responsibility it named is shown to be already represented. Recorded in
[ADR-014](ADRS.md). No table was invented to satisfy a name.
