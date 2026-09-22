# OpenGTM / BoRo GTM Engine — M0 + M1 Implementation Pack

This pack is the implementation handoff for Claude Code.

## Goal
Build the first two milestones of the Market Intelligence subsystem that will become the foundation of OpenGTM (public/open-source core) and BoRo GTM Engine (private BoRo strategy/runtime).

## Milestone definitions

### M0 — Market Intelligence Foundation
M0 ingests and reproduces the existing BoRo Studio International Market Intelligence 2026 dataset.

It must provide:
- PostgreSQL domain model and migrations.
- Import of the supplied 2026 market-intelligence JSON as an immutable snapshot.
- Market registry for the 63-economy normalization universe.
- Source catalog and observation provenance.
- Versioned scoring models and score runs.
- Reproducible calculation of the current base market ranking.
- Read-only REST API for markets, snapshots, sources, scoring models, rankings, score components and TAM/SAM/SOM data.
- Deterministic tests proving that the imported scoring model reproduces the supplied score values within a defined tolerance.

### M1 — Contextual Market Intelligence
M1 turns the base country ranking into a contextual scoring system.

It must provide:
- First-class entities for verticals, ICPs, offers and channels.
- Market × Vertical profiles.
- Contextual score runs that accept `market + vertical + ICP + channel + offer/ticket` as context.
- Separate concepts for score, confidence and data coverage.
- Research-gap detection for missing evidence/metrics.
- API endpoints to calculate and inspect contextual rankings.
- Seed strategy objects for BoRo's initial commercial hypotheses.
- No company discovery, people enrichment, sending or CRM automation yet.

## Files
- `00_CONTEXT_AND_SCOPE.md` — product context, scope and non-goals.
- `01_ARCHITECTURE.md` — technical architecture and module boundaries.
- `02_DOMAIN_MODEL_AND_DATABASE.md` — entities, relationships and schema rules.
- `03_IMPORTER_AND_DATA_CONTRACT.md` — immutable snapshot importer and validation rules.
- `04_SCORING_ENGINE.md` — base + contextual scoring specification.
- `05_API_SPEC.md` — REST contract for M0/M1.
- `06_TEST_AND_ACCEPTANCE.md` — acceptance criteria and test matrix.
- `07_ADRS.md` — architecture decisions that should not be casually changed.
- `08_CLAUDE_CODE_PROMPT.md` — copy/paste prompt for Claude Code.
- `market_intelligence_v1.schema.json` — validation schema for the source JSON.
- `boro_market_intelligence_top50.json` — source dataset to ingest.

## Implementation principle
Do not build a large microservice estate. Start as a modular monolith with asynchronous-ready boundaries. One developer should be able to run the entire system locally with Docker Compose.

The intended public product is **OpenGTM**. BoRo-specific strategy data must remain configuration/seed data, not hard-coded business logic.
