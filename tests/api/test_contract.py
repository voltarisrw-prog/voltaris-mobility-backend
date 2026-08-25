"""Contract tests.

The frontend's TypeScript interfaces are the specification. These assert the exact
field names and shapes it destructures, because a missing or renamed key produces
`undefined` at runtime rather than a type error — the failure mode that actually
reaches production.
"""

from __future__ import annotations

from tests.conftest import auth_header, login, make_user, make_vehicle

# Mirrors `VehicleSummary` in src/types/vehicle.ts.
SUMMARY_FIELDS = {
    "id",
    "slug",
    "make",
    "model",
    "variant",
    "year",
    "price",
    "currency",
    "rental_price_per_day",
    "mileage_km",
    "battery_kwh",
    "range_km",
    "body_type",
    "condition",
    "location",
    "listing_mode",
    "status",
    "verified",
    "primary_image",
    "published_at",
}

# `VehicleDetail` adds these.
DETAIL_EXTRA = {
    "description",
    "images",
    "seller",
    "drivetrain",
    "power_kw",
    "seats",
    "doors",
    "charging",
    "features",
    "financing_available",
    "test_drive_available",
    "purchase_enabled",
    "rental_enabled",
    "faqs",
    "updated_at",
}

PAGE_FIELDS = {"items", "page", "per_page", "total", "total_pages"}


async def test_vehicle_list_matches_the_page_type(api, db):
    await make_vehicle(db)
    response = await api.get("/api/v1/vehicles")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= PAGE_FIELDS
    assert set(body["items"][0]) >= SUMMARY_FIELDS


async def test_status_is_the_lowercase_union_the_frontend_expects(api, db):
    """Stored status is an uppercase seven-state lifecycle; the wire format is a
    four-value lowercase union. Leaking the lifecycle would expose DRAFT."""
    await make_vehicle(db, status="AVAILABLE")
    body = (await api.get("/api/v1/vehicles")).json()
    assert body["items"][0]["status"] == "available"


async def test_draft_and_pending_vehicles_are_never_public(api, db):
    for hidden in ("DRAFT", "PENDING_REVIEW", "REJECTED"):
        await make_vehicle(db, status=hidden)
    await make_vehicle(db, status="AVAILABLE")

    body = (await api.get("/api/v1/vehicles")).json()
    assert body["total"] == 1
    assert {item["status"] for item in body["items"]} == {"available"}


async def test_internal_pricing_never_appears_in_a_public_response(api, db):
    vehicle = await make_vehicle(db, seller_expected_price=25_000_000, agency_price=27_000_000)

    for path in ("/api/v1/vehicles", f"/api/v1/vehicles/by-slug/{vehicle['slug']}"):
        text = (await api.get(path)).text
        assert "25000000" not in text
        assert "seller_expected_price" not in text
        assert "internal" not in text
        assert "Seller is motivated" not in text
        # The public price must still be there.
        assert "27000000" in text


async def test_detail_matches_the_vehicle_detail_type(api, db):
    vehicle = await make_vehicle(db)
    body = (await api.get(f"/api/v1/vehicles/by-slug/{vehicle['slug']}")).json()
    assert set(body) >= (SUMMARY_FIELDS | DETAIL_EXTRA)
    assert set(body["charging"]) >= {"ac_kw", "dc_kw", "port_type", "dc_10_80_minutes"}
    assert set(body["seller"]) >= {"type", "display_name", "verified"}


async def test_private_seller_phone_is_never_published(api, db):
    vehicle = await make_vehicle(db)
    body = (await api.get(f"/api/v1/vehicles/by-slug/{vehicle['slug']}")).json()
    # A private seller's number is what the enquiry flow exists to protect.
    assert "phone" not in body["seller"]
    assert body["seller"]["type"] == "private"


async def test_unknown_slug_is_a_404_with_the_error_envelope(api, db):
    response = await api.get("/api/v1/vehicles/by-slug/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VEHICLE_NOT_FOUND"
    assert body["error"]["request_id"].startswith("req_")


async def test_facets_match_the_facet_type(api, db):
    await make_vehicle(db)
    body = (await api.get("/api/v1/vehicles/facets")).json()
    assert set(body) >= {"makes", "bodies", "locations", "price", "range"}
    assert set(body["makes"][0]) == {"value", "label", "count"}
    assert set(body["price"]) == {"min", "max"}


async def test_filters_use_the_frontend_query_keys(api, db):
    await make_vehicle(db, agency_price=20_000_000)
    await make_vehicle(db, agency_price=45_000_000)

    cheap = (await api.get("/api/v1/vehicles?maxPrice=30000000")).json()
    assert cheap["total"] == 1
    assert cheap["items"][0]["price"] == 20_000_000

    by_make = (await api.get("/api/v1/vehicles?make=byd")).json()
    assert by_make["total"] == 2

    none = (await api.get("/api/v1/vehicles?make=tesla")).json()
    assert none["total"] == 0


async def test_sort_by_price_orders_correctly(api, db):
    await make_vehicle(db, agency_price=45_000_000)
    await make_vehicle(db, agency_price=20_000_000)
    body = (await api.get("/api/v1/vehicles?sort=price_asc")).json()
    prices = [item["price"] for item in body["items"]]
    assert prices == sorted(prices)


async def test_compare_preserves_the_requested_order(api, db):
    first = await make_vehicle(db)
    second = await make_vehicle(db)
    body = (
        await api.post("/api/v1/vehicles/compare", json={"ids": [second["_id"], first["_id"]]})
    ).json()
    # The comparison columns must line up with the ids in the URL.
    assert [item["id"] for item in body] == [second["_id"], first["_id"]]


async def test_compare_refuses_more_than_four(api, db):
    response = await api.post("/api/v1/vehicles/compare", json={"ids": ["a", "b", "c", "d", "e"]})
    assert response.status_code == 422


async def test_sitemap_shape(api, db):
    await make_vehicle(db)
    body = (await api.get("/api/v1/vehicles/sitemap")).json()
    assert set(body) >= PAGE_FIELDS
    assert set(body["items"][0]) == {"slug", "updated_at", "status"}


async def test_anonymous_visitor_can_send_an_enquiry(api, db):
    vehicle = await make_vehicle(db)
    response = await api.post(
        "/api/v1/inquiries",
        json={
            "vehicle_id": vehicle["_id"],
            "full_name": "Amani Test",
            "email": "amani@example.com",
            "phone": "0788123456",
            "message": "Is the battery report available?",
            "preferred_channel": "whatsapp",
        },
    )
    assert response.status_code == 201
    assert set(response.json()) == {"reference", "status"}


async def test_homepage_enquiry_needs_no_vehicle(api, db):
    """The general form has no vehicle_id — that lead is the one worth having."""
    response = await api.post(
        "/api/v1/inquiries",
        json={
            "full_name": "Amani Test",
            "email": "general@example.com",
            "phone": "0788123456",
            "message": "Looking for an electric SUV under 40M.",
            "topic": "buying",
            "source": "homepage",
        },
    )
    assert response.status_code == 201
    stored = await db["inquiries"].find_one({"email": "general@example.com"})
    assert stored["vehicle_id"] is None
    assert stored["topic"] == "buying"
    assert stored["source"] == "homepage"


async def test_honeypot_submission_is_swallowed_not_stored(api, db):
    response = await api.post(
        "/api/v1/inquiries",
        json={
            "full_name": "Bot",
            "email": "bot@example.com",
            "phone": "0788123456",
            "message": "buy cheap watches online now",
            "company_website": "http://spam.example",
        },
    )
    # A normal-looking response, so probing teaches the operator nothing.
    assert response.status_code == 201
    assert await db["inquiries"].count_documents({"email": "bot@example.com"}) == 0


async def test_enquiry_flood_from_one_address_is_throttled(api, db):
    payload = {
        "full_name": "Amani Test",
        "email": "flood@example.com",
        "phone": "0788123456",
        "message": "Please send me details about everything.",
    }
    for _ in range(5):
        assert (await api.post("/api/v1/inquiries", json=payload)).status_code == 201
    blocked = await api.post("/api/v1/inquiries", json=payload)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"


async def test_test_drive_request_and_public_status_lookup(api, db):
    vehicle = await make_vehicle(db)
    created = await api.post(
        "/api/v1/test-drives",
        json={
            "vehicle_id": vehicle["_id"],
            "full_name": "Amani Test",
            "email": "amani@example.com",
            "phone": "0788123456",
            "preferred_date": "2030-01-15",
            "preferred_time_slot": "morning",
            "location_slug": "kigali-gasabo",
        },
    )
    assert created.status_code == 201
    body = created.json()
    # "requested", never "confirmed" — the frontend tells the customer as much.
    assert body["status"] == "requested"
    assert body["scheduled_for"] is None

    status = await api.get(f"/api/v1/test-drives/{body['reference']}")
    assert status.status_code == 200
    # The reference is the only credential, so it must leak nothing personal.
    assert set(status.json()) == {"reference", "status", "scheduled_for"}


async def test_test_drive_in_the_past_is_rejected(api, db):
    vehicle = await make_vehicle(db)
    response = await api.post(
        "/api/v1/test-drives",
        json={
            "vehicle_id": vehicle["_id"],
            "full_name": "Amani Test",
            "email": "amani@example.com",
            "phone": "0788123456",
            "preferred_date": "2020-01-15",
            "preferred_time_slot": "morning",
            "location_slug": "kigali-gasabo",
        },
    )
    assert response.status_code == 422


async def test_account_profile_matches_the_frontend_type(api, db):
    user = await make_user(db)
    token = await login(api, user)
    body = (await api.get("/api/v1/me", headers=auth_header(token))).json()
    assert set(body) == {
        "id",
        "full_name",
        "email",
        "phone",
        "email_verified",
        "preferred_language",
        "marketing_opt_in",
        "created_at",
    }


async def test_profile_update_cannot_escalate(api, db):
    user = await make_user(db)
    token = await login(api, user)
    await api.patch(
        "/api/v1/me",
        json={"full_name": "New Name", "roles": ["SUPER_ADMIN"], "email_verified": True},
        headers=auth_header(token),
    )
    stored = await db["users"].find_one({"_id": user["_id"]})
    assert stored["roles"] == ["BUYER"]
    assert stored["name"] == "New Name"


async def test_saving_a_vehicle_twice_is_idempotent(api, db):
    user = await make_user(db)
    token = await login(api, user)
    vehicle = await make_vehicle(db)

    for _ in range(3):
        response = await api.put(
            f"/api/v1/me/saved-vehicles/{vehicle['_id']}", headers=auth_header(token)
        )
        assert response.status_code == 204

    saved = (await api.get("/api/v1/me/saved-vehicles", headers=auth_header(token))).json()
    assert saved["total"] == 1
    assert set(saved["items"][0]) >= SUMMARY_FIELDS


async def test_one_customer_cannot_delete_anothers_saved_search(api, db):
    alice = await make_user(db, email="alice@example.com")
    bob = await make_user(db, email="bob@example.com")
    alice_token = await login(api, alice)

    created = await api.post(
        "/api/v1/me/saved-searches",
        json={"label": "Cheap SUVs", "query": "body=suv&maxPrice=30000000"},
        headers=auth_header(alice_token),
    )
    search_id = created.json()["id"]

    await api.delete(
        f"/api/v1/me/saved-searches/{search_id}", headers=auth_header(await login(api, bob))
    )
    still_there = (
        await api.get("/api/v1/me/saved-searches", headers=auth_header(alice_token))
    ).json()
    assert len(still_there) == 1


async def test_account_routes_require_a_session(api, db):
    for path in ("/api/v1/me", "/api/v1/me/saved-vehicles", "/api/v1/me/notifications"):
        assert (await api.get(path)).status_code == 401
