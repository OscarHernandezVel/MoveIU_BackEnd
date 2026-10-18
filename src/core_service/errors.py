"""Domain error hierarchy.

Errors carry a stable machine-readable ``code`` plus parameters. They never carry
user-facing text: the API layer localises them through the message catalog and maps
them to HTTP status codes, so the domain stays free of presentation concerns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class DomainError(Exception):
    """Base class of every expected, business-level error."""

    default_code = "internal_error"

    def __init__(
        self,
        code: str | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        details: Sequence[str] | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.params: dict[str, Any] = dict(params or {})
        self.details: list[str] = list(details or [])
        super().__init__(f"{self.code} {self.params}" if self.params else self.code)


class ValidationError(DomainError):
    default_code = "validation_error"


class NotFoundError(DomainError):
    default_code = "not_found"


class ConflictError(DomainError):
    default_code = "conflict"


class AuthenticationError(DomainError):
    default_code = "authentication_failed"


class AuthorizationError(DomainError):
    default_code = "forbidden"


class RateLimitedError(DomainError):
    default_code = "rate_limited"


class ServiceUnavailableError(DomainError):
    default_code = "service_unavailable"


class PrivacyViolationError(DomainError):
    """Raised when personal data is about to leave the trust boundary (e.g. towards an LLM)."""

    default_code = "privacy_violation"
