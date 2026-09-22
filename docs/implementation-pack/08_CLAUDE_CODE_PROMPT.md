# Claude Code Prompt — Build OpenGTM M0 + M1

Copy/paste the prompt below into Claude Code and attach this entire implementation pack, especially `boro_market_intelligence_top50.json`.

---

You are the lead engineer implementing **OpenGTM M0 + M1**, the first two milestones of an evidence-driven GTM Market Intelligence engine built by BoRo Studio.

I am attaching an implementation pack. Treat these files as the source of truth for scope and acceptance criteria:

- `README.md`
- `00_CONTEXT_AND_SCOPE.md`
- `01_ARCHITECTURE.md`
- `02_DOMAIN_MODEL_AND_DATABASE.md`
- `03_IMPORTER_AND_DATA_CONTRACT.md`
- `04_SCORING_ENGINE.md`
- `05_API_SPEC.md`
- `06_TEST_AND_ACCEPTANCE.md`
- `07_ADRS.md`
- `market_intelligence_v1.schema.json`
- `boro_market_intelligence_top50.json`

## Mission

Implement M0 and M1 completely and leave the repository in a clean, tested, runnable state.

### M0 — Market Intelligence Foundation

Implement:
1. PostgreSQL data model + migrations.
2. Immutable snapshot import of `boro_market_intelligence_top50.json`.
3. 63-market registry covering the full normalization universe.
4. Source catalog and provenance.
5. Normalized market observations.
6. Market categories, competition assessments, technology-growth metadata, TAM/SAM/SOM and deep-dive projections where present.
7. Versioned scoring model `market-attractiveness:1.0`.
8. Imported reference score run.
9. Deterministic reference-reproduction score run that reproduces the supplied ranking/scores.
10. Honest native-recalculation mode for components whose raw prerequisites are actually available.
11. REST APIs defined in the spec.
12. CLI import/recalculation commands.
13. Unit + integration + golden tests.
14. Docker Compose / environment documentation sufficient for a new developer to run it.

### M1 — Contextual Market Intelligence

Implement:
1. vertical registry.
2. ICP registry.
3. offer registry.
4. channel registry.
5. market × vertical profiles.
6. versioned contextual scoring model.
7. contextual ranking endpoint accepting market + vertical + ICP + offer + channel + ticket.
8. separate `score`, `confidence` and `coverage` outputs.
9. research-gap detection for missing contextual evidence.
10. seed BoRo's initial strategy objects from the implementation pack.
11. tests proving missing data reduces coverage rather than being treated as zero-fit.

Do **not** implement company discovery, company scraping, people discovery, email enrichment, cold-email sending, inbox/replies, CRM sync or Bayesian updating. Those belong to later milestones.

## Required engineering behavior

### 1. Inspect first
Before editing anything:
- inspect the repository structure,
- inspect current dependencies,
- identify whether there is already a backend/frontend/database stack,
- read all attached specifications,
- produce a short implementation plan in your response/log.

If the repository is empty or does not already dictate another compatible stack, use:
- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Alembic
- PostgreSQL 16+
- pytest
- Docker Compose
- Redis only if genuinely needed for the M0/M1 job abstraction

Do not introduce Kafka, Kubernetes or a microservice architecture.

### 2. Preserve architecture decisions
The ADRs are constraints, not suggestions.

Especially:
- never put a mutable `score` field on the canonical market table,
- imported snapshots are immutable/content-hashed,
- NULL means unknown, never zero,
- score/confidence/coverage are separate,
- OpenGTM logic stays generic and BoRo-specific data lives in seeds/configuration,
- M2 features are out of scope.

### 3. Do not fake raw data
The source dataset contains derived `subscores`, but not every raw prerequisite used by the original research formula is exposed. In particular, the original ICP-density formula uses implied population, while the JSON does not provide standalone population data for each country.

Therefore implement two explicit score modes:

**A. `reference_reproduction`**
- reproduce the published score from imported supplied component values,
- verify the top50 ranking exactly,
- this is the golden M0 reproducibility path.

**B. `native_recalculation`**
- recalculate only components whose raw prerequisites exist,
- do not reverse-engineer or invent hidden inputs,
- mark unavailable components as uncovered,
- lower coverage and create research gaps.

Never add country-specific hacks just to force the score.

### 4. Make the importer idempotent and transactional
- validate JSON schema + semantic invariants,
- compute SHA-256,
- importing an identical source twice must not duplicate data,
- a failed import must not leave partial rows.

### 5. Exact dataset expectations
The provided source has:
- 50 ranked international markets,
- Chile as home-market benchmark,
- 12 additional normalization-universe markets,
- 63 total economies in the normalization universe.

Golden values include approximately:
- US 81.908 rank 1
- UK 72.401 rank 2
- Germany 72.115 rank 3
- Australia 69.741 rank 4
- France 69.626 rank 5
- Spain 67.183 rank 9
- UAE 63.093 rank 17
- Poland 60.138 rank 23
- Chile benchmark 42.616

These values belong in tests/fixtures, never production business logic.

### 6. Country identities
Use deterministic ISO mapping and test it. At minimum correctly map:
- United States → US
- United Kingdom → GB
- Germany → DE
- Australia → AU
- Canada → CA
- France → FR
- Spain → ES
- Italy → IT
- United Arab Emirates → AE
- Poland → PL
- Chile → CL
- Czechia → CZ
- South Korea → KR
- Taiwan → TW application identifier

Do not silently accept an unresolved market identity.

### 7. API
Implement the endpoints described in `05_API_SPEC.md` under `/api/v1`.

Prioritize:
- snapshots
- markets
- observations
- sources
- market-size
- deep-dive
- scoring models
- score runs
- base rankings
- verticals/ICPs/offers/channels
- contextual rankings
- market × vertical profiles
- research gaps

Generate OpenAPI automatically through FastAPI.

### 8. M1 contextual score
Do not pretend M1 knows the true revenue probability of each market.

Implement a configurable/versioned contextual model with the initial structure in the specification:
- base market prior
- vertical density/fit
- ICP availability
- ticket compatibility
- channel accessibility/compliance
- strategic reuse/localization

The model must be data/config-driven.

Unknown contextual inputs:
- must not become a score of zero,
- must reduce coverage/confidence,
- must produce research gaps,
- and low-coverage markets should not be ranked as directly comparable unless `allow_low_coverage=true`.

### 9. Seed strategy data
Seed at least these verticals:
- commercial_hvac
- mechanical_contractors
- industrial_maintenance
- facilities_management
- refrigeration
- electrical_contractors
- elevator_service
- industrial_equipment_service
- energy_services

Seed ICP:
`boro_field_service_midmarket_v1`

Definition should include approximately:
- B2B
- 20–150 employees preferred
- 10–75 field workers/technicians preferred
- recurring service/maintenance
- positive signals such as installed assets, work orders, maintenance contracts, dispatch, inventory/parts, multi-location, emergency service and field evidence

Seed offers:
- operations_architecture_sprint (roughly US$3k–7.5k)
- operations_os_core (roughly US$15k–30k)
- operations_os_scale (roughly US$25k–60k)
- operations_transformation (roughly US$40k–100k+)

Seed channels:
- email
- phone
- linkedin
- partner
- event
- multichannel

Treat price values as seed configuration, not engine constants.

### 10. Tests are part of the implementation
Run the complete suite before considering the task finished.

At minimum write tests for:
- schema validation
- semantic import validation
- idempotent re-import
- transaction rollback on invalid import
- source integrity
- ISO mapping
- NULL behavior
- fact-type preservation
- base score reproduction
- rank reproduction
- confidence/coverage behavior
- contextual missing-data behavior
- research-gap de-duplication
- required API responses
- clean DB → migrate → import → score → API query integration flow

No test may require external internet.

### 11. Documentation required before completion
Update/create:
- repository README
- `.env.example`
- local startup commands
- migration instructions
- import command
- recalculation command
- test command
- explanation of reference reproduction vs native recalculation
- known limitations/data gaps

### 12. Work autonomously but do not silently redefine scope
If you discover a contradiction in the attached specs:
1. preserve data integrity and reproducibility,
2. choose the smallest reversible implementation,
3. document the decision in a new ADR,
4. continue unless the contradiction makes correct implementation impossible.

Do not stop to ask stylistic questions that can be resolved from the specs.

## Implementation order

Use this sequence:

1. Repository audit and implementation plan.
2. Foundation/config/database.
3. ORM entities + Alembic migrations.
4. Source JSON/Pydantic validation.
5. Importer + ISO/source/metric mappings.
6. M0 read API.
7. Reference scoring model + score-run persistence.
8. Golden reproducibility tests.
9. Native recalculation/coverage behavior.
10. M1 strategy entities + seeds.
11. Market × vertical profiles.
12. Contextual scoring model.
13. Research-gap detector.
14. M1 API.
15. Full integration tests.
16. Documentation/refactor only after behavior is correct.

## Definition of done

Do not report completion until:
- migrations work from an empty database,
- the provided JSON imports successfully,
- a second identical import is idempotent,
- all 63 markets exist,
- source/provenance data is queryable,
- the reference ranking reproduces the source values within tolerance,
- M1 contextual scoring returns score/confidence/coverage separately,
- missing contextual data generates research gaps instead of fake zeros,
- all tests pass,
- the app starts through the documented local workflow.

At the end, provide me:
1. concise summary of what you built,
2. final directory tree for the relevant modules,
3. migrations created,
4. commands to run locally,
5. test results,
6. API endpoint summary,
7. any new ADRs,
8. unresolved data gaps or risks,
9. what should be implemented next in M2 — but DO NOT implement M2.
