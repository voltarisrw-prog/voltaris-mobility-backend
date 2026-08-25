"""The money path, end to end, plus every way it can be attacked."""

from __future__ import annotations

import json

from app.core.security import sign_webhook
from tests.conftest import auth_header, login, make_user, make_vehicle

WEBHOOK_SECRET = "test-webhook-secret"


async def create_order(api, token, vehicle_id, **extra):
    return await api.post(
        "/api/v1/orders",
        json={"vehicle_id": vehicle_id, "kind": "purchase"},
        headers={**auth_header(token), **extra},
    )


def webhook_request(payload: dict, secret: str = WEBHOOK_SECRET):
    raw = json.dumps(payload).encode()
    return raw, {
        "X-Voltaris-Signature": sign_webhook(raw, secret),
        "Content-Type": "application/json",
    }


async def test_full_happy_path_order_to_settled_commission(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db, agency_price=27_000_000, seller_expected_price=25_000_000)

    order = await create_order(api, token, vehicle["_id"])
    assert order.status_code == 201
    order_body = order.json()
    # The price came from the vehicle, not from the request.
    assert order_body["total"] == 27_000_000
    assert order_body["status"] == "PENDING"

    # The vehicle is now held.
    assert (await db["vehicles"].find_one({"_id": vehicle["_id"]}))["status"] == "RESERVED"

    session = await api.post(
        f"/api/v1/orders/{order_body['id']}/checkout-session", headers=auth_header(token)
    )
    assert session.status_code == 201
    payment_id = session.json()["payment_id"]

    state = await api.get(f"/api/v1/orders/{order_body['id']}/payment", headers=auth_header(token))
    assert state.json()["state"] == "PENDING"

    raw, headers = webhook_request(
        {
            "id": "evt_success_1",
            "type": "payment.succeeded",
            "data": {"payment_id": payment_id, "amount": 27_000_000, "currency": "RWF"},
        }
    )
    hook = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert hook.status_code == 200, hook.text
    assert hook.json()["payment_status"] == "PAID"

    final = await api.get(f"/api/v1/orders/{order_body['id']}/payment", headers=auth_header(token))
    assert final.json()["state"] == "PAID"

    assert (await db["orders"].find_one({"_id": order_body["id"]}))["status"] == "PAID"
    assert (await db["vehicles"].find_one({"_id": vehicle["_id"]}))["status"] == "SOLD"

    commission = await db["commissions"].find_one({"order_id": order_body["id"]})
    assert commission["owner_settlement"] == 25_000_000
    assert commission["agency_commission"] == 2_000_000
    assert commission["owner_settlement"] + commission["agency_commission"] == 27_000_000


async def test_frontend_cannot_declare_a_payment_successful(api, db):
    """The whole architecture in one test: no client-reachable route sets PAID."""
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)
    order = (await create_order(api, token, vehicle["_id"])).json()

    for attempt in (
        api.post(
            f"/api/v1/orders/{order['id']}/payment",
            json={"status": "PAID"},
            headers=auth_header(token),
        ),
        api.patch(
            f"/api/v1/orders/{order['id']}", json={"status": "PAID"}, headers=auth_header(token)
        ),
        api.post(
            "/api/v1/payments",
            json={"order_id": order["id"], "status": "PAID"},
            headers=auth_header(token),
        ),
    ):
        response = await attempt
        assert response.status_code in (404, 405), response.text

    assert (await db["orders"].find_one({"_id": order["id"]}))["status"] == "PENDING"


async def test_unsigned_webhook_is_rejected(api, db):
    raw = json.dumps({"id": "evt_x", "type": "payment.succeeded", "data": {}}).encode()
    response = await api.post(
        "/api/v1/webhooks/payments", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_SIGNATURE_INVALID"


async def test_webhook_signed_with_the_wrong_secret_is_rejected_and_recorded(api, db):
    raw, headers = webhook_request(
        {"id": "evt_forged", "type": "payment.succeeded", "data": {}}, secret="attacker"
    )
    response = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert response.status_code == 401

    # Probing must be visible in the event log, not silently dropped.
    event = await db["payment_events"].find_one({"provider_event_id": "evt_forged"})
    assert event is not None
    assert event["signature_valid"] is False
    assert "signature" in event["outcome"]


async def test_duplicate_webhook_does_not_pay_twice(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)
    order = (await create_order(api, token, vehicle["_id"])).json()
    session = await api.post(
        f"/api/v1/orders/{order['id']}/checkout-session", headers=auth_header(token)
    )
    payment_id = session.json()["payment_id"]

    payload = {
        "id": "evt_dupe",
        "type": "payment.succeeded",
        "data": {"payment_id": payment_id, "amount": order["total"], "currency": "RWF"},
    }
    raw, headers = webhook_request(payload)

    first = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert first.json()["status"] == "applied"

    second = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    # 200 so the provider stops retrying, but nothing was applied a second time.
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    assert await db["commissions"].count_documents({"order_id": order["id"]}) == 1


async def test_webhook_with_a_tampered_amount_is_rejected(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db, agency_price=27_000_000, seller_expected_price=25_000_000)
    order = (await create_order(api, token, vehicle["_id"])).json()
    session = await api.post(
        f"/api/v1/orders/{order['id']}/checkout-session", headers=auth_header(token)
    )

    # Correctly signed, but claims the customer paid 100 RWF for a 27M vehicle.
    raw, headers = webhook_request(
        {
            "id": "evt_cheap",
            "type": "payment.succeeded",
            "data": {"payment_id": session.json()["payment_id"], "amount": 100, "currency": "RWF"},
        }
    )
    response = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_AMOUNT_MISMATCH"

    assert (await db["orders"].find_one({"_id": order["id"]}))["status"] != "PAID"
    assert await db["commissions"].count_documents({}) == 0


async def test_webhook_with_a_mismatched_currency_is_rejected(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)
    order = (await create_order(api, token, vehicle["_id"])).json()
    session = await api.post(
        f"/api/v1/orders/{order['id']}/checkout-session", headers=auth_header(token)
    )
    raw, headers = webhook_request(
        {
            "id": "evt_usd",
            "type": "payment.succeeded",
            "data": {
                "payment_id": session.json()["payment_id"],
                "amount": order["total"],
                "currency": "USD",
            },
        }
    )
    response = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_CURRENCY_MISMATCH"


async def test_webhook_for_an_unknown_payment_is_rejected(api, db):
    raw, headers = webhook_request(
        {
            "id": "evt_ghost",
            "type": "payment.succeeded",
            "data": {"payment_id": "does-not-exist", "amount": 1, "currency": "RWF"},
        }
    )
    response = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert response.status_code == 404


async def test_paid_payment_cannot_be_paid_again_by_a_new_event(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)
    order = (await create_order(api, token, vehicle["_id"])).json()
    session = await api.post(
        f"/api/v1/orders/{order['id']}/checkout-session", headers=auth_header(token)
    )
    payment_id = session.json()["payment_id"]

    base = {"payment_id": payment_id, "amount": order["total"], "currency": "RWF"}
    raw, headers = webhook_request({"id": "evt_a", "type": "payment.succeeded", "data": base})
    await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)

    # A different event id, so replay protection does not catch it — the state
    # machine has to. PAID -> PAID is not a legal transition.
    raw2, headers2 = webhook_request({"id": "evt_b", "type": "payment.succeeded", "data": base})
    second = await api.post("/api/v1/webhooks/payments", content=raw2, headers=headers2)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert await db["commissions"].count_documents({"order_id": order["id"]}) == 1


async def test_failed_payment_leaves_the_order_unpaid(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)
    order = (await create_order(api, token, vehicle["_id"])).json()
    session = await api.post(
        f"/api/v1/orders/{order['id']}/checkout-session", headers=auth_header(token)
    )
    raw, headers = webhook_request(
        {
            "id": "evt_fail",
            "type": "payment.failed",
            "data": {"payment_id": session.json()["payment_id"]},
        }
    )
    response = await api.post("/api/v1/webhooks/payments", content=raw, headers=headers)
    assert response.json()["payment_status"] == "FAILED"
    assert (await db["orders"].find_one({"_id": order["id"]}))["status"] == "PAYMENT_PENDING"
    assert await db["commissions"].count_documents({}) == 0
