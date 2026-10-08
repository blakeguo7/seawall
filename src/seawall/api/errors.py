"""API error types for Seawall."""

from __future__ import annotations


class SeawallApiError(RuntimeError):
    """Base class for upstream API failures."""


class AuthenticationFailure(SeawallApiError):
    """Raised when the upstream service rejects the provided credentials."""


class RateLimitFailure(SeawallApiError):
    """Raised when the upstream service rejects the request due to rate limits."""


class RequestFailure(SeawallApiError):
    """Raised for generic request or transport failures."""
