# services/

Deployable service entry points. `api/` holds the Dockerfile for the FastAPI
application; the application code itself lives in `packages/boro_gtm` so the
modular-monolith boundaries stay in one package (ADR-001).
