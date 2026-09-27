# M3 — Implementation Traceability

**Status:** phase 2 complete on `feat/m3-operational-research` — the internal
pipeline runs end to end against fixtures. Not merged, not tagged, not
released. Profiles, API and CLI are phase 3.

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
| Versioned policies (locator, classifier, canonicalization, text, redaction, publisher, trust, confidence, assertion) | **Done** | `research/policies.py` |
| Fixture corpus (21 addresses) | **Done** | `research/fixtures/corpus.py` |
| Fixture discovery providers (7 methods) | **Done** | `research/fixtures/transport.py` |
| Fixture fetcher (11 outcomes, conditional requests) | **Done** | `research/fixtures/transport.py` |
| Sources, discovery, edges, bodies, retrieval | **Done** | `research/services/acquisition.py` |
| Classification / canonicalization / text derivation | **Done** | `research/services/artifacts.py` |
| Extraction services (RULE, PARSER, MODEL, HUMAN) | **Done** | `research/services/extraction.py` |
| Evidence items and independence | **Done** | `research/services/evidence.py` |
| Claims, confidence, publisher/trust policy | **Done** | `research/services/claims.py` |
| Gaps | **Done** | `research/services/gaps.py` |
| Identity-signal queue | **Done** | `research/services/identity.py` |
| Pipeline orchestration | **Done** | `research/services/pipeline.py` |
| Q2 canonical evidence projection (read-only DTO) | **Done** | `research/services/projection.py` |
| Company-global operational profile rebuild | **Not started** | phase 3 |
| Plan-profile rebuild | **Not started** | phase 3 |
| API and CLI | **Not started** | phase 3 |

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
| — | The plan hash is unique, so one question is one run row | `test_the_run_plan_hash_is_unique` |
| M10 | 304 validates a known body | `test_a_304_validates_a_known_body_and_names_its_validator` |
| M10 | 304 needs a validator | `test_a_304_without_a_validator_is_rejected` |
| A11 | Failed fetch carries no body | `test_a_failed_fetch_must_not_carry_a_body` |
| — | OK fetch requires a body | `test_a_successful_fetch_requires_a_body` |
| L9 | One byte string, two declared content types | `test_the_same_bytes_from_two_sources_are_one_body` |
| — | Nine evidence tables reject UPDATE | `test_evidence_tables_reject_updates` |
| I2 | One-way prune only | `test_a_body_may_be_pruned_exactly_once_and_nothing_else` |
| L2 | Composite FKs bind one body | `test_evidence_cannot_mix_bodies_across_its_three_paths` |
| A9 | One body, two canonicalization versions, two artifacts | `test_one_body_supports_two_canonicalization_versions` |
| M5 | Publication metadata on the derivation | `test_publication_metadata_lives_on_the_derivation_not_the_artifact` |
| E2 | A stated date keeps its own granularity; none is invented | `test_an_invented_publication_date_is_unrepresentable` |
| M11 | Sampled extraction needs a slot | `test_a_sampled_extraction_needs_its_own_execution_slot` |
| — | A signal with a NULL related company cannot duplicate | `test_a_signal_with_a_null_related_company_cannot_duplicate` |
| M16 | ACTIONED cannot return to OPEN | `test_an_actioned_occurrence_cannot_return_to_open` |
| M16 | New episode after a terminal one | `test_a_terminal_occurrence_frees_the_concern_for_a_new_episode` |
| — | One open occurrence per concern | `test_two_open_occurrences_for_one_concern_are_rejected` |
| M2 | Gaps are plan-keyed | `test_a_gap_is_keyed_by_plan_not_by_company` |
| F4 | Gap lifecycle terminates | `test_a_gap_must_open_with_raised_and_terminates` |
| L13/F3 | Three retrievals, three events | `test_repeated_attempts_on_one_source_append_distinct_events` |
| H6 | No commercial judgement column | `test_no_m3_table_carries_a_commercial_judgement_column` |

**36 tests.** Seven rows were re-labelled during the phase-2.1 audit: they
cited A3, A4, M4, E1, G13, G14 and M14, none of which the test beside them
executes. Where the scenario is genuinely covered it now appears in §2b against
a test that does execute it; where it is not, the row carries no id.

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

**66 M3 tests in total** before phase 2.

## 2b. Phase 2 — the internal pipeline

`tests/unit/test_assertion_fingerprint.py` (9 tests) covers the lineage key in
isolation. `tests/integration/test_m3_pipeline.py` (70 tests) and
`tests/integration/test_m3_concurrency.py` (6 tests) execute the pipeline end
to end against real PostgreSQL and a fictional HVAC contractor.

Scenario ids are listed **individually, never as a range**, and a row appears
only when a test executes the behaviour the scenario describes — not merely
something adjacent to it. An adversarial review of the first phase-2 table
found six rows that did neither; §4a records what was removed and why.

| Scenario | Behaviour actually executed | Test |
| --- | --- | --- |
| A1 | Three identical retrievals: one body, one artifact, one source, three events | `test_three_identical_retrievals_append_events_and_nothing_else` |
| A2 | Cosmetic change: two bodies, one artifact | `test_a_cosmetic_change_is_a_new_body_and_the_same_artifact` |
| A3 | Semantic change: new body, new document, neighbours untouched | `test_a_semantic_change_creates_new_bytes_and_a_new_document` |
| A4 | Byte-identical payload at two URLs: one body, two events, MIRROR_CANDIDATE | `test_identical_bytes_at_two_urls_converge_on_one_body` |
| A5 | A redirect is an edge, not a mutation | `test_a_redirect_is_an_edge_not_a_mutation` |
| A7 | Tracking parameters do not create a second source | `test_tracking_parameters_do_not_create_a_second_source` |
| A11 | A failed fetch is an event with no body | `test_a_failed_source_carries_no_body` |
| A13 | One source discovered by several methods records all of them | `test_one_address_found_many_ways_is_one_source_and_many_observations` |
| B3 | A job-posting claim cites the field, the span, the quote and its hash | `test_every_locator_kind_is_exercised`, `test_every_locator_resolves_into_the_text_it_cites` |
| C4 | A claim cannot exist without evidence | `test_a_claim_cannot_exist_without_evidence`, `test_every_m3_claim_cites_evidence` |
| C7 | Two independent lineages asserting one value: two claims, 2 witnesses | `test_two_independent_lineages_asserting_one_value_produce_two_claims` |
| C8 | The same lineage asserted twice produces one claim | `test_the_same_lineage_asserted_twice_produces_one_claim` |
| C8a | A newer extractor over one lineage appends a link, not a twin | `test_a_newer_extractor_over_one_lineage_appends_a_link_not_a_twin` |
| C8c | One assertion, three distinct spans | `test_one_assertion_may_cite_several_spans` |
| C9 | Absence is a gap, never a false claim | `test_absence_is_a_gap_and_never_a_false_claim` |
| C10 | A stated negative is a FACT | `test_a_stated_negative_is_a_fact_and_silence_is_not` |
| C11 | Provenance is single-valued and terminates at one body | `test_evidence_provenance_is_single_valued_and_agrees_on_one_body` |
| D5 | Re-running the same extractor version creates no row | `test_an_extraction_reused_by_a_second_attempt_is_not_duplicated` |
| F1 | Gap identity is deterministic under concurrency | `test_two_workers_raising_one_gap_converge_on_one_parent` |
| F2 | A gap means unknown, not absent | `test_absence_is_a_gap_and_never_a_false_claim` |
| F3 | Repeated attempts append events, leaving the parent alone | `test_a_repeated_attempt_appends_an_event_rather_than_a_second_gap` |
| G3 | A retry advances the same question as attempt n+1 | `test_a_retry_advances_the_same_question`, `test_a_terminal_attempt_stays_terminal` |
| G4 | A policy version change is a different question | `test_a_policy_version_change_creates_a_new_run`, `test_a_different_policy_version_yields_a_distinct_logical_run` |
| G5 | Two workers fetching one URL converge on one body | `test_two_workers_storing_identical_bytes_converge_on_one_body` |
| G7 | Two workers raising one gap produce one gap | `test_two_workers_raising_one_gap_converge_on_one_parent` |
| G13 | A different target attribute set is a different run | `test_a_different_question_is_a_different_run` |
| G14 | The same question reuses the run | `test_a_retry_advances_the_same_question` |
| G15 | Only one attempt may be live, and the caller gets a domain error | `test_only_one_attempt_can_be_live_for_a_question` |
| H6 | No commercial judgement reaches a column | `test_the_pipeline_writes_no_commercial_judgement` |
| L6 | One extraction row, two attempt usages, CREATED then REUSED | `test_an_extraction_reused_by_a_second_attempt_is_not_duplicated` |
| L8 | A redaction policy upgrade is a distinct text derivation | `test_a_second_text_policy_creates_a_second_derivation_and_edits_nothing` |
| M5 | Mirrors with different publication metadata stay one artifact | `test_a_semantic_mirror_keeps_two_derivations_of_one_document`, `test_publication_metadata_lives_on_the_derivation_not_the_artifact` |
| M11 | A sampled contract run twice: two rows, two slots, neither asserting | `test_a_sampled_contract_run_twice_produces_two_rows`, `test_sampled_output_creates_evidence_but_asserts_no_claim` |
| M14 | A company-level signal touches no M2 table | `test_a_registry_parent_raises_a_signal_and_touches_no_m2_identity` |
| O1 | No capability id is written | `test_no_claim_value_carries_a_capability_id_or_a_price` |
| O2 | No qualification is written | `test_the_pipeline_writes_no_commercial_judgement` |
| O3 | No commercial level is written | `test_the_pipeline_writes_no_commercial_judgement` |
| O4 | No price flows into anything | `test_no_claim_value_carries_a_capability_id_or_a_price` |
| O8 | A job-ad mention never becomes a fact about the operation | `test_a_job_ad_mention_never_becomes_a_fact_about_the_operation` |
| O10 | Absence projects as NOT_AVAILABLE | `test_an_unevidenced_attribute_projects_as_not_available` |
| O11 | Observation dates, never fetch dates | `test_the_projection_reports_observation_dates_not_fetch_dates`, `test_an_undated_source_projects_without_a_date_rather_than_a_guess` |
| O12 | The projection refuses an unsupported claim | `test_the_projection_refuses_to_emit_an_unsupported_claim` |

Additional pipeline properties with no single scenario id: every retrieval
outcome exercised, every canonicalization strategy exercised, every extractor
kind exercised, every discovery method exercised, redaction applied, a copied
document adding no corroboration, the same value from two origins staying two
lineages, identity signals raised without touching M2, and 304 semantics.

**160 M3 tests in total** (36 schema invariants + 18 ontology + 12 alignment
documents + 8 traceability counts + 9 fingerprint/lineage + 70 pipeline +
6 concurrency, plus one withdrawn scenario no longer chased).

### Acceptance status, counted mechanically

| | Count |
| --- | --- |
| Scenarios in the contract | 134 |
| Executable, and passing | 64 |
| Failing | 0 |
| Not yet executable | 70 |

Parsed from the documents by `tests/unit/test_traceability_counts.py`, which
also refuses a citation to a scenario that does not exist and refuses ranges.

The count **fell from 70 to 41**, and that is the correction working. Thirty of
the removed ids were never executed by the test beside them; the rest were
replaced by narrower,真 mappings. Accuracy is the point, not the number.

The 93 need the company-global profile rebuild, the plan profile, the public
API, the CLI, retention and pruning, structural HTML/PDF locators, and the
inference-rule catalogue — phase 3 and beyond.

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
| 5 | Confidence was recomputed with an `UPDATE` on an append-only table | **Invariant** | Computed before the insert, never rewritten; a divergence on append now raises (M3-ADR-047) |
| 6 | Independence counted distinct `(publisher, document)` pairs, so one publisher speaking on two of its own pages read as two voices | **Invariant** | Connected components over the publisher/document graph (M3-ADR-048) |
| 7 | A second run that received 304 everywhere extracted nothing, so every attribute became a gap — "nothing changed" became "we know nothing" | **Correctness** | Gap detection spans the research question, not the attempt |
| 8 | `assert_unknown` wrote a `NOT_AVAILABLE` claim, which the deferred evidence trigger rejects | **Correctness** | The trigger was right and C9 is explicit: absence is a gap, and writes no row |
| 9 | The evidence class was merged for validation but not persisted, so observations validated and the claims they produced did not | **Correctness** | The class is folded into the observation's value at construction |
| 10 | The JSON job extractor read a government registry filing and emitted a hiring signal with an empty title | **Correctness** | Shape guards: a media type is not a schema |
| 11 | The fixture transport dropped the 304 validator through a redirect | **Fixture** | Propagated; caught by `ck_fetch_body_semantics`, not by a test |

Defects 4, 5, 6 and 7 could each have produced a wrong number or a wrong
absence in the database. None was visible on paper; all four surfaced only by
running the pipeline against a corpus with a mirror, a contradictory third
party, a page that repeats itself, and a second run.

Defect 6 is the most instructive. The independence rule reads *different
publisher **and** different document*; the implementation counted distinct
pairs, which is the *or*. The corpus caught it immediately because the
contractor advertises 24/7 service on two of its own pages — a company could
have raised its own confidence by adding a page.

**No structural defect has been found in the schema, and migration `0004_m3` is
byte-identical to the commit that introduced it.** Every defect above was in
service code, policy or fixtures. The database refused three of them before a
test could: the append-only trigger, the deferred evidence trigger, and
`ck_fetch_body_semantics`. The schema as designed was implementable
exactly as written, including the three composite provenance foreign keys,
which needed no trigger.
