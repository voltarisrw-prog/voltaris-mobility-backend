"""Guarantees that only a real MongoDB can prove.

The mocked suite exercises logic; it cannot enforce unique or partial indexes, and
it has no transactions. Everything that depends on the *database* rather than on our
code is asserted here, and skipped when no MongoDB is reachable.

Run with:  MONGODB_TEST_URI=mongodb://localhost:27017 pytest tests/integration
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio

MONGODB_TEST_URI = os.getenv("MONGODB_TEST_URI")

pytestmark = pytest.mark.skipif(
    not MONGODB_TEST_URI,
    reason="needs a real MongoDB; set MONGODB_TEST_URI to run",
)


@pytest_asyncio.fixture
async def real_db():
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.infrastructure.database.indexes import ensure_indexes

    client = AsyncIOMotorClient(MONGODB_TEST_URI, serverSelectionTimeoutMS=2_000)
    name = f"voltaris_it_{uuid.uuid4().hex[:8]}"
    db = client[name]
    await ensure_indexes(db)
    yield db
    await client.drop_database(name)
    client.close()


async def test_unique_email_index_is_enforced(real_db):
    from pymongo.errors import DuplicateKeyError

    base = {"email": "clash@example.com", "roles": ["BUYER"], "created_at": datetime.now(UTC)}
    await real_db["users"].insert_one({"_id": uuid.uuid4().hex, **base})
    with pytest.raises(DuplicateKeyError):
        await real_db["users"].insert_one({"_id": uuid.uuid4().hex, **base})


async def test_partial_index_allows_one_active_order_per_vehicle(real_db):
    from pymongo.errors import DuplicateKeyError

    vehicle_id = uuid.uuid4().hex

    def order(status: str) -> dict:
        return {
            "_id": uuid.uuid4().hex,
            "reference": uuid.uuid4().hex[:10],
            "vehicle_id": vehicle_id,
            "status": status,
            "created_at": datetime.now(UTC),
        }

    await real_db["orders"].insert_one(order("PENDING"))

    # A second active order on the same vehicle must be impossible.
    with pytest.raises(DuplicateKeyError):
        await real_db["orders"].insert_one(order("PAYMENT_PENDING"))

    # But a cancelled order does not hold the vehicle, so a fresh one is allowed.
    await real_db["orders"].update_one(
        {"vehicle_id": vehicle_id}, {"$set": {"status": "CANCELLED"}}
    )
    await real_db["orders"].insert_one(order("PENDING"))


async def test_concurrent_orders_leave_exactly_one_winner(real_db):
    """The guarantee the mocked suite cannot make."""
    from pymongo.errors import DuplicateKeyError

    vehicle_id = uuid.uuid4().hex

    async def attempt() -> bool:
        try:
            await real_db["orders"].insert_one(
                {
                    "_id": uuid.uuid4().hex,
                    "reference": uuid.uuid4().hex[:10],
                    "vehicle_id": vehicle_id,
                    "status": "PENDING",
                    "created_at": datetime.now(UTC),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    results = await asyncio.gather(*[attempt() for _ in range(25)])
    assert sum(results) == 1


async def test_duplicate_provider_event_is_impossible(real_db):
    from pymongo.errors import DuplicateKeyError

    event = {
        "provider": "stub",
        "provider_event_id": "evt_race",
        "signature_valid": True,
        "received_at": datetime.now(UTC),
    }
    await real_db["payment_events"].insert_one({"_id": uuid.uuid4().hex, **event})
    with pytest.raises(DuplicateKeyError):
        await real_db["payment_events"].insert_one({"_id": uuid.uuid4().hex, **event})


async def test_one_commission_row_per_order(real_db):
    from pymongo.errors import DuplicateKeyError

    order_id = uuid.uuid4().hex
    row = {"order_id": order_id, "gross_sale": 1, "created_at": datetime.now(UTC)}
    await real_db["commissions"].insert_one({"_id": uuid.uuid4().hex, **row})
    with pytest.raises(DuplicateKeyError):
        await real_db["commissions"].insert_one({"_id": uuid.uuid4().hex, **row})


async def test_marketplace_query_uses_an_index_and_does_not_scan(real_db):
    """Explain plan assertion: the browse query must never be a collection scan."""
    for i in range(200):
        await real_db["vehicles"].insert_one(
            {
                "_id": uuid.uuid4().hex,
                "slug": f"v-{i}",
                "status": "AVAILABLE",
                "make_slug": "byd" if i % 2 else "nissan",
                "body_type": "suv",
                "location": {"slug": "kigali"},
                "agency_price": 20_000_000 + i,
                "published_at": datetime.now(UTC),
            }
        )

    plan = await real_db.command(
        "explain",
        {
            "find": "vehicles",
            "filter": {"status": "AVAILABLE", "make_slug": "byd", "body_type": "suv"},
            "sort": {"published_at": -1},
            "limit": 24,
        },
        verbosity="executionStats",
    )
    stage = plan["queryPlanner"]["winningPlan"]
    assert "COLLSCAN" not in str(stage), f"browse query is scanning: {stage}"
