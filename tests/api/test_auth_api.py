from __future__ import annotations

from tests.conftest import auth_header, login, make_user


async def test_register_then_login(api, db):
    response = await api.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Amani Test",
            "email": "amani@example.com",
            "phone": "0788123456",
            "password": "correct-horse-battery",
        },
    )
    assert response.status_code == 201
    assert response.json()["verification_required"] is True

    login_response = await api.post(
        "/api/v1/auth/login",
        json={"email": "amani@example.com", "password": "correct-horse-battery"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["access_token"]


async def test_registration_cannot_self_assign_a_role(api, db):
    """Mass-assignment escalation: a `roles` field in the body must be ignored."""
    response = await api.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Sneaky",
            "email": "sneaky@example.com",
            "password": "correct-horse-battery",
            "roles": ["SUPER_ADMIN"],
            "status": "ACTIVE",
            "email_verified": True,
        },
    )
    assert response.status_code == 201
    stored = await db["users"].find_one({"email": "sneaky@example.com"})
    assert stored["roles"] == ["BUYER"]
    assert stored["email_verified"] is False


async def test_wrong_password_and_unknown_email_are_indistinguishable(api, db):
    await make_user(db, email="real@example.com", password="correct-horse-battery")

    wrong = await api.post(
        "/api/v1/auth/login", json={"email": "real@example.com", "password": "nope-nope-nope"}
    )
    unknown = await api.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    # Identical bodies apart from the request id — no account enumeration.
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_brute_force_locks_the_account(api, db):
    await make_user(db, email="target@example.com", password="correct-horse-battery")
    for _ in range(5):
        await api.post(
            "/api/v1/auth/login", json={"email": "target@example.com", "password": "wrong-wrong-1"}
        )
    blocked = await api.post(
        "/api/v1/auth/login",
        json={"email": "target@example.com", "password": "correct-horse-battery"},
    )
    assert blocked.status_code == 423
    assert blocked.json()["error"]["code"] == "ACCOUNT_LOCKED"


async def test_protected_route_requires_a_token(api, db):
    response = await api.get("/api/v1/orders")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_logout_revokes_the_session_immediately(api, db):
    user = await make_user(db)
    token = await login(api, user)

    assert (await api.get("/api/v1/orders", headers=auth_header(token))).status_code == 200
    assert (await api.post("/api/v1/auth/logout", headers=auth_header(token))).status_code == 204

    # The JWT is still cryptographically valid; the session is not. Server-side
    # revocation is what makes logout mean anything.
    after = await api.get("/api/v1/orders", headers=auth_header(token))
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_refresh_rotates_and_burns_the_old_token(api, db):
    user = await make_user(db)
    first = await api.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    original_refresh = first.json()["refresh_token"]

    rotated = await api.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != original_refresh

    # Replaying the burnt token must fail AND kill the session — that is the whole
    # point of rotation: a leaked token is detected the moment it is used twice.
    replay = await api.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "TOKEN_REVOKED"

    still_valid = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert still_valid.status_code == 401, "session should be dead after reuse detection"


async def test_suspended_user_cannot_use_a_valid_token(api, db):
    user = await make_user(db)
    token = await login(api, user)
    await db["users"].update_one({"_id": user["_id"]}, {"$set": {"status": "SUSPENDED"}})

    response = await api.get("/api/v1/orders", headers=auth_header(token))
    assert response.status_code == 403


async def test_error_responses_never_leak_internals(api, db):
    response = await api.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"}
    )
    body = response.text
    for leak in ("Traceback", "password_hash", "mongomock", "app/modules", "no such user"):
        assert leak not in body
    assert response.json()["error"]["request_id"].startswith("req_")
