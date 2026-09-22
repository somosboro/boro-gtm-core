"""Structured JSON logging with correlation ids."""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid

from pythonjsonlogger import json as jsonlogger

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class ContextFilter(logging.Filter):
    """Inject the ambient request/correlation id into every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = request_id_var.get()
        return True


def new_request_id() -> str:
    return uuid.uuid4().hex


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure root logging once, idempotently."""
    root = logging.getLogger()
    root.handlers.clear()

    # stderr, so a CLI command's JSON result on stdout stays machine-parseable.
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    if json_output:
        handler.setFormatter(
            jsonlogger.JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
                rename_fields={"asctime": "ts", "levelname": "level"},
            )
        )
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").propagate = False
