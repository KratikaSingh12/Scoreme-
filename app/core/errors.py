"""All platform errors live here so every layer raises the same types.

API-facing errors carry an HTTP status code and a stable error code.
The API layer turns them into JSON responses in one place (app/main.py).
"""

from typing import Any


class PlatformError(Exception):
    """Base class for errors that should become a clean API response."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class InputValidationError(PlatformError):
    status_code = 422
    code = "invalid_input"


class Unauthorized(PlatformError):
    status_code = 401
    code = "unauthorized"


class WorkflowNotFound(PlatformError):
    status_code = 404
    code = "workflow_not_found"


class RequestNotFound(PlatformError):
    status_code = 404
    code = "request_not_found"


class IdempotencyConflict(PlatformError):
    """Same idempotency key was reused with a different payload."""

    status_code = 409
    code = "idempotency_conflict"


class InvalidStateTransition(PlatformError):
    status_code = 409
    code = "invalid_state_transition"


class ConcurrentModification(PlatformError):
    """Optimistic lock failed: someone else changed the request first."""

    status_code = 409
    code = "concurrent_modification"


class ConfigError(PlatformError):
    status_code = 400
    code = "invalid_config"


class PersistenceError(PlatformError):
    """A write to storage failed and was rolled back."""

    status_code = 503
    code = "persistence_failure"


class DuplicateRequestKey(Exception):
    """Raised by storage when the idempotency key already exists (race case)."""


# ---------------------------------------------------------------------------
# External dependency errors (not API errors, handled inside the executor)
# ---------------------------------------------------------------------------


class DependencyError(Exception):
    """Base for failures of an external service."""

    transient = False


class TransientDependencyError(DependencyError):
    """Worth retrying: timeouts, 503s, connection resets."""

    transient = True


class DependencyTimeout(TransientDependencyError):
    pass


class DependencyUnavailable(TransientDependencyError):
    pass


class PermanentDependencyError(DependencyError):
    """Not worth retrying: bad request, unknown entity, auth failure."""


class RetriesExhausted(Exception):
    def __init__(self, last_error: Exception, attempts: int):
        super().__init__(f"gave up after {attempts} attempts: {last_error}")
        self.last_error = last_error
        self.attempts = attempts
