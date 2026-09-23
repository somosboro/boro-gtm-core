# Changelog

All notable changes to this project are documented here.

## [0.2.0] — 2026-09-23

**M2 Company Discovery and Entity Resolution.**

**M2.** Given noisy, overlapping, partially wrong records from several
providers, maintain a defensible registry of commercial organizations with
traceable evidence for every claim. M0/M1 are unchanged: their suite passes
untouched and a discovery run writes nothing into them.

### Added

* **Four-tier storage.** A durable identity anchor (`companies`, never
  truncated), append-only evidence (provider entities, versions, byte bodies,
  sightings, normalizations, claims, resolution decisions), truncatable derived
  projections keyed only by natural keys, and configuration/operational tables.
* **Provider abstraction** with three declared identity capabilities —
  `NATIVE_EXTERNAL_ID`, `DERIVED_STABLE_KEY`, `CONTENT_ONLY` — and a versioned,
  per-provider canonicalization strategy that participates in version identity.
  Three **fixture** adapters exercise all three, one per media type. **No
  production provider has been selected or implemented**, and no shipped
  adapter can reach the network.
* **Versioned attribute registry** (12 attributes) defining value kind, type,
  units, permitted fact types, cardinality, conflict and projection strategy.
  Typed shadows are derived by a single extractor, so a claim's JSON value and
  its indexed shadow cannot disagree.
* **Entity resolution** in three tiers — deterministic identity-domain match,
  weighted candidate scoring, new identity — with an `AMBIGUOUS` review queue
  that creates and merges nothing. Chains are linear by construction: two
  partial unique indexes, a composite self-FK and a CHECK make a second root,
  a fork, a cross-entity supersession and a self-supersession unrepresentable.
* **Deterministic projections.** Every derived table rebuilds byte-identically
  from evidence alone, reads no clock, and cites the claim ids behind each row.
* **Temporal history** for market presence and relationships: intervals, not
  overwrites. Provider silence never closes an interval; only a positive
  assertion or a retraction does.
* **M1 firewall.** Discovery counts are provider-biased and are not market
  density. There is no promotion path; the one named function raises 501. A
  test asserts M0/M1 row counts are identical across a full discovery run.
* 19 read/write API paths (46 total), a `discovery` CLI group, and a
  PostgreSQL `SKIP LOCKED` job queue.

### Changed

* `tests/integration/test_end_to_end.py::test_no_m2_tables_exist` becomes
  `test_no_m3_tables_exist`: `companies` is now legitimately present as M2's
  identity anchor, while people, outbound and CRM tables remain forbidden.

### Design corrections found by implementing the design

Revision 6 of `docs/M2_COMPANY_DISCOVERY_DESIGN.md` records thirteen defects
that building the specification exposed in it. The consequential ones:

* The gate on canonical writes could not be the run's `status` column, because
  `normalize_run` overwrites it — a failed fetch was laundered into a
  resolvable run by normalizing it. The gate is now the durable
  `fetch_completed_at` (M2-ADR-031).
* `provider_entities.identity_collision` was specified as stored state on an
  append-only table, so it could never be set once the second version arrived.
  It was always `false` and both branches reading it were dead code. Collision
  is now a derived predicate, and it blocks **creation** as well as matching —
  the previous condition let a colliding key mint a company (M2-ADR-034).
* The domain identity policy was enforced only on the read path, so the
  projection stored `IDENTITY` for shared hosting domains (M2-ADR-032).
* A record the adapter could not interpret aborted the whole page and discarded
  the raw evidence of every record after it (M2-ADR-033).
* Nullable `JSONB` columns stored Python `None` as the JSON value `null` rather
  than SQL `NULL`, making an absence claim unrepresentable under its own CHECK
  constraint.
* A run's versions were attributed through the query that first created them,
  so a second run over unchanged records resolved nothing and a run that died
  after fetching stranded its evidence for good. Attribution now goes through
  `provider_record_sightings`, which is what that table is for — and with that
  fixed, two further defects became reachable: a re-run reported `created`
  having created nothing, and it appended a duplicate set of claims.

Test count is now 402 (283 M0/M1, unchanged; 119 new for M2).

## [0.1.1] — 2026-09-22

Packaging fix. No behavioural change to the engine.

### Fixed
* `pyproject.toml` declared `readme`, `keywords` and `classifiers` inside
  `[project.optional-dependencies]` rather than `[project]`, so a clean
  `pip install -e .` failed with
  `project.optional-dependencies.readme must be array`. Existing editable
  installs never re-validated the manifest, so this only surfaced on a fresh
  build. **v0.1.0 is superseded by this release and should not be used.**

### Added
* `tests/unit/test_packaging.py` — nine stdlib-only checks that parse the
  manifest and assert optional-dependency groups hold only arrays, no
  `[project]` key has leaked into another table, the declared readme exists,
  package discovery points at the real source root, the console-script target
  is importable, project URLs are HTTPS and the declared version matches the
  package. Reintroducing the original defect fails two of them.

Test count is now 283.

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

[0.2.0]: https://github.com/somosboro/boro-gtm-core/releases/tag/v0.2.0
[0.1.1]: https://github.com/somosboro/boro-gtm-core/releases/tag/v0.1.1
[0.1.0]: https://github.com/somosboro/boro-gtm-core/releases/tag/v0.1.0
