"""Stable error semantics.

Clients branch on `error.code`, never on prose or on the HTTP status alone. The
codes are part of the published contract: adding one is a MINOR change, changing
what an existing one means is MAJOR.
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException, status


class ErrorCode(str, Enum):
    invalid_request = "invalid_request"
    unauthenticated = "unauthenticated"
    unauthorized = "unauthorized"
    not_found = "not_found"
    conflict = "conflict"
    upgrade_required = "upgrade_required"


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        super().__init__(
            status_code=status_code,
            detail={"error": {"code": code.value, "message": message}},
        )


def unauthenticated(message: str = "A valid bearer token is required.") -> ApiError:
    return ApiError(status.HTTP_401_UNAUTHORIZED, ErrorCode.unauthenticated, message)


def unauthorized(message: str = "The caller is not a member of this tenant.") -> ApiError:
    """Deliberately identical whether the tenant is absent or merely forbidden.

    Distinguishing them would let any authenticated caller enumerate tenant ids.
    """
    return ApiError(status.HTTP_403_FORBIDDEN, ErrorCode.unauthorized, message)


def not_found(message: str = "No such resource.") -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, ErrorCode.not_found, message)


def conflict(message: str) -> ApiError:
    """Idempotency and optimistic-concurrency failures, and illegal transitions.

    All three are the same thing from the client's side: the state it believed it
    was acting on is not the state that exists. Re-read and decide.
    """
    return ApiError(status.HTTP_409_CONFLICT, ErrorCode.conflict, message)


def invalid_request(message: str) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, ErrorCode.invalid_request, message)


def upgrade_required(message: str) -> ApiError:
    return ApiError(status.HTTP_426_UPGRADE_REQUIRED, ErrorCode.upgrade_required, message)
