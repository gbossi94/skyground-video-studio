"""Domain errors shared by CLI, services and API (standard library only)."""

from __future__ import annotations


class StudioError(Exception):
    """Base class for every expected Skyground failure."""

    status_code = 400


class NotFound(StudioError):
    status_code = 404


class PermissionDenied(StudioError):
    status_code = 403


class Unauthorized(StudioError):
    status_code = 401


class Conflict(StudioError):
    """Raised when a write is based on a revision that is no longer current."""

    status_code = 409

    def __init__(
        self, message: str, *, current: dict | list | None = None, etag: str | None = None
    ):
        super().__init__(message)
        self.current = current
        self.etag = etag


class ValidationError(StudioError):
    status_code = 422


class ConfigurationError(StudioError):
    """Raised at startup when the environment is not deployable."""

    status_code = 500
