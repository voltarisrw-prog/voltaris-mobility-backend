"""Authentication: registration, login, refresh rotation, revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
    new_session_id,
    verify_password,
)
from app.infrastructure.database.client import Collections
from app.modules.audit.service import AuditService
from app.modules.auth.google import GoogleIdentity, IdentityProvider, new_nonce, new_state
from app.modules.users.models import Role, UserStatus


class AuthService:
    def __init__(self, db: AsyncIOMotorDatabase, audit: AuditService) -> None:
        self._db = db
        self._audit = audit
        self._settings = get_settings()

    # -- brute force -------------------------------------------------------

    async def _attempt_key(self, email: str, ip: str | None) -> str:
        # Keyed on both, so one attacker cannot lock out a victim by hammering their
        # address from elsewhere, and rotating IPs does not reset the counter.
        return f"{email.lower()}|{ip or 'unknown'}"

    async def _check_lockout(self, key: str) -> None:
        row = await self._db[Collections.LOGIN_ATTEMPTS].find_one({"key": key})
        if row and row["count"] >= self._settings.login_max_attempts:
            raise AppError(ErrorCode.ACCOUNT_LOCKED, detail=f"{row['count']} failed attempts")

    async def _register_failure(self, key: str) -> None:
        await self._db[Collections.LOGIN_ATTEMPTS].update_one(
            {"key": key},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "_id": uuid.uuid4().hex,
                    "expires_at": datetime.now(UTC)
                    + timedelta(seconds=self._settings.login_lockout_seconds),
                },
            },
            upsert=True,
        )

    async def _clear_failures(self, key: str) -> None:
        await self._db[Collections.LOGIN_ATTEMPTS].delete_one({"key": key})

    # -- registration ------------------------------------------------------

    async def register(
        self, *, name: str, email: str, phone: str | None, password: str, ip: str | None = None
    ) -> dict[str, Any]:
        if len(password) < self._settings.password_min_length:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                detail=f"password shorter than {self._settings.password_min_length}",
            )

        user_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        try:
            await self._db[Collections.USERS].insert_one(
                {
                    "_id": user_id,
                    "name": name,
                    "email": email.lower(),
                    "phone": phone,
                    "password_hash": hash_password(password),
                    # Roles are assigned by the server. A `roles` field in the request
                    # body is ignored, which closes the mass-assignment escalation.
                    "roles": [Role.BUYER.value],
                    "status": UserStatus.PENDING_VERIFICATION.value,
                    "email_verified": False,
                    "phone_verified": False,
                    "mfa_enabled": False,
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                }
            )
        except DuplicateKeyError as exc:
            raise AppError(ErrorCode.EMAIL_ALREADY_REGISTERED, detail=email) from exc

        await self._audit.record(
            action="user.registered",
            entity_type="user",
            entity_id=user_id,
            actor_id=user_id,
            after={"email": email.lower(), "roles": [Role.BUYER.value]},
            ip=ip,
        )
        return {"user_id": user_id, "verification_required": True}

    # -- login -------------------------------------------------------------

    async def login(
        self, *, email: str, password: str, otp: str | None = None, ip: str | None = None
    ) -> dict[str, Any]:
        key = await self._attempt_key(email, ip)
        await self._check_lockout(key)

        user = await self._db[Collections.USERS].find_one({"email": email.lower()})

        if user is None:
            # Hash anyway. Returning early on an unknown address makes the response
            # measurably faster and turns login into an account-enumeration oracle.
            hash_password(password)
            await self._register_failure(key)
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="no such user")

        if not user.get("password_hash"):
            # A provider-only account. Same generic error as a wrong password, so
            # this does not reveal which addresses are Google accounts.
            await self._register_failure(key)
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="no password set on this account")

        if not verify_password(password, user["password_hash"]):
            await self._register_failure(key)
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="bad password")

        if user["status"] == UserStatus.SUSPENDED.value:
            raise AppError(ErrorCode.FORBIDDEN, detail="account suspended")
        if user.get("deleted_at") is not None:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="deleted account")

        if user.get("mfa_enabled") and not otp:
            raise AppError(ErrorCode.MFA_REQUIRED, detail="otp required")

        await self._clear_failures(key)

        # Transparent upgrade if the Argon2 parameters have since been raised.
        if user.get("password_hash") and needs_rehash(user["password_hash"]):
            await self._db[Collections.USERS].update_one(
                {"_id": user["_id"]}, {"$set": {"password_hash": hash_password(password)}}
            )

        return await self._issue_session(user, ip=ip)

    async def _issue_session(self, user: dict[str, Any], *, ip: str | None) -> dict[str, Any]:
        session_id = new_session_id()
        now = datetime.now(UTC)
        access = create_token(
            subject=user["_id"], token_type="access", roles=user["roles"], session_id=session_id
        )
        refresh = create_token(
            subject=user["_id"], token_type="refresh", roles=user["roles"], session_id=session_id
        )
        claims = decode_token(refresh, expected_type="refresh")

        await self._db[Collections.SESSIONS].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "session_id": session_id,
                "user_id": user["_id"],
                # Only the current refresh jti is valid. Rotation burns the old one.
                "refresh_jti": claims.jti,
                "created_at": now,
                "last_used_at": now,
                "revoked_at": None,
                "ip": ip,
                "expires_at": now + timedelta(seconds=self._settings.refresh_token_ttl_seconds),
            }
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": self._settings.access_token_ttl_seconds,
            "user": {
                "id": user["_id"],
                "full_name": user.get("name") or "",
                "email": user["email"],
                "roles": user["roles"],
                "email_verified": user.get("email_verified", False),
                "mfa_enabled": user.get("mfa_enabled", False),
            },
        }

    async def refresh(self, *, refresh_token: str, ip: str | None = None) -> dict[str, Any]:
        claims = decode_token(refresh_token, expected_type="refresh")
        session = await self._db[Collections.SESSIONS].find_one({"session_id": claims.session_id})

        if session is None or session.get("revoked_at") is not None:
            raise AppError(ErrorCode.TOKEN_REVOKED, detail="session not active")

        if session["refresh_jti"] != claims.jti:
            # A superseded refresh token was presented. Either it leaked and is being
            # replayed, or a client is buggy. Kill the session either way — the whole
            # point of rotation is detecting exactly this.
            await self._db[Collections.SESSIONS].update_one(
                {"session_id": claims.session_id},
                {"$set": {"revoked_at": datetime.now(UTC), "revoked_reason": "reuse_detected"}},
            )
            await self._audit.record(
                action="auth.refresh_reuse_detected",
                entity_type="session",
                entity_id=claims.session_id,
                actor_id=claims.subject,
                ip=ip,
            )
            raise AppError(ErrorCode.TOKEN_REVOKED, detail="refresh token reuse")

        user = await self._db[Collections.USERS].find_one({"_id": claims.subject})
        if user is None or user.get("deleted_at") is not None:
            raise AppError(ErrorCode.TOKEN_REVOKED, detail="user gone")

        access = create_token(
            subject=user["_id"],
            token_type="access",
            # Re-read from the database: a role revoked mid-session must not survive
            # in the refreshed token just because it was in the old one.
            roles=user["roles"],
            session_id=claims.session_id,
        )
        new_refresh = create_token(
            subject=user["_id"],
            token_type="refresh",
            roles=user["roles"],
            session_id=claims.session_id,
        )
        new_claims = decode_token(new_refresh, expected_type="refresh")
        await self._db[Collections.SESSIONS].update_one(
            {"session_id": claims.session_id},
            {"$set": {"refresh_jti": new_claims.jti, "last_used_at": datetime.now(UTC)}},
        )
        return {
            "access_token": access,
            "refresh_token": new_refresh,
            "expires_in": self._settings.access_token_ttl_seconds,
        }

    async def logout(self, *, session_id: str, actor_id: str) -> None:
        await self._db[Collections.SESSIONS].update_one(
            {"session_id": session_id},
            {"$set": {"revoked_at": datetime.now(UTC), "revoked_reason": "logout"}},
        )
        await self._audit.record(
            action="auth.logout", entity_type="session", entity_id=session_id, actor_id=actor_id
        )

    async def session_is_active(self, session_id: str) -> bool:
        session = await self._db[Collections.SESSIONS].find_one({"session_id": session_id})
        return session is not None and session.get("revoked_at") is None

    # -- Google sign-in ----------------------------------------------------

    async def begin_google(self, provider: IdentityProvider) -> dict[str, Any]:
        """Start the flow. The state and nonce are stored server-side, not in a
        cookie the caller controls, and expire in ten minutes."""
        state = new_state()
        nonce = new_nonce()
        await self._db[Collections.OAUTH_STATES].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "state": state,
                "nonce": nonce,
                "provider": provider.name,
                "created_at": datetime.now(UTC),
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            }
        )
        return {"authorization_url": provider.authorization_url(state=state, nonce=nonce)}

    async def complete_google(
        self, provider: IdentityProvider, *, code: str, state: str, ip: str | None = None
    ) -> dict[str, Any]:
        # Consume the state atomically. find_one_and_delete means a replayed
        # callback finds nothing, so one authorization code is usable exactly once.
        record = await self._db[Collections.OAUTH_STATES].find_one_and_delete(
            {"state": state, "provider": provider.name}
        )
        if record is None:
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="unknown or reused oauth state")
        if record["expires_at"] < datetime.now(UTC).replace(tzinfo=record["expires_at"].tzinfo):
            raise AppError(ErrorCode.INVALID_CREDENTIALS, detail="oauth state expired")

        identity = await provider.exchange_code(code=code, nonce=record["nonce"])
        user = await self._link_or_create(identity, provider_name=provider.name, ip=ip)
        return await self._issue_session(user, ip=ip)

    async def _link_or_create(
        self, identity: GoogleIdentity, *, provider_name: str, ip: str | None
    ) -> dict[str, Any]:
        """One account per person.

        Resolution order matters. The provider subject is checked first because it is
        stable — a Google account can change its email address, and matching on email
        alone would strand the user with a second account.

        Falling back to email is what makes "sign up with a password, later sign in
        with Google" work. It is also an account-takeover vector if the provider has
        not verified the address, so unverified identities are refused outright rather
        than being allowed to create a parallel account.
        """
        by_subject = await self._db[Collections.USERS].find_one(
            {"identities.provider": provider_name, "identities.subject": identity.subject}
        )
        if by_subject is not None:
            return by_subject

        if not identity.email_verified:
            raise AppError(
                ErrorCode.EMAIL_NOT_VERIFIED,
                detail=f"{provider_name} reports {identity.email} as unverified",
            )

        existing = await self._db[Collections.USERS].find_one({"email": identity.email})
        if existing is not None:
            await self._db[Collections.USERS].update_one(
                {"_id": existing["_id"]},
                {
                    "$push": {
                        "identities": {"provider": provider_name, "subject": identity.subject}
                    },
                    # Google has verified it, so the local flag can be trusted now.
                    "$set": {
                        "email_verified": True,
                        "status": UserStatus.ACTIVE.value
                        if existing["status"] == UserStatus.PENDING_VERIFICATION.value
                        else existing["status"],
                        "updated_at": datetime.now(UTC),
                    },
                },
            )
            await self._audit.record(
                action="auth.identity_linked",
                entity_type="user",
                entity_id=existing["_id"],
                actor_id=existing["_id"],
                after={"provider": provider_name},
                ip=ip,
            )
            return await self._db[Collections.USERS].find_one({"_id": existing["_id"]})

        user_id = uuid.uuid4().hex
        now_ts = datetime.now(UTC)
        document = {
            "_id": user_id,
            "name": identity.name,
            "email": identity.email,
            "phone": None,
            # No password. Sign-in is via the provider until one is set, and
            # verify_password against None always fails.
            "password_hash": None,
            "roles": [Role.BUYER.value],
            "status": UserStatus.ACTIVE.value,
            "email_verified": True,
            "phone_verified": False,
            "mfa_enabled": False,
            "identities": [{"provider": provider_name, "subject": identity.subject}],
            "created_at": now_ts,
            "updated_at": now_ts,
            "deleted_at": None,
        }
        try:
            await self._db[Collections.USERS].insert_one(document)
        except DuplicateKeyError:
            # Lost a race with a concurrent first sign-in for the same address.
            found = await self._db[Collections.USERS].find_one({"email": identity.email})
            if found is None:
                raise
            return found

        await self._audit.record(
            action="user.registered",
            entity_type="user",
            entity_id=user_id,
            actor_id=user_id,
            after={"email": identity.email, "roles": [Role.BUYER.value], "via": provider_name},
            ip=ip,
        )
        return document
