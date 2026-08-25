"""Cookie-mode authentication: the transport the browser actually uses."""

from __future__ import annotations

from app.core.sessions import CSRF_COOKIE, REFRESH_COOKIE, SESSION_COOKIE
from tests.conftest import make_user, make_vehicle


async def sign_in(api, user):
    response = await api.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert response.status_code == 200, response.text
    return response


async def test_login_sets_httponly_session_cookies(api, db):
    user = await make_user(db)
    response = await sign_in(api, user)

    jar = {c.name: c for c in response.cookies.jar}
    assert SESSION_COOKIE in jar
    assert REFRESH_COOKIE in jar
    assert CSRF_COOKIE in jar

    raw = response.headers.get_list("set-cookie")
    session_header = next(h for h in raw if h.startswith(SESSION_COOKIE))
    csrf_header = next(h for h in raw if h.startswith(CSRF_COOKIE))

    # The session token must be unreadable by JavaScript; the CSRF token must not be,
    # because the browser has to echo it back in a header.
    assert "HttpOnly" in session_header
    assert "HttpOnly" not in csrf_header
    assert "SameSite=lax" in session_header.lower().replace("samesite=lax", "SameSite=lax")


async def test_the_session_token_never_appears_in_a_readable_cookie(api, db):
    user = await make_user(db)
    response = await sign_in(api, user)
    access = response.json()["access_token"]
    csrf_header = next(
        h for h in response.headers.get_list("set-cookie") if h.startswith(CSRF_COOKIE)
    )
    assert access not in csrf_header


async def test_cookie_alone_authenticates_a_read(api, db):
    user = await make_user(db)
    await sign_in(api, user)
    # No Authorization header — httpx replays the cookie jar, exactly like a browser.
    response = await api.get("/api/v1/orders")
    assert response.status_code == 200


async def test_cookie_mutation_without_the_csrf_header_is_rejected(api, db):
    """The CSRF guard. A cross-site form can make the browser send the cookie; it
    cannot read the cookie, so it cannot set the matching header."""
    user = await make_user(db)
    await sign_in(api, user)
    vehicle = await make_vehicle(db)

    response = await api.post("/api/v1/orders", json={"vehicle_id": vehicle["_id"]})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_cookie_mutation_with_a_wrong_csrf_header_is_rejected(api, db):
    user = await make_user(db)
    await sign_in(api, user)
    vehicle = await make_vehicle(db)

    response = await api.post(
        "/api/v1/orders",
        json={"vehicle_id": vehicle["_id"]},
        headers={"X-CSRF-Token": "attacker-guessed-value"},
    )
    assert response.status_code == 403


async def test_cookie_mutation_with_the_matching_header_succeeds(api, db):
    user = await make_user(db)
    login = await sign_in(api, user)
    vehicle = await make_vehicle(db)

    response = await api.post(
        "/api/v1/orders",
        json={"vehicle_id": vehicle["_id"]},
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert response.status_code == 201


async def test_bearer_mutations_skip_csrf(api, db):
    """An attacker able to set an Authorization header already holds the token, so
    the double-submit check would add nothing for API clients."""
    user = await make_user(db)
    login = await sign_in(api, user)
    vehicle = await make_vehicle(db)

    fresh = await api.post(
        "/api/v1/orders",
        json={"vehicle_id": vehicle["_id"]},
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert fresh.status_code == 201


async def test_refresh_works_from_the_cookie_with_no_body(api, db):
    user = await make_user(db)
    await sign_in(api, user)
    response = await api.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_logout_clears_the_cookies_and_revokes_the_session(api, db):
    user = await make_user(db)
    login = await sign_in(api, user)

    out = await api.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": login.json()["csrf_token"]}
    )
    assert out.status_code == 204

    after = await api.get("/api/v1/orders")
    assert after.status_code == 401


async def test_session_endpoint_returns_the_wrapped_shape_the_frontend_expects(api, db):
    user = await make_user(db, email="shape@example.com")
    await sign_in(api, user)
    response = await api.get("/api/v1/auth/session")
    assert response.status_code == 200
    body = response.json()
    assert "user" in body
    assert body["user"]["email"] == "shape@example.com"
    assert body["user"]["full_name"] == "Test Person"
    assert body["user"]["roles"] == ["BUYER"]
