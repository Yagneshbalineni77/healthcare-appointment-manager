"""Domain exceptions.

Business rules raise these; a single handler in ``app.main`` turns them into
JSON with a stable machine-readable ``code``. Routers stay free of HTTP noise
and the frontend can branch on ``code`` instead of parsing prose.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class. ``status_code``/``code`` drive the HTTP response."""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None, **extra):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.extra = extra


class SlotUnavailable(DomainError):
    """The requested slot is already held or confirmed by someone else."""

    status_code = 409
    code = "SLOT_TAKEN"


class SlotNotBookable(DomainError):
    """Outside working hours, on a leave day, in the past, or off the slot grid."""

    status_code = 422
    code = "SLOT_NOT_BOOKABLE"


class HoldExpired(DomainError):
    status_code = 410
    code = "HOLD_EXPIRED"


class PatientDoubleBooking(DomainError):
    status_code = 409
    code = "PATIENT_BUSY"


class InvalidState(DomainError):
    status_code = 409
    code = "INVALID_STATE"


class NotFound(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class PermissionDenied(DomainError):
    status_code = 403
    code = "FORBIDDEN"


class Conflict(DomainError):
    status_code = 409
    code = "CONFLICT"


class IntegrationDisabled(DomainError):
    status_code = 503
    code = "INTEGRATION_DISABLED"
