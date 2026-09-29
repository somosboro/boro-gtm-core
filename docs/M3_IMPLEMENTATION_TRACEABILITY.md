# M3 — Implementation Traceability

**Status:** **M3 complete on `feat/m3-operational-research`** — all 134 live
MUST scenarios are executable and passing. Not merged, not tagged, not
released, pending an independent audit.

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
| Company-global operational profile rebuild | **Done** | `research/services/profiles.py` |
| Plan-profile rebuild and coverage | **Done** | `research/services/profiles.py` |
| Read-time staleness | **Done** | `research/services/staleness.py` |
| Inference rules | **Done** | `research/services/inference.py` |
| Structural locators and resolution | **Done** | `research/services/locators.py` |
| Retention and pruning | **Done** | `research/services/retention.py`, migration `0005_m3_retention` |
| Application services and job integration | **Done** | `research/services/application.py` |
| Human and identity review | **Done** | `research/services/review.py` |
| API | **Done** | `research/api/routes.py`, `research/api/schemas.py` |
| CLI | **Done** | `cli.py` (`research` command group) |
| Production research providers | **Not implemented, by design** | fixtures only (§14) |

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

`tests/integration/test_m3_pipeline.py` (70) and
`tests/integration/test_m3_concurrency.py` (16) execute the acquisition
pipeline end to end against real PostgreSQL and a fictional HVAC contractor.
`tests/unit/test_assertion_fingerprint.py` (9) covers the lineage key alone.

Scenario ids are listed **individually, never as a range**, and a row appears
only when a test executes the behaviour the scenario describes — not something
adjacent to it.

| Scenario | Behaviour actually executed | Test |
| --- | --- | --- |
| A1 | Three identical retrievals: one body, one artifact, one source, three events | `test_three_identical_retrievals_append_events_and_nothing_else` |
| A2 | Cosmetic change: two bodies, one artifact | `test_a_cosmetic_change_is_a_new_body_and_the_same_artifact` |
| A3 | Semantic change: new body, new document, neighbours untouched | `test_a_semantic_change_creates_new_bytes_and_a_new_document` |
| A4 | Byte-identical payload at two URLs: one body, two events, MIRROR_CANDIDATE | `test_identical_bytes_at_two_urls_converge_on_one_body` |
| A5 | A redirect is an edge, not a mutation | `test_a_redirect_is_an_edge_not_a_mutation` |
| A6 | A declared canonical is an edge with its witnessing fetch; nothing merges | `test_a_declared_canonical_url_is_evidence_not_an_instruction` |
| A7 | Tracking parameters do not create a second source | `test_tracking_parameters_do_not_create_a_second_source` |
| A8 | The page 404s later; evidence, quotes and claims are byte-identical | `test_a_disappearing_page_does_not_erase_its_evidence` |
| A9 | One body, two canonicalization versions, two artifacts | `test_one_body_supports_two_canonicalization_versions` |
| A10 | A text policy upgrade: second derivation, no new body/artifact, no refetch | `test_a_text_policy_upgrade_needs_no_refetch` |
| A11 | A failed fetch is an event with no body | `test_a_failed_source_carries_no_body` |
| A12 | One source, two questions, each run's discovery preserved | `test_one_source_serves_two_research_runs_without_losing_provenance` |
| A13 | One source discovered by several methods records all of them | `test_one_address_found_many_ways_is_one_source_and_many_observations` |
| A14 | A redirect learned again appends; the sources are byte-identical | `test_a_redirect_learned_later_mutates_nothing` |
| B1 | An HTML claim cites a CSS path, a heading path, the quote and its hash | `test_an_html_claim_cites_a_css_path_a_heading_path_and_the_quote` |
| B2 | A PDF claim cites page, section, offsets, quote and hash | `test_a_pdf_claim_cites_a_page_a_section_and_offsets` |
| B3 | A job-posting claim cites the field, the span, the quote and its hash | `test_every_locator_kind_is_exercised`, `test_every_locator_resolves_into_the_text_it_cites` |
| B4 | Offsets shift; the quote hash rehomes the locator | `test_a_locator_survives_reprocessing_by_quote_hash` |
| B5 | A rotted locator weakens to a stated weight and deletes nothing | `test_a_rotted_locator_weakens_and_never_deletes` |
| C1 | Conflicting sources coexist as an envelope with a labelled best | `test_a_contradiction_projects_as_an_envelope_with_a_labelled_best` |
| C2 | Precedence is a total order; a more recent earliest retrieval wins | `test_insertion_order_does_not_change_the_stored_projection`, `test_precedence_prefers_fact_type_then_trust_then_date_then_id`, `test_a_more_recent_earliest_retrieval_wins_when_all_else_ties`, `test_the_claim_id_remains_the_final_tiebreak` |
| C3 | An inference is INFERENCE with a rule id and version; FACT is refused | `test_an_inference_is_asserted_as_inference_with_a_named_rule` |
| C4 | A claim cannot exist without evidence | `test_a_claim_cannot_exist_without_evidence`, `test_every_m3_claim_cites_evidence` |
| C5 | One claim over a multi-origin lineage carries every input's evidence | `test_an_inference_carries_every_input_claims_evidence` |
| C6 | One artifact supports several claims | `test_one_artifact_supports_several_claims` |
| C7 | Two independent lineages asserting one value: two claims, 2 witnesses | `test_two_independent_lineages_asserting_one_value_produce_two_claims` |
| C8 | The same lineage asserted twice produces one claim | `test_the_same_lineage_asserted_twice_produces_one_claim` |
| C8a | A newer extractor over one lineage appends a link, not a twin | `test_a_newer_extractor_over_one_lineage_appends_a_link_not_a_twin` |
| C8b | A newer extractor disagreeing creates a claim | `test_a_newer_extractor_disagreeing_with_itself_creates_a_claim` |
| C8c | One assertion, three distinct spans | `test_one_assertion_may_cite_several_spans` |
| C9 | Absence is a gap, never a false claim | `test_absence_is_a_gap_and_never_a_false_claim` |
| C10 | A stated negative is an observation; its fact type follows the source | `test_a_stated_negative_is_an_observation_not_absence` |
| C11 | Provenance is single-valued and terminates at one body | `test_evidence_provenance_is_single_valued_and_agrees_on_one_body` |
| D1 | Extractor provenance is complete, and a model names its model | `test_extractor_provenance_is_complete` |
| D2 | Extractor confidence is not claim confidence | `test_model_confidence_does_not_become_claim_confidence` |
| D3 | A low-confidence reading yields INSUFFICIENT_EVIDENCE and a durable candidate, not a claim | `test_a_low_confidence_extraction_yields_a_gap_not_a_claim`, `test_a_low_confidence_observation_is_discoverable_after_the_process_ends` |
| D4 | Re-extraction reuses; the earlier row is byte-identical | `test_re_extraction_adds_evidence_and_never_rewrites` |
| D5 | Re-running the same extractor version creates no row | `test_an_extraction_reused_by_a_second_attempt_is_not_duplicated` |
| D6 | A confirmation appends a HUMAN lineage and a durable record; the sampled extraction is byte-identical | `test_a_human_confirmation_appends_an_assertable_lineage`, `test_a_confirmation_appends_a_human_lineage_and_a_durable_record`, `test_confirming_an_observation_appends_a_human_lineage` |
| D8 | The subject company is derived from provenance; a second company gets nothing | `test_the_subject_company_comes_from_provenance`, `test_evidence_captured_for_one_company_cannot_be_confirmed_into_another`, `test_the_api_contract_has_no_company_field` |
| D9 | One confirmation covers one attribute at one span | `test_confirming_one_observation_confirms_only_that_observation`, `test_two_observations_on_one_document_need_two_decisions`, `test_a_confirmation_produces_at_most_one_claim_and_records_it` |
| D10 | Only a durable candidate is reviewable | `test_an_ordinary_deterministic_observation_is_not_reviewable`, `test_both_reviewable_reasons_are_distinguished` |
| D11 | A candidate outlives the process that raised it | `test_a_low_confidence_observation_is_discoverable_after_the_process_ends` |
| D12 | Review history reports only persisted or derivable values | `test_review_history_round_trips_through_a_fresh_session`, `test_a_rejection_history_row_reports_no_created_evidence`, `test_reviews_are_listed_for_an_observation`, `test_history_distinguishes_two_readings_of_one_span` |
| D13 | Two readings of one span are two review candidates | `test_two_observations_of_one_span_are_two_candidates`, `test_the_database_keys_a_candidate_on_the_observation`, `test_the_fingerprint_is_a_function_of_the_reading_alone` |
| D14 | A decision closes one question, not its siblings | `test_deciding_one_observation_leaves_its_sibling_pending`, `test_two_reviewers_on_sibling_observations_do_not_collide`, `test_a_reviewed_observation_leaves_the_pending_queue` |
| D15 | A rejection names the observation it disbelieves | `test_a_rejection_is_specific_to_one_observation`, `test_the_database_refuses_a_review_of_no_candidate` |
| D16 | A confirmation reviews the historical reading, not a re-run | `test_a_confirmation_survives_the_extractor_being_replaced`, `test_a_confirmation_survives_a_pruned_body_and_text`, `test_a_candidate_naming_an_absent_observation_is_not_reviewable` |
| D17 | A human review does not mutate the attempt it reviews | `test_a_review_does_not_append_usage_to_the_terminal_attempt`, `test_the_terminal_attempt_row_cannot_be_rewritten_at_all` |
| D18 | The same reading on two sources is two review questions | `test_one_reading_on_two_sources_is_two_review_questions`, `test_the_database_keys_a_candidate_on_the_occurrence`, `test_the_lineage_tag_is_part_of_observation_identity` |
| D19 | Only the first decision acts | `test_a_later_confirmation_after_a_rejection_is_dissent_only`, `test_a_later_rejection_does_not_withdraw_a_confirmed_claim`, `test_the_database_permits_only_one_operative_review`, `test_the_database_forbids_a_dissent_that_asserts` |
| D20 | Two simultaneous first reviewers produce one operative decision | `test_two_operators_deciding_first_produce_exactly_one_operative` |
| D21 | An operative confirmation reconciles the account | `test_an_operative_confirmation_reconciles_the_whole_account`, `test_the_operator_round_trip` |
| D22 | A rejection leaves the question open | `test_a_rejection_leaves_the_gap_open_and_claims_nothing` |
| D23 | A queued row cannot name another account | `test_a_candidate_cannot_claim_another_companys_provenance`, `test_a_candidates_company_cannot_disagree_with_its_provenance`, `test_the_pending_queue_never_leaks_another_companys_candidates` |
| D24 | Every gap event names exactly one actor | `test_a_gap_event_must_name_exactly_one_actor` |
| D25 | Splitting a reading into occurrences does not inflate confidence | `test_confirming_every_mirror_of_one_reading_does_not_inflate_confidence` |
| P1 | Scope comes from M2, never from the company name | `test_scope_is_the_identity_domain_and_the_alternates_m2_accepted`, `test_a_group_domain_is_never_researched_as_the_company`, `test_a_blocklisted_platform_host_cannot_be_crawled_even_as_identity`, `test_a_defunct_domain_is_out_of_scope`, `test_a_company_with_no_domain_is_reported_not_guessed` |
| P2 | Discovery never leaves the company's own domains | `test_discovery_stays_on_the_companys_own_domains`, `test_an_operator_seed_outside_scope_is_refused`, `test_two_companies_researched_in_turn_do_not_share_evidence` |
| P3 | Every hop is checked at the address it resolves to | `test_a_public_name_resolving_to_a_private_address_is_refused`, `test_cloud_metadata_addresses_are_refused`, `test_an_ipv4_mapped_ipv6_address_cannot_smuggle_a_private_address`, `test_one_private_address_among_several_refuses_the_whole_host`, `test_a_globally_routable_address_is_the_only_thing_allowed`, `test_only_http_and_https_are_allowed`, `test_credentials_in_the_url_are_refused`, `test_a_redirect_to_a_private_address_is_refused_at_the_new_hop`, `test_a_redirect_to_a_public_host_that_resolves_privately_is_refused` |
| P4 | A denied source is recorded, not evaded | `test_a_denied_source_is_recorded_not_worked_around`, `test_a_disallowed_path_is_not_fetched`, `test_a_blanket_disallow_stops_everything`, `test_a_rule_naming_our_agent_specifically_is_obeyed`, `test_a_redirect_into_a_disallowed_path_is_refused`, `test_an_honest_user_agent_identifies_us_and_says_how_to_reach_us`, `test_a_crawl_delay_is_honoured_when_the_site_asks_for_one` |
| P5 | A crawl budget is bounded and says when it stopped | `test_a_retrieval_budget_stops_the_run_and_says_so`, `test_depth_is_bounded`, `test_a_redirect_chain_is_bounded`, `test_a_document_over_the_byte_limit_reports_too_large_and_keeps_nothing`, `test_the_budget_defaults_are_conservative` |
| P6 | Live retrieval produces ordinary M3 provenance | `test_a_live_run_produces_evidence_claims_and_a_reviewable_queue`, `test_a_second_live_run_is_conditional_and_cheap`, `test_the_attempt_records_the_scope_it_ran_under`, `test_the_fixture_path_still_works_and_touches_no_network`, `test_a_live_transport_without_a_provider_is_refused` |
| P7 | A live claim walks back to a page on that company's site | `test_a_live_run_produces_evidence_claims_and_a_reviewable_queue`, `test_a_review_candidate_from_a_live_run_is_answerable`, `test_an_unreachable_site_is_a_failed_retrieval_not_an_empty_success` |
| D7 | A rejection persists actor, time and rationale, and asserts nothing | `test_a_rejection_is_durable_and_asserts_nothing`, `test_a_rejection_never_asserts_the_negative`, `test_the_database_forbids_a_rejection_that_names_a_claim`, `test_rejecting_an_observation_persists_and_asserts_nothing` |
| E1 | The retrieval date is never the observation date | `test_a_retrieval_date_is_never_used_as_the_observation_date` |
| E2 | A stated date keeps its own granularity; none is invented | `test_an_invented_publication_date_is_unrepresentable` |
| E3 | Time passing rewrites no claim | `test_time_passing_never_rewrites_a_claim` |
| E4 | Undated evidence reports UNKNOWN_AGE | `test_an_undated_source_reports_unknown_age` |
| E5 | The stored projection reads no clock | `test_the_projection_reads_no_clock` |
| E6 | A removed posting keeps its claim | `test_a_disappearing_page_does_not_erase_its_evidence` |
| F1 | Gap identity is deterministic under concurrency | `test_two_workers_raising_one_gap_converge_on_one_parent` |
| F2 | A gap means unknown, not absent | `test_gaps_state_insufficient_evidence_and_never_absence` |
| F3 | Repeated attempts append events; the parent is untouched | `test_a_repeated_attempt_appends_an_event_rather_than_a_second_gap` |
| F4 | A gap closes by an event carrying the claim id; the parent is unchanged | `test_a_gap_closes_by_an_event_with_no_update_to_the_parent` |
| F5 | A non-applicable attribute leaves both sides of coverage untouched | `test_a_non_applicable_attribute_leaves_both_sides_untouched` |
| F6 | Coverage, confidence and contradiction stay three numbers | `test_coverage_confidence_and_contradiction_stay_separate` |
| F7 | The three "we do not have it" kinds are mutually exclusive | `test_a_low_confidence_attribute_gets_insufficient_and_not_no_evidence` |
| F8 | An unrelated failed source raises no UNRESOLVABLE gap | `test_an_unrelated_failed_source_raises_no_unresolvable_gap`, `test_unresolvable_is_attributed_only_to_a_pursued_attribute` |
| G1 | A run that has not extracted asserts nothing | `test_a_run_that_has_not_extracted_asserts_nothing` |
| G2 | A stage cannot be skipped, and an advance sets no earlier timestamp | `test_a_later_stage_cannot_launder_an_incomplete_earlier_stage` |
| G3 | A retry advances the question as attempt n+1 | `test_a_retry_advances_the_same_question`, `test_a_terminal_attempt_stays_terminal` |
| G4 | A policy version change is a different question | `test_a_policy_version_change_creates_a_new_run`, `test_a_different_policy_version_yields_a_distinct_logical_run` |
| G5 | Two workers fetching one URL converge on one body | `test_two_workers_storing_identical_bytes_converge_on_one_body` |
| G6 | Two workers extracting one text derivation produce one result | `test_two_workers_extracting_one_text_derivation_produce_one_result` |
| G7 | Two workers raising one gap produce one gap | `test_two_workers_raising_one_gap_converge_on_one_parent` |
| G9 | A dead source is retried, and both failures are recorded | `test_a_failed_fetch_is_retried_then_recorded` |
| G10 | One bad source does not lose the others' evidence | `test_one_bad_source_does_not_lose_the_others_evidence` |
| G11 | A terminal attempt never reopens | `test_a_terminal_attempt_can_never_be_reopened` |
| G12 | Evidence remembers which attempt captured it | `test_evidence_remembers_which_attempt_captured_it` |
| G13 | A different target attribute set is a different run | `test_a_different_question_is_a_different_run` |
| G14 | The same question reuses the run | `test_a_retry_advances_the_same_question` |
| G15 | Only one attempt may be live, and the caller gets a domain error | `test_only_one_attempt_can_be_live_for_a_question`, `test_a_second_live_attempt_is_409_not_an_integrity_error` |
| G16 | Two terminal transitions cannot both land | `test_a_gap_cannot_hold_two_terminal_events`, `test_an_occurrence_cannot_hold_two_terminal_events` |
| G17 | The losing terminal writer gets a domain conflict, under true interleaving | `test_two_workers_ending_one_gap_differently_produce_one_terminal`, `test_two_workers_ending_one_occurrence_differently_produce_one_terminal` |
| H1 | A full research run writes no M2 row | `test_m2_is_unchanged_by_a_research_run` |
| H2 | M3 creates no company | `test_m2_is_unchanged_by_a_research_run` |
| H3 | M3 alters no resolution decision | `test_m2_is_unchanged_by_a_research_run` |
| H4 | An identity conflict raises a signal and stops | `test_an_identity_conflict_raises_a_signal_and_stops` |
| H5 | A group or platform domain is never a seed | `test_a_group_domain_is_never_crawled_as_the_companys_site` |
| H6 | No commercial judgement reaches a column | `test_the_pipeline_writes_no_commercial_judgement` |
| H7 | No person record is created | `test_no_person_record_is_created` |
| I1 | A pruned body leaves provenance intelligible | `test_pruning_everything_leaves_every_claim_walkable`, `test_a_claim_remains_explainable_after_its_body_is_pruned` |
| I2 | Pruning is the only permitted mutation | `test_restoring_a_pruned_body_is_rejected`, `test_an_extraction_still_rejects_every_other_update` |
| I3 | Retention deletes no row | `test_retention_deletes_no_row` |
| I4 | A prune cannot launder any other mutation, on all three payload tables | `test_a_prune_cannot_carry_an_illegal_mutation`, `test_a_prune_without_a_timestamp_is_rejected`, `test_a_legal_prune_still_succeeds` |
| J1 | A script fingerprint cannot exceed HYPOTHESIS | `test_a_script_fingerprint_cannot_exceed_hypothesis` |
| J2 | A job mention of a tool cannot exceed PROXY | `test_a_job_mention_of_a_tool_cannot_exceed_proxy` |
| J3 | An explicit company statement may be FACT | `test_an_explicit_company_statement_may_be_fact` |
| J4 | Operating-model attributes cannot be FACT | `test_operating_model_attributes_cannot_be_fact` |
| L1 | Each evidence item names the source it was observed at | `test_one_body_from_two_sources_resolves_to_one_intended_source` |
| L2 | Composite FKs bind one body | `test_evidence_cannot_mix_bodies_across_its_three_paths` |
| L3 | A copy and the page it copied can never both be counted | `test_two_sources_serving_identical_bytes_do_not_corroborate`, `test_a_mirror_never_joins_a_set_with_the_page_it_copied`, `test_independence_matches_the_frozen_rule` |
| L4 | A company page and a registry do corroborate | `test_a_company_page_and_a_registry_do_corroborate`, `test_the_chain_topology_counts_three_independent_witnesses` |
| L5 | Two pages on one site do not corroborate | `test_two_pages_on_one_site_do_not_corroborate`, `test_corroboration_on_the_corpus_follows_the_pairwise_rule` |
| L6 | One extraction row, two attempt usages, CREATED then REUSED | `test_an_extraction_reused_by_a_second_attempt_is_not_duplicated` |
| L7 | A model version change is a distinct extraction contract | `test_a_model_version_change_is_a_distinct_extraction_contract` |
| L8 | A redaction policy upgrade is a distinct text derivation | `test_a_second_text_policy_creates_a_second_derivation_and_edits_nothing` |
| L9 | One byte string, two declared content types | `test_the_same_bytes_from_two_sources_are_one_body` |
| L10 | The sniffed type names its classifier version | `test_the_sniffed_type_names_its_classifier_version` |
| L11 | The same question with different seeds is one run | `test_the_same_question_with_different_seeds_is_one_run` |
| L12 | A different vertical is a different question | `test_a_different_vertical_is_a_different_question` |
| L13 | Three retrievals in one attempt, three ATTEMPTED events | `test_repeated_attempts_on_one_source_append_distinct_events` |
| L14 | A signal with a NULL related company cannot duplicate | `test_a_signal_with_a_null_related_company_cannot_duplicate` |
| L15 | New evidence appends to an existing signal | `test_new_evidence_appends_to_an_existing_signal` |
| L16 | An ACTIONED signal cannot return to OPEN | `test_an_actioned_signal_cannot_return_to_open` |
| L17 | Two search queries in one attempt both survive | `test_two_search_queries_in_one_attempt_both_survive` |
| L18 | A machine-observed edge cannot exist without its fetch | `test_a_machine_observed_edge_cannot_exist_without_its_fetch` |
| L19 | Historical confidence stays explainable after a trust upgrade | `test_a_trust_policy_upgrade_can_re_assert_without_rewriting_history` |
| M1 | Two research plans for one company coexist | `test_two_research_plans_for_one_company_coexist` |
| M2 | Plan-specific gaps do not collide across plans | `test_two_plans_may_hold_different_gap_state_for_one_attribute`, `test_a_gap_is_keyed_by_plan_not_by_company` |
| M3 | An identity conflict needs no fabricated claim | `test_an_identity_conflict_raises_a_signal_and_stops` |
| M4 | Evidence names the exact derivation it came from | `test_evidence_names_the_exact_derivation_it_came_from` |
| M5 | Mirrors with different publication metadata stay one artifact | `test_a_semantic_mirror_keeps_two_derivations_of_one_document`, `test_publication_metadata_lives_on_the_derivation_not_the_artifact` |
| M6 | A trust policy upgrade re-asserts without rewriting history | `test_a_trust_policy_upgrade_can_re_assert_without_rewriting_history` |
| M7 | The same number in different units does not collide | `test_the_same_number_in_different_units_does_not_collide` |
| M8 | Claim confidence reproduces exactly from its own row | `test_claim_confidence_reproduces_exactly_from_its_own_row` |
| M9 | A publisher policy upgrade does not re-score history | `test_a_publisher_policy_upgrade_does_not_re_score_history` |
| M10 | A 304 validates a known body and names its validator | `test_a_304_validates_a_known_body_and_names_its_validator`, `test_a_304_without_a_validator_is_rejected` |
| M11 | A sampled contract run twice: two rows, two slots, neither asserting | `test_a_sampled_contract_run_twice_produces_two_rows`, `test_sampled_output_creates_evidence_but_asserts_no_claim` |
| M12 | A second attempt may carry different seeds | `test_a_second_attempt_may_carry_different_seeds` |
| M13 | Attempt seeds freeze once the attempt starts | `test_attempt_seed_inputs_freeze_once_execution_begins` |
| M14 | A company-level signal touches no M2 table | `test_a_registry_parent_raises_a_signal_and_touches_no_m2_identity`, `test_the_identity_review_path_writes_no_m2_identity` |
| M15 | New evidence leaves signal identity untouched | `test_new_evidence_appends_to_an_existing_signal` |
| M16 | ACTIONED cannot return to OPEN; a terminal occurrence frees the concern | `test_an_actioned_occurrence_cannot_return_to_open`, `test_a_terminal_occurrence_frees_the_concern_for_a_new_episode` |
| N1 | The seeded registry matches the design table exactly | `test_seeded_registry_matches_the_design_table_exactly` |
| N2 | Required attributes drive coverage's denominator | `test_required_attributes_drive_coverages_denominator` |
| O1 | No capability id is written | `test_no_claim_value_carries_a_capability_id_or_a_price` |
| O2 | No qualification is written | `test_the_pipeline_writes_no_commercial_judgement` |
| O3 | No commercial level is written | `test_the_pipeline_writes_no_commercial_judgement` |
| O4 | No price flows into anything | `test_no_claim_value_carries_a_capability_id_or_a_price` |
| O5 | No canonical signal is left uncovered | `test_no_canonical_signal_is_left_uncovered` |
| O6 | The matrix cites only registered attributes | `test_the_matrix_cites_only_attributes_that_are_actually_registered` |
| O7 | Process observations are observational, not judgemental | `test_process_observations_are_observational_not_judgemental` |
| O8 | A job-ad mention never becomes a fact about the operation | `test_a_job_ad_mention_never_becomes_a_fact_about_the_operation` |
| O9 | Evidence class ceilings bound fact type, over every capped attribute | `test_evidence_class_ceilings_bound_every_capped_attribute` |
| O10 | Absence projects as NOT_AVAILABLE | `test_an_unevidenced_attribute_projects_as_not_available` |
| O11 | Observation dates, never fetch dates | `test_the_projection_reports_observation_dates_not_fetch_dates`, `test_an_undated_source_projects_without_a_date_rather_than_a_guess` |
| O12 | The projection refuses an unsupported claim | `test_the_projection_refuses_to_emit_an_unsupported_claim` |
| O13 | The outbound standard forbids feature selling | `test_the_outbound_standard_forbids_feature_selling` |
| O14 | M3 writes exactly two canonical fields | `test_m3_writes_exactly_two_canonical_fields` |
| O15 | Every canonical field and stage has an owner | `test_every_canonical_q1_field_has_exactly_one_owning_milestone`, `test_every_canonical_sales_stage_is_mapped_or_explicitly_out_of_scope` |
| O16 | The contract carries no economics | `test_the_committed_contract_carries_no_commercial_economics` |
| O17 | Contract and canonical YAML agree | `test_the_canonical_yaml_matches_the_committed_contract` |
| O18 | The validator fails when the sales motion is reordered | `test_the_validator_fails_when_the_sales_motion_is_reordered` |

Properties with no single scenario id: every retrieval outcome, every
canonicalization strategy, every extractor kind and every discovery method
exercised; redaction applied; a copied document adding no corroboration; the
same value from two origins staying two lineages; projections rebuilding
byte-identically from empty; the API's raw-payload, pagination and error
contracts; the CLI's fixture-only execution gate.

**545 M3 tests in total** across twenty files, four of them covering the live first-party research path.

### Acceptance status, counted mechanically

| | Count |
| --- | --- |
| Scenarios in the contract | 165 |
| Executable, and passing | 165 |
| Failing | 0 |
| Not yet executable | 0 |

Parsed from the documents by `tests/unit/test_traceability_counts.py`, which
also refuses a citation to a scenario that does not exist, refuses ranges, and
refuses to let the withdrawn G8 be claimed.

The count moved 134 → 139 → 145 → 150 → 158 → **165**, and upward is the only direction
it should move for this reason: each audit proved behaviours the branch did not
have, so scenarios were added rather than the implementation being declared
adequate. The number is recomputed from the acceptance document's headings, not
typed here and matched — no phase targets a count.

M3 Live added **P1–P7** for the production first-party research path: M2-derived
scope, domain containment, per-hop address safety, politeness, budget honesty,
ordinary provenance, and a claim that walks back to a page on the company's own
site.

Phase 3.4 added **D18–D25**. Where the earlier phases were driven by audits, this
one was driven by asking what an operator actually needs to be true:

* **D18** — a candidate keyed `(run, fingerprint)` merged independent publishers.
  Reproduced by instrumenting `raise_candidate`: 30 attempts, 14 landed. Sixteen
  review questions never reached anyone.
* **D19** — every decision ran the full confirmation, so a second reviewer
  silently reversed a colleague.
* **D20** — "the first decision wins" was a claim about luck until a row lock and
  a partial unique index made it a claim about serialization.
* **D21** — a confirmation created the claim and left the gap open, the profile
  empty and coverage stale. The operator's own screen contradicted itself.
* **D22** — the other half: a rejection must *not* close the gap.
* **D23** — `company_id`, `run_id` and `attempt_id` were stored on the candidate
  although all three are derivable, which is three ways to show an operator
  another account's evidence.
* **D24** — a gap closed by a person was recorded as the work of a machine
  attempt, because `attempt_id` was NOT NULL.
* **D25** — the counterweight to D18, added after checking rather than assuming:
  splitting one reading into three reviewable occurrences must not let three
  confirmations look like three independent witnesses. It does not — the
  corroborating publisher count stays 1.

Phase 3.3 added **D13–D17**, all on one root cause:

* **D13** — a review candidate was keyed `(evidence_item_id, run_id)`. An
  evidence item is keyed on its locator, so two rules matching one span share
  one, and `ON CONFLICT DO NOTHING` silently discarded the second question.
* **D14** — the pending queue treated an evidence item as decided if *any*
  review existed for it, so deciding one reading removed its sibling.
* **D15** — a rejection keyed on the evidence item could not say which reading
  the reviewer disbelieved.
* **D16** — confirmation re-ran the source extractor over the derived text, so a
  durable candidate depended on the current extractor registry and became
  unanswerable once retention pruned the body.
* **D17** — the confirmation appended a HUMAN reading to the usage set of an
  already-terminal research attempt.

Phase 3.2 added **D8–D12** and **G17**, and corrected **C10**:

* **D8** — a caller-supplied company id let evidence captured for one company
  be confirmed into another.
* **D9** — confirming one span created four claims and recorded that the
  reviewer had approved all four.
* **D10** — "reviewable" meant knowing an evidence UUID.
* **D11** — a low-confidence deferral lived in transient state, so D3's review
  candidate did not survive the process. D3 was passing unimplemented.
* **D12** — the history endpoint emitted an invented empty list.
* **G17** — the losing terminal writer still met a raw `IntegrityError`, and
  the test that claimed otherwise was not the race.
* **C10** demanded a `FACT` that the source policy forbids; the scenario was
  wrong, not the implementation.

* **D6 split into D6 and D7.** D6 read "a model claim a reviewer *rejects* …
  a HUMAN extraction and a *new claim* are appended" — a rejection must not
  append a claim, and a sampled reading awaiting review has no claim to reject.
* **F7** — `NO_EVIDENCE` and `INSUFFICIENT_EVIDENCE` were raised together for
  one attribute.
* **F8** — `UNRESOLVABLE_SOURCE` was attached to `targets[0]`, so `branch_count`
  read as unreachable while holding three claims.
* **G16** — two terminal transitions could both land; a gap ended `RESOLVED`
  *and* `ABANDONED`.
* **I4** — one UPDATE could combine a legal prune with a forged
  `raw_body_sha256`.

Earlier, four scenarios reached the contract by **building what they asked
for**: B1 and B2 wanted structural paths, B4 and B5 quote-hash resolution and a
rotted-pointer weight.

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
