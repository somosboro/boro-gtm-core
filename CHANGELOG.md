# Changelog

All notable changes to this project are documented here.

## [0.1.0] — 2026-09-22

**M0/M1 Market Intelligence Foundation.** First public baseline.

### Added — M0, Market Intelligence Foundation

* **Immutable, content-addressed snapshots.** The 63-economy BoRo Studio
  International Market Intelligence 2026 artifact imports as an append-only
  snapshot identified by a canonical SHA-256. Reindenting or reordering the
  source is a no-op; a semantic change under the same key is an
  `IMPORT_CONFLICT`. The literal byte digest is retained separately.
* **Market registry** for all 63 economies with deterministic ISO 3166-1
  resolution — 50 ranked international markets, 1 home-market benchmark,
  12 normalization-universe-only markets. An unresolved market name aborts
  the import rather than creating a silent duplicate.
* **Typed, dated, sourced observations.** 509 observations carrying fact type,
  availability, temporal granularity, methodology and graded source
  attribution (`EXPLICIT` / `METRIC_HINT` / `MARKET_LEVEL`).
* **Source catalog** with 15 entries and full observation provenance links.
* TAM/SAM/SOM estimates, deep-dive metadata, market categories and competition
  assessments, imported only where the source supplies them.
* **Versioned scoring model** `market-attractiveness:1.0`, seven weighted
  components summing to 100, stored as configuration rather than code.
* **Exact reference reproduction.** Max score delta **0.0** across all 51
  scored markets; **0** rank mismatches across the published top 50.
* **Honest native recalculation.** Components are recomputed from raw
  observations only where every prerequisite exists; the rest are reported as
  uncovered and emit research gaps. Native runs do not pretend to match the
  reference run.
* **Read API** and a Typer CLI for import and recalculation.

### Added — M1, Contextual Market Intelligence

* Vertical, ICP, offer and channel registries, with BoRo seed data kept as
  configuration so the engine stays generic.
* Market × vertical profiles derived **only** from snapshot evidence; nothing
  quantitative is invented where the source is silent.
* Versioned `contextual-market-fit:1.0` model scoring
  market × vertical × ICP × offer × channel × ticket.
* First-class contextual dimensions on `score_runs` as real foreign keys, so a
  context is durable and queryable rather than JSON-only.
* **Deterministic research gaps**, de-duplicated by a fingerprint scoped to the
  dimensions each metric actually depends on, written with
  `INSERT ... ON CONFLICT` so concurrent writers never surface an
  `IntegrityError`.

### Semantics

* **Score, confidence and coverage are three separate values** and are never
  collapsed by the API.
* **Unknown is not zero.** Uncovered components leave both the numerator and
  the denominator; the score renormalizes over covered weight and coverage
  falls.
* **Absence is not a fact type.** `N/D` is accepted from a source document and
  translated to `availability = NOT_AVAILABLE` with NULL value and NULL fact
  type. CHECK constraints enforce the pairing in both directions.
* **Temporal provenance never invents precision.** A year-only period cannot
  carry an exact date; a constraint makes that state unrepresentable. Missing
  usable dates degrade confidence rather than being treated as current.
* **One confidence algorithm**, in which fact type, temporal precision and the
  source's own HIGH/MEDIUM/LOW label all materially participate.
* **Standard competition ranking** for ties — `80, 80, 72, 65` → `1, 1, 3, 4`.
  A rank depends on score alone. Ranking is coverage-gated via an explicit
  per-model `minimum_rank_coverage`; a result below it keeps its score and is
  returned unranked.

### Database invariants

* 17 CHECK constraints covering every closed vocabulary.
* `market_snapshots` and `market_observations` are append-only: a
  `BEFORE UPDATE` trigger rejects rewrites while leaving INSERT, DELETE
  cascades and TRUNCATE-based test teardown untouched.
* Two Alembic migrations, verified from a completely empty database.

### Testing

274 tests — unit and integration — with zero skips and no network access.
Integration tests run the real migrations against live PostgreSQL. Golden
values are asserted from the database, not from memory.

### Known limitations

Documented in full in the README. In short: population is absent from the
snapshot (native coverage caps at 0.80), 12 of 63 universe markets carry a
score and nothing else, vertical-level firm counts do not exist, and TAM/SAM/SOM
plus deep dives cover 20 of 50 markets.

### Not included

M2 Company Discovery is **designed but not implemented**. There is no company
table, provider adapter, discovery job, enrichment, people/buyer, campaign or
CRM code in this release. See [`docs/M2_COMPANY_DISCOVERY_DESIGN.md`](docs/M2_COMPANY_DISCOVERY_DESIGN.md).

[0.1.0]: https://github.com/somosboro/boro-gtm-core/releases/tag/v0.1.0
