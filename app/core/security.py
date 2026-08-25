"""Password hashing, token issue/verify, and webhook signature checking."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

# Argon2id with parameters at the OWASP recommendation for a server-side hash.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4, hash_len=32)

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except Exception:
        # Any failure — wrong password, malformed hash, unsupported variant — is a
        # failed verification. Never distinguish, and never raise to the caller.
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return True


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    token_type: TokenType
    roles: tuple[str, ...]
    session_id: str
    jti: str
    expires_at: int


def create_token(
    *,
    subject: str,
    token_type: TokenType,
    roles: list[str],
    session_id: str,
    ttl_seconds: int | None = None,
) -> str:
    settings = get_settings()
    ttl = ttl_seconds or (
        settings.access_token_ttl_seconds
        if token_type == "access"
        else settings.refresh_token_ttl_seconds
    )
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "roles": roles,
        "sid": session_id,
        # A unique id per token, so a single refresh token can be burned on rotation
        # without ending the whole session.
        "jti": uuid.uuid4().hex,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "iss": settings.service_name,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, expected_type: TokenType) -> TokenClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            # Pinned: without this an attacker can present `alg: none` or downgrade
            # an RS256 deployment to HS256 signed with the public key.
            algorithms=[settings.jwt_algorithm],
            issuer=settings.service_name,
            options={"require": ["exp", "iat", "sub", "typ", "sid", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError(ErrorCode.TOKEN_EXPIRED, detail=str(exc)) from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(ErrorCode.TOKEN_INVALID, detail=str(exc)) from exc

    if payload.get("typ") != expected_type:
        # A refresh token presented as an access token is an attack, not a mistake.
        raise AppError(
            ErrorCode.TOKEN_INVALID,
            detail=f"expected {expected_type} token, got {payload.get('typ')}",
        )

    return TokenClaims(
        subject=str(payload["sub"]),
        token_type=expected_type,
        roles=tuple(payload.get("roles", [])),
        session_id=str(payload["sid"]),
        jti=str(payload["jti"]),
        expires_at=int(payload["exp"]),
    )


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def new_opaque_token() -> tuple[str, str]:
    """Return (plaintext, sha256) for reset and verification links.

    Only the digest is stored. A database leak then yields nothing usable, and the
    lookup is still a single indexed equality match.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_webhook_signature(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int,
    now: int | None = None,
) -> None:
    """Verify a `t=<unix>,v1=<hex>` signature over `<t>.<body>`.

    The timestamp is inside the signed material, so an attacker cannot replay an old
    body with a fresh timestamp. Comparison is constant-time.
    """
    parts = dict(piece.split("=", 1) for piece in signature_header.split(",") if "=" in piece)
    timestamp = parts.get("t")
    provided = parts.get("v1")
    if not timestamp or not provided:
        raise AppError(ErrorCode.WEBHOOK_SIGNATURE_INVALID, detail="malformed signature header")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise AppError(ErrorCode.WEBHOOK_SIGNATURE_INVALID, detail="bad timestamp") from exc

    current = now if now is not None else int(time.time())
    if abs(current - sent_at) > tolerance_seconds:
        raise AppError(
            ErrorCode.WEBHOOK_SIGNATURE_INVALID,
            detail=f"timestamp outside tolerance ({current - sent_at}s)",
        )

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise AppError(ErrorCode.WEBHOOK_SIGNATURE_INVALID, detail="digest mismatch")


def sign_webhook(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Test and provider-simulator helper — produces the header format above."""
    ts = timestamp if timestamp is not None else int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"
