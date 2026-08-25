"""Test fixtures.

These run against `mongomock_motor`, an in-memory Motor-compatible double. That
exercises every line of service and route logic, but it does NOT enforce unique
indexes, partial indexes, or transactions. The concurrency guarantees that depend
on those are asserted separately in tests/integration/test_index_contract.py, which
requires a real MongoDB and is skipped when one is not reachable. This limitation is
recorded in TESTING.md — it is the single biggest gap in this suite.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-hs256-usage")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test-webhook-secret")

from app.core.config import get_settings
from app.infrastructure.database import client as db_client
from app.infrastructure.database.client import Collections
from app.modules.users.models import Role, UserStatus
from app.modules.vehicles.models import VehicleStatus


@pytest.fixture(autouse=True)
def _settings_cache_reset():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Any]:
    mock = AsyncMongoMockClient()
    database = mock["voltaris_test"]
    db_client.database.client = mock
    db_client.database.db = database
    yield database
    db_client.database.client = None
    db_client.database.db = None


@pytest_asyncio.fixture
async def api(db) -> AsyncIterator[AsyncClient]:
    """App client with lifespan bypassed — the mock database is already installed."""
    from app.main import create_app

    app = create_app()
    _ = db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# --------------------------------------------------------------------- factories


async def make_user(
    db,
    *,
    email: str | None = None,
    password: str = "correct-horse-battery",
    roles: list[str] | None = None,
    status: str = UserStatus.ACTIVE.value,
) -> dict[str, Any]:
    from app.core.security import hash_password

    user_id = uuid.uuid4().hex
    document = {
        "_id": user_id,
        "name": "Test Person",
        "email": (email or f"user-{user_id[:8]}@example.com").lower(),
        "phone": "0788123456",
        "password_hash": hash_password(password),
        "roles": roles or [Role.BUYER.value],
        "status": status,
        "email_verified": True,
        "phone_verified": False,
        "mfa_enabled": False,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "deleted_at": None,
    }
    await db[Collections.USERS].insert_one(document)
    document["password"] = password
    return document


async def make_vehicle(
    db,
    *,
    agency_price: int = 27_000_000,
    seller_expected_price: int = 25_000_000,
    status: str = VehicleStatus.AVAILABLE.value,
    purchase_enabled: bool = True,
    commission_bps: int | None = 700,
) -> dict[str, Any]:
    vehicle_id = uuid.uuid4().hex
    document = {
        "_id": vehicle_id,
        "slug": f"byd-atto-3-2023-kigali-{vehicle_id[:6]}",
        "dealer_id": None,
        "owner_id": None,
        "make": "BYD",
        "make_slug": "byd",
        "model": "Atto 3",
        "variant": None,
        "year": 2023,
        "condition": "used",
        "body_type": "suv",
        "mileage_km": 18_400,
        "agency_price": agency_price,
        "currency": "RWF",
        "rental_price_per_day": None,
        "battery_kwh": 60.5,
        "range_km": 420,
        "power_kw": 150,
        "charging": {"ac_kw": 7, "dc_kw": 88, "port_type": "CCS2", "dc_10_80_minutes": 40},
        "seats": 5,
        "doors": 5,
        "drivetrain": "fwd",
        "location": {"city": "Kigali", "district": "Gasabo", "slug": "kigali"},
        "description": "A well-kept Atto 3.",
        "features": [],
        "images": [],
        "status": status,
        "verified": True,
        "purchase_enabled": purchase_enabled,
        "rental_enabled": False,
        "test_drive_available": True,
        "internal": {
            "seller_expected_price": seller_expected_price,
            "commission_bps": commission_bps,
            "internal_notes": "Seller is motivated, will take less.",
            "acquisition_cost": None,
        },
        "version": 0,
        "published_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "deleted_at": None,
    }
    await db[Collections.VEHICLES].insert_one(document)
    return document


async def login(api: AsyncClient, user: dict[str, Any]) -> str:
    """Signs in and returns the bearer token.

    The login response also sets httpOnly cookies, which httpx keeps in its jar. The
    tests use the bearer header so they exercise the API-client path; the cookie path
    is covered separately in tests/security/test_cookie_sessions.py.
    """
    response = await api.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": user["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def seeded(db, api):
    """Minimum data for the smoke checks: a purchasable vehicle and a dealer."""
    await make_vehicle(db, agency_price=21_000_000, seller_expected_price=19_500_000)
    await make_vehicle(db, agency_price=27_000_000, seller_expected_price=25_000_000)
    await db[Collections.DEALERS].insert_one(
        {
            "_id": uuid.uuid4().hex,
            "slug": "kigali-ev-motors",
            "name": "Kigali EV Motors",
            "status": "active",
            "verified": True,
            "city": "Kigali",
            "public_contact": False,
        }
    )
    yield db
