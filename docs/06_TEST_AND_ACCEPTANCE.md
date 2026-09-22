# 06 — Test and Acceptance Plan

## M0 acceptance criteria

M0 is DONE only when all of the following pass.

### A. Environment
- one documented local startup path
- PostgreSQL starts via Docker Compose
- backend starts successfully
- migrations run from a clean DB
- health endpoint returns success

### B. Import
- supplied JSON validates
- source hash is persisted
- reimport is idempotent
- exactly 63 canonical markets are represented from the source universe
- exactly 50 ranked international markets are imported
- Chile is represented as home-market benchmark
- normalization-universe-only markets are retained
- all source catalog entries are imported
- NULL values remain NULL
- TAM/SAM/SOM only exists where source supplies it

### C. Provenance
- every imported market retains its source-key references
- source catalog can be queried via API
- fact type is preserved where supplied
- no unsupported per-metric source attribution is fabricated

### D. Base scoring
- scoring model `market-attractiveness:1.0` exists
- model components/weights sum to 100
- imported reference score run exists
- recalculated/reference-reproduction run reproduces the published top50 score values within <= 0.005
- top50 rank order exactly matches source
- United States score approximately 81.908
- United Kingdom approximately 72.401
- Germany approximately 72.115
- Chile benchmark approximately 42.616

### E. API
- required M0 GET endpoints documented and tested
- base score-run POST endpoint works
- pagination/filters do not corrupt ranking semantics

### F. Tests
- unit tests for importer mappings
- unit tests for percentile/scoring helpers
- unit tests for missing-value behavior
- integration test: clean DB → import → score → query
- no test depends on external internet

## M1 acceptance criteria

### A. Strategy entities
Seeded and queryable:
- verticals
- ICPs
- offers
- channels

### B. Initial BoRo seed
At minimum:
- `commercial_hvac`
- `industrial_maintenance`
- `facilities_management`
- `mechanical_contractors`

ICP:
- `boro_field_service_midmarket_v1`

Offers:
- `operations_architecture_sprint`
- `operations_os_core`

Channels:
- `email`
- `phone`
- `linkedin`
- `partner`
- `multichannel`

### C. Contextual scoring
- accepts market + vertical + ICP + offer + channel + ticket
- never mutates base market score records
- returns score/confidence/coverage separately
- exposes component explanations
- unknown contextual evidence lowers coverage rather than becoming zero-fit
- below-threshold coverage prevents comparable ranking unless override flag is supplied

### D. Research gaps
- missing metrics create deterministic research gaps
- no duplicate open gap for the same context + metric
- resolved gaps are not recreated unless underlying snapshot/model context changes

### E. M1 API
- contextual ranking endpoint works against 10-market initial portfolio
- market × vertical profile endpoints work
- research gap list works

### F. No M2 leakage
M1 must not include:
- company scrapers
- company database
- people discovery
- email finding
- outbound sending
- CRM sync

## Quality gates

### Code quality
- type hints for public Python interfaces
- Pydantic schemas separate from ORM models where appropriate
- no SQL built by string concatenation
- migrations committed
- no secrets in repository
- `.env.example` provided

### Documentation
- README startup instructions
- architecture decisions recorded
- score formula documented
- known data gaps documented

### Performance targets
Dataset is small; correctness matters more than optimization.

Still:
- top50/base ranking request should normally complete in <500ms from persisted score run
- score recalculation for 63 markets should complete in a few seconds locally
- importer should be transactional

## Golden tests

Create a fixture from the supplied JSON and assert at least:

```text
US score 81.908 rank 1
GB score 72.401 rank 2
DE score 72.115 rank 3
AU score 69.741 rank 4
FR score 69.626 rank 5
ES score 67.183 rank 9
AE score 63.093 rank 17
PL score 60.138 rank 23
CL benchmark score 42.616
```

Do not hard-code these into production logic; they belong in tests/fixtures.
