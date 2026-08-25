"""Smoke test: does a running Voltaris API actually work?

    python scripts/smoke.py http://localhost:8000

Exercises the real HTTP surface end to end — health, the public marketplace, an
anonymous enquiry, registration and login, the account area, the order and payment
path, and the security boundaries that must hold. Prints a pass/fail line per check
and exits non-zero if any fail, so it also works as a deploy gate.

The checks live here rather than in the test suite so they can be pointed at
staging or production. `tests/api/test_smoke_checks.py` runs this exact list
against the app in-process, so the script itself is covered by CI.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

Check = Callable[[httpx.AsyncClient], Awaitable[str]]

#: Ordered, and the order matters: the auth check signs in and leaves the bearer
#: token on the shared client, which every account, order, and payment check after
#: it depends on. Registration order is execution order.
CHECKS: list[tuple[str, Check]] = []


def check(name: str) -> Callable[[Check], Check]:
    def register(fn: Check) -> Check:
        CHECKS.append((name, fn))
        return fn

    return register


API = "/api/v1"


# -- liveness -----------------------------------------------------------------


@check("health: process is up")
async def _healthz(client: httpx.AsyncClient) -> str:
    body = (await client.get("/healthz")).raise_for_status().json()
    assert body["status"] == "ok", body
    return body["service"]


@check("health: database is reachable")
async def _readyz(client: httpx.AsyncClient) -> str:
    body = (await client.get("/readyz")).json()
    # `degraded` is a legitimate response, not an exception — but it means the
    # database is not connected, so the rest of this run would be meaningless.
    assert body["status"] == "ready", f"NOT READY: {body}"
    return f"indexes {body.get('indexes')}"


# -- public marketplace -------------------------------------------------------


@check("marketplace: vehicles list")
async def _vehicles(client: httpx.AsyncClient) -> str:
    body = (await client.get(f"{API}/vehicles")).raise_for_status().json()
    assert {"items", "page", "total", "total_pages"} <= set(body), body
    if body["total"] == 0:
        return "0 vehicles — run scripts/seed.py"
    return f"{body['total']} vehicles"


@check("marketplace: internal pricing is not exposed")
async def _no_leak(client: httpx.AsyncClient) -> str:
    text = (await client.get(f"{API}/vehicles")).text
    for secret in ("seller_expected_price", "internal_notes", "commission_bps"):
        assert secret not in text, f"LEAKED {secret}"
    return "clean"


@check("marketplace: filters and sorting")
async def _filters(client: httpx.AsyncClient) -> str:
    cheap = (await client.get(f"{API}/vehicles?maxPrice=25000000&sort=price_asc")).json()
    prices = [i["price"] for i in cheap["items"] if i["price"]]
    assert prices == sorted(prices), "price_asc did not sort"
    assert all(p <= 25_000_000 for p in prices), "maxPrice not applied"
    return f"{len(prices)} under 25M, correctly ordered"


@check("marketplace: facets")
async def _facets(client: httpx.AsyncClient) -> str:
    body = (await client.get(f"{API}/vehicles/facets")).raise_for_status().json()
    assert {"makes", "bodies", "locations", "price", "range"} <= set(body)
    return f"{len(body['makes'])} makes"


@check("marketplace: vehicle detail by slug")
async def _detail(client: httpx.AsyncClient) -> str:
    listing = (await client.get(f"{API}/vehicles")).json()
    if not listing["items"]:
        return "skipped — no vehicles"
    slug = listing["items"][0]["slug"]
    body = (await client.get(f"{API}/vehicles/by-slug/{slug}")).raise_for_status().json()
    assert {"seller", "charging", "features", "faqs"} <= set(body)
    return slug


@check("marketplace: unknown slug returns the error envelope")
async def _not_found(client: httpx.AsyncClient) -> str:
    response = await client.get(f"{API}/vehicles/by-slug/definitely-not-a-real-slug")
    assert response.status_code == 404, response.status_code
    body = response.json()
    assert body["success"] is False and body["error"]["code"] == "VEHICLE_NOT_FOUND", body
    assert body["error"]["request_id"], "no request id"
    return body["error"]["request_id"]


@check("catalog: dealers, content, charging")
async def _catalog(client: httpx.AsyncClient) -> str:
    dealers = (await client.get(f"{API}/dealers")).raise_for_status().json()
    guides = (await client.get(f"{API}/content/articles?kind=guide")).raise_for_status().json()
    posts = (await client.get(f"{API}/content/articles?kind=blog")).raise_for_status().json()
    charging = (await client.get(f"{API}/charging/locations")).raise_for_status().json()
    return (
        f"{dealers['total']} dealers, {guides['total']} guides, "
        f"{posts['total']} posts, {charging['total']} chargers"
    )


# -- leads --------------------------------------------------------------------


@check("leads: anonymous enquiry is accepted")
async def _inquiry(client: httpx.AsyncClient) -> str:
    body = (
        (
            await client.post(
                f"{API}/inquiries",
                json={
                    "full_name": "Smoke Test",
                    "email": f"smoke-{uuid.uuid4().hex[:8]}@example.com",
                    "phone": "0788123456",
                    "message": "This is an automated smoke test enquiry.",
                    "topic": "buying",
                    "source": "smoke-test",
                },
            )
        )
        .raise_for_status()
        .json()
    )
    assert body["status"] == "received"
    return body["reference"]


@check("leads: honeypot submission is silently dropped")
async def _honeypot(client: httpx.AsyncClient) -> str:
    response = await client.post(
        f"{API}/inquiries",
        json={
            "full_name": "Bot",
            "email": f"bot-{uuid.uuid4().hex[:8]}@example.com",
            "phone": "0788123456",
            "message": "buy cheap watches online now",
            "company_website": "http://spam.example",
        },
    )
    # Looks like a success on purpose — probing must teach an operator nothing.
    assert response.status_code == 201, response.status_code
    return "accepted and discarded"


# -- authentication -----------------------------------------------------------


@check("auth: register, login, session")
async def _auth(client: httpx.AsyncClient) -> str:
    email = f"smoke-{uuid.uuid4().hex[:10]}@example.com"
    password = "smoke-test-password-1234"

    created = await client.post(
        f"{API}/auth/register",
        json={
            "full_name": "Smoke Test",
            "email": email,
            "phone": "0788123456",
            "password": password,
        },
    )
    assert created.status_code == 201, created.text

    login = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    tokens = login.json()
    assert tokens["access_token"] and tokens["csrf_token"], tokens

    session = await client.get(
        f"{API}/auth/session", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert session.status_code == 200, session.text
    user = session.json()["user"]
    # Registration must always yield BUYER, whatever the request body said.
    assert user["roles"] == ["BUYER"], user["roles"]
    client.headers["Authorization"] = f"Bearer {tokens['access_token']}"
    return f"{user['email']} as {user['roles'][0]}"


@check("auth: registration cannot self-assign a role")
async def _no_escalation(client: httpx.AsyncClient) -> str:
    email = f"sneaky-{uuid.uuid4().hex[:10]}@example.com"
    await client.post(
        f"{API}/auth/register",
        json={
            "full_name": "Sneaky",
            "email": email,
            "password": "smoke-test-password-1234",
            "roles": ["SUPER_ADMIN"],
            "email_verified": True,
        },
    )
    login = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "smoke-test-password-1234"}
    )
    roles = login.json()["user"]["roles"]
    assert roles == ["BUYER"], f"ESCALATION: got {roles}"
    return "roles ignored, as they must be"


@check("auth: unknown email and wrong password are indistinguishable")
async def _no_enumeration(client: httpx.AsyncClient) -> str:
    a = await client.post(
        f"{API}/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-here"}
    )
    b = await client.post(
        f"{API}/auth/login", json={"email": "root@voltaris.rw", "password": "wrong-password-here"}
    )
    assert a.status_code == b.status_code == 401, (a.status_code, b.status_code)
    assert a.json()["error"]["message"] == b.json()["error"]["message"]
    return "identical responses"


# -- account ------------------------------------------------------------------


@check("account: profile and saved vehicles")
async def _account(client: httpx.AsyncClient) -> str:
    profile = (await client.get(f"{API}/me")).raise_for_status().json()
    assert {"id", "full_name", "email", "phone"} <= set(profile)

    listing = (await client.get(f"{API}/vehicles")).json()
    if not listing["items"]:
        return "profile ok, no vehicles to save"

    vehicle_id = listing["items"][0]["id"]
    # Saving twice must be idempotent.
    for _ in range(2):
        saved = await client.put(f"{API}/me/saved-vehicles/{vehicle_id}")
        assert saved.status_code == 204, saved.status_code

    body = (await client.get(f"{API}/me/saved-vehicles")).json()
    assert body["total"] == 1, f"expected 1 save, got {body['total']}"
    await client.delete(f"{API}/me/saved-vehicles/{vehicle_id}")
    return "profile, save, idempotent re-save, unsave"


# -- money --------------------------------------------------------------------


@check("orders: price comes from the vehicle, not the request")
async def _order_pricing(client: httpx.AsyncClient) -> str:
    listing = (await client.get(f"{API}/vehicles")).json()
    purchasable = [i for i in listing["items"] if i["price"] and i["status"] == "available"]
    if not purchasable:
        return "skipped — nothing purchasable"

    vehicle = purchasable[0]
    order = await client.post(
        f"{API}/orders",
        json={"vehicle_id": vehicle["id"], "kind": "purchase", "total": 1, "price": 1},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    if order.status_code == 409:
        return "skipped — vehicle already has an active order"
    assert order.status_code == 201, order.text
    body = order.json()
    assert body["total"] == vehicle["price"], f"PRICE TAMPERED: {body['total']}"
    return f"{body['reference']} at {body['total']} {body['currency']}"


@check("payments: no client route can mark an order paid")
async def _no_fake_payment(client: httpx.AsyncClient) -> str:
    orders = (await client.get(f"{API}/orders")).json()
    if not orders["items"]:
        return "skipped — no orders"
    order_id = orders["items"][0]["id"]

    for method, path, payload in (
        ("post", f"{API}/orders/{order_id}/payment", {"status": "PAID"}),
        ("patch", f"{API}/orders/{order_id}", {"status": "PAID"}),
        ("post", f"{API}/payments", {"order_id": order_id, "status": "PAID"}),
    ):
        response = await getattr(client, method)(path, json=payload)
        assert response.status_code in (404, 405), f"{path} answered {response.status_code}"

    state = (await client.get(f"{API}/orders/{order_id}")).json()
    assert state["status"] != "PAID", "ORDER MARKED PAID BY A CLIENT"
    return "all three attempts rejected"


@check("payments: unsigned webhook is rejected")
async def _webhook(client: httpx.AsyncClient) -> str:
    response = await client.post(
        f"{API}/webhooks/payments",
        json={"id": "evt_smoke", "type": "payment.succeeded", "data": {"payment_id": "x"}},
    )
    assert response.status_code == 401, response.status_code
    assert response.json()["error"]["code"] == "WEBHOOK_SIGNATURE_INVALID"
    return "signature required"


# -- boundaries ---------------------------------------------------------------


@check("security: a buyer cannot reach the admin console")
async def _console_closed(client: httpx.AsyncClient) -> str:
    response = await client.get(f"{API}/console/overview")
    assert response.status_code == 403, response.status_code
    return "403 as expected"


@check("security: account routes reject an anonymous caller")
async def _account_closed(client: httpx.AsyncClient) -> str:
    # Strip the credentials from this one request rather than building a second
    # client: a new client would discard the transport it was given, which breaks
    # any in-process run and quietly changes what is under test.
    saved_header = client.headers.pop("Authorization", None)
    saved_cookies = dict(client.cookies)
    client.cookies.clear()
    try:
        response = await client.get(f"{API}/me")
    finally:
        if saved_header:
            client.headers["Authorization"] = saved_header
        for name, value in saved_cookies.items():
            client.cookies.set(name, value)

    assert response.status_code == 401, response.status_code
    return "401 as expected"


@check("security: response headers are set")
async def _headers(client: httpx.AsyncClient) -> str:
    headers = (await client.get("/healthz")).headers
    for name in ("x-content-type-options", "x-frame-options", "referrer-policy"):
        assert name in headers, f"missing {name}"
    assert headers.get("x-request-id"), "missing x-request-id"
    return "nosniff, DENY, referrer-policy, request id"


# -- runner -------------------------------------------------------------------


#: Errors that mean "nothing is listening yet", as distinct from "the app said no".
CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


async def wait_for_server(client: httpx.AsyncClient, attempts: int = 30) -> bool:
    """Block until the server actually answers, or give up.

    `docker compose up -d` returns when the container STARTS, not when the app is
    ready — and this one has to reach Atlas first. In that window Docker's port
    proxy accepts the TCP connection and closes it immediately, which surfaces as
    a bare `ReadError`. Without this wait every check reports that same error, and
    the real message ("it is still booting") is buried under twenty identical
    lines.
    """
    for attempt in range(1, attempts + 1):
        try:
            await client.get("/healthz", timeout=3)
            if attempt > 1:
                print(f"  server answered after ~{attempt * 2}s\n")
            return True
        except CONNECTION_ERRORS:
            if attempt == 1:
                print("  waiting for the server to accept connections...")
            await asyncio.sleep(2)
        except httpx.HTTPError:
            # Answering with an error is still answering. Let the checks judge it.
            return True
    return False


UNREACHABLE = """
  The server at {url} is not answering.

    1. Is it running?          docker compose ps
    2. What do the logs say?   docker compose logs --tail 40 api
    3. Still booting?          reaching Atlas can take a few seconds
    4. Right port?             the API listens on 8000

  Not running any checks — they would all report the same connection error.
"""


async def run(base_url: str) -> int:
    print(f"\n  Voltaris API smoke test — {base_url}\n  {'-' * 62}")

    async with httpx.AsyncClient(base_url=base_url, timeout=20, follow_redirects=True) as client:
        if not await wait_for_server(client):
            print(UNREACHABLE.format(url=base_url))
            return 1

        failures = 0
        for name, fn in CHECKS:
            try:
                detail = await fn(client)
                print(f"  \033[32mPASS\033[0m  {name:52s} {detail}")
            except AssertionError as exc:
                failures += 1
                print(f"  \033[31mFAIL\033[0m  {name:52s} {exc}")
            except CONNECTION_ERRORS as exc:
                # Losing the connection mid-run means the app died. Continuing
                # would print a wall of identical noise instead of the reason.
                print(f"  \033[31mERR \033[0m  {name:52s} connection lost ({type(exc).__name__})")
                print("\n  The server stopped responding mid-run.")
                print("  Check: docker compose logs --tail 40 api\n")
                return 1
            except Exception as exc:
                failures += 1
                print(f"  \033[31mERR \033[0m  {name:52s} {type(exc).__name__}: {exc}")

    print(f"  {'-' * 62}")
    if failures:
        print(f"  {failures} of {len(CHECKS)} checks failed\n")
    else:
        print(f"  all {len(CHECKS)} checks passed\n")
    return 1 if failures else 0


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    raise SystemExit(asyncio.run(run(base.rstrip("/"))))


if __name__ == "__main__":
    main()


_ = Any
