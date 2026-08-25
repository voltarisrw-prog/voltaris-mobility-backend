"""Google sign-in: account linking, and the takeover vectors it must not open."""

from __future__ import annotations

import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.audit.service import AuditService
from app.modules.auth.google import GoogleIdentity
from app.modules.auth.service import AuthService
from app.modules.users.models import Role, UserStatus
from tests.conftest import make_user


class StubGoogle:
    """Stands in for Google. Returns whatever identity the test needs, so the
    linking rules can be exercised without a network."""

    name = "google"

    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity

    def authorization_url(self, *, state: str, nonce: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}&nonce={nonce}"

    async def exchange_code(self, *, code: str, nonce: str) -> GoogleIdentity:
        _ = code, nonce
        return self.identity


def identity(email="amani@example.com", subject="google-sub-1", verified=True):
    return GoogleIdentity(subject=subject, email=email, email_verified=verified, name="Amani Test")


def service(db) -> AuthService:
    return AuthService(db, AuditService(db))


async def test_first_google_sign_in_creates_a_buyer(db):
    auth = service(db)
    provider = StubGoogle(identity())
    started = await auth.begin_google(provider)
    state = started["authorization_url"].split("state=")[1].split("&")[0]

    result = await auth.complete_google(provider, code="code", state=state)
    assert result["user"]["email"] == "amani@example.com"
    assert result["user"]["roles"] == [Role.BUYER.value]

    stored = await db["users"].find_one({"email": "amani@example.com"})
    assert stored["email_verified"] is True
    assert stored["status"] == UserStatus.ACTIVE.value
    # No password on a provider-only account.
    assert stored["password_hash"] is None


async def test_google_links_to_an_existing_password_account(db):
    """One account per person: signing in with Google must not create a second."""
    await make_user(db, email="amani@example.com")
    auth = service(db)
    provider = StubGoogle(identity())

    started = await auth.begin_google(provider)
    state = started["authorization_url"].split("state=")[1].split("&")[0]
    await auth.complete_google(provider, code="code", state=state)

    assert await db["users"].count_documents({"email": "amani@example.com"}) == 1
    stored = await db["users"].find_one({"email": "amani@example.com"})
    assert stored["identities"][0]["subject"] == "google-sub-1"
    # The original password still works — linking adds a route, it does not replace one.
    assert stored["password_hash"] is not None


async def test_unverified_google_email_cannot_take_over_an_account(db):
    """The critical one. Anyone can create a Google account claiming an address;
    only a verified one proves control of it."""
    await make_user(db, email="victim@example.com")
    auth = service(db)
    provider = StubGoogle(identity(email="victim@example.com", verified=False))

    started = await auth.begin_google(provider)
    state = started["authorization_url"].split("state=")[1].split("&")[0]

    with pytest.raises(AppError) as exc:
        await auth.complete_google(provider, code="code", state=state)
    assert exc.value.code is ErrorCode.EMAIL_NOT_VERIFIED

    stored = await db["users"].find_one({"email": "victim@example.com"})
    assert "identities" not in stored or not stored.get("identities")


async def test_returning_user_is_matched_by_subject_not_email(db):
    """A Google account can change its email. Matching on subject keeps the same
    Voltaris account instead of stranding the user with a duplicate."""
    auth = service(db)
    first = StubGoogle(identity(email="old@example.com", subject="stable-sub"))
    started = await auth.begin_google(first)
    state = started["authorization_url"].split("state=")[1].split("&")[0]
    created = await auth.complete_google(first, code="c", state=state)

    second = StubGoogle(identity(email="new@example.com", subject="stable-sub"))
    started2 = await auth.begin_google(second)
    state2 = started2["authorization_url"].split("state=")[1].split("&")[0]
    returning = await auth.complete_google(second, code="c", state=state2)

    assert returning["user"]["id"] == created["user"]["id"]
    assert await db["users"].count_documents({}) == 1


async def test_oauth_state_is_single_use(db):
    """Replaying the callback must fail — the state is consumed atomically."""
    auth = service(db)
    provider = StubGoogle(identity())
    started = await auth.begin_google(provider)
    state = started["authorization_url"].split("state=")[1].split("&")[0]

    await auth.complete_google(provider, code="code", state=state)
    with pytest.raises(AppError) as exc:
        await auth.complete_google(provider, code="code", state=state)
    assert exc.value.code is ErrorCode.INVALID_CREDENTIALS


async def test_unknown_state_is_rejected(db):
    auth = service(db)
    with pytest.raises(AppError):
        await auth.complete_google(StubGoogle(identity()), code="c", state="attacker-chosen")


async def test_password_login_fails_cleanly_on_a_google_only_account(api, db):
    auth = service(db)
    provider = StubGoogle(identity())
    started = await auth.begin_google(provider)
    state = started["authorization_url"].split("state=")[1].split("&")[0]
    await auth.complete_google(provider, code="c", state=state)

    response = await api.post(
        "/api/v1/auth/login",
        json={"email": "amani@example.com", "password": "guessing-a-password"},
    )
    # Same generic error as any wrong password: does not reveal which addresses
    # are Google-only accounts.
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
