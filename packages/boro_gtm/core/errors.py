"""Domain error taxonomy and the JSON error contract from 05_API_SPEC.md."""

from __future__ import annotations

from typing import Any


class GtmError(Exception):
    """Base class for all domain errors.

    ``code`` is the stable machine-readable identifier returned to API clients;
    ``http_status`` is only consulted by the HTTP layer.
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationError(GtmError):
    code = "VALIDATION_ERROR"
    http_status = 422


class NotFoundError(GtmError):
    code = "NOT_FOUND"
    http_status = 404


class SnapshotNotFoundError(NotFoundError):
    code = "SNAPSHOT_NOT_FOUND"


class ModelNotFoundError(NotFoundError):
    code = "MODEL_NOT_FOUND"


class MarketNotFoundError(NotFoundError):
    code = "MARKET_NOT_FOUND"


class InsufficientCoverageError(GtmError):
    code = "INSUFFICIENT_COVERAGE"
    http_status = 409


class ImportConflictError(GtmError):
    code = "IMPORT_CONFLICT"
    http_status = 409


class ScoreReproductionFailedError(GtmError):
    code = "SCORE_REPRODUCTION_FAILED"
    http_status = 500


class CountryResolutionError(ValidationError):
    """Raised when a market name cannot be deterministically mapped to ISO codes."""

    code = "VALIDATION_ERROR"
