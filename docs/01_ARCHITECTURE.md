# 01 — Architecture

## Architecture style

Use a **modular monolith + worker-ready architecture**, not microservices.

Target local developer experience:

```bash
git clone ...
cp .env.example .env
docker compose up --build
```

## Recommended stack

### Backend
- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Alembic

### Database
- PostgreSQL 16+
- `pgvector` extension may be enabled now, but M0/M1 do not depend on embeddings.

### Async / jobs
- Redis
- Dramatiq or another lightweight worker queue
- M0/M1 only require the abstraction and optional import/recalculation jobs; do not overbuild job infrastructure.

### Frontend
A full UI is not required for acceptance. If the existing repository already contains a Next.js app, add a minimal Market Intelligence read-only view only after backend acceptance criteria pass.

### Observability
- structured JSON logs
- request IDs / correlation IDs
- score-run IDs
- import-run IDs
- provider/job cost tracking can be scaffolded but is not required for M0/M1

## Logical modules

```text
app/
  core/
    config
    db
    logging
    errors

  market_intelligence/
    domain/
    repositories/
    services/
    api/
    importers/
    scoring/
    research_gaps/

  strategy/
    domain/
    repositories/
    api/

  jobs/
    domain/
    api/
```

Exact directory layout may adapt to the existing repository, but preserve these boundaries.

## Core flow — M0

```text
Source JSON
   ↓
Validation
   ↓
Immutable MarketSnapshot
   ↓
Market Registry
   ↓
Source Catalog
   ↓
Observations / estimates / deep-dive metadata
   ↓
ScoringModel v1
   ↓
ScoreRun
   ↓
Recalculated Market Scores
   ↓
Reproducibility Tests
   ↓
REST API
```

## Core flow — M1

```text
Market observations
      +
Vertical
      +
ICP
      +
Offer/ticket
      +
Channel
      ↓
ContextualScoreRequest
      ↓
Component evaluator
      ↓
Score + Confidence + Coverage
      ↓
Contextual ranking
      ↓
Research gap detector
```

## Important architectural constraints

### 1. No mutable country score
Never add a `score` column to the canonical market/country record.

A score is always the product of:
- scoring model/version
- input snapshot
- normalization universe
- optional context
- time

### 2. Raw snapshot is immutable
The original JSON must be retained as an immutable snapshot artifact/hash. Parsed rows are projections of that source, not replacements for it.

### 3. Provenance is first-class
No observation or score component should be impossible to trace back to:
- a source,
- a snapshot,
- or a deterministic formula.

### 4. NULL means unknown, not zero
Missing country-level technology-spend or TAM/SAM/SOM data must remain unknown.

### 5. Score != confidence != coverage
Three independent values:
- `score`: estimated attractiveness/fit, normally 0–100
- `confidence`: confidence in the estimate, 0–1
- `coverage`: proportion of required evidence/metrics available, 0–1

### 6. Generic core, BoRo as configuration
OpenGTM logic must not hard-code BoRo's market list, verticals or pricing.

BoRo data should be seeds/configuration.

### 7. Reproducibility before feature breadth
M0 is not accepted if the engine cannot reproduce the source score run.
