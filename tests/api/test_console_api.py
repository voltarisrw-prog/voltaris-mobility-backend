from __future__ import annotations

from app.modules.users.models import Role
from tests.conftest import auth_header, login, make_user, make_vehicle


async def super_admin(db, api, email="root@example.com"):
    user = await make_user(db, email=email, roles=[Role.SUPER_ADMIN.value])
    return user, await login(api, user)


async def test_buyer_cannot_reach_the_console(api, db):
    buyer = await make_user(db)
    token = await login(api, buyer)
    for path in ("/api/v1/console/overview", "/api/v1/console/collections"):
        response = await api.get(path, headers=auth_header(token))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_admin_can_monitor_but_cannot_inspect_raw_collections(api, db):
    """ADMIN gets the dashboard. Reading raw documents is SUPER_ADMIN only."""
    admin = await make_user(db, email="admin@example.com", roles=[Role.ADMIN.value])
    token = await login(api, admin)

    assert (
        await api.get("/api/v1/console/overview", headers=auth_header(token))
    ).status_code == 200

    blocked = await api.post(
        "/api/v1/console/inspect",
        json={"collection": "users"},
        headers=auth_header(token),
    )
    assert blocked.status_code == 403


async def test_super_admin_sees_the_whole_system(api, db):
    _, token = await super_admin(db, api)
    await make_vehicle(db)

    overview = await api.get("/api/v1/console/overview", headers=auth_header(token))
    assert overview.status_code == 200
    body = overview.json()
    for section in ("users", "vehicles", "orders", "money_7d", "alerts"):
        assert section in body
    assert body["vehicles"]["available"] == 1


async def test_super_admin_can_read_internal_pricing(api, db):
    """The point of the role: financial fields hidden everywhere else are visible."""
    _, token = await super_admin(db, api)
    await make_vehicle(db, seller_expected_price=25_000_000)

    response = await api.post(
        "/api/v1/console/inspect",
        json={"collection": "vehicles"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    vehicle = response.json()["items"][0]
    assert vehicle["internal"]["seller_expected_price"] == 25_000_000
    assert vehicle["internal"]["internal_notes"]


async def test_password_hashes_stay_hidden_from_super_admins(api, db):
    _, token = await super_admin(db, api)
    response = await api.post(
        "/api/v1/console/inspect",
        json={"collection": "users"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    assert "$argon2id$" not in response.text
    for row in response.json()["items"]:
        assert row["password_hash"] == "[redacted]"


async def test_inspect_refuses_javascript_operators(api, db):
    _, token = await super_admin(db, api)
    response = await api.post(
        "/api/v1/console/inspect",
        json={"collection": "users", "query": {"$where": "1==1"}},
        headers=auth_header(token),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_inspect_refuses_unlisted_collections(api, db):
    _, token = await super_admin(db, api)
    response = await api.post(
        "/api/v1/console/inspect",
        json={"collection": "system.indexes"},
        headers=auth_header(token),
    )
    assert response.status_code == 403


async def test_inspect_is_capped_and_audited(api, db):
    admin, token = await super_admin(db, api)
    for _ in range(5):
        await make_vehicle(db)

    response = await api.post(
        "/api/v1/console/inspect",
        json={"collection": "vehicles", "limit": 2},
        headers=auth_header(token),
    )
    assert response.json()["count"] == 2
    assert response.json()["truncated"] is True

    entry = await db["audit_logs"].find_one({"action": "admin.inspect"})
    assert entry is not None
    assert entry["actor_id"] == admin["_id"]


async def test_suspending_a_user_kills_their_live_sessions(api, db):
    _, admin_token = await super_admin(db, api)
    victim = await make_user(db, email="victim@example.com")
    victim_token = await login(api, victim)

    assert (await api.get("/api/v1/orders", headers=auth_header(victim_token))).status_code == 200

    suspend = await api.post(
        f"/api/v1/console/users/{victim['_id']}/status",
        json={"status": "SUSPENDED", "reason": "fraud investigation"},
        headers=auth_header(admin_token),
    )
    assert suspend.status_code == 200

    # Their token is still valid JWT; the session is gone.
    after = await api.get("/api/v1/orders", headers=auth_header(victim_token))
    assert after.status_code in (401, 403)


async def test_super_admin_cannot_change_their_own_roles(api, db):
    admin, token = await super_admin(db, api)
    response = await api.post(
        f"/api/v1/console/users/{admin['_id']}/roles",
        json={"roles": ["BUYER"], "reason": "testing"},
        headers=auth_header(token),
    )
    assert response.status_code == 403


async def test_last_super_admin_cannot_be_demoted(api, db):
    """Locking every super admin out of the system must not be one request away."""
    _, token = await super_admin(db, api)
    other = await make_user(db, email="second@example.com", roles=[Role.SUPER_ADMIN.value])
    other_token = await login(api, other)

    # Demoting one of two is fine.
    first = await api.post(
        f"/api/v1/console/users/{other['_id']}/roles",
        json={"roles": ["ADMIN"], "reason": "stepping down"},
        headers=auth_header(token),
    )
    assert first.status_code == 200

    # Demoting the last one is refused, whoever asks.
    admin_doc = await db["users"].find_one({"email": "root@example.com"})
    last = await api.post(
        f"/api/v1/console/users/{admin_doc['_id']}/roles",
        json={"roles": ["ADMIN"], "reason": "oops"},
        headers=auth_header(other_token),
    )
    assert last.status_code == 403


async def test_role_changes_are_audited_with_a_before_image(api, db):
    admin, token = await super_admin(db, api)
    target = await make_user(db, email="promote@example.com")

    await api.post(
        f"/api/v1/console/users/{target['_id']}/roles",
        json={"roles": ["FINANCE"], "reason": "joined the finance team"},
        headers=auth_header(token),
    )
    entry = await db["audit_logs"].find_one({"action": "admin.user_roles_changed"})
    assert entry["before"]["roles"] == ["BUYER"]
    assert entry["after"]["roles"] == ["FINANCE"]
    assert entry["actor_id"] == admin["_id"]


async def test_no_write_endpoint_accepts_a_raw_document(api, db):
    """There is no update/delete/aggregate/command equivalent of /inspect."""
    _, token = await super_admin(db, api)
    for method, path, payload in (
        ("post", "/api/v1/console/update", {"collection": "orders", "set": {"total": 1}}),
        ("post", "/api/v1/console/delete", {"collection": "orders"}),
        ("post", "/api/v1/console/aggregate", {"pipeline": []}),
        ("post", "/api/v1/console/command", {"command": {"ping": 1}}),
        ("post", "/api/v1/console/execute", {"js": "db.orders.drop()"}),
    ):
        response = await getattr(api, method)(path, json=payload, headers=auth_header(token))
        assert response.status_code == 404, f"{path} should not exist"
