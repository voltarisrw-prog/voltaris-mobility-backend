"""Centralised error taxonomy and the single response envelope.

Every failure the client can see is an ``AppError``. Anything else is a bug, gets
logged in full internally, and reaches the client as ``INTERNAL_ERROR`` with a
request id and nothing else. Stack traces, driver errors, and provider messages
never cross the boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Authentication and authorization
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    MFA_REQUIRED = "MFA_REQUIRED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    FORBIDDEN = "FORBIDDEN"

    # Resources
    USER_NOT_FOUND = "USER_NOT_FOUND"
    VEHICLE_NOT_FOUND = "VEHICLE_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"

    # Business rules
    INVALID_REQUEST = "INVALID_REQUEST"
    VEHICLE_UNAVAILABLE = "VEHICLE_UNAVAILABLE"
    VEHICLE_NOT_PURCHASABLE = "VEHICLE_NOT_PURCHASABLE"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"

    # Money
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_ALREADY_PROCESSED = "PAYMENT_ALREADY_PROCESSED"
    PAYMENT_AMOUNT_MISMATCH = "PAYMENT_AMOUNT_MISMATCH"
    PAYMENT_CURRENCY_MISMATCH = "PAYMENT_CURRENCY_MISMATCH"
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    WEBHOOK_REPLAY_DETECTED = "WEBHOOK_REPLAY_DETECTED"

    # Infrastructure and protocol
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: HTTP status per code. Kept in one table so a new code cannot ship without a
#: deliberate decision about what it means on the wire.
STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.TOKEN_INVALID: 401,
    ErrorCode.TOKEN_REVOKED: 401,
    ErrorCode.MFA_REQUIRED: 401,
    ErrorCode.ACCOUNT_LOCKED: 423,
    ErrorCode.EMAIL_NOT_VERIFIED: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.USER_NOT_FOUND: 404,
    ErrorCode.VEHICLE_NOT_FOUND: 404,
    ErrorCode.ORDER_NOT_FOUND: 404,
    ErrorCode.PAYMENT_NOT_FOUND: 404,
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.VEHICLE_UNAVAILABLE: 409,
    ErrorCode.VEHICLE_NOT_PURCHASABLE: 409,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.EMAIL_ALREADY_REGISTERED: 409,
    ErrorCode.PAYMENT_FAILED: 402,
    ErrorCode.PAYMENT_ALREADY_PROCESSED: 409,
    ErrorCode.PAYMENT_AMOUNT_MISMATCH: 409,
    ErrorCode.PAYMENT_CURRENCY_MISMATCH: 409,
    ErrorCode.WEBHOOK_SIGNATURE_INVALID: 401,
    ErrorCode.WEBHOOK_REPLAY_DETECTED: 409,
    ErrorCode.DUPLICATE_REQUEST: 409,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 422,
    ErrorCode.CONCURRENT_MODIFICATION: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
    ErrorCode.NOT_CONFIGURED: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: Client-facing copy. The `detail` a service raises is for logs, not for users —
#: it may name an internal id, a collection, or a provider reference.
MESSAGE_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.AUTHENTICATION_REQUIRED: "Sign in to continue.",
    ErrorCode.INVALID_CREDENTIALS: "That email and password combination did not work.",
    ErrorCode.TOKEN_EXPIRED: "Your session has expired. Sign in again.",
    ErrorCode.TOKEN_INVALID: "Your session is not valid. Sign in again.",
    ErrorCode.TOKEN_REVOKED: "Your session has been ended. Sign in again.",
    ErrorCode.MFA_REQUIRED: "Enter the code from your authenticator app.",
    ErrorCode.ACCOUNT_LOCKED: "Too many failed attempts. Try again later.",
    ErrorCode.EMAIL_NOT_VERIFIED: "Confirm your email address to continue.",
    ErrorCode.FORBIDDEN: "This account does not have access to that.",
    ErrorCode.USER_NOT_FOUND: "We could not find that account.",
    ErrorCode.VEHICLE_NOT_FOUND: "This vehicle is no longer listed.",
    ErrorCode.ORDER_NOT_FOUND: "We could not find that order.",
    ErrorCode.PAYMENT_NOT_FOUND: "We could not find that payment.",
    ErrorCode.INVALID_REQUEST: "Some details need fixing before this can be sent.",
    ErrorCode.VEHICLE_UNAVAILABLE: "Someone is already buying this vehicle.",
    ErrorCode.VEHICLE_NOT_PURCHASABLE: "This vehicle cannot be bought online.",
    ErrorCode.INVALID_STATE_TRANSITION: "That action is not possible from the current state.",
    ErrorCode.EMAIL_ALREADY_REGISTERED: "An account already exists for that email.",
    ErrorCode.PAYMENT_FAILED: "The payment did not go through.",
    ErrorCode.PAYMENT_ALREADY_PROCESSED: "This payment has already been settled.",
    ErrorCode.PAYMENT_AMOUNT_MISMATCH: "The payment amount does not match the order.",
    ErrorCode.PAYMENT_CURRENCY_MISMATCH: "The payment currency does not match the order.",
    ErrorCode.WEBHOOK_SIGNATURE_INVALID: "Signature verification failed.",
    ErrorCode.WEBHOOK_REPLAY_DETECTED: "This event has already been handled.",
    ErrorCode.DUPLICATE_REQUEST: "This request has already been made.",
    ErrorCode.IDEMPOTENCY_KEY_REUSED: "That idempotency key was used for a different request.",
    ErrorCode.CONCURRENT_MODIFICATION: (
        "This record changed while you were working. Reload and try again."
    ),
    ErrorCode.RATE_LIMITED: "Too many requests. Wait a moment and try again.",
    ErrorCode.PROVIDER_UNAVAILABLE: "A service we depend on is unavailable. Try again shortly.",
    ErrorCode.NOT_CONFIGURED: "This feature is not configured on this environment.",
    ErrorCode.INTERNAL_ERROR: "Something went wrong on our side.",
}


class AppError(Exception):
    """A failure the client is allowed to see."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = STATUS_BY_CODE[code]
        self.public_message = MESSAGE_BY_CODE[code]
        # Internal-only. Logged, never serialised into a response.
        self.detail = detail
        self.context = context or {}
        super().__init__(detail or code.value)


def error_body(code: ErrorCode, request_id: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": {
            "code": code.value,
            "message": MESSAGE_BY_CODE[code],
            "request_id": request_id,
        },
    }
