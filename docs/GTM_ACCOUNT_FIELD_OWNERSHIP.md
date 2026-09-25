# Canonical Q1 Account / Opportunity Fields — Ownership

**Status:** adopted with M3 design revision 5.

The canonical account/opportunity model defines 25 minimum fields. They span
the whole sales motion, so **there is no single point at which they are all
knowable**.

The governing rule: **before a field is legitimately known it is `NULL` /
`UNKNOWN`.** Never a default, never a zero, never a placeholder. A giant
account row pre-filled with fake values is the failure this table exists to
prevent — it is the same defect as turning missing evidence into `false`, one
layer up.

Legend — **Mutability:** `append-only` (evidence-bearing, superseded not
overwritten), `mutable` (a working value that legitimately changes),
`derived` (a projection, never written directly).

| # | Field | Owner | Earliest legitimate stage | Source | Nullable before | Mutability | Provenance required | Who may write |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `account_id` | **M2** | `ACCOUNT_DISCOVERY` | M2 canonical identity | No — it is the key | identity | resolution chain | M2 only |
| 2 | `market` | **M1/M2** | `ACCOUNT_DISCOVERY` | M0/M1 market registry | Yes | mutable | market id | M2 |
| 3 | `vertical` | **M1/M2** | `ACCOUNT_DISCOVERY` | M1 vertical registry | Yes | mutable | vertical id | M2 |
| 4 | `icp_id` | **M1** | `ACCOUNT_DISCOVERY` | M1 ICP registry | Yes | mutable | icp id | M1/M2 |
| 5 | `evidence[]` | **M3** | `EVIDENCE` | M3 evidence items + claims, projected as Q2 | Yes (empty) | **append-only** | full M3 walk | M3 only |
| 6 | `evidence_confidence` | **M3** | `EVIDENCE` | derived from claim confidence | Yes | **derived** | assertion contract | M3 projection |
| 7 | `operational_hypotheses[]` | **M4** | `EVIDENCE` | derived from `EV-*` signals | Yes (empty) | **append-only** | supporting claim + evidence ids | M4 only |
| 8 | `buyer_role` | **M5** | `OUTREACH` | buyer research | Yes | mutable | source | M5 |
| 9 | `problem_class` | **M4/M8** | `DIAGNOSTIC_CALL` | hypothesis confirmed in diagnostic | Yes | mutable | diagnostic record | M8 |
| 10 | `qualification_score` | **M7** | `QUALIFICATION` | canonical six-dimension rubric | **Yes — always null before** | mutable | rubric scores | **M7 only** |
| 11 | `qualification_route` | **M7** | `QUALIFICATION` | routing rule over the score | Yes | derived | score | M7 only |
| 12 | `trigger` | **M8** | `DIAGNOSTIC_CALL` | prospect response | Yes | mutable | response record | M6/M8 |
| 13 | `budget_band` | **M8** | `DIAGNOSTIC_CALL` | prospect response | **Yes — never inferred from size** | mutable | response record | M8 only |
| 14 | `architecture_status` | **M9** | `ARCHITECTURE_SPRINT` | sprint state | Yes | mutable | sprint record | M9 |
| 15 | `sprint_status` | **M9** | `ARCHITECTURE_SPRINT_SALE` | sprint state | Yes | mutable | sprint record | M9 |
| 16 | `implementation_mode_candidates[]` | **M9** | `ARCHITECTURE_SPRINT` | approved architecture | Yes | mutable | architecture output | M9 |
| 17 | `commercial_level_candidate` | **M8/M9** | `DIAGNOSTIC_CALL` at the earliest | scope discussed with prospect | **Yes** | mutable | diagnostic record | M8/M9 — **never from public evidence** |
| 18 | `commercial_level_final` | **M9** | `IMPLEMENTATION_PROPOSAL` | 12-dimension classifier over **approved architecture** | **Yes — always null before architecture** | mutable | classifier scores | **M9 only** |
| 19 | `selected_capabilities[]` | **M9** | `IMPLEMENTATION_PROPOSAL` | architecture decision | **Yes — always empty before** | mutable | architecture output | **M9 only** |
| 20 | `price_floor_usd` | **M9** | `IMPLEMENTATION_PROPOSAL` | normalized economics | Yes | derived | NEDC computation | finance/M9 |
| 21 | `quoted_price_usd` | **M9** | `IMPLEMENTATION_PROPOSAL` | proposal | Yes | mutable | proposal record | M9 |
| 22 | `normalized_delivery_cost_usd` | **M9** | `IMPLEMENTATION_PROPOSAL` | finance model | Yes | derived | cost model version | finance |
| 23 | `normalized_gross_margin` | **M9** | `IMPLEMENTATION_PROPOSAL` | finance model | Yes | derived | cost model version | finance |
| 24 | `next_step` | **M6–M9** | any | motion state | Yes | mutable | stage | whichever milestone owns the stage |
| 25 | `care_eligibility` | **M9** | `GO_LIVE` | delivery state | Yes | mutable | delivery record | M9 |

## Fields M3 may never write

Ten of the twenty-five, called out because each is a plausible-looking mistake:

`qualification_score`, `qualification_route`, `budget_band`, `buyer_role`,
`commercial_level_candidate`, `commercial_level_final`,
`selected_capabilities[]`, `price_floor_usd`, `quoted_price_usd`,
`normalized_gross_margin`.

M3 writes exactly two: `evidence[]` and `evidence_confidence`. It contributes
to `market`, `vertical` and `icp_id` only by reading M1/M2.

## Why not one account row

Because a row with 25 columns invites defaults, and a default in
`qualification_score` is indistinguishable from a real score once written. The
canonical fields should be assembled as a **projection** over the milestone
that owns each one, so a field that nobody has legitimately written has nothing
to read — rather than a `0` that a later reader mistakes for a judgement.
