"""Ordering, idempotency, object-level authorization, and the reservation guard."""

from __future__ import annotations

import asyncio

from tests.conftest import auth_header, login, make_user, make_vehicle


async def order(api, token, vehicle_id, key: str | None = None):
    headers = auth_header(token)
    if key:
        headers["Idempotency-Key"] = key
    return await api.post(
        "/api/v1/orders", json={"vehicle_id": vehicle_id, "kind": "purchase"}, headers=headers
    )


async def test_price_is_taken_from_the_vehicle_not_the_request(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db, agency_price=27_000_000)

    response = await api.post(
        "/api/v1/orders",
        json={
            "vehicle_id": vehicle["_id"],
            "kind": "purchase",
            # Every one of these is ignored — the schema does not accept them.
            "total": 1,
            "amount": 1,
            "price": 1,
            "currency": "USD",
            "discount": 99,
        },
        headers=auth_header(token),
    )
    assert response.status_code == 201
    assert response.json()["total"] == 27_000_000
    assert response.json()["currency"] == "RWF"


async def test_idempotency_key_replays_instead_of_double_ordering(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)

    first = await order(api, token, vehicle["_id"], key="client-key-1")
    second = await order(api, token, vehicle["_id"], key="client-key-1")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert await db["orders"].count_documents({}) == 1


async def test_reusing_a_key_with_a_different_body_is_rejected(api, db):
    user = await make_user(db)
    token = await login(api, user)
    first_vehicle = await make_vehicle(db)
    second_vehicle = await make_vehicle(db)

    await order(api, token, first_vehicle["_id"], key="shared-key")
    clash = await order(api, token, second_vehicle["_id"], key="shared-key")

    assert clash.status_code == 422
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_idempotency_keys_are_scoped_per_user(api, db):
    """One customer's key must not collide with another's and replay their order."""
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    alice_token = await login(api, alice)
    bob_token = await login(api, bob)
    first_vehicle = await make_vehicle(db)
    second_vehicle = await make_vehicle(db)

    a = await order(api, alice_token, first_vehicle["_id"], key="same-key")
    b = await order(api, bob_token, second_vehicle["_id"], key="same-key")

    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


async def test_second_customer_cannot_order_a_reserved_vehicle(api, db):
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    vehicle = await make_vehicle(db)

    first = await order(api, await login(api, alice), vehicle["_id"])
    assert first.status_code == 201

    second = await order(api, await login(api, bob), vehicle["_id"])
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "VEHICLE_UNAVAILABLE"
    assert await db["orders"].count_documents({}) == 1


async def test_concurrent_checkout_produces_exactly_one_order(api, db):
    """Ten simultaneous attempts on one vehicle.

    Under mongomock the unique partial index is not enforced, so what is being
    exercised here is the conditional reserve — the `status: AVAILABLE` guard on the
    update, which rolls the losing order back. The index is the second line of
    defence and is asserted against a real MongoDB in tests/integration.
    """
    vehicle = await make_vehicle(db)
    tokens = []
    for i in range(10):
        user = await make_user(db, email=f"racer{i}@example.com")
        tokens.append(await login(api, user))

    responses = await asyncio.gather(
        *[order(api, token, vehicle["_id"]) for token in tokens], return_exceptions=True
    )
    created = [r for r in responses if not isinstance(r, Exception) and r.status_code == 201]

    assert len(created) == 1, f"expected exactly one winner, got {len(created)}"
    assert await db["orders"].count_documents({}) == 1
    assert (await db["vehicles"].find_one({"_id": vehicle["_id"]}))["status"] == "RESERVED"


async def test_cancelling_releases_the_vehicle_for_someone_else(api, db):
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    vehicle = await make_vehicle(db)

    first = await order(api, await login(api, alice), vehicle["_id"])
    order_id = first.json()["id"]

    from app.modules.audit.service import AuditService
    from app.modules.orders.models import OrderStatus
    from app.modules.orders.service import OrderService

    service = OrderService(db, AuditService(db))
    await service.transition(order_id=order_id, target=OrderStatus.CANCELLED, actor_id=alice["_id"])

    assert (await db["vehicles"].find_one({"_id": vehicle["_id"]}))["status"] == "AVAILABLE"
    retry = await order(api, await login(api, bob), vehicle["_id"])
    assert retry.status_code == 201


async def test_customer_cannot_read_another_customers_order(api, db):
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    vehicle = await make_vehicle(db)

    created = await order(api, await login(api, alice), vehicle["_id"])
    order_id = created.json()["id"]

    stolen = await api.get(f"/api/v1/orders/{order_id}", headers=auth_header(await login(api, bob)))
    # 404 rather than 403: Bob learns nothing, not even that the id is real.
    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == "ORDER_NOT_FOUND"


async def test_customer_cannot_open_a_checkout_session_on_another_order(api, db):
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    vehicle = await make_vehicle(db)
    created = await order(api, await login(api, alice), vehicle["_id"])

    hijack = await api.post(
        f"/api/v1/orders/{created.json()['id']}/checkout-session",
        headers=auth_header(await login(api, bob)),
    )
    assert hijack.status_code == 404


async def test_order_list_is_scoped_and_paginated(api, db):
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    alice_token = await login(api, alice)

    for _ in range(3):
        vehicle = await make_vehicle(db)
        await order(api, alice_token, vehicle["_id"])

    mine = await api.get("/api/v1/orders?limit=2", headers=auth_header(alice_token))
    assert mine.status_code == 200
    assert len(mine.json()["items"]) == 2

    theirs = await api.get("/api/v1/orders", headers=auth_header(await login(api, bob)))
    assert theirs.json()["items"] == []


async def test_unavailable_vehicle_cannot_be_ordered(api, db):
    user = await make_user(db)
    token = await login(api, user)
    sold = await make_vehicle(db, status="SOLD")

    response = await order(api, token, sold["_id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VEHICLE_UNAVAILABLE"


async def test_vehicle_with_purchase_disabled_cannot_be_ordered(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db, purchase_enabled=False)

    response = await order(api, token, vehicle["_id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VEHICLE_NOT_PURCHASABLE"


async def test_internal_pricing_never_appears_in_an_order_response(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db, seller_expected_price=25_000_000)

    body = (await order(api, token, vehicle["_id"])).text
    assert "25000000" not in body
    assert "seller_expected_price" not in body
    assert "internal" not in body
    assert "Seller is motivated" not in body
    assert "commission" not in body.lower()
