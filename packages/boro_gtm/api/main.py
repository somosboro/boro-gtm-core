"""FastAPI application factory.

OpenAPI is generated automatically by FastAPI from the route signatures and
response models.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from boro_gtm import __version__
from boro_gtm.core.config import get_settings
from boro_gtm.core.db import get_engine
from boro_gtm.core.errors import GtmError
from boro_gtm.core.logging import configure_logging, new_request_id, request_id_var

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Refuse to serve a database that does not match this build.

        Catching drift at boot turns an intermittent 500 on whichever endpoint
        happens to touch the missing column into one clear message, once.
        """
        if settings.schema_check_on_startup:
            from boro_gtm.core.schema_check import assert_schema_matches

            assert_schema_matches(get_engine())
        yield

    app = FastAPI(
        lifespan=lifespan,
        title="BoRo GTM Core",
        version=__version__,
        description=(
            "Evidence-driven market intelligence and commercial experimentation "
            "infrastructure. Implemented scope: M0 market-intelligence "
            "foundation and M1 contextual market intelligence. Scores always "
            "carry separate confidence and coverage, and missing evidence "
            "lowers coverage rather than being scored as zero. "
            "'BoRo GTM Core' is a working codename."
        ),
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
    )

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        token = request_id_var.set(
            request.headers.get("X-Request-ID") or new_request_id()
        )
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id_var.get() or ""
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(GtmError)
    async def domain_error_handler(_: Request, exc: GtmError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        """Unexpected failures still answer in the documented envelope.

        The detail is logged, never returned: a database error message can
        carry schema and query internals. The client gets a stable code it can
        branch on instead of the bare string ``Internal Server Error``.
        """
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "The request could not be completed.",
                    "details": {},
                }
            },
        )

    @app.get(f"{API_PREFIX}/health", tags=["health"])
    def health() -> dict[str, Any]:
        database = "ok"
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - depends on env
            logger.warning("Health check database probe failed: %s", exc)
            database = "unavailable"
        schema = "unknown"
        if database == "ok":
            from boro_gtm.core.schema_check import check_schema

            try:
                schema = "ok" if check_schema(get_engine()).ok else "drift"
            except Exception as exc:  # pragma: no cover - depends on env
                logger.warning("Schema probe failed: %s", exc)
        healthy = database == "ok" and schema == "ok"
        return {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "database": database,
            "schema": schema,
            "app": settings.app_name,
        }

    from boro_gtm.discovery.api.routes import router as discovery_router
    from boro_gtm.market_intelligence.api.routes import router as mi_router
    from boro_gtm.strategy.api.routes import router as strategy_router

    app.include_router(mi_router, prefix=API_PREFIX)
    app.include_router(strategy_router, prefix=API_PREFIX)
    app.include_router(discovery_router, prefix=API_PREFIX)
    return app


app = create_app()
