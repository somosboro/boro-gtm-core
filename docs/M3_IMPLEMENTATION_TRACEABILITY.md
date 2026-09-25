# M3 — Implementation Traceability

**Status:** in progress on `feat/m3-operational-research`. Not merged, not
released.

Maps each acceptance scenario to the code path, the database invariant that
enforces it, and the test that executes it. A scenario with no test is listed
as **not yet executable** rather than quietly assumed.

Design baseline: `c170ce8` (revision 4), amended to revision 4.1 by M3-ADR-039
and reconciled to **revision 5** against the canonical commercial ontology
(M3-ADR-040 … 046) — see §4 and
[M3_CANONICAL_COMMERCIAL_ALIGNMENT.md](M3_CANONICAL_COMMERCIAL_ALIGNMENT.md).

---

## 1. What is implemented

| Layer | State | Where |
| --- | --- | --- |
| Enums and closed vocabularies | **Done** | `research/enums.py` |
| 23-table ORM model | **Done** | `research/domain/models.py` |
| Migration `0004_m3` | **Done** | `migrations/versions/0004_m3_operational_research.py` |
| Database invariants (triggers, composite FKs, partial indexes) | **Done** | migration `0004_m3` |
| Additive M2 changes | **Done** | `discovery/domain/models.py`, migration |
| Attribute registry (42 attributes) | **Done** | `research/registry.py` |
| Canonical commercial contract + drift gate | **Done** | `commercial/contract.py`, `commercial/CANONICAL_CONTRACT.json` |
| Registry seeding | **Done** | `research/seeds.py` |
| Fixture discovery providers | **Not started** | — |
| Fixture fetcher (11 outcomes) | **Not started** | — |
| Canonicalization / classification / text derivation | **Not started** | — |
| Extraction services | **Not started** | — |
| Evidence, claims, confidence, publisher policy | **Not started** | — |
| Gaps and identity-signal services | **Not started** | — |
| Projections (global and plan) | **Not started** | — |
| Pipeline orchestration | **Not started** | — |
| API and CLI | **Not started** | — |

## 2. Scenarios with executable tests

All in `tests/integration/test_m3_schema_invariants.py`, against real
PostgreSQL.

| Scenario | Invariant | Test |
| --- | --- | --- |
| N1 | Seeded registry equals the design table | `test_seeded_registry_matches_the_design_table_exactly` |
| N1 | Seeding is idempotent | `test_seeding_twice_creates_no_duplicates` |
| G11 | Terminal attempt never reopens | `test_a_terminal_attempt_can_never_be_reopened` |
| G11 | Only legal transitions | `test_an_illegal_transition_is_rejected` |
| M13 | Seeds freeze at execution start | `test_attempt_seed_inputs_freeze_once_execution_begins` |
| G15 | One live attempt per run | `test_only_one_live_attempt_per_run` |
| G3 | Retry creates attempt n+1 | `test_a_terminal_attempt_frees_the_run_for_a_retry` |
| G13/G14 | Plan hash is the run identity | `test_the_run_plan_hash_is_unique` |
| M10 | 304 validates a known body | `test_a_304_validates_a_known_body_and_names_its_validator` |
| M10 | 304 needs a validator | `test_a_304_without_a_validator_is_rejected` |
| A11 | Failed fetch carries no body | `test_a_failed_fetch_must_not_carry_a_body` |
| — | OK fetch requires a body | `test_a_successful_fetch_requires_a_body` |
| L9/A4 | One body, two declared types | `test_the_same_bytes_from_two_sources_are_one_body` |
| A3/C-series | Evidence tables reject UPDATE | `test_evidence_tables_reject_updates` (9 tables) |
| I2 | One-way prune only | `test_a_body_may_be_pruned_exactly_once_and_nothing_else` |
| L2 | Composite FKs bind one body | `test_evidence_cannot_mix_bodies_across_its_three_paths` |
| M4/A9 | One body, two canonicalization versions | `test_one_body_supports_two_canonicalization_versions` |
| M5 | Publication metadata on the derivation | `test_publication_metadata_lives_on_the_derivation_not_the_artifact` |
| E1/E2 | No invented publication precision | `test_an_invented_publication_date_is_unrepresentable` |
| M11 | Sampled extraction needs a slot | `test_a_sampled_extraction_needs_its_own_execution_slot` |
| M14 | NULL-related signal cannot duplicate | `test_a_signal_with_a_null_related_company_cannot_duplicate` |
| M16 | ACTIONED cannot return to OPEN | `test_an_actioned_occurrence_cannot_return_to_open` |
| M16 | New episode after a terminal one | `test_a_terminal_occurrence_frees_the_concern_for_a_new_episode` |
| — | One open occurrence per concern | `test_two_open_occurrences_for_one_concern_are_rejected` |
| M2 | Gaps are plan-keyed | `test_a_gap_is_keyed_by_plan_not_by_company` |
| F4 | Gap lifecycle terminates | `test_a_gap_must_open_with_raised_and_terminates` |
| L13/F3 | Three retrievals, three events | `test_repeated_attempts_on_one_source_append_distinct_events` |
| H6 | No commercial judgement column | `test_no_m3_table_carries_a_commercial_judgement_column` |

**36 tests, 28 distinct scenarios covered.**

### Canonical commercial alignment

`tests/unit/test_commercial_ontology.py` (18 tests) and
`tests/unit/test_canonical_alignment_docs.py` (12 tests) cover section O:

| Scenario | Test |
| --- | --- |
| O1/O3 | `test_m3_defines_no_capability_selection`, `test_m3_cannot_set_a_commercial_level` |
| O2 | `test_m3_cannot_populate_canonical_qualification` |
| O4 | `test_price_cannot_flow_backward_into_classification` |
| O5 | `test_no_canonical_signal_is_left_uncovered` |
| O6 | `test_the_matrix_cites_only_attributes_that_are_actually_registered` |
| O7 | `test_process_observations_are_observational_not_judgemental` |
| O8 | `test_a_process_observation_reaches_fact_only_by_explicit_statement` |
| O13 | `test_the_outbound_standard_forbids_feature_selling` |
| O14 | `test_m3_writes_exactly_two_canonical_fields` |
| O15 | `test_every_canonical_q1_field_has_exactly_one_owning_milestone`, `test_every_canonical_sales_stage_is_mapped_or_explicitly_out_of_scope` |
| O16 | `test_the_committed_contract_carries_no_commercial_economics` |
| O17 | `test_the_canonical_yaml_matches_the_committed_contract` |
| O18 | `test_the_validator_fails_when_the_sales_motion_is_reordered` |

O9–O12 need the extraction and projection services and are **not yet
executable**.

**66 M3 tests in total.**

## 3. Scenarios not yet executable

The remaining scenarios depend on services that are not built yet: source
discovery, fetching, extraction, evidence assembly, claim assertion,
confidence, publisher independence, projections, API and CLI. They are listed
in `M3_ACCEPTANCE_CRITERIA.md` and are **not** claimed as passing.

## 4. Design defects found during implementation

| # | Defect | Classification | Correction |
| --- | --- | --- | --- |
| 1 | Revision 4 §7 prose said "twenty-four attributes" while its own tables listed thirty-one | Documentation | Prose corrected to thirty-one; acceptance N1 asserts the seeded registry equals the design table, so the two cannot drift again (M3-ADR-039) |
| 2 | Nine canonical `EV-*` signals had no M3 primitive; the design had no way to know, because nothing compared the two | Coverage | Eleven `PROCESS_OBSERVATION` attributes added; O5/O6 parse the coverage matrix and fail on an uncovered signal or an unregistered attribute (M3-ADR-043) |
| 3 | The revision 5 coverage summary said "5 DIRECT · 5 PARTIAL · 8 NONE" while the table above it said 4 · 5 · 9 | Documentation | Counts corrected; `test_coverage_improved_rather_than_being_declared` now counts the table's own rows. Third hand-written count in this milestone to drift, which is the argument for deriving them |
| 4 | The eleven process observations allowed `FACT` without an evidence-class ceiling, so a job-ad mention could have been asserted as a fact about how a company works | **Invariant** | All eleven made `evidence_class_capped`; only `EXPLICIT_COMPANY_STATEMENT` reaches `FACT` (O8) |

Defect 4 is the only one that could have produced a wrong claim in the
database. It was found by writing the acceptance test for a property the design
asserted but the code did not enforce — the registry's ceiling applied only to
technology attributes, and the new primitives were not marked.

No structural defect has been found in the schema. The schema as designed was implementable
exactly as written, including the three composite provenance foreign keys,
which needed no trigger.
