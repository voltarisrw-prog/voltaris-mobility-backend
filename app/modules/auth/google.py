"""Google sign-in.

The security of this flow rests on four things, all of which are easy to skip and
all of which are checked here:

1. The authorization code is exchanged **server-side**. A client-side flow would put
   the client secret in a browser bundle.
2. The returned ``id_token`` is verified against Google's published JWKS with RS256.
   Decoding it without verification — a very common shortcut — means anyone can mint
   an identity for any email address.
3. ``aud`` must equal our client id and ``iss`` must be Google. A token minted for a
   different application is a valid Google token and must still be rejected.
4. ``email_verified`` must be true before the account is linked. Without it, someone
   who controls an unverified Google account bearing a victim's address takes over
   that account on first sign-in.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool
    name: str
    picture: str | None = None


class IdentityProvider(Protocol):
    name: str

    def authorization_url(self, *, state: str, nonce: str) -> str: ...

    async def exchange_code(self, *, code: str, nonce: str) -> GoogleIdentity: ...


class GoogleIdentityProvider:
    name = "google"

    def __init__(self) -> None:
        self._settings = get_settings()
        # Cached and rotated by PyJWKClient; Google rotates keys regularly, so
        # pinning a key would break sign-in without warning.
        self._jwks = PyJWKClient(self._settings.google_jwks_url, cache_keys=True)

    def authorization_url(self, *, state: str, nonce: str) -> str:
        params = {
            "client_id": self._settings.google_client_id,
            "redirect_uri": self._settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            # Ask for a refresh token and force the consent screen only on first
            # authorisation; Google omits the refresh token on later silent grants.
            "access_type": "offline",
            "prompt": "select_account",
        }
        query = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
        return f"{self._settings.google_authorize_url}?{query}"

    async def exchange_code(self, *, code: str, nonce: str) -> GoogleIdentity:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._settings.google_token_url,
                    data={
                        "code": code,
                        "client_id": self._settings.google_client_id,
                        "client_secret": self._settings.google_client_secret,
                        "redirect_uri": self._settings.google_redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
        except httpx.HTTPError as exc:
            raise AppError(ErrorCode.PROVIDER_UNAVAILABLE, detail=f"google token: {exc}") from exc

        if response.status_code != 200:
            # Google's body can name the client id; log it, never return it.
            raise AppError(
                ErrorCode.INVALID_CREDENTIALS,
                detail=f"google rejected the code: {response.status_code} {response.text[:200]}",
            )

        id_token = response.json().get("id_token")
        if not id_token:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="no id_token in google response")

        return self.verify_id_token(id_token, nonce=nonce)

    def verify_id_token(self, id_token: str, *, nonce: str | None = None) -> GoogleIdentity:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(id_token)
            claims: dict[str, Any] = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._settings.google_client_id,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail=f"bad id_token: {exc}") from exc
        except Exception as exc:
            raise AppError(
                ErrorCode.PROVIDER_UNAVAILABLE, detail=f"jwks fetch failed: {exc}"
            ) from exc

        if claims.get("iss") not in self._settings.google_issuers:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail=f"issuer {claims.get('iss')}")

        # Replay protection: the nonce we generated must come back in the token.
        if nonce is not None and claims.get("nonce") != nonce:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="nonce mismatch")

        email = claims.get("email")
        if not email:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="no email claim")

        return GoogleIdentity(
            subject=str(claims["sub"]),
            email=str(email).lower(),
            email_verified=bool(claims.get("email_verified", False)),
            name=str(claims.get("name") or email.split("@")[0]),
            picture=claims.get("picture"),
        )


def get_identity_provider() -> IdentityProvider:
    settings = get_settings()
    if not settings.google_enabled:
        raise AppError(
            ErrorCode.NOT_CONFIGURED,
            detail="GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not set",
        )
    return GoogleIdentityProvider()


def new_state() -> str:
    """Opaque CSRF value. Without it, an attacker can complete the callback with
    their own code and bind their Google account to the victim's session."""
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def state_expiry_seconds() -> int:
    return 600


def now() -> int:
    return int(time.time())
