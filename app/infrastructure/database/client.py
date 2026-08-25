"""Mongo connection lifecycle and the collection registry."""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import get_settings


class Database:
    """Holds the client for the process. One pool, created at startup."""

    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


database = Database()


async def connect() -> AsyncIOMotorDatabase:
    settings = get_settings()
    database.client = AsyncIOMotorClient(
        settings.mongodb_uri,
        maxPoolSize=settings.mongodb_max_pool_size,
        minPoolSize=settings.mongodb_min_pool_size,
        serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
        connectTimeoutMS=settings.mongodb_timeout_ms,
        # Reads must never silently return stale data after a failover for money paths.
        retryWrites=True,
        tz_aware=True,
    )
    database.db = database.client[settings.mongodb_database]
    return database.db


async def disconnect() -> None:
    if database.client is not None:
        database.client.close()
        database.client = None
        database.db = None


def get_db() -> AsyncIOMotorDatabase:
    if database.db is None:
        raise RuntimeError("Database is not connected. Call connect() during startup.")
    return database.db


class Collections:
    USERS = "users"
    SESSIONS = "sessions"
    LOGIN_ATTEMPTS = "login_attempts"
    OAUTH_STATES = "oauth_states"
    VEHICLES = "vehicles"
    DEALERS = "dealers"
    INQUIRIES = "inquiries"
    TEST_DRIVES = "test_drives"
    SAVED_VEHICLES = "saved_vehicles"
    SAVED_SEARCHES = "saved_searches"
    NOTIFICATIONS = "notifications"
    SELLER_LISTINGS = "seller_listings"
    MEDIA = "media"
    ARTICLES = "articles"
    CHARGING_LOCATIONS = "charging_locations"
    ORDERS = "orders"
    PAYMENTS = "payments"
    PAYMENT_EVENTS = "payment_events"
    COMMISSIONS = "commissions"
    IDEMPOTENCY = "idempotency_keys"
    AUDIT_LOGS = "audit_logs"


async def ping(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    return await db.command("ping")
