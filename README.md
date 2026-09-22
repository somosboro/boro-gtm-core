# BoRo GTM Core — Working Codename

> **Naming:** "BoRo GTM Core" is a working codename. The public open-source
> name is deliberately undecided — see [ADR-008](docs/ADRS.md). The Python
> namespace (`boro_gtm`), the `GTM_` environment prefix and the database name
> are the only brand-bearing identifiers; no table, column, metric key or API
> path carries a product name.

An evidence-driven GTM Market Intelligence engine. This repository implements
**M0 (Market Intelligence Foundation)** and **M1 (Contextual Market
Intelligence)**.

## What it does

* Imports the BoRo Studio International Market Intelligence 2026 artifact as an
  **immutable, content-hashed snapshot**.
* Builds a 63-economy market registry with deterministic ISO identities, full
  source provenance, normalized observations, TAM/SAM/SOM and deep-dive data.
* **Reproduces the published ranking exactly** — every top-50 score to within
  0.0 and every rank position — through a versioned scoring model.
* Recalculates natively from raw observations where the prerequisites genuinely
  exist, and reports what it cannot compute instead of faking it.
* Scores markets **in context** (vertical x ICP x offer x channel x ticket),
  returning `score`, `confidence` and `coverage` as three separate numbers.
* Turns missing evidence into de-duplicated **research gaps** rather than
  zero-fit verdicts.

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Then, in another shell:

```bash
docker compose exec api python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json
```

```bash
docker compose exec api python -m boro_gtm.cli strategy seed
```

API docs: <http://localhost:8000/api/v1/docs>

## Quick start (local, no Docker)

Requires Python 3.12+ and PostgreSQL 16+.

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

```bash
createdb gtm_core && createdb gtm_core_test
```

```bash
cp .env.example .env
```

Point `GTM_DATABASE_URL` / `GTM_TEST_DATABASE_URL` at your server, then:

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
| Recalculate | `python -m boro_gtm.cli market-intelligence recalculate --snapshot MI-2026-09-21-V1 --model market-attractiveness:1.0 --mode reference_reproduction` |
| Native recalculation | same, with `--mode native_recalculation` |
| Seed strategy objects | `python -m boro_gtm.cli strategy seed` |
| Tests | `pytest -q` |
| Lint | `ruff check packages tests` |

The import command is idempotent and transactional: re-running it returns the
existing snapshot and writes nothing.

## The two score modes

The published artifact exposes derived component values but not every raw input
behind them. Rather than reverse-engineer the missing inputs, the engine keeps
two clearly separated modes.

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
| Chile (home benchmark) | 42.616 | unranked |

Measured reproduction delta across all 51 scored markets: **0.0**. Rank
mismatches across the top 50: **0**.

### `native_recalculation`

Recomputes components from raw observations, and only where every required raw
metric exists. It does not attempt to match the reference run, and the tests
assert that it *differs* rather than pretending otherwise. See
[ADR-010](docs/ADRS.md).

Every market in a native run reports `coverage = 0.80`, because
`icp_density_proxy_20` (20 of 100 weight) requires a population figure the
snapshot never supplies — and emits a persisted `population` research gap for
each of the 51 markets with raw data.

## Score, confidence and coverage

Three independent numbers, never collapsed:

* **score** — estimated attractiveness or fit, 0–100.
* **confidence** — how much the underlying evidence can be trusted, 0–1.
* **coverage** — the share of required evidence that is actually present, 0–1.

**Unknown is not zero.** A missing component is removed from *both* the
numerator and the denominator:

```
covered_weight = sum(weight of scoreable components)
score          = earned_points / covered_weight * 100
coverage       = covered_weight / total_model_weight
```

Coverage drops, a research gap is recorded, and the score stays a statement
about what is actually known. A market with unknown vertical density scores
*higher* than one measured to have poor density — the test suite asserts
exactly that, including a case where two markets with deliberately unequal
coverage earn identical scores.

Base **reference reproduction** is the documented exception: it sums the
published components verbatim, because reproducing the artifact exactly is the
point of that mode.

**Ranking is coverage-gated, never score-gated.** Each model carries an
explicit `minimum_rank_coverage` (0.5 for both seeded models). A result below
it keeps its score and coverage and is returned **unranked** with
`unranked_reason = "coverage_below_minimum"`. Contextual callers may override
per request with `allow_low_coverage`.

## API

All endpoints under `/api/v1`. OpenAPI at `/api/v1/openapi.json`.

**Market intelligence** — `GET /health`, `/market-intelligence/snapshots`,
`/market-intelligence/snapshots/{key}`, `/markets`, `/markets/{iso2}`,
`/markets/{iso2}/observations`, `/markets/{iso2}/sources`,
`/markets/{iso2}/market-size`, `/markets/{iso2}/deep-dive`, `/sources`,
`/sources/{key}`.

**Scoring** — `GET /scoring-models`, `/scoring-models/{key}/{version}`,
`/score-runs`, `/score-runs/{id}`, `/score-runs/{id}/ranking`,
`/score-runs/{id}/markets/{iso2}`; `POST /score-runs/base`.

**Strategy and context (M1)** — `GET /verticals`, `/icps`, `/offers`,
`/channels`, `/markets/{iso2}/verticals`,
`/markets/{iso2}/verticals/{vertical_key}`, `/research-gaps`;
`POST /contextual-rankings`, `/research-gaps/{id}/status`.

Errors use one shape:

```json
{"error": {"code": "INSUFFICIENT_COVERAGE", "message": "...", "details": {}}}
```

Codes: `VALIDATION_ERROR`, `NOT_FOUND`, `SNAPSHOT_NOT_FOUND`, `MODEL_NOT_FOUND`,
`MARKET_NOT_FOUND`, `INSUFFICIENT_COVERAGE`, `IMPORT_CONFLICT`,
`SCORE_REPRODUCTION_FAILED`.

## Layout

```text
packages/boro_gtm/
  core/                     config, db, logging, errors, enums, registry
  market_intelligence/
    domain/                 M0 ORM models
    importers/              contract, countries, mappings, snapshot_importer
    scoring/                percentile, definitions, base_engine, contextual_engine
    research_gaps/          detector
    services/               scoring_service, contextual_service
    api/                    M0 routes and schemas
  strategy/
    domain/                 M1 ORM models
    seeds/                  BoRo seed data and loader
    api/                    M1 routes and schemas
  api/main.py               FastAPI app factory
  cli.py                    Typer CLI
migrations/                 Alembic
services/api/               Dockerfile
tests/                      unit, integration, golden fixtures
docs/                       specs and ADRs
```

## Evidence model

Observations separate *what is known* from *how good it is* from *when it was
true*:

| Column | Meaning |
| --- | --- |
| `availability` | `OBSERVED` or `NOT_AVAILABLE` |
| `fact_type` | FACT / PROXY / ESTIMATE / INFERENCE / HYPOTHESIS — NULL when unavailable |
| `value_numeric` | NULL when unavailable, never 0 |
| `observed_at` | An exact date, only when the source states one |
| `period_label` + `period_granularity` | "2025" + YEAR; never widened into a date |
| `created_at` | Ingest time |

Database CHECK constraints enforce the pairing in both directions, and
`market_snapshots` and `market_observations` are append-only: a `BEFORE UPDATE`
trigger rejects any rewrite (ADR-018).

## Known data gaps and limitations

1. **Population is absent.** `icp_density_proxy_20` cannot be natively
   recomputed. It is reported as uncovered and emits a research gap for all 51
   markets with raw data. (ADR-007, ADR-010)
2. **12 of 63 universe markets have no raw metrics.** They carry a published
   score only. Native percentiles are therefore taken over a 51-market observed
   sub-universe, which is recorded in `universe_definition`.
3. **Software spending is unknown for 21 markets** and is deliberately not an
   input to the score, per the source's own method notes.
4. **Country technology-spending growth is mostly a regional proxy** (Gartner's
   Europe benchmark at 11%) rather than a country figure. It is stored as
   `PROXY` where a value exists and as `NOT_AVAILABLE` where it does not, and
   is excluded from scoring either way.
5. **Vertical-level firm counts do not exist in this snapshot.** Market x
   vertical profiles record that the research named a vertical a priority; they
   carry no invented `fit_score`, TAM or SAM. This is the single largest driver
   of reduced contextual coverage.
6. **TAM/SAM/SOM exists for 20 of 50 markets, deep dives for 20 of 50.**
   Markets outside the deep-dive set (Poland among them) have materially lower
   contextual coverage.
7. **Per-metric provenance is partly inferential.** Only
   `technology_spending_growth` names its own source. Other links are recorded
   as `METRIC_HINT` or `MARKET_LEVEL` and labelled as such rather than
   presented as exact citations.
8. **`ticket_usd` on a market x vertical profile is market-level**, not
   vertical-specific, and says so in its `evidence` blob.
9. **Four deep-dive labels are intentionally unmapped** ("Building technology",
   "Installation technology", "fire/service", "equipment") because a defensible
   vertical mapping would be a guess.
10. **Confidence weights are engine defaults**, not scientific truths. They are
    stored in the model definition and versioned with it.

## Documentation

* [ADRs](docs/ADRS.md) — ADR-008 (naming), ADR-009 (percentile convention),
  ADR-010 (native recalculation honesty), ADR-012 (home benchmarks),
  ADR-014 (`market_gtm_profiles` removed), ADR-015 (absence is not a fact type),
  ADR-016 (temporal provenance), ADR-017 (canonical snapshot identity),
  ADR-018 (append-only evidence tables), ADR-019 (supplied schema preserved)
* [Design note — market_gtm_profiles](docs/DESIGN_NOTE_market_gtm_profiles.md)
* [Scoring model](docs/SCORING.md) — formulas, weights, both modes, confidence
* Original implementation pack: `docs/00_*.md` … `docs/07_ADRS.md`

## Scope boundary

M1 deliberately contains **no** company discovery, company scraping, people
discovery, email enrichment, outbound sending, inbox handling, CRM sync or
Bayesian updating. Those begin at M2.
