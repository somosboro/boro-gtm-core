# 05 — REST API Specification

Use `/api/v1` prefix.

Responses should include stable IDs plus human-readable keys.

## Health

### `GET /api/v1/health`
Return DB connectivity and app version.

## Snapshots

### `GET /api/v1/market-intelligence/snapshots`
List imported snapshots.

### `GET /api/v1/market-intelligence/snapshots/{snapshot_key}`
Return metadata, counts, hash and score runs.

## Markets

### `GET /api/v1/markets`
Filters:
- region
- category
- min_score (requires score_run)
- home_market
- limit/offset

### `GET /api/v1/markets/{iso2}`
Return canonical market record plus optional latest snapshot projection.

### `GET /api/v1/markets/{iso2}/observations`
Filters:
- snapshot
- metric_key
- fact_type

### `GET /api/v1/markets/{iso2}/sources`
Return source/provenance relationships.

### `GET /api/v1/markets/{iso2}/market-size`
Return TAM/SAM/SOM records.

### `GET /api/v1/markets/{iso2}/deep-dive`
Return deep-dive metadata when present.

## Sources

### `GET /api/v1/sources`
### `GET /api/v1/sources/{source_key}`

## Scoring models

### `GET /api/v1/scoring-models`
### `GET /api/v1/scoring-models/{key}/{version}`
Return formula/configuration and components.

## Score runs / rankings

### `GET /api/v1/score-runs`
Filters:
- snapshot
- model
- kind

### `GET /api/v1/score-runs/{id}`

### `GET /api/v1/score-runs/{id}/ranking`
Return ordered markets with score/confidence/coverage.

### `GET /api/v1/score-runs/{id}/markets/{iso2}`
Return score components and explanations.

### `POST /api/v1/score-runs/base`
Create/recalculate a base score run.

Example:

```json
{
  "snapshot_key": "MI-2026-09-21-V1",
  "model_key": "market-attractiveness",
  "model_version": "1.0",
  "mode": "reference_reproduction"
}
```

## M1 strategy registries

### `GET /api/v1/verticals`
### `GET /api/v1/icps`
### `GET /api/v1/offers`
### `GET /api/v1/channels`

Read-only is sufficient for M1 if seeds/config drive definitions. CRUD is optional and must not delay acceptance.

## Market × Vertical profiles

### `GET /api/v1/markets/{iso2}/verticals`
### `GET /api/v1/markets/{iso2}/verticals/{vertical_key}`

Return profile + confidence + coverage + known gaps.

## Contextual scoring

### `POST /api/v1/contextual-rankings`

Request:

```json
{
  "snapshot_key": "MI-2026-09-21-V1",
  "model_key": "contextual-market-fit",
  "model_version": "1.0",
  "vertical_key": "commercial_hvac",
  "icp_key": "boro_field_service_midmarket_v1",
  "offer_key": "operations_architecture_sprint",
  "channel_key": "multichannel",
  "ticket_usd": 3000,
  "market_iso2": ["US", "GB", "DE", "AU", "CA", "FR", "ES", "IT", "AE", "PL"],
  "allow_low_coverage": false
}
```

Response:

```json
{
  "score_run_id": "...",
  "context": {},
  "results": [
    {
      "market": {"iso2": "US", "name": "United States"},
      "score": 0.0,
      "confidence": 0.0,
      "coverage": 0.0,
      "rank": 1,
      "components": [],
      "research_gaps": []
    }
  ]
}
```

The actual score may be absent / non-comparable when required coverage is too low.

## Research gaps

### `GET /api/v1/research-gaps`
Filters:
- market
- vertical
- status
- priority

### `POST /api/v1/research-gaps/{id}/status`
Optional for M1. If implemented, only manage lifecycle; do not perform web research in this milestone.

## Error contract

Use consistent JSON errors:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "...",
    "details": {}
  }
}
```

Important error codes:
- `VALIDATION_ERROR`
- `SNAPSHOT_NOT_FOUND`
- `MODEL_NOT_FOUND`
- `INSUFFICIENT_COVERAGE`
- `IMPORT_CONFLICT`
- `SCORE_REPRODUCTION_FAILED`
