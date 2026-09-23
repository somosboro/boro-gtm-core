# BoRo GTM Core

**Evidence-driven market intelligence and commercial experimentation infrastructure.**

Most go-to-market tooling produces confident numbers from thin evidence. This
project takes the opposite position: a score is worthless unless you can trace
every component back to a source, see how much of the model was actually
measured, and tell the difference between *bad* and *unknown*.

> **Status — what exists today**
>
> | Milestone | Scope | State |
> | --- | --- | --- |
> | **M0** | Market Intelligence Foundation | **Implemented** |
> | **M1** | Contextual Market Intelligence | **Implemented** |
> | **M2** | Company Discovery + Entity Resolution | **Implemented** |
> | M3+ | Operational research, qualification, experiments, revenue | Roadmap only |
>
> M2 discovers and resolves **companies**. There is no people, enrichment,
> outbound or CRM code in this repository. The three provider adapters that
> ship are **fixtures**: they read local files, no production provider has been
> selected, and no test or adapter can reach the network.

---

## Why it exists

BoRo Studio sells operational architecture to B2B companies with complex
field-service workflows. Choosing which markets to enter is an evidence
problem, not an opinion problem — and the underlying research concluded that
market attractiveness is **contextual**: a single immutable `country.score`
cannot answer "is Germany a good market *for commercial HVAC, for this ICP,
through this channel, at this ticket size*".

So the engine models:

```
market_score(country, vertical, ICP, channel, ticket, data_snapshot)
```

and refuses to pretend it knows more than it does.

## Evidence-first philosophy

Five principles do most of the work:

**1. No score without provenance.** Every component traces to a source, a
snapshot and a deterministic formula. The canonical `markets` table has no
score column at all — a score is always the output of a versioned run.

**2. Evidence is typed.** Every observed value carries what kind of evidence it
is:

| Fact type | Meaning |
| --- | --- |
| `FACT` | Directly reported source datum |
| `PROXY` | Observable surrogate for a missing comparable measure |
| `ESTIMATE` | Derived range or modelled figure |
| `INFERENCE` | Analytical interpretation |
| `HYPOTHESIS` | Requires commercial validation |

**3. Absence is not a fact type.** "We have no data" is recorded as
`availability = NOT_AVAILABLE` with a NULL value and a NULL fact type — never
as zero, never as a sixth evidence kind.

**4. Score, confidence and coverage are three different numbers.**

| | Range | Question it answers |
| --- | --- | --- |
| `score` | 0–100 | How attractive does the model think this is? |
| `confidence` | 0–1 | How much can the underlying evidence be trusted? |
| `coverage` | 0–1 | How much of the model could actually be computed? |

A market can score 90 on 30% coverage. The API will tell you so rather than
collapsing it into one reassuring number.

**5. Unknown is not negative.** A component that cannot be computed leaves
*both* the numerator and the denominator:

```
covered_weight = sum(weight of scoreable components)
score          = earned_points / covered_weight * 100
coverage       = covered_weight / total_model_weight
```

Coverage drops, a research gap is recorded, and the score stays a statement
about what is known. A market with unknown vertical density scores **higher**
than one measured to have poor density — the test suite asserts exactly that.

## Two scoring modes

The source research artifact publishes derived component values but not every
raw input behind them. Rather than reverse-engineer the missing inputs, the
engine keeps two clearly separated modes.

### `reference_reproduction`

Recomputes each market's total from the component values stored at import.
This is the parity path and the golden acceptance test.

| Market | Score | Rank |
| --- | --- | --- |
| United States | 81.908 | 1 |
| United Kingdom | 72.401 | 2 |
| Germany | 72.115 | 3 |
| Australia | 69.741 | 4 |
| France | 69.626 | 5 |
| Spain | 67.183 | 9 |
| United Arab Emirates | 63.093 | 17 |
| Poland | 60.138 | 23 |
| Chile *(home benchmark)* | 42.616 | unranked |

Measured reproduction delta across all 51 scored markets: **0.0**. Rank
mismatches across the published top 50: **0**.

### `native_recalculation`

Recomputes components from raw observations, and only where every required raw
metric exists. It does not attempt to match the reference run, and the tests
assert that it *differs* rather than pretending otherwise.

Every market in a native run reports `coverage = 0.80`, because
`icp_density_proxy_20` (20 of 100 weight) needs a population figure the
snapshot never supplies — and emits a persisted `population` research gap for
each of the 51 markets with raw data.

## Ranking semantics

Ranking is **coverage-gated, never score-gated**. Each model carries an
explicit `minimum_rank_coverage`. A result below it keeps its score and
coverage and is returned *unranked*, with
`unranked_reason = "coverage_below_minimum"`.

Ties use **standard competition ranking** — `80, 80, 72, 65` → `1, 1, 3, 4`. A
rank is a function of score alone; input order, query order and ISO code cannot
change it. The real dataset exercises this: in native mode Malaysia and
Slovakia both score `35.100000` and both hold rank **42**, with Romania at
rank **44**.

Rank sequences may therefore contain gaps by design. Consumers must not assume
`max(rank) == count(ranked)`.

## Architecture

A modular monolith with worker-ready boundaries — no microservices, no Kafka,
no Kubernetes.

```
Source JSON
   ↓ canonical hash, schema + semantic validation
Immutable MarketSnapshot          ← append-only, content-addressed
   ↓
Market registry · Source catalog · Observations (typed, dated, sourced)
   ↓
ScoringModel (versioned, config-driven)
   ↓
ScoreRun → MarketScore → MarketScoreComponent
   ↓
Contextual run: market × vertical × ICP × offer × channel × ticket
   ↓
score + confidence + coverage + explanations + research gaps
```

Snapshots are content-addressed by a **canonical** digest, so reindenting or
reordering a source file is a no-op import, while a semantic change under the
same snapshot key is an `IMPORT_CONFLICT`. `market_snapshots` and
`market_observations` are append-only — a `BEFORE UPDATE` trigger rejects any
rewrite.

## Quickstart

### Docker

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

```bash
docker compose exec api python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json
```

```bash
docker compose exec api python -m boro_gtm.cli strategy seed
```

API docs: <http://localhost:8000/api/v1/docs>

### Local

Requires Python 3.12+ and PostgreSQL 16+.

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

```bash
createdb gtm_core && createdb gtm_core_test && cp .env.example .env
```

```bash
alembic upgrade head
```

```bash
python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json
```

```bash
python -m boro_gtm.cli strategy seed
```

```bash
uvicorn boro_gtm.api.main:app --reload --port 8000
```

## Commands

| Task | Command |
| --- | --- |
| Migrate | `alembic upgrade head` |
| Import snapshot | `python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json` |
| Reference reproduction | `python -m boro_gtm.cli market-intelligence recalculate --snapshot MI-2026-09-21-V1 --mode reference_reproduction` |
| Native recalculation | `python -m boro_gtm.cli market-intelligence recalculate --snapshot MI-2026-09-21-V1 --mode native_recalculation` |
| Seed strategy objects | `python -m boro_gtm.cli strategy seed` |
| Seed discovery registry | `python -m boro_gtm.cli discovery seed` |
| Discovery run (fixtures) | `python -m boro_gtm.cli discovery run --provider fixture_json_directory --fixture ./data/fixtures/discovery_sample.json --market DE` |
| Rebuild projections | `python -m boro_gtm.cli discovery project` |
| M1 write fingerprint | `python -m boro_gtm.cli discovery firewall` |
| Tests | `pytest -q` |
| Lint | `ruff check packages tests migrations` |

The import command is idempotent and transactional: re-running it returns the
existing snapshot and writes nothing.

## API

All endpoints under `/api/v1`; OpenAPI at `/api/v1/openapi.json`. 46 paths, 47 operations.

**Market intelligence** — `/health`, `/market-intelligence/snapshots[/{key}]`,
`/markets`, `/markets/{iso2}`, `/markets/{iso2}/observations`,
`/markets/{iso2}/sources`, `/markets/{iso2}/market-size`,
`/markets/{iso2}/deep-dive`, `/sources[/{key}]`

**Scoring** — `/scoring-models[/{key}/{version}]`, `/score-runs[/{id}]`,
`/score-runs/{id}/ranking`, `/score-runs/{id}/markets/{iso2}`,
`POST /score-runs/base`

**Strategy and context (M1)** — `/verticals`, `/icps`, `/offers`, `/channels`,
`/markets/{iso2}/verticals[/{vertical_key}]`, `/research-gaps`,
`POST /contextual-rankings`, `POST /research-gaps/{id}/status`

**Companies (M2)** — `/companies[/{id}]`, `/companies/{id}/claims`,
`/companies/{id}/locations`, `/companies/{id}/market-presences`,
`/companies/{id}/verticals`, `/companies/{id}/relationships`

**Discovery and resolution (M2)** — `/discovery-providers`,
`/attribute-registry`, `/discovery-runs[/{id}]`, `/discovery-runs/{id}/queries`,
`/discovery-runs/{id}/versions`, `/provider-entities/{id}/versions`,
`/provider-entities/{id}/resolution-chain`,
`/entity-resolution/decisions`, `/entity-resolution/ambiguous`,
`POST /entity-resolution/decisions`, `/projections/runs`,
`POST /projections/rebuild`

Every claim carries `availability`, `fact_type` and `confidence` separately,
and names both the provider record version it came from and the resolution
decision that attributed it.

One error shape throughout:

```json
{"error": {"code": "INSUFFICIENT_COVERAGE", "message": "...", "details": {}}}
```

## Evidence model

| Column | Meaning |
| --- | --- |
| `availability` | `OBSERVED` or `NOT_AVAILABLE` |
| `fact_type` | FACT / PROXY / ESTIMATE / INFERENCE / HYPOTHESIS — NULL when unavailable |
| `value_numeric` | NULL when unavailable, never 0 |
| `observed_at` | An exact date, only when the source states one |
| `period_label` + `period_granularity` | `"2025"` + `YEAR`; never widened into a date |
| `created_at` | Ingest time |

A CHECK constraint makes `observed_at` present *if and only if* granularity is
`DATE`, which makes invented precision unrepresentable. In the current snapshot
no metric is day-precise, so `observed_at` is NULL throughout — the correct,
honest result.

## Testing and reproducibility

```bash
pytest -q          # 402 tests (283 M0/M1, 119 M2)
ruff check packages tests migrations
```

Integration tests run the real Alembic migrations against a live PostgreSQL
database, so "migrations work from an empty database" is exercised on every
run rather than asserted once. No test reaches the network.

Release acceptance is reproducible from an empty database: migrate → import →
re-import (no-op) → seed → reference reproduction → native recalculation →
contextual ranking, with golden values asserted from the database rather than
from memory. The M2 half runs in the same CI job: seed the discovery registry →
fetch from fixtures → resolve → rebuild projections twice and compare digests →
assert every M0/M1 row count is unchanged.

## Repository structure

```
packages/boro_gtm/
  core/                     config, db, logging, errors, enums, registry
  market_intelligence/
    domain/                 M0 ORM models
    importers/              contract, countries, mappings, snapshot_importer
    scoring/                percentile, confidence, ranking, definitions, engines
    research_gaps/          deterministic gap detector
    services/               scoring_service, contextual_service
    api/                    M0 routes and schemas
  strategy/
    domain/ seeds/ api/     M1 registries, BoRo seed data, M1 routes
  discovery/
    domain/                 M2 ORM models (anchor, evidence, projections)
    providers/              adapter contract, canonicalizers, fixture adapters
    services/               ingestion, resolution, claims, projection, runs,
                            jobs, firewall
    registry.py seeds.py    versioned attribute registry and seeding
    api/                    M2 routes and schemas
  api/main.py               FastAPI app factory
  cli.py                    Typer CLI
migrations/                 Alembic (0001 initial, 0002 remediation,
                            0003 M2 company discovery) — head 0003_m2
services/api/               Dockerfile
tests/                      unit, integration, golden fixtures
docs/                       ADRs, scoring reference, M2 design set, source pack
data/                       supplied snapshot + JSON Schema (verbatim),
                            discovery fixtures
```

## Current evidence gaps and limitations

Stated plainly, because pretending otherwise would defeat the point.

1. **Population is absent from the snapshot**, so `icp_density_proxy_20` cannot
   be natively recomputed. Native coverage is capped at 0.80 and 51 research
   gaps are recorded.
2. **12 of the 63 universe markets carry a published score and nothing else.**
   Native percentiles are ranked over the 51-market observed sub-universe, and
   that reduced universe is recorded on every run.
3. **Vertical-level firm counts do not exist in this snapshot.** Market ×
   vertical profiles record that research named a vertical a priority; they
   carry no invented `fit_score`, TAM or SAM. This is the single largest driver
   of reduced contextual coverage.
4. **TAM/SAM/SOM exists for 20 of 50 markets; deep dives for 20 of 50.**
   Markets outside the deep-dive set have materially lower contextual coverage.
5. **Per-metric provenance is partly inferential.** Only
   `technology_spending_growth` names its own source; other links are labelled
   `METRIC_HINT` or `MARKET_LEVEL` rather than presented as exact citations.
6. **Confidence weights are engine defaults, not scientific truths.** They are
   stored in the model definition and versioned with it.
7. **Software spending is unknown for 21 markets** and is deliberately not a
   score input, per the source's own method notes.
8. **Single snapshot.** Cross-snapshot trend analysis is untested.

## Roadmap

The wider BoRo GTM Engine is intended as market-to-revenue intelligence
infrastructure:

```
Market Intelligence                      ← M0  implemented
Market × Vertical × ICP × Channel × Ticket ← M1  implemented
Company Discovery                        ← M2  implemented
Operational Research                     ← M3  roadmap
Evidence                                 ← M3  roadmap
Explainable Qualification                ← M4  roadmap
Buyer Discovery                          ← M5  roadmap
Commercial Experiments                   ← M6  roadmap
Pipeline / Revenue                       ← M7  roadmap
Market Learning                          ← M8  roadmap
```

M0, M1 and M2 are implemented. Everything below them is design or intent.

A boundary already designed for: **company discovery counts must never silently
become factual vertical market density.** Provider coverage, indexing and query
strategy introduce bias, so discovery output may only ever enter M1 as a
`PROXY` carrying methodology, provider, query definition, retrieval date and
calibration. That promotion path is **not implemented**: the one function
named for it raises `501 DENSITY_PROMOTION_NOT_IMPLEMENTED`, and a test asserts
M0/M1 row counts are byte-identical before and after a full discovery run. See
[the M2 design](docs/M2_COMPANY_DISCOVERY_DESIGN.md).

## Documentation

* [ADRs](docs/ADRS.md) — 20 decision records, including the naming decision,
  the percentile convention, native-recalculation honesty, evidence semantics
  and ranking ties
* [Scoring reference](docs/SCORING.md)
* [M2 design](docs/M2_COMPANY_DISCOVERY_DESIGN.md) — revision 6 records the
  sixteen defects that implementing revision 5 exposed in it ·
  [M2 schema graph](docs/M2_SCHEMA_GRAPH.md) ·
  [M2 acceptance criteria](docs/M2_ACCEPTANCE_CRITERIA.md) ·
  [M2 ADRs](docs/M2_ADRS.md)
* [Documentation index](docs/README.md)

## Naming

*BoRo GTM Core* is a working codename for the market-intelligence core of the
internal BoRo GTM Engine. The Python namespace (`boro_gtm`), the `GTM_`
environment prefix and the database name are the only brand-bearing
identifiers — no table, column, metric key or API path carries a product name,
so the final public name can change without touching the schema
([ADR-008](docs/ADRS.md)).

---

Built by [BoRo Studio](https://somosboro.com).
