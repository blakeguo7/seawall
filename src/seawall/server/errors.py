"""Errors that become HTTP responses."""

from __future__ import annotations


class ApiError(Exception):
    """A request that cannot be served, with the status and machine-readable code to answer with."""

    def __init__(
        self, status: int, code: str, message: str, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def to_body(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")
